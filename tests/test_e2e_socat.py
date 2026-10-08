"""End-to-end over a real pty pair: socat makes two ptys, the fake device sits
on one, `IrToy.open_serial` (pyserial-asyncio-fast) on the other.

Skipped when socat or serial_asyncio_fast is missing.
"""

import asyncio
import os
import shutil
import tempfile

import pytest

from custom_components.fire_blaster.irtoy import IrToy

from .fake_irtoy import FakeIrToy, serve_stream

pytest.importorskip("serial_asyncio_fast")
pytestmark = pytest.mark.skipif(shutil.which("socat") is None, reason="socat not installed")


async def _wait_for(path: str, timeout: float = 5.0) -> None:
    loop = asyncio.get_running_loop()
    end = loop.time() + timeout
    while not os.path.exists(path):
        if loop.time() > end:
            raise TimeoutError(path)
        await asyncio.sleep(0.05)


async def test_e2e_over_pty():
    from serial_asyncio_fast import open_serial_connection

    d = tempfile.mkdtemp()
    host, dev = os.path.join(d, "host"), os.path.join(d, "dev")
    proc = await asyncio.create_subprocess_exec(
        "socat", "-d", "-d", f"pty,raw,echo=0,link={host}", f"pty,raw,echo=0,link={dev}",
        stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
    )
    try:
        await _wait_for(host)
        await _wait_for(dev)
        fake = FakeIrToy()
        dreader, dwriter = await open_serial_connection(url=dev, baudrate=115200)
        server = asyncio.create_task(serve_stream(fake, dreader, dwriter))

        toy = await IrToy.open_serial(host)
        assert toy.version == "V222"
        assert await toy.set_carrier(36000) == 36144
        await toy.transmit([9000, 4500, 560])
        many = [210 + 21 * (i % 50) for i in range(1023)]
        await toy.transmit(many, carrier_hz=38000)
        await asyncio.sleep(0.05)
        assert len(fake.transmits) == 2
        assert fake.transmits[0].units == [429, 214, 27]
        assert fake.transmits[0].carrier_hz == 36144
        assert len(fake.transmits[1].units) == 1023
        assert fake.transmits[1].carrier_hz == 37974
        await toy.close()
        server.cancel()
        dwriter.close()
    finally:
        proc.terminate()
        await proc.wait()
