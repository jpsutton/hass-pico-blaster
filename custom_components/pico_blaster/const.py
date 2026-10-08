"""Constants for the Pico Blaster integration."""

from __future__ import annotations

DOMAIN = "pico_blaster"

CONF_PORT = "port"
CONF_SERIAL_NUMBER = "serial_number"
CONF_CARRIER = "carrier"
CONF_PROFILES_DIR = "profiles_dir"

DEFAULT_CARRIER = 38000
DEFAULT_PROFILES_SUBDIR = "pico_blaster/profiles"  # under /config

USB_VID = 0x04D8
USB_PID = 0xFD08

ATTR_CODE = "code"
ATTR_CARRIER = "carrier"
ATTR_REPEAT = "repeat"
ATTR_DELAY = "delay"

SERVICE_SEND_IR = "send_ir"
SERVICE_RELOAD_PROFILES = "reload_profiles"

# Reconnect backoff, seconds.
RECONNECT_MIN = 2.0
RECONNECT_MAX = 60.0
