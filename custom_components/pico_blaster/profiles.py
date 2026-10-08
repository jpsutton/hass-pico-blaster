# Copyright (c) Jared Sutton. MIT License.
# Shared with the author's fire-blaster firmware project (same code, same license).
"""IR code profiles: one TOML file per device code set.

    id = "amazon-4495"
    brand = "LG"
    name = "LG TV"
    device_type = "tv"
    confidence = 6          # optional, higher sorts first in setup mode
    blast_count = 1         # optional, frames per transmission
    carrier = 38000         # optional, only used by pulse-text codes

    [codes]
    POWER_TOGGLE = ["0000 006D ...", "0000 006D ..."]   # toggle-bit variants alternate per press
    VOLUME_UP = "+9000 -4500 +560 ..."                    # ir-ctl pulse text also works

Function names follow the Amazon database (VOLUME_UP, VOLUME_DOWN,
MUTE_TOGGLE, POWER_TOGGLE, POWER_ON, POWER_OFF, INPUT_SCROLL, ...).
"""

from __future__ import annotations

import logging
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from . import pronto
from .pronto import IrSignal, ProntoError

log = logging.getLogger(__name__)



@dataclass
class Profile:
    id: str
    brand: str
    name: str
    device_type: str
    codes: dict[str, tuple[str, ...]]
    confidence: int = 0
    blast_count: int = 1
    carrier: int = pronto.DEFAULT_CARRIER_HZ
    path: Path | None = None
    _decoded: dict[str, tuple[IrSignal, ...]] = field(default_factory=dict, repr=False, compare=False)

    @property
    def label(self) -> str:
        return f"{self.brand} / {self.name} [{self.id}]"

    def has(self, function: str) -> bool:
        return bool(self.codes.get(function))

    def variants(self, function: str) -> tuple[IrSignal, ...]:
        """Decoded signals for a function; empty if missing or undecodable."""
        if function not in self._decoded:
            signals = []
            for text in self.codes.get(function, ()):
                try:
                    signals.append(self._decode(text))
                except ProntoError as e:
                    log.warning("%s: bad %s code: %s", self.label, function, e)
            self._decoded[function] = tuple(signals)
        return self._decoded[function]

    def _decode(self, text: str) -> IrSignal:
        if text.lstrip()[:1] in ("+", "-"):
            durations = pronto.parse_pulses(text)
            if len(durations) % 2:
                durations.append(pronto.DEFAULT_GAP_US)
            return IrSignal(self.carrier, tuple(durations))
        return pronto.decode(text)

    @classmethod
    def from_toml(cls, path: Path) -> Profile:
        with open(path, "rb") as f:
            data = tomllib.load(f)
        codes: dict[str, tuple[str, ...]] = {}
        for function, value in data.get("codes", {}).items():
            variants = (value,) if isinstance(value, str) else tuple(value)
            codes[function] = tuple(v for v in variants if v.strip())
        return cls(
            id=data.get("id") or path.stem,
            brand=data.get("brand", "Unknown"),
            name=data.get("name", path.stem),
            device_type=data.get("device_type", "tv"),
            codes=codes,
            confidence=int(data.get("confidence", 0)),
            blast_count=max(1, int(data.get("blast_count", 1))),
            carrier=int(data.get("carrier", pronto.DEFAULT_CARRIER_HZ)),
            path=path,
        )


class ProfileSet:
    def __init__(self, profiles: list[Profile]):
        self.by_id = {p.id: p for p in profiles}

    def __len__(self) -> int:
        return len(self.by_id)

    def get(self, profile_id: str | None) -> Profile | None:
        return self.by_id.get(profile_id) if profile_id else None

    @classmethod
    def load(cls, dirs: list[Path]) -> ProfileSet:
        """Load every *.toml under the given dirs; later dirs override earlier ids."""
        found: dict[str, Profile] = {}
        for d in dirs:
            if not d.is_dir():
                continue
            for path in sorted(d.rglob("*.toml")):
                try:
                    profile = Profile.from_toml(path)
                except (OSError, tomllib.TOMLDecodeError, ValueError, TypeError) as e:
                    log.warning("skipping profile %s: %s", path, e)
                    continue
                found[profile.id] = profile
        log.info("loaded %d profiles from %s", len(found), ", ".join(map(str, dirs)))
        return cls(list(found.values()))

