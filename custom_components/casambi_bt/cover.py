"""Support for Casambi curtain motors."""

from __future__ import annotations

import asyncio
import logging
from typing import Any, cast

from CasambiBt import Unit, UnitControlType, _operation

from homeassistant.components.cover import (
    ATTR_POSITION,
    CoverDeviceClass,
    CoverEntity,
    CoverEntityFeature,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import CasambiApi
from .const import DOMAIN
from .curtain_profiles import (
    CURTAIN_PROFILES,
    CurtainProfile,
    pack_state,
    pct_to_raw,
    slider_to_pct,
    slider_to_raw,
)
from .entities import CasambiUnitEntity, TypedEntityDescription

_LOGGER = logging.getLogger(__name__)


def _profile_for(unit: Unit) -> CurtainProfile | None:
    """Return the curtain profile for a unit if it is a known, matching motor."""
    profile = CURTAIN_PROFILES.get(unit.unitType.id)
    if profile is None:
        return None

    slider = unit.unitType.get_control(UnitControlType.SLIDER)
    if (
        unit.unitType.stateLength != profile.state_length
        or slider is None
        or slider.offset != profile.pos_offset
        or slider.length != profile.pos_length
    ):
        _LOGGER.warning(
            "Unit %s (%i) has curtain fixture ID %i but its controls do not match "
            "the expected layout (stateLength=%i, slider=%s); skipping it.",
            unit.name,
            unit.deviceId,
            unit.unitType.id,
            unit.unitType.stateLength,
            slider,
        )
        return None

    return profile


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Create the Casambi curtain entities."""
    casa_api: CasambiApi = hass.data[DOMAIN][config_entry.entry_id]

    entities: list[CasambiCurtain] = []
    for unit in casa_api.get_units():
        profile = _profile_for(unit)
        if profile is not None:
            entities.append(CasambiCurtain(casa_api, unit, profile))

    _LOGGER.info("Adding %i curtain entities.", len(entities))
    async_add_entities(entities)


class CasambiCurtain(CasambiUnitEntity, CoverEntity):
    """A Casambi curtain motor."""

    _attr_device_class = CoverDeviceClass.CURTAIN

    def __init__(self, api: CasambiApi, unit: Unit, profile: CurtainProfile) -> None:
        """Initialize a Casambi curtain entity."""
        desc = TypedEntityDescription(key=unit.uuid, name=None, entity_type="cover")
        super().__init__(api, desc, unit)
        self._profile = profile

        features = (
            CoverEntityFeature.OPEN
            | CoverEntityFeature.CLOSE
            | CoverEntityFeature.SET_POSITION
        )
        if profile.stop_bit is not None:
            features |= CoverEntityFeature.STOP
        self._attr_supported_features = features

    @property
    def _slider(self) -> int | None:
        unit = cast("Unit", self._obj)
        if unit.state is None:
            return None
        return unit.state.slider

    @property
    def current_cover_position(self) -> int | None:
        """Return the current position (0 closed .. 100 open)."""
        slider = self._slider
        if slider is None:
            return None
        return slider_to_pct(self._profile, slider)

    @property
    def is_closed(self) -> bool | None:
        """Return True if the curtain is fully closed."""
        position = self.current_cover_position
        if position is None:
            return None
        return position == 0

    def _current_raw(self) -> int:
        slider = self._slider
        return 0 if slider is None else slider_to_raw(self._profile, slider)

    async def _send_state(self, payload: bytes) -> None:
        unit = cast("Unit", self._obj)
        await self._api.execute(
            lambda: self._api.casa._send(  # noqa: SLF001
                unit, payload, _operation.OpCode.SetState
            )
        )

    async def _press(self, bit: int) -> None:
        """Send the current position with a button bit set."""
        unit = cast("Unit", self._obj)
        pos_raw = self._current_raw()
        pressed = pack_state(self._profile, pos_raw, bit)
        release_ms = self._profile.press_release_ms

        if release_ms is None:
            await self._send_state(pressed)
            return

        released = pack_state(self._profile, pos_raw, None)

        async def press_and_release() -> None:
            # One execute() call covers the whole press so a retry repeats it.
            await self._api.casa._send(  # noqa: SLF001
                unit, pressed, _operation.OpCode.SetState
            )
            await asyncio.sleep(release_ms / 1000)
            await self._api.casa._send(  # noqa: SLF001
                unit, released, _operation.OpCode.SetState
            )

        await self._api.execute(press_and_release)

    async def async_set_cover_position(self, **kwargs: Any) -> None:
        """Move the curtain to a position."""
        pos_raw = pct_to_raw(self._profile, int(kwargs[ATTR_POSITION]))
        await self._send_state(pack_state(self._profile, pos_raw, None))

    async def async_open_cover(self, **kwargs: Any) -> None:
        """Open the curtain."""
        if self._profile.open_bit is None:
            await self.async_set_cover_position(**{ATTR_POSITION: 100})
            return
        await self._press(self._profile.open_bit)

    async def async_close_cover(self, **kwargs: Any) -> None:
        """Close the curtain."""
        if self._profile.close_bit is None:
            await self.async_set_cover_position(**{ATTR_POSITION: 0})
            return
        await self._press(self._profile.close_bit)

    async def async_stop_cover(self, **kwargs: Any) -> None:
        """Stop the curtain."""
        if self._profile.stop_bit is not None:
            await self._press(self._profile.stop_bit)
