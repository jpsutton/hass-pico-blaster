"""Tests for the HA-free parts of device.py, pronto.py and profiles.py."""

from pathlib import Path

import pytest

from custom_components.pico_blaster import pronto
from custom_components.pico_blaster.device import PicoBlasterDevice, ProfileSendError, SendError, parse_code
from custom_components.pico_blaster.irtoy import IrToy
from custom_components.pico_blaster.profiles import Profile, ProfileSet

from .fake_irtoy import FakeIrToy, make_pair

RC6_VOLUME_UP = (
    "+2664 -888 +444 -444 +444 -444 +444 -888 +444 -888 +1332 -888 +444 -444 +444 -444 +444 -444 +444 -444 "
    "+444 -444 +444 -444 +444 -444 +444 -444 +444 -444 +444 -444 +888 -444 +444 -444 +444 -444 +444 -888 "
    "+444 -444 +444 -444 +444 -444 +444 -444 +888 -888 +444 -444 +444 -444 +444 -444 +444 -444 +888 -888 "
    "+444 -444 +444 -444 +444 -444 +444"
)
NEC_PRONTO = "0000 006D 0003 0000 0157 00AB 0015 0015 0015 0E6C"


def test_parse_code_pulse_text():
    sig = parse_code(RC6_VOLUME_UP, 36000)
    assert sig.carrier == 36000
    assert len(sig.once) % 2 == 0  # gap appended
    assert sig.once[-1] == pronto.DEFAULT_GAP_US
    assert sig.once[:3] == (2664, 888, 444)


def test_parse_code_pronto():
    sig = parse_code(NEC_PRONTO, 36000)
    assert abs(sig.carrier - 38000) < 100  # 006D: the code's own carrier wins over the default
    assert len(sig.once) == 6
    assert abs(sig.once[0] - 9000) < 50


def test_parse_code_bad():
    with pytest.raises(pronto.ProntoError):
        parse_code("hello world", 38000)
    with pytest.raises(pronto.ProntoError):
        parse_code("+100 +100", 38000)


def test_pronto_roundtrip():
    enc = pronto.encode(38000, [9000, 4500, 560, 40000])
    sig = pronto.decode(enc)
    assert abs(sig.carrier - 38000) < 100
    assert all(abs(a - b) <= 30 for a, b in zip(sig.once, (9000, 4500, 560, 40000)))


def test_profile_from_toml(tmp_path: Path):
    (tmp_path / "denon.toml").write_text(
        'id = "denon-avr"\nbrand = "Denon"\nname = "AVR"\ncarrier = 36000\n'
        '[codes]\nVOLUME_UP = "+900 -450 +560"\n'
        'POWER_TOGGLE = ["0000 006D 0001 0000 0157 00AB", "0000 006D 0001 0000 0157 0056"]\n'
    )
    (tmp_path / "broken.toml").write_text("this is not toml = = =\n")
    ps = ProfileSet.load([tmp_path])
    assert len(ps) == 1
    p = ps.get("denon-avr")
    assert isinstance(p, Profile)
    assert p.carrier == 36000
    v = p.variants("VOLUME_UP")
    assert len(v) == 1 and v[0].carrier == 36000 and v[0].once == (900, 450, 560, pronto.DEFAULT_GAP_US)
    assert len(p.variants("POWER_TOGGLE")) == 2
    assert p.variants("MISSING") == ()


async def make_device(tmp_path: Path | None = None) -> tuple[FakeIrToy, PicoBlasterDevice]:
    fake, reader, writer = make_pair()
    dev = PicoBlasterDevice("fake", "Fake", carrier=38000, profiles_dir=tmp_path)
    dev.toy = IrToy(reader, writer, name="fake")
    await dev.toy.setup()
    dev.version = dev.toy.version
    if tmp_path:
        dev.load_profiles()
    return fake, dev


async def test_device_send_pulse_text():
    fake, dev = await make_device()
    sig = dev.resolve(None, "+9000 -4500 +560")
    await dev.send_signal(sig)
    assert len(fake.transmits) == 1
    assert fake.transmits[0].units == [429, 214, 27]
    assert fake.transmits[0].carrier_hz == 37974


async def test_device_send_pronto_uses_own_carrier():
    fake, dev = await make_device()
    sig = dev.resolve(None, "0000 0073 0003 0000 0060 0020 0010 0010 0010 0400")  # 0x73 -> ~36 kHz
    await dev.send_signal(sig)
    assert fake.transmits[0].carrier_hz == 36144


async def test_device_repeats_and_pronto_repeat_frame():
    fake, dev = await make_device()
    # once = 3 bursts, repeat = 1 burst pair
    sig = dev.resolve(None, "0000 006D 0002 0001 0157 00AB 0015 0015 0157 0056")
    await dev.send_signal(sig, repeats=3, delay=0)
    assert len(fake.transmits) == 3
    assert len(fake.transmits[0].units) == 3  # once frame (trailing space dropped)
    assert len(fake.transmits[1].units) == 1  # repeat frame
    assert len(fake.transmits[2].units) == 1


async def test_device_profile_toggle_variants(tmp_path: Path):
    (tmp_path / "tv.toml").write_text(
        'id = "tv"\nbrand = "X"\nname = "TV"\n[codes]\nPOWER_TOGGLE = ["+100 -100 +100", "+200 -200 +200"]\n'
    )
    fake, dev = await make_device(tmp_path)
    for expected in ([5, 5, 5], [10, 10, 10], [5, 5, 5]):
        await dev.send_signal(dev.resolve("tv", "POWER_TOGGLE"))
        assert fake.transmits[-1].units == expected
    with pytest.raises(ProfileSendError, match="unknown profile"):
        dev.resolve("nope", "POWER_TOGGLE")
    with pytest.raises(ProfileSendError, match="no usable code"):
        dev.resolve("tv", "VOLUME_UP")


async def test_device_bad_code():
    fake, dev = await make_device()
    with pytest.raises(SendError, match="bad IR code"):
        dev.resolve(None, "garbage")


async def test_device_not_connected():
    dev = PicoBlasterDevice("/dev/null", "x")
    assert not dev.available
    with pytest.raises(SendError, match="not connected"):
        await dev.send_frame([100, 100, 100], 38000)


async def test_device_protocol_error_resetup_in_place():
    fake, dev = await make_device()
    fake.wrong_count = True
    with pytest.raises(SendError):
        await dev.send_frame([100, 100, 100], 38000)
    # setup was re-run in place; device still available
    assert dev.available
    fake.wrong_count = False
    await dev.send_frame([100, 100, 100], 38000)
    assert len(fake.transmits) == 2
