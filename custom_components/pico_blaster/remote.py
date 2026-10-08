"""Remote entity: each command is a Pronto/pulse-text code or a profile function."""

from __future__ import annotations

import logging
from collections.abc import Iterable
from typing import Any

import voluptuous as vol
from homeassistant.components.remote import (
    ATTR_DELAY_SECS,
    ATTR_DEVICE,
    ATTR_NUM_REPEATS,
    DEFAULT_DELAY_SECS,
    RemoteEntity,
    RemoteEntityFeature,
)
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import entity_platform
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import PicoBlasterConfigEntry
from .const import ATTR_CARRIER, ATTR_CODE, ATTR_DELAY, ATTR_REPEAT, CONF_SERIAL_NUMBER, DOMAIN, SERVICE_SEND_IR
from .device import PicoBlasterDevice, ProfileSendError, SendError, parse_code
from .irtoy import MIN_CARRIER_HZ
from .pronto import ProntoError

_LOGGER = logging.getLogger(__name__)

SEND_IR_SCHEMA = {
    vol.Required(ATTR_CODE): cv.string,
    vol.Optional(ATTR_CARRIER): vol.All(vol.Coerce(int), vol.Range(min=MIN_CARRIER_HZ)),
    vol.Optional(ATTR_REPEAT, default=1): vol.All(vol.Coerce(int), vol.Range(min=1, max=50)),
    vol.Optional(ATTR_DELAY, default=0.0): vol.All(vol.Coerce(float), vol.Range(min=0, max=10)),
}


async def async_setup_entry(
    hass: HomeAssistant, entry: PicoBlasterConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    async_add_entities([PicoBlasterRemote(entry)])
    platform = entity_platform.async_get_current_platform()
    platform.async_register_entity_service(SERVICE_SEND_IR, SEND_IR_SCHEMA, "async_send_ir")


class PicoBlasterRemote(RemoteEntity):
    _attr_has_entity_name = True
    _attr_name = None
    _attr_should_poll = False
    _attr_supported_features = RemoteEntityFeature(0)
    _attr_is_on = True
    _attr_assumed_state = True

    def __init__(self, entry: PicoBlasterConfigEntry) -> None:
        self._device: PicoBlasterDevice = entry.runtime_data
        self._attr_unique_id = entry.unique_id or entry.entry_id
        serial = entry.data.get(CONF_SERIAL_NUMBER)
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, self._attr_unique_id)},
            name=entry.title,
            manufacturer="Pico Blaster",  # USB string on the stick reads "fire-blaster" (the firmware project)
            model="Pico IR stick (IR Toy compatible)",
            serial_number=serial,
            sw_version=self._device.version,
        )

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(self._device.add_listener(self.async_write_ha_state))

    @property
    def available(self) -> bool:
        return self._device.available

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {
            "port": self._device.port,
            "firmware": self._device.version,
            "default_carrier": self._device.default_carrier,
            "profiles": sorted(self._device.profiles.by_id),
        }

    async def async_turn_on(self, **kwargs: Any) -> None:
        self._attr_is_on = True
        self.async_write_ha_state()

    async def async_turn_off(self, **kwargs: Any) -> None:
        # Nothing to turn off; keep the entity honest.
        self._attr_is_on = True
        self.async_write_ha_state()

    async def async_send_command(self, command: Iterable[str], **kwargs: Any) -> None:
        profile = kwargs.get(ATTR_DEVICE)
        repeats = int(kwargs.get(ATTR_NUM_REPEATS, 1))
        delay = float(kwargs.get(ATTR_DELAY_SECS, DEFAULT_DELAY_SECS))
        for cmd in command:
            try:
                signal = self._device.resolve(profile, cmd)
            except ProfileSendError as err:
                raise ServiceValidationError(str(err)) from err
            except SendError as err:
                raise ServiceValidationError(str(err)) from err
            try:
                await self._device.send_signal(signal, repeats=repeats, delay=delay)
            except SendError as err:
                raise HomeAssistantError(str(err)) from err

    async def async_send_ir(self, code: str, carrier: int | None = None, repeat: int = 1, delay: float = 0.0) -> None:
        """pico_blaster.send_ir entity service."""
        try:
            signal = parse_code(code, carrier or self._device.default_carrier)
        except ProntoError as err:
            raise ServiceValidationError(f"bad IR code: {err}") from err
        if carrier and signal.carrier != carrier and code.strip()[0] not in "+-":
            _LOGGER.debug("send_ir: Pronto code carries %d Hz, overriding with %d Hz", signal.carrier, carrier)
            signal = type(signal)(carrier, signal.once, signal.repeat)
        try:
            await self._device.send_signal(signal, repeats=repeat, delay=delay)
        except SendError as err:
            raise HomeAssistantError(str(err)) from err
