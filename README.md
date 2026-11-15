# Home Assistant integration for Casambi using Bluetooth

[![Discord](https://img.shields.io/discord/1186445089317326888)](https://discord.gg/jgZVugfx)

This is a Home Assistant integration for Casambi networks using Bluetooth. Since this is an unofficial implementation of the rather complex undocumented protocol used by the Casambi app there may be issues in networks configured differently to the one used to test this integration.
Please see the information below on how to report such issues.

This repository is a fork of [lkempf/casambi-bt-hass](https://github.com/lkempf/casambi-bt-hass). It adds a reconnect watchdog (no HA restart needed after a node loses power), support for curtain motors and wall-switch button events.

A more mature HA integration for Casambi networks can be found under [https://github.com/hellqvio86/home_assistant_casambi](https://github.com/hellqvio86/home_assistant_casambi). This integration requires a network gateway to always connect the network to the Casambi cloud.

## Network configuration

See [https://github.com/lkempf/casambi-bt#casambi-network-setup](https://github.com/lkempf/casambi-bt#casambi-network-setup) for the proper network configuration. If you get "Unexcpected error" or "Failed to connect" different network configurations are the most common cause. Due to the high complexity of the protocol I won't be able to support all configurations allthough I might try if the suggested config doesn't work and the fix isn't to complex.

## Installation

### Manual

Place the `casambi_bt` folder in the `custom_components` folder.

### HACS

Add `https://github.com/Astogems/casambi-bt-hass-ctrlspace` as a custom repository (type "Integration") in the HACS store (HACS -> integrations -> custom repositories):

1. Setup HACS https://hacs.xyz/
2. Select HACS from the left sidebar
3. Search for `Casambi **Bluetooth**` in the searchbar at the top and select it. If you can't find it you might have to add this repository as a custom repository.
4. Click the Download button at the bottom right
5. Restart Home Assistant

## Features

Functionality exposed to HA:
- Lights
- Light groups
- Scenes
- Curtain motors as `cover` entities (see below)
- Wall-switch button presses as events (see below)

Supported control types:
- Dimmer
- White
- Rgb
- OnOff
- Temperature (Only for units since there are some open problems for groups.)
- Vertical

Not supported yet:
- Sensors
- Additional control types (e.g. temperature, ...)

### Curtain motors

Curtain motors are recognised by their Casambi fixture ID. Supported:

| Fixture ID | Model | Vendor |
|---|---|---|
| 36404 | M-CMPN-02 | siqitech |

Each motor becomes a `cover` entity (device class `curtain`) with open, close, stop and set position. Groups of motors don't get a cover.

Not yet verified on hardware: which end of the range is "open" and whether stop halts a moving curtain. If the cover moves the wrong way, please open an issue with a debug log.

### Wall-switch button events

Every button event from a wall switch fires `casambi_bt_switch_event` on the Home Assistant event bus:

| Field | Meaning |
|---|---|
| `entry_id` | Config entry of the network |
| `unit_id` | Casambi device ID of the switch |
| `unit_uuid`, `unit_name` | The switch unit, if known |
| `button` | Button number, as reported by the library |
| `event` | `press`, `release`, `hold`, `release_after_hold` or `unknown` |
| `action` | Raw action byte from the library (may be `null`) |
| `message_type`, `flags` | Raw protocol fields |

Identical events within 150 ms are dropped as BLE duplicates. Button numbers come from reverse engineering and may not match the physical layout; hold/release are less reliable than press.

Note for users of [rankjie's fork](https://github.com/rankjie/casambi-bt-hass): the event name and the `unit_id`/`button` fields match, but the event type is in `event` here, and `action` is a raw number rather than a string.

Each button also gets an `event` entity (`event.<switch>_button_<n>`, device class `button`). Because a switch doesn't tell the integration how many buttons it has, a button's entity is created the first time that button is pressed, so press every button once after installing. These entities are restored on restart.
- Networks with classic firmware

## Reporting issues

Before reporting issues make sure that you have the debug log enabled for all relevant components. This can be done by placing the following in `configuration.yaml` of your HA installation:

```yaml
logger:
  default: info
  logs:
    CasambiBt: debug
    custom_components.casambi_bt: debug
```

The log might contain sensitive information about the network (including your network password and the email address used for the network) so sanitize it first or mail it to the address on my github profile referencing your issue.

## Development

When developing you might also want to change [https://github.com/lkempf/casambi-bt](casambi-bt). To make this more convenient run
```
pip install -e PATH_TO_CASAMBI_BT_REPO
```
in the homeassistant venv and then start HA with
```
hass -c config --skip-pip-packages casambi-bt
```

If you are unsure what these terms mean you might want to have a look at [https://developers.home-assistant.io/docs/development_environment](https://developers.home-assistant.io/docs/development_environment) first.
