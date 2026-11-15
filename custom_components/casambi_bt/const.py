"""Constants for the Casambi Bluetooth integration."""

from typing import Final

from homeassistant.const import Platform

DOMAIN: Final = "casambi_bt"

PLATFORMS = [
    Platform.BINARY_SENSOR,
    Platform.COVER,
    Platform.EVENT,
    Platform.LIGHT,
    Platform.NUMBER,
    Platform.SCENE,
]

CONF_IMPORT_GROUPS: Final = "import_groups"

# Fired on the HA event bus for every (de-duplicated) wall-switch event.
SWITCH_EVENT: Final = f"{DOMAIN}_switch_event"

# Event types exposed by the button event entities.
BUTTON_EVENT_TYPES: Final = ["press", "release", "hold", "release_after_hold"]
