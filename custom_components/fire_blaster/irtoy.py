"""Host side of the USB IR Toy v2 "sampling mode" transmit protocol.

Speaks to a fire-blaster Pico IR stick (or a real IR Toy, firmware v20+)
over a CDC ACM serial port. Mirrors the command sequence the Linux `ir_toy`
driver uses so both devices behave the same.

This module has no Home Assistant imports so it can be unit-tested alone.
The I/O objects are duck-typed: a reader with `readexactly(n)` and `read(n)`,
and a writer with `write(b)`, `drain()`, `close()` and `wait_closed()`, which
is what `asyncio.StreamReader`/`StreamWriter` and
`serial_asyncio_fast.open_serial_connection` provide.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Iterable, Sequence

log = logging.getLogger(__name__)

UNIT_US = 21  # one IR Toy sample, as the kernel and the firmware round it
MAX_DURATIONS = 2048  # firmware buffer
MAX_SAMPLE = 0xFFFE  # 0xffff is the terminator
MIN_CARRIER_HZ = 11800
MIN_FIRMWARE = 20
HANDSHAKE_MAX = 64

CMD_TIMEOUT = 0.5  # seconds per command reply
DEFAULT_BAUDRATE = 115200

RESET = bytes([0xFF, 0xFF, 0, 0, 0, 0, 0])
CMD_VERSION = b"v"
CMD_SAMPLE_MODE = b"s"
CMD_LEAVE_SAMPLE_MODE = b"\x00"
CMD_SET_CARRIER = 0x06
CMD_TX = bytes([0x26, 0x24, 0x25, 0x03])  # handshake, byte count, notify, start
TERMINATOR = b"\xff\xff"
REPLY_SAMPLE_MODE = b"S01"


class IrToyError(Exception):
    """Base error. After one of these the device needs `setup()` again."""


class IrToyTimeout(IrToyError):
    """The device did not answer in time."""


class IrToyProtocolError(IrToyError):
    """The device answered something unexpected."""


class IrToyOverflow(IrToyError):
    """The device reported `F`: its buffer overflowed."""


def carrier_pr2(carrier_hz: int) -> int:
    """PR2 timer value for a carrier; the device runs 48e6 / (16 * (PR2 + 1))."""
    if carrier_hz < MIN_CARRIER_HZ:
        raise ValueError(f"carrier {carrier_hz} Hz is below the {MIN_CARRIER_HZ} Hz minimum")
    return max(0, min(255, round(48_000_000 / (16 * carrier_hz)) - 1))


def actual_carrier_hz(pr2: int) -> int:
    return 48_000_000 // (16 * (pr2 + 1))  # firmware truncates


def encode_durations(durations_us: Iterable[int]) -> bytes:
    """Durations in µs -> big-endian u16 samples plus the ff ff terminator."""
    out = bytearray()
    for us in durations_us:
        v = round(us / UNIT_US)
        v = min(max(v, 1), MAX_SAMPLE)
        out += v.to_bytes(2, "big")
    out += TERMINATOR
    return bytes(out)


def prepare_durations(durations_us: Sequence[int]) -> tuple[list[int], int]:
    """Validate a mark/space list and split off any trailing space.

    Returns (durations ending on a mark, trailing space in µs or 0).
    """
    durations = [int(d) for d in durations_us]
    if any(d < 0 for d in durations):
        raise ValueError("durations must be non-negative")
    trailing = 0
    if len(durations) % 2 == 0 and durations:
        trailing = durations.pop()
    if not durations:
        raise ValueError("nothing to transmit")
    if len(durations) > MAX_DURATIONS:
        raise ValueError(f"{len(durations)} durations exceeds the device limit of {MAX_DURATIONS}")
    return durations, trailing


class IrToy:
    """One IR Toy compatible device on an already-open byte stream."""

    def __init__(self, reader, writer, name: str = "irtoy") -> None:
        self._reader = reader
        self._writer = writer
        self.name = name
        self.hw_version: int | None = None
        self.fw_version: int | None = None
        self._ready = False
        self._carrier_pr2: int | None = None
        self._lock = asyncio.Lock()

    # -- construction ------------------------------------------------------

    @classmethod
    async def open_serial(cls, port: str, baudrate: int = DEFAULT_BAUDRATE) -> IrToy:
        """Open a serial port and run setup. Raises OSError/SerialException or IrToyError."""
        from serial_asyncio_fast import open_serial_connection  # noqa: PLC0415

        reader, writer = await open_serial_connection(
            url=port,
            baudrate=baudrate,
            bytesize=8,
            parity="N",
            stopbits=1,
            xonxoff=False,
            rtscts=False,
            dsrdtr=False,
        )
        toy = cls(reader, writer, name=port)
        try:
            await toy.setup()
        except BaseException:
            await toy.close()
            raise
        return toy

    @property
    def ready(self) -> bool:
        return self._ready

    @property
    def version(self) -> str:
        if self.hw_version is None:
            return "unknown"
        return f"V{self.hw_version}{self.fw_version:02d}"

    # -- low level ---------------------------------------------------------

    async def _write(self, data: bytes) -> None:
        self._writer.write(data)
        await self._writer.drain()

    async def _read(self, n: int, timeout: float = CMD_TIMEOUT) -> bytes:
        try:
            return await asyncio.wait_for(self._reader.readexactly(n), timeout)
        except asyncio.TimeoutError as e:
            self._ready = False
            raise IrToyTimeout(f"{self.name}: no reply within {timeout:.1f}s") from e
        except asyncio.IncompleteReadError as e:
            self._ready = False
            raise IrToyProtocolError(f"{self.name}: stream closed") from e

    async def _discard_input(self) -> None:
        while True:
            try:
                data = await asyncio.wait_for(self._reader.read(4096), 0.02)
            except asyncio.TimeoutError:
                return
            if not data:
                return

    async def _expect(self, want: bytes, what: str) -> None:
        got = await self._read(len(want))
        if got != want:
            self._ready = False
            raise IrToyProtocolError(f"{self.name}: bad {what} reply {got!r}, expected {want!r}")

    # -- protocol ----------------------------------------------------------

    async def setup(self) -> None:
        """Reset, read the version and enter sampling mode."""
        async with self._lock:
            await self._setup_locked()

    async def _setup_locked(self) -> None:
        self._ready = False
        self._carrier_pr2 = None
        await self._write(RESET)
        await asyncio.sleep(0.005)
        await self._discard_input()

        await self._write(CMD_VERSION)
        reply = await self._read(4)
        if len(reply) != 4 or reply[0:1] != b"V" or not reply[1:].isdigit():
            raise IrToyProtocolError(f"{self.name}: bad version reply {reply!r}")
        n = int(reply[1:])
        self.hw_version, self.fw_version = n // 100, n % 100
        if self.fw_version < MIN_FIRMWARE:
            raise IrToyProtocolError(f"{self.name}: firmware {self.fw_version} too old, need >= {MIN_FIRMWARE}")

        await self._write(CMD_SAMPLE_MODE)
        await self._expect(REPLY_SAMPLE_MODE, "sampling mode")
        self._ready = True

    async def set_carrier(self, carrier_hz: int) -> int:
        """Set the carrier. Returns the carrier the device will really use."""
        async with self._lock:
            return await self._set_carrier_locked(carrier_hz)

    async def _set_carrier_locked(self, carrier_hz: int) -> int:
        if not self._ready:
            raise IrToyError(f"{self.name}: not set up")
        pr2 = carrier_pr2(carrier_hz)
        if pr2 != self._carrier_pr2:
            await self._write(bytes([CMD_SET_CARRIER, pr2, 0]))
            self._carrier_pr2 = pr2
        return actual_carrier_hz(pr2)

    async def transmit(self, durations_us: Sequence[int], carrier_hz: int | None = None) -> None:
        """Send one IR frame. `durations_us` alternates mark/space, starting with a mark.

        Holds the device lock for the whole exchange. On any failure the
        device is marked not ready and the error is raised; call `setup()`
        (or `recover()`) before the next transmit.
        """
        durations, trailing_space = prepare_durations(durations_us)
        data = encode_durations(durations)
        total_us = sum(durations)

        async with self._lock:
            if not self._ready:
                raise IrToyError(f"{self.name}: not set up")
            if carrier_hz is not None:
                await self._set_carrier_locked(carrier_hz)
            try:
                await self._transmit_locked(data, total_us)
            except IrToyError:
                self._ready = False
                raise
            if trailing_space:
                await asyncio.sleep(trailing_space / 1e6)

    async def _transmit_locked(self, data: bytes, total_us: int) -> None:
        # Leave and re-enter sampling mode, as the kernel does; a real IR Toy needs it.
        await self._write(CMD_LEAVE_SAMPLE_MODE)
        await self._write(CMD_SAMPLE_MODE)
        await self._expect(REPLY_SAMPLE_MODE, "sampling mode")

        await self._write(CMD_TX)

        offset = 0
        while offset < len(data):
            handshake = (await self._read(1))[0]
            if handshake == 0 or handshake > HANDSHAKE_MAX:
                raise IrToyProtocolError(f"{self.name}: bad handshake {handshake:#x}")
            chunk = data[offset : offset + handshake]
            await self._write(chunk)
            offset += len(chunk)

        # Wait for 't' + u16 count, skipping any stray handshake bytes.
        timeout = CMD_TIMEOUT + total_us / 1e6
        strays = 0
        while True:
            b = await self._read(1, timeout)
            if b == b"t":
                break
            strays += 1
            if strays > 8:
                raise IrToyProtocolError(f"{self.name}: no byte-count reply, last byte {b!r}")
        count = int.from_bytes(await self._read(2, timeout), "big")
        if count != len(data):
            raise IrToyProtocolError(f"{self.name}: device received {count} bytes, sent {len(data)}")
        status = await self._read(1, timeout)
        if status == b"F":
            raise IrToyOverflow(f"{self.name}: device buffer overflow")
        if status != b"C":
            raise IrToyProtocolError(f"{self.name}: bad completion reply {status!r}")

    async def recover(self) -> None:
        """Re-run setup after an error."""
        await self.setup()

    async def close(self) -> None:
        self._ready = False
        try:
            self._writer.close()
            wait_closed = getattr(self._writer, "wait_closed", None)
            if wait_closed is not None:
                await asyncio.wait_for(wait_closed(), 1.0)
        except Exception:  # noqa: BLE001 - closing is best effort
            pass
