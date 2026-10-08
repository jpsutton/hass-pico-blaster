"""Stub the few Home Assistant modules the package __init__ imports, so the
HA-free modules (irtoy, pronto, profiles, device) can be tested without HA
installed. If homeassistant is importable, the real one is used."""

import sys
import types

try:
    import homeassistant  # noqa: F401
except ImportError:
    def _mod(name, **attrs):
        m = types.ModuleType(name)
        m.__dict__.update(attrs)
        sys.modules[name] = m
        return m

    class _Any:
        def __class_getitem__(cls, item):
            return cls

        def __getattr__(self, name):
            return _Any()

    _mod("homeassistant")
    _mod("homeassistant.config_entries", ConfigEntry=_Any)
    _mod("homeassistant.const", Platform=_Any())
    _mod("homeassistant.core", HomeAssistant=_Any, ServiceCall=_Any)
    _mod("homeassistant.exceptions", ConfigEntryNotReady=Exception)
