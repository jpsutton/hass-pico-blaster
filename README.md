# fire-blaster IR for Home Assistant

Home Assistant custom integration that sends infrared through a
[fire-blaster](https://github.com/jpsutton/fire-blaster) Pico IR stick (or a
real Dangerous Prototypes USB IR Toy, firmware v20 or newer) over its USB
serial port.

It exists for hosts whose kernel lacks the `ir_toy` driver, such as Home
Assistant OS on an ODROID-C4. There the stick enumerates as a plain CDC ACM
serial port (`/dev/ttyACM0`), and this integration speaks the IR Toy
"sampling mode" transmit protocol itself.

## Install

With HACS: add this repository as a custom repository (type: Integration),
install "fire-blaster IR", restart Home Assistant.

By hand: copy `custom_components/fire_blaster` into `/config/custom_components/`
and restart.

## Setup

Plug the stick in. Home Assistant discovers it by USB id (`04D8:FD08`) and
offers it under Settings → Devices & services → Discovered. Confirm, done.

Or add it manually: Settings → Devices & services → Add integration →
"fire-blaster IR", then pick the serial port. The `/dev/serial/by-id/...` path
is preferred; it survives replugs and reboots.

The flow opens the port and checks the firmware version before creating the
entry. If the host kernel *does* have `ir_toy`, the driver claims the stick as
a LIRC device and no serial port appears; the flow tells you so. LIRC is out
of scope here.

Options (gear icon on the integration):

| Option | Default | Meaning |
|---|---|---|
| Default carrier | 38000 Hz | Carrier for pulse-text codes (Pronto codes carry their own) |
| Profiles directory | `fire_blaster/profiles` | Where profile TOML files live, relative to `/config` or absolute |

## Entity

One `remote` entity per stick. `is_on` is always on. It goes unavailable when
the port disappears and comes back on its own (retry with backoff, 2 s to
60 s). One warning is logged per outage.

### `remote.send_command`

Each command string is one IR code:

- **ir-ctl pulse text**: `+9000 -4500 +560 -560 +560 ...` (sent at the
  default carrier option);
- **Pronto hex**: `0000 006D 0022 0002 ...` (carrier from the code).

```yaml
action: remote.send_command
target:
  entity_id: remote.fire_blaster_ir
data:
  command: "+9000 -4500 +560 -560 +560"
  num_repeats: 3
  delay_secs: 0.1
```

With profile files (below), `device` is a profile id and `command` a function
name:

```yaml
action: remote.send_command
target:
  entity_id: remote.fire_blaster_ir
data:
  device: denon-avr
  command: VOLUME_UP
```

### `fire_blaster.send_ir`

Same thing with explicit fields, for scripts and automations:

```yaml
action: fire_blaster.send_ir
target:
  entity_id: remote.fire_blaster_ir
data:
  code: "+2664 -888 +444 -444 ..."
  carrier: 36000
  repeat: 1
  delay: 0
```

`carrier` overrides the default for pulse text and the embedded frequency for
Pronto codes.

### `fire_blaster.reload_profiles`

Re-reads the profile directory without a restart.

## Profiles

fire-blaster profile files are TOML, one per device:

```toml
id = "denon-avr"
brand = "Denon"
name = "Denon AVR"
carrier = 38000           # for pulse-text codes

[codes]
VOLUME_UP = "+3450 -1600 +500 -300 ..."
POWER_TOGGLE = ["0000 006D ...", "0000 006D ..."]   # toggle-bit variants alternate per press
```

Put them under `/config/fire_blaster/profiles/` (or the configured directory).
Every `*.toml` below that directory is loaded; later files override earlier
ones with the same `id`. Loaded ids show up in the entity's `profiles`
attribute.

## Limits

- At most 2048 durations per frame (the firmware buffer); the entity drops a
  trailing space and sleeps it after the frame instead.
- Carrier at least 11800 Hz, quantized by the device's 8-bit timer (38000 Hz
  is really 37974 Hz). Duty cycle is fixed at 33% by the firmware.
- Keep frames under about 500 ms for compatibility with a real IR Toy.
- One transmit at a time per stick; concurrent calls queue.

## Verified

2026-10-07, HAOS 15.2 on ODROID-C4, HA 2025.9.1, RP2040-One stick, firmware V222:
`rc6_mce` VOLUME_UP pulse text sent at 36000 Hz through `fire_blaster.send_ir`
decoded as scancode `0x800f0410` (`KEY_VOLUMEUP`) on an `mceusb` receiver,
110 of 110 frames.

## Development

```bash
python -m venv .venv && .venv/bin/pip install pytest pytest-asyncio pyserial-asyncio-fast ruff
.venv/bin/pytest
```

The tests drive the protocol class against an in-process port of the
firmware's state machine (`tests/fake_irtoy.py`). `tests/test_e2e_socat.py`
runs the same thing across a real pty pair when `socat` is installed.
Home Assistant itself is not needed for the tests.

`pronto.py` and `profiles.py` are vendored from fire-blaster.

## License

MIT.
