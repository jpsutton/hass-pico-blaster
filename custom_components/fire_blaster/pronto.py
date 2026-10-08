# Vendored from fire-blaster (https://github.com/jpsutton/fire-blaster),
# src/fireblaster/pronto.py. Copyright (c) Jared Sutton. MIT License.
"""Pronto hex <-> pulse/space conversion.

A learned Pronto code is a list of 16-bit hex words:

    0000 FFFF NNNN RRRR  <NNNN burst pairs>  <RRRR burst pairs>

- word 0: code type. 0000 = learned, modulated; 0100 = learned, unmodulated.
- word 1: carrier frequency code. One unit is FFFF * 0.241246 us, which is also
  one carrier period, so carrier_hz = 1e6 / (FFFF * 0.241246).
- word 2: number of burst pairs in the "once" sequence (sent on first press).
- word 3: number of burst pairs in the "repeat" sequence (sent while held).
- burst pairs: (on, off) durations counted in carrier periods.

Durations everywhere else in this package are microseconds, alternating
pulse/space, starting with a pulse, the same convention `ir-ctl` uses.
"""

from __future__ import annotations

from dataclasses import dataclass

PRONTO_CLOCK_US = 0.241246
DEFAULT_GAP_US = 40000
DEFAULT_CARRIER_HZ = 38000  # the usual consumer IR carrier (NEC and most TVs)


class ProntoError(ValueError):
    pass


@dataclass(frozen=True)
class IrSignal:
    carrier: int  # Hz; 0 means unmodulated
    once: tuple[int, ...]
    repeat: tuple[int, ...] = ()

    def frame(self, repeat: bool = False) -> tuple[int, ...]:
        """Durations to send for a first press, or for a held-key repeat.

        Many learned codes only have a repeat sequence (once is empty); in that
        case the repeat sequence is also what a first press sends.
        """
        if repeat:
            return self.repeat or self.once
        return self.once or self.repeat


def decode(text: str) -> IrSignal:
    try:
        words = [int(w, 16) for w in text.split()]
    except ValueError:
        raise ProntoError(f"not a Pronto hex string: {text[:40]!r}") from None
    if len(words) < 4:
        raise ProntoError("Pronto code needs at least 4 header words")

    kind, freq, n_once, n_repeat = words[:4]
    if kind not in (0x0000, 0x0100):
        raise ProntoError(f"unsupported Pronto code type {kind:04X} (only learned codes 0000/0100)")
    if freq == 0:
        raise ProntoError("Pronto frequency word is zero")
    if n_once + n_repeat == 0:
        raise ProntoError("Pronto code has no burst pairs")
    needed = 4 + 2 * (n_once + n_repeat)
    if len(words) < needed:
        raise ProntoError(f"Pronto code truncated: header promises {needed} words, got {len(words)}")

    unit = freq * PRONTO_CLOCK_US
    durations = [round(w * unit) for w in words[4:needed]]
    carrier = round(1e6 / unit) if kind == 0x0000 else 0
    return IrSignal(carrier, tuple(durations[: 2 * n_once]), tuple(durations[2 * n_once :]))


def encode(carrier: int, once: list[int] | tuple[int, ...], repeat: list[int] | tuple[int, ...] = ()) -> str:
    """Encode a modulated signal as a learned (0000) Pronto code."""
    if carrier <= 0:
        raise ProntoError("encode needs a carrier frequency")
    if len(once) % 2 or len(repeat) % 2:
        raise ProntoError("sequences must be whole pulse/space pairs (end with a space)")
    if not once and not repeat:
        raise ProntoError("nothing to encode")

    freq = round(1e6 / (carrier * PRONTO_CLOCK_US))
    unit = freq * PRONTO_CLOCK_US
    bursts = [max(1, round(d / unit)) for d in (*once, *repeat)]
    if max(bursts) > 0xFFFF:
        raise ProntoError("a duration is too long to fit in a Pronto word at this carrier")
    words = [0x0000, freq, len(once) // 2, len(repeat) // 2, *bursts]
    return " ".join(f"{w:04X}" for w in words)


def parse_pulses(text: str) -> list[int]:
    """Parse `ir-ctl` style text ("+9000 -4500 +560 ...") into durations.

    `#` comments and `carrier`/`timeout` lines are ignored. Signs are checked
    so that pulses and spaces really alternate.
    """
    durations: list[int] = []
    for line in text.splitlines():
        line = line.split("#", 1)[0].strip()
        if not line or line.split()[0] in ("carrier", "timeout"):
            continue
        for token in line.split():
            if token[0] not in "+-":
                raise ProntoError(f"expected +pulse/-space token, got {token!r}")
            want = "+" if len(durations) % 2 == 0 else "-"
            if token[0] != want:
                raise ProntoError(f"pulse/space out of order at {token!r}")
            try:
                durations.append(int(token[1:]))
            except ValueError:
                raise ProntoError(f"bad duration {token!r}") from None
    if not durations:
        raise ProntoError("no durations found")
    return durations


def format_pulses(durations: tuple[int, ...] | list[int]) -> str:
    return " ".join(f"{'+' if i % 2 == 0 else '-'}{d}" for i, d in enumerate(durations))


