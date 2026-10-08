"""In-process fake of the Pico Blaster stick's IR Toy firmware.

Python port of the firmware's irtoy.c state machine, wrapped in
asyncio StreamReader/StreamWriter look-alikes so `IrToy` can drive it with
no serial port.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

CMD_RESET = 0x00
CMD_TX_START = 0x03
CMD_SET_CARRIER = 0x06
CMD_VERSION = ord("v")
CMD_SAMPLE_MODE = ord("s")

HANDSHAKE_SPACE = 62
MAX_DURATIONS = 2048

COMMAND, SAMPLE, CARRIER, TX_DATA = range(4)


def carrier_hz(pr2: int) -> int:
    return 48_000_000 // (16 * (pr2 + 1))


@dataclass
class Transmit:
    units: list[int]
    carrier_hz: int


@dataclass
class FakeIrToy:
    """State machine. `feed()` consumes host bytes; replies go to `out`."""

    version: bytes = b"V222"
    handshake_space: int = HANDSHAKE_SPACE
    out: bytearray = field(default_factory=bytearray)
    transmits: list[Transmit] = field(default_factory=list)
    # Fault injection
    stray_handshake_before_t: bool = False
    wrong_count: bool = False
    force_overflow: bool = False
    silent: bool = False  # never reply (timeout test)

    state: int = COMMAND
    carrier: int = carrier_hz(78)
    carrier_arg: list[int] = field(default_factory=list)
    units: list[int] = field(default_factory=list)
    tx_bytes: int = 0
    high_byte: int = -1
    overflow: bool = False

    def _send(self, data: bytes) -> None:
        if not self.silent:
            self.out += data

    def _handshake(self) -> None:
        self._send(bytes([self.handshake_space]))

    def _tx_start(self) -> None:
        self.state = TX_DATA
        self.units = []
        self.tx_bytes = 0
        self.high_byte = -1
        self.overflow = False
        self._handshake()

    def _tx_finish(self) -> None:
        overflow = self.overflow or self.force_overflow
        if not overflow and self.units:
            self.transmits.append(Transmit(list(self.units), self.carrier))
        if self.stray_handshake_before_t:
            self._handshake()
        count = self.tx_bytes + (1 if self.wrong_count else 0)
        self._send(b"t" + count.to_bytes(2, "big"))
        self._send(b"F" if overflow else b"C")
        self.state = SAMPLE

    def _tx_byte(self, b: int) -> bool:
        self.tx_bytes += 1
        if self.high_byte < 0:
            self.high_byte = b
            return False
        v = (self.high_byte << 8) | b
        self.high_byte = -1
        if v == 0xFFFF:
            return True
        if len(self.units) < MAX_DURATIONS:
            self.units.append(v)
        else:
            self.overflow = True
        return False

    def _command_byte(self, b: int) -> None:
        if b == CMD_RESET:
            self.state = COMMAND
        elif b == CMD_VERSION:
            self._send(self.version)
        elif b == CMD_SAMPLE_MODE:
            self.state = SAMPLE
            self._send(b"S01")
        elif b == CMD_SET_CARRIER:
            if self.state == SAMPLE:
                self.state = CARRIER
                self.carrier_arg = []
        elif b == CMD_TX_START:
            if self.state == SAMPLE:
                self._tx_start()
        # 0x24-0x26 and 0xff: ignored

    def feed(self, data: bytes) -> None:
        tx_data = False
        for b in data:
            if self.state == TX_DATA:
                tx_data = True
                if self._tx_byte(b):
                    tx_data = False
                    self._tx_finish()
            elif self.state == CARRIER:
                self.carrier_arg.append(b)
                if len(self.carrier_arg) == 2:
                    self.carrier = carrier_hz(self.carrier_arg[0])
                    self.state = SAMPLE
            else:
                self._command_byte(b)
        if tx_data and self.state == TX_DATA:
            self._handshake()


class FakeWriter:
    """StreamWriter look-alike that feeds the fake device and pushes its
    replies into a real asyncio.StreamReader."""

    def __init__(self, device: FakeIrToy, reader: asyncio.StreamReader) -> None:
        self.device = device
        self.reader = reader
        self.closed = False
        self.written = bytearray()
        self.writes: list[bytes] = []

    def write(self, data: bytes) -> None:
        if self.closed:
            raise OSError("write to closed port")
        self.written += data
        self.writes.append(bytes(data))
        self.device.feed(bytes(data))
        if self.device.out:
            self.reader.feed_data(bytes(self.device.out))
            self.device.out.clear()

    async def drain(self) -> None:
        await asyncio.sleep(0)

    def close(self) -> None:
        self.closed = True
        self.reader.feed_eof()

    async def wait_closed(self) -> None:
        return


def make_pair(device: FakeIrToy | None = None) -> tuple[FakeIrToy, asyncio.StreamReader, FakeWriter]:
    device = device or FakeIrToy()
    reader = asyncio.StreamReader()
    writer = FakeWriter(device, reader)
    return device, reader, writer


async def serve_stream(device: FakeIrToy, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    """Run the fake device over a real byte stream (for the socat end-to-end test)."""
    while True:
        data = await reader.read(64)
        if not data:
            return
        device.feed(data)
        if device.out:
            writer.write(bytes(device.out))
            device.out.clear()
            await writer.drain()
