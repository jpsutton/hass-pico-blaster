import asyncio

import pytest

from custom_components.pico_blaster import irtoy
from custom_components.pico_blaster.irtoy import IrToy, IrToyOverflow, IrToyProtocolError, IrToyTimeout

from .fake_irtoy import FakeIrToy, make_pair

NEC_START = [9000, 4500, 560]


async def open_fake(**kw) -> tuple[FakeIrToy, IrToy]:
    device, reader, writer = make_pair(FakeIrToy(**kw))
    toy = IrToy(reader, writer, name="fake")
    await toy.setup()
    return device, toy


async def test_setup():
    device, reader, writer = make_pair()
    toy = IrToy(reader, writer)
    await toy.setup()
    assert toy.ready
    assert (toy.hw_version, toy.fw_version) == (2, 22)
    assert toy.version == "V222"
    # reset, version, sample mode, in that order
    assert writer.writes[0] == bytes([0xFF, 0xFF, 0, 0, 0, 0, 0])
    assert writer.writes[1] == b"v"
    assert writer.writes[2] == b"s"
    assert device.state == 1  # SAMPLE


async def test_setup_rejects_old_firmware():
    device, reader, writer = make_pair(FakeIrToy(version=b"V219"))
    toy = IrToy(reader, writer)
    with pytest.raises(IrToyProtocolError, match="too old"):
        await toy.setup()
    assert not toy.ready


async def test_setup_garbage_version():
    device, reader, writer = make_pair(FakeIrToy(version=b"XYZW"))
    toy = IrToy(reader, writer)
    with pytest.raises(IrToyProtocolError, match="bad version"):
        await toy.setup()


def test_carrier_pr2():
    assert irtoy.carrier_pr2(38000) == 78
    assert irtoy.carrier_pr2(36000) == 82
    assert irtoy.actual_carrier_hz(78) == 37974
    assert irtoy.actual_carrier_hz(82) == 36144
    assert irtoy.carrier_pr2(11800) == 253
    with pytest.raises(ValueError):
        irtoy.carrier_pr2(11000)
    assert irtoy.carrier_pr2(1_000_000) == 2  # clamped at the low end: round(3)-1


def test_encode_durations():
    assert irtoy.encode_durations([9000, 4500, 560]) == bytes.fromhex("01ad 00d6 001b ffff")
    assert irtoy.encode_durations([0]) == bytes.fromhex("0001 ffff")
    assert irtoy.encode_durations([10_000_000]) == bytes.fromhex("fffe ffff")


def test_prepare_durations():
    assert irtoy.prepare_durations([9000, 4500, 560]) == ([9000, 4500, 560], 0)
    assert irtoy.prepare_durations([9000, 4500, 560, 40000]) == ([9000, 4500, 560], 40000)
    with pytest.raises(ValueError):
        irtoy.prepare_durations([])
    with pytest.raises(ValueError):
        irtoy.prepare_durations([1] * 2049)
    # 2048 entries end on a space; dropping it leaves 2047 marks/spaces
    assert len(irtoy.prepare_durations([1] * 2048)[0]) == 2047


async def test_set_carrier():
    device, toy = await open_fake()
    assert await toy.set_carrier(36000) == 36144
    assert device.carrier == 36144
    # same PR2 again: no extra write
    _, reader, writer = make_pair(device)


async def test_nec_transmit():
    device, reader, writer = make_pair()
    toy = IrToy(reader, writer)
    await toy.setup()
    n_setup = len(writer.writes)
    await toy.transmit(NEC_START, carrier_hz=38000)
    assert len(device.transmits) == 1
    tx = device.transmits[0]
    assert tx.units == [429, 214, 27]
    assert tx.carrier_hz == 37974
    # Command order: carrier, 00, s, 26 24 25 03, data
    cmds = writer.writes[n_setup:]
    assert cmds[0] == bytes([0x06, 78, 0])
    assert cmds[1] == b"\x00"
    assert cmds[2] == b"s"
    assert cmds[3] == bytes([0x26, 0x24, 0x25, 0x03])
    assert cmds[4] == bytes.fromhex("01ad 00d6 001b ffff")
    assert toy.ready
    assert device.state == 1  # back in SAMPLE


async def test_transmit_trailing_space_sleeps():
    device, toy = await open_fake()
    loop = asyncio.get_running_loop()
    t0 = loop.time()
    await toy.transmit([560, 560, 560, 30000])
    assert device.transmits[0].units == [27, 27, 27]
    assert loop.time() - t0 >= 0.025


async def test_long_transmit_many_handshakes():
    device, reader, writer = make_pair()
    toy = IrToy(reader, writer)
    await toy.setup()
    n_setup = len(writer.writes)
    many = [210 + 21 * (i % 50) for i in range(1023)]
    await toy.transmit(many)
    assert len(device.transmits) == 1
    assert device.transmits[0].units == [10 + i % 50 for i in range(1023)]
    data_writes = writer.writes[n_setup + 3 :]
    assert all(len(w) <= 62 for w in data_writes)
    assert len(data_writes) == -(-(2 * 1024) // 62)  # ceil
    assert b"".join(data_writes).endswith(b"\xff\xff")


async def test_max_durations_transmit():
    device, toy = await open_fake()
    await toy.transmit([21] * 2047)
    assert len(device.transmits[0].units) == 2047


async def test_stray_handshake_before_t():
    device, toy = await open_fake(stray_handshake_before_t=True)
    await toy.transmit(NEC_START)
    assert len(device.transmits) == 1
    assert toy.ready


async def test_wrong_count_then_resetup():
    device, toy = await open_fake(wrong_count=True)
    with pytest.raises(IrToyProtocolError, match="received"):
        await toy.transmit(NEC_START)
    assert not toy.ready
    with pytest.raises(irtoy.IrToyError, match="not set up"):
        await toy.transmit(NEC_START)
    device.wrong_count = False
    await toy.recover()
    assert toy.ready
    await toy.transmit(NEC_START)
    assert len(device.transmits) == 2


async def test_overflow_reply():
    device, toy = await open_fake(force_overflow=True)
    with pytest.raises(IrToyOverflow):
        await toy.transmit(NEC_START)
    assert not toy.ready
    device.force_overflow = False
    await toy.setup()
    await toy.transmit(NEC_START)
    assert toy.ready


async def test_timeout(monkeypatch):
    monkeypatch.setattr(irtoy, "CMD_TIMEOUT", 0.05)
    device, toy = await open_fake()
    device.silent = True
    with pytest.raises(IrToyTimeout):
        await toy.transmit(NEC_START)
    assert not toy.ready
    device.silent = False
    await toy.setup()
    await toy.transmit(NEC_START)
    assert len(device.transmits) == 1


async def test_setup_discards_stale_input():
    device, reader, writer = make_pair()
    reader.feed_data(b"\x3e\x3etC")  # junk left over from an aborted transmit
    toy = IrToy(reader, writer)
    await toy.setup()
    assert toy.ready


async def test_concurrent_transmits_serialize():
    device, toy = await open_fake()
    await asyncio.gather(*(toy.transmit(NEC_START) for _ in range(5)))
    assert len(device.transmits) == 5
    assert all(t.units == [429, 214, 27] for t in device.transmits)
