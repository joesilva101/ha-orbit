"""Constants for the Orbit Garage Door Opener integration."""

from __future__ import annotations

from datetime import timedelta

DOMAIN = "orbit_gdo"

PLATFORMS = ["cover", "light", "button", "binary_sensor", "sensor"]

CONF_EMAIL = "email"
CONF_PASSWORD = "password"
#: Option: refuse open/close from Home Assistant while the wall console lockout
#: is on. Off by default, matching the phone app (the lockout only blocks the
#: wireless remotes).
CONF_RESPECT_LOCKOUT = "respect_lockout"

# State arrives as MQTT pushes within ~1s. This periodic full resync is only a
# safety net (missed pushes, reconnects).
DEFAULT_SCAN_INTERVAL = timedelta(seconds=120)

MANUFACTURER = "Skylink"
