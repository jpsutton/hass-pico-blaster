"""Config flow: USB discovery or manual serial port, validated by talking to the stick."""

from __future__ import annotations

import glob
import logging
import os
from typing import Any

import voluptuous as vol
from homeassistant.components import usb
from homeassistant.config_entries import ConfigEntry, ConfigFlow, ConfigFlowResult, OptionsFlow
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.selector import (
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    SelectOptionDict,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
    TextSelector,
)
from homeassistant.helpers.service_info.usb import UsbServiceInfo

from .const import (
    CONF_CARRIER,
    CONF_PORT,
    CONF_PROFILES_DIR,
    CONF_SERIAL_NUMBER,
    DEFAULT_CARRIER,
    DEFAULT_PROFILES_SUBDIR,
    DOMAIN,
    USB_PID,
    USB_VID,
)
from .irtoy import MIN_CARRIER_HZ, IrToy, IrToyError

_LOGGER = logging.getLogger(__name__)


class CannotConnect(Exception):
    pass


class BadDevice(Exception):
    pass


def _kernel_has_ir_toy() -> bool:
    """True if the ir_toy kernel driver has claimed an IR Toy (so there is no tty)."""
    for uevent in glob.glob("/sys/class/rc/rc*/uevent"):
        try:
            with open(uevent, encoding="ascii") as f:
                if "DRV_NAME=ir_toy" in f.read():
                    return True
        except OSError:
            continue
    return False


def _list_ports() -> list[tuple[str, str, str | None]]:
    """(by-id or device path, label, serial number) for every USB serial port, IR Toys first."""
    from serial.tools import list_ports  # noqa: PLC0415

    out = []
    for p in list_ports.comports():
        if p.vid is None:
            continue  # onboard UARTs (ttyS*) can't be the stick
        path = usb.get_serial_by_id(p.device)
        is_toy = p.vid == USB_VID and p.pid == USB_PID
        label = usb.human_readable_device_name(
            p.device,
            p.serial_number,
            p.manufacturer,
            p.description,
            f"{p.vid:04X}" if p.vid is not None else None,
            f"{p.pid:04X}" if p.pid is not None else None,
        )
        out.append((is_toy, path, label, p.serial_number))
    out.sort(key=lambda t: (not t[0], t[1]))
    return [(path, label, serial) for _, path, label, serial in out]


async def _validate(hass: HomeAssistant, port: str) -> str:
    """Open the port, run setup, return the version string."""
    if not os.path.exists(port):
        if await hass.async_add_executor_job(_kernel_has_ir_toy):
            raise BadDevice("kernel_ir_toy")
        raise CannotConnect(f"{port} does not exist")
    try:
        toy = await IrToy.open_serial(port)
    except IrToyError as err:
        raise BadDevice(str(err)) from err
    except Exception as err:  # noqa: BLE001 - SerialException, OSError, ...
        raise CannotConnect(str(err)) from err
    version = toy.version
    await toy.close()
    return version


class PicoBlasterConfigFlow(ConfigFlow, domain=DOMAIN):
    VERSION = 1

    def __init__(self) -> None:
        self._port: str | None = None
        self._serial: str | None = None
        self._title: str = "IR blaster"
        self._version: str | None = None

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> PicoBlasterOptionsFlow:
        return PicoBlasterOptionsFlow()

    # -- USB discovery -----------------------------------------------------

    async def async_step_usb(self, discovery_info: UsbServiceInfo) -> ConfigFlowResult:
        self._port = await self.hass.async_add_executor_job(usb.get_serial_by_id, discovery_info.device)
        self._serial = discovery_info.serial_number
        unique_id = self._serial or self._port
        await self.async_set_unique_id(unique_id)
        self._abort_if_unique_id_configured(updates={CONF_PORT: self._port})

        manufacturer = discovery_info.manufacturer or ""
        is_pico_blaster = "fire-blaster" in manufacturer.lower()  # USB manufacturer string set by the firmware
        self._title = "Pico Blaster" if is_pico_blaster else "USB IR Toy"
        if self._serial:
            self._title += f" {self._serial[-4:]}"

        try:
            self._version = await _validate(self.hass, self._port)
        except (CannotConnect, BadDevice) as err:
            _LOGGER.debug("USB discovery on %s: %s", self._port, err)
            return self.async_abort(reason="cannot_connect")

        self.context["title_placeholders"] = {"name": self._title, "port": self._port}
        return await self.async_step_usb_confirm()

    async def async_step_usb_confirm(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            return self._create()
        self._set_confirm_only()
        return self.async_show_form(
            step_id="usb_confirm",
            description_placeholders={"name": self._title, "port": self._port or "", "version": self._version or ""},
        )

    # -- manual ------------------------------------------------------------

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        ports = await self.hass.async_add_executor_job(_list_ports)

        if user_input is not None:
            port = (user_input.get("manual_port") or user_input.get(CONF_PORT) or "").strip()
            if not port:
                errors["base"] = "no_port"
            else:
                serial = next((s for p, _, s in ports if p == port), None)
                try:
                    version = await _validate(self.hass, port)
                except BadDevice as err:
                    errors["base"] = "kernel_ir_toy" if str(err) == "kernel_ir_toy" else "not_ir_toy"
                    _LOGGER.debug("validate %s: %s", port, err)
                except CannotConnect as err:
                    errors["base"] = "cannot_connect"
                    _LOGGER.debug("validate %s: %s", port, err)
                else:
                    await self.async_set_unique_id(serial or port)
                    self._abort_if_unique_id_configured(updates={CONF_PORT: port})
                    self._port, self._serial, self._version = port, serial, version
                    self._title = f"IR blaster {serial[-4:]}" if serial else "IR blaster"
                    return self._create()

        options = [SelectOptionDict(value=p, label=f"{label}  ({p})") for p, label, _ in ports]
        schema: dict[Any, Any] = {}
        if options:
            schema[vol.Optional(CONF_PORT, default=options[0]["value"])] = SelectSelector(
                SelectSelectorConfig(options=options, mode=SelectSelectorMode.DROPDOWN)
            )
        schema[vol.Optional("manual_port", default="")] = TextSelector()
        return self.async_show_form(step_id="user", data_schema=vol.Schema(schema), errors=errors)

    def _create(self) -> ConfigFlowResult:
        assert self._port
        return self.async_create_entry(
            title=self._title,
            data={CONF_PORT: self._port, CONF_SERIAL_NUMBER: self._serial},
            options={CONF_CARRIER: DEFAULT_CARRIER},
        )


class PicoBlasterOptionsFlow(OptionsFlow):
    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            return self.async_create_entry(data=user_input)
        opts = self.config_entry.options
        schema = vol.Schema(
            {
                vol.Required(CONF_CARRIER, default=opts.get(CONF_CARRIER, DEFAULT_CARRIER)): NumberSelector(
                    NumberSelectorConfig(
                        min=MIN_CARRIER_HZ, max=1_000_000, step=1, mode=NumberSelectorMode.BOX, unit_of_measurement="Hz"
                    )
                ),
                vol.Optional(CONF_PROFILES_DIR, default=opts.get(CONF_PROFILES_DIR, DEFAULT_PROFILES_SUBDIR)): TextSelector(),
            }
        )
        return self.async_show_form(step_id="init", data_schema=schema)
