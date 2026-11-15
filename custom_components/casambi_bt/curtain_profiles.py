"""Curtain motor profiles and state packing helpers.

This module deliberately imports neither Home Assistant nor CasambiBt so the
bit packing can be unit tested on its own.

The casambi-bt library discards the names of controls it does not know (the
curtain's Stop/Open/Close are of type "button" and become UNKOWN), so motors
are recognised by their Casambi fixture ID instead of by heuristics.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

# The library scales SLIDER controls to 8 bits when parsing state:
# state.slider = raw << (8 - length).
SLIDER_RESOLUTION: Final = 8


@dataclass(frozen=True)
class CurtainProfile:
    """Describes how a curtain motor lays out its state bytes."""

    stop_bit: int | None
    open_bit: int | None
    close_bit: int | None
    pos_offset: int
    pos_length: int
    # True if raw position 0 means fully OPEN (Home Assistant uses 0 == closed).
    inverted: bool
    # If set, a "press" is followed after this many ms by the same state with
    # the press bit cleared.
    press_release_ms: int | None
    state_length: int

    @property
    def pos_max(self) -> int:
        """Return the largest raw position value."""
        return (1 << self.pos_length) - 1


CURTAIN_PROFILES: Final[dict[int, CurtainProfile]] = {
    # siqitech M-CMPN-02, mode EXT/Elements, fixture
    # https://api.casambi.com/fixture/36404
    36404: CurtainProfile(
        stop_bit=0,
        open_bit=1,
        close_bit=2,
        pos_offset=3,
        pos_length=7,
        inverted=False,  # UNVERIFIED - to be set from hardware experiment E1
        press_release_ms=None,  # UNVERIFIED - to be set from experiment E2
        state_length=2,
    ),
}


def pack_state(profile: CurtainProfile, pos_raw: int, press_bit: int | None) -> bytes:
    """Build the outgoing state bytes for a position and an optional press bit."""
    value = (pos_raw & profile.pos_max) << profile.pos_offset
    if press_bit is not None:
        value |= 1 << press_bit
    return value.to_bytes(profile.state_length, "little")


def pct_to_raw(profile: CurtainProfile, pct: int) -> int:
    """Convert a Home Assistant position (0 closed .. 100 open) to a raw value."""
    pct = max(0, min(100, pct))
    if profile.inverted:
        pct = 100 - pct
    return round(pct * profile.pos_max / 100)


def slider_to_raw(profile: CurtainProfile, slider: int) -> int:
    """Undo the library's 8-bit scaling of the slider control."""
    return slider >> (SLIDER_RESOLUTION - profile.pos_length)


def slider_to_pct(profile: CurtainProfile, slider: int) -> int:
    """Convert the library's 8-bit slider value to a Home Assistant position."""
    pct = round(slider_to_raw(profile, slider) * 100 / profile.pos_max)
    return 100 - pct if profile.inverted else pct
