"""Event entities for Casambi wall-switch buttons."""

from __future__ import annotations

import logging
import re

from CasambiBt import Unit
from CasambiBt._switch import ButtonEventType, SwitchEvent

from homeassistant.components.event import EventDeviceClass, EventEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import CasambiApi
from .const import BUTTON_EVENT_TYPES, DOMAIN
from .entities import CasambiUnitEntity, TypedEntityDescription

_LOGGER = logging.getLogger(__name__)

_BUTTON_SUFFIX = re.compile(r"-button-(\d+)$")


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up button event entities.

    The integration can't tell in advance which units are switches or how many
    buttons they have, so an entity is created the first time a button is
    pressed. Entities seen before are re-created from the entity registry at
    startup so automations keep working before the next press.
    """
    casa_api: CasambiApi = hass.data[DOMAIN][config_entry.entry_id]
    entities: dict[tuple[int, int], CasambiButtonEvent] = {}

    units_by_uuid = {u.uuid: u for u in casa_api.get_units()}
    prefix = f"{casa_api.casa.networkId}-unit-"
    registry = er.async_get(hass)
    for entry in er.async_entries_for_config_entry(registry, config_entry.entry_id):
        if entry.domain != Platform.EVENT or not entry.unique_id.startswith(prefix):
            continue
        match = _BUTTON_SUFFIX.search(entry.unique_id)
        if match is None:
            continue
        uuid = entry.unique_id[len(prefix) : match.start()]
        unit = units_by_uuid.get(uuid)
        if unit is None:
            _LOGGER.debug("Not restoring %s: unit is gone.", entry.entity_id)
            continue
        button = int(match.group(1))
        entities[(unit.deviceId, button)] = CasambiButtonEvent(casa_api, unit, button)

    _LOGGER.info("Restoring %i button event entities.", len(entities))
    async_add_entities(list(entities.values()))

    @callback
    def handle_switch_event(ev: SwitchEvent) -> None:
        if ev.event == ButtonEventType.UNKNOWN:
            return

        entity = entities.get((ev.unit_id, ev.button))
        if entity is not None:
            entity.handle_event(ev)
            return

        unit = casa_api.get_unit_by_device_id(ev.unit_id)
        if unit is None:
            _LOGGER.debug("Switch event for unknown unit %i; ignoring.", ev.unit_id)
            return

        _LOGGER.info(
            "Creating event entity for button %i of unit %s.", ev.button, unit.name
        )
        entity = CasambiButtonEvent(casa_api, unit, ev.button)
        entities[(ev.unit_id, ev.button)] = entity
        # The entity is added asynchronously; it fires this event once added.
        entity.handle_event(ev)
        async_add_entities([entity])

    config_entry.async_on_unload(casa_api.register_switch_listener(handle_switch_event))


class CasambiButtonEvent(CasambiUnitEntity, EventEntity):
    """One button of a Casambi wall switch."""

    _attr_device_class = EventDeviceClass.BUTTON
    _attr_event_types = BUTTON_EVENT_TYPES

    def __init__(self, api: CasambiApi, unit: Unit, button: int) -> None:
        """Initialize a button event entity."""
        desc = TypedEntityDescription(
            key=unit.uuid, name=f"Button {button}", entity_type=f"button-{button}"
        )
        super().__init__(api, desc, unit)
        self._button = button
        self._added = False
        self._pending: SwitchEvent | None = None

    @callback
    def handle_event(self, ev: SwitchEvent) -> None:
        """Record a switch event for this button."""
        if not self._added:
            # Not in HA yet (just created) or disabled: keep the latest event
            # so a freshly created entity still reports the press that made it.
            self._pending = ev
            return
        self._trigger_event(
            ev.event.name.lower(), {"button": ev.button, "action": ev.action}
        )
        self.async_write_ha_state()

    async def async_added_to_hass(self) -> None:
        """Run when the entity is added to hass."""
        await super().async_added_to_hass()
        self._added = True
        if self._pending is not None:
            ev, self._pending = self._pending, None
            self.handle_event(ev)

    async def async_will_remove_from_hass(self) -> None:
        """Run when the entity will be removed from hass."""
        self._added = False
        await super().async_will_remove_from_hass()
