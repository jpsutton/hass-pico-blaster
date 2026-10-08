"""Owns one serial port: connects, reconnects with backoff, serializes sends.

Also resolves what to send: raw Pronto hex / pulse text, or a function from
a fire-blaster profile file (with toggle-bit variants alternated per press).
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from pathlib import Path

from .const import DEFAULT_CARRIER, RECONNECT_MAX, RECONNECT_MIN
from .irtoy import IrToy, IrToyError
from .profiles import ProfileSet
from .pronto import DEFAULT_GAP_US, IrSignal, ProntoError, decode, parse_pulses

_LOGGER = logging.getLogger(__name__)


class SendError(Exception):
    """A send could not be completed (device offline, bad code, ...)."""


class ProfileSendError(SendError):
    """Bad profile id / function name."""


def parse_code(text: str, default_carrier: int) -> IrSignal:
    """Pronto hex or ir-ctl pulse text -> IrSignal."""
    text = text.strip()
    if not text:
        raise ProntoError("empty code")
    if text[0] in "+-":
        durations = parse_pulses(text)
        if len(durations) % 2:
            durations.append(DEFAULT_GAP_US)
        return IrSignal(default_carrier, tuple(durations))
    return decode(text)


class FireBlasterDevice:
    def __init__(self, port: str, name: str, carrier: int = DEFAULT_CARRIER, profiles_dir: Path | None = None) -> None:
        self.port = port
        self.name = name
        self.default_carrier = carrier
        self.profiles_dir = profiles_dir
        self.profiles = ProfileSet([])
        self.toy: IrToy | None = None
        self.version: str | None = None
        self._send_lock = asyncio.Lock()
        self._reconnect_task: asyncio.Task | None = None
        self._listeners: list[Callable[[], None]] = []
        self._outage_logged = False
        self._toggle_index: dict[tuple[str, str], int] = {}
        self._closing = False

    # -- availability ------------------------------------------------------

    @property
    def available(self) -> bool:
        return self.toy is not None and self.toy.ready

    def add_listener(self, cb: Callable[[], None]) -> Callable[[], None]:
        self._listeners.append(cb)

        def remove() -> None:
            self._listeners.remove(cb)

        return remove

    def _notify(self) -> None:
        for cb in list(self._listeners):
            cb()

    # -- connection --------------------------------------------------------

    async def connect(self) -> None:
        """Open the port once. Raises on failure."""
        toy = await IrToy.open_serial(self.port)
        self.toy = toy
        self.version = toy.version
        if self._outage_logged:
            _LOGGER.warning("%s: IR blaster on %s is back (%s)", self.name, self.port, toy.version)
            self._outage_logged = False
        else:
            _LOGGER.info("%s: connected to %s (%s)", self.name, self.port, toy.version)
        self._notify()

    def _lost(self, err: BaseException) -> None:
        if not self._outage_logged:
            _LOGGER.warning("%s: IR blaster on %s unavailable: %s; retrying", self.name, self.port, err)
            self._outage_logged = True
        else:
            _LOGGER.debug("%s: still unavailable: %s", self.name, err)
        self._notify()
        self.schedule_reconnect()

    def schedule_reconnect(self) -> None:
        if self._closing or (self._reconnect_task and not self._reconnect_task.done()):
            return
        self._reconnect_task = asyncio.get_running_loop().create_task(self._reconnect_loop())

    async def _reconnect_loop(self) -> None:
        delay = RECONNECT_MIN
        while not self._closing:
            await asyncio.sleep(delay)
            if self.toy is not None:
                await self.toy.close()
                self.toy = None
            try:
                await self.connect()
                return
            except Exception as err:  # noqa: BLE001 - any failure: keep retrying
                _LOGGER.debug("%s: reconnect failed: %s", self.name, err)
                delay = min(delay * 2, RECONNECT_MAX)

    async def close(self) -> None:
        self._closing = True
        if self._reconnect_task:
            self._reconnect_task.cancel()
        if self.toy is not None:
            await self.toy.close()
            self.toy = None
        self._notify()

    # -- profiles ----------------------------------------------------------

    def load_profiles(self) -> int:
        """Blocking: read every *.toml under profiles_dir."""
        if self.profiles_dir is None:
            self.profiles = ProfileSet([])
        else:
            self.profiles = ProfileSet.load([self.profiles_dir])
        return len(self.profiles)

    def resolve(self, device: str | None, command: str) -> IrSignal:
        """Turn a remote.send_command (device, command) pair into a signal."""
        if device:
            profile = self.profiles.get(device)
            if profile is None:
                raise ProfileSendError(f"unknown profile {device!r}; loaded: {sorted(self.profiles.by_id) or 'none'}")
            variants = profile.variants(command)
            if not variants:
                raise ProfileSendError(f"profile {device!r} has no usable code for {command!r}")
            key = (device, command)
            i = self._toggle_index.get(key, 0) % len(variants)
            self._toggle_index[key] = i + 1
            return variants[i]
        try:
            return parse_code(command, self.default_carrier)
        except ProntoError as err:
            raise SendError(f"bad IR code: {err}") from err

    # -- sending -----------------------------------------------------------

    async def send_signal(self, signal: IrSignal, repeats: int = 1, delay: float = 0.0) -> None:
        """Send a signal `repeats` times. First frame is `once`, later ones `repeat`."""
        carrier = signal.carrier or self.default_carrier
        async with self._send_lock:
            for i in range(max(1, repeats)):
                frame = signal.frame(repeat=i > 0)
                if not frame:
                    continue
                if i and delay > 0:
                    await asyncio.sleep(delay)
                await self._transmit(list(frame), carrier)

    async def send_frame(self, durations: list[int], carrier: int, repeats: int = 1, delay: float = 0.0) -> None:
        async with self._send_lock:
            for i in range(max(1, repeats)):
                if i and delay > 0:
                    await asyncio.sleep(delay)
                await self._transmit(durations, carrier)

    async def _transmit(self, durations: list[int], carrier: int) -> None:
        toy = self.toy
        if toy is None or not toy.ready:
            raise SendError(f"{self.name}: IR blaster on {self.port} is not connected")
        try:
            await toy.transmit(durations, carrier_hz=carrier)
        except ValueError as err:
            raise SendError(str(err)) from err
        except IrToyError as err:
            # Protocol-level trouble: try one re-setup in place before giving up.
            _LOGGER.debug("%s: transmit failed (%s), re-running setup", self.name, err)
            try:
                await toy.setup()
            except (IrToyError, OSError) as err2:
                self._lost(err2)
                raise SendError(f"{self.name}: {err2}") from err2
            raise SendError(f"{self.name}: {err}") from err
        except OSError as err:
            # Serial error / unplugged.
            self._lost(err)
            raise SendError(f"{self.name}: {err}") from err
