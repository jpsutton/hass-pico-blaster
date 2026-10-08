"""Pico Blaster: send IR through a Pico Blaster IR stick (USB IR Toy compatible)."""

from __future__ import annotations

import logging
from pathlib import Path

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import ConfigEntryNotReady

from .const import (
    CONF_CARRIER,
    CONF_PORT,
    CONF_PROFILES_DIR,
    DEFAULT_CARRIER,
    DEFAULT_PROFILES_SUBDIR,
    DOMAIN,
    SERVICE_RELOAD_PROFILES,
)
from .device import PicoBlasterDevice

_LOGGER = logging.getLogger(__name__)

PLATFORMS = [Platform.REMOTE]

type PicoBlasterConfigEntry = ConfigEntry[PicoBlasterDevice]


def _profiles_dir(hass: HomeAssistant, entry: ConfigEntry) -> Path:
    configured = entry.options.get(CONF_PROFILES_DIR)
    if configured:
        p = Path(configured)
        return p if p.is_absolute() else Path(hass.config.path(configured))
    return Path(hass.config.path(DEFAULT_PROFILES_SUBDIR))


async def async_setup_entry(hass: HomeAssistant, entry: PicoBlasterConfigEntry) -> bool:
    device = PicoBlasterDevice(
        port=entry.data[CONF_PORT],
        name=entry.title,
        carrier=int(entry.options.get(CONF_CARRIER, DEFAULT_CARRIER)),
        profiles_dir=_profiles_dir(hass, entry),
    )
    count = await hass.async_add_executor_job(device.load_profiles)
    _LOGGER.debug("%s: %d profiles from %s", entry.title, count, device.profiles_dir)

    try:
        await device.connect()
    except Exception as err:  # noqa: BLE001 - serial/protocol errors of any kind
        await device.close()
        raise ConfigEntryNotReady(f"cannot open {device.port}: {err}") from err

    entry.runtime_data = device
    entry.async_on_unload(entry.add_update_listener(_async_options_updated))
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    if not hass.services.has_service(DOMAIN, SERVICE_RELOAD_PROFILES):

        async def _reload_profiles(call: ServiceCall) -> None:
            for e in hass.config_entries.async_loaded_entries(DOMAIN):
                dev: PicoBlasterDevice = e.runtime_data
                n = await hass.async_add_executor_job(dev.load_profiles)
                dev.notify()
                _LOGGER.info("%s: reloaded %d profiles from %s", e.title, n, dev.profiles_dir)

        hass.services.async_register(DOMAIN, SERVICE_RELOAD_PROFILES, _reload_profiles)

    return True


async def _async_options_updated(hass: HomeAssistant, entry: ConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: PicoBlasterConfigEntry) -> bool:
    ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    await entry.runtime_data.close()
    if not hass.config_entries.async_loaded_entries(DOMAIN):
        hass.services.async_remove(DOMAIN, SERVICE_RELOAD_PROFILES)
    return ok
