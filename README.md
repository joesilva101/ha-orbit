# ha-orbit — Skylink Orbit garage door openers for Home Assistant

A custom Home Assistant integration (installable through the Marketplace, formerly
HACS) for **Skylink Orbit** Wi‑Fi garage door openers controlled by the
**Orbit Home** app (`com.mtqqdemo.skylink`).

Forked from [xerktech/ha-orbit](https://github.com/xerktech/ha-orbit), whose
integration scaffold used placeholder endpoints; this fork implements the real
protocol.

> **Not** the same as `bhyve-home-assistant` / `com.orbit.orbitsmarthome`, which
> are *Orbit Irrigation's* B‑hyve sprinklers — a different company that happens
> to share the name "Orbit".

## Status

✅ **Working against real hardware** (Skylink `GDO` hubs, firmware
`WF2012021040204`). The protocol was recovered from the Orbit Home Android app
and verified live:

* **Login** — signed REST call to `iot.skyhm.net:8444` (`cmd=act_login`).
* **Everything else** — MQTT over TLS to `iot.skyhm.net:1899`: device list,
  real‑time state pushes (≈1 s), and commands.

Details are in [`orbitclient/const.py`](custom_components/orbit_gdo/orbitclient/const.py).
Nova (`NOVA_A`/`NOVA_B`/`NVMini`) controllers are not yet supported.

## What you get

| Entity                     | Description                                                       |
| -------------------------- | ----------------------------------------------------------------- |
| Cover (`garage`)           | Open / close / stop, live state incl. opening/closing/part‑open   |
| Light                      | Opener light on/off (assumed state: the hub does not report it)   |
| Button "Door button"       | Raw wall‑button press (disabled by default)                       |
| Binary sensor "Remote lockout" | On while the wall console lockout is on (appears once the hub first reports it) |
| Binary sensor "Close refused" | On for a few seconds when a blocked photo‑eye stops the door closing |
| Binary sensor              | Connectivity, auto‑close active, door‑sensor battery/problem (diagnostic) |
| Sensor                     | Wi‑Fi signal (diagnostic, disabled by default)                    |

**Auto‑close active** is on while the door is counting down to close by itself:
for the whole timer‑to‑close period set on the wall console's selector switch
(it turns on when the door finishes opening, or stops part‑way while opening;
turning the switch off during the countdown turns it off), or for the ~10 s beep‑and‑flash
warning before a close sent from the app or Home Assistant. The selector
switch's position is never reported, so Home Assistant only learns the timer is
running once the door opens.

### How open / close work

The opener only accepts a single "button press", like the app's one button:
*open → stop → close → stop → …*. Home Assistant's Open, Close and Stop are
mapped onto it, and nothing is ever refused, so you never need the wall
console:

| Door is | Open | Close | Stop |
| --- | --- | --- | --- |
| Open / closed | Nothing if already there, otherwise one press | Same | — |
| Moving | Closing: stops it, then reverses it (two presses) | Opening: stops it, then reverses it (with the close warning) | One press |
| Stopped part‑way | One press | One press | — |
| In a close warning | Cancels the close | Disabled | Cancels the close |

**From part‑way, one press reverses the last direction**, whichever button
you use, exactly as in the app. So a door that stopped while opening will
close (after the warning), even from **Open**; the cover shows that at once,
and **Stop** cancels it during the warning. You can't open such a door further
without it moving down first: that's how the opener works. A part‑way door
shows as 50% open so both buttons stay enabled (50% is not a measurement: the
hub reports no position).

When reversing a moving door, the second press is only sent once the hub
reports the door stopped (about a second); if it doesn't, you get an error and
the door is left stopped.

**Closing from Home Assistant or the app is delayed ~10 s.** Whenever a remote
press would close the door (it's fully open, or stopped part‑way after
opening), the opener beeps and flashes its light first, a safety requirement
for unattended closing. The door doesn't move, and keeps reporting where it
is, until the warning ends. Pressing the button again during the warning
**cancels** the close, and the door stays where it is. That's the app's single
button tapped twice; a wireless remote press cancels it too.

Home Assistant's cover has separate Open, Stop and Close buttons, but all of
them send that same single press. During the warning the cover shows
**Closing** (like the app's hourglass), so:

* **Close** is disabled, so a double tap can't cancel the close by mistake.
* **Stop** sends the second press and cancels the close. (So does an
  `open_cover` call from an automation.)

This works whether the close came from Home Assistant or from the phone app.
**A timer‑to‑close countdown can't be cancelled from Home Assistant**: to the
cloud it looks the same as an open door, and a press would start closing it
rather than cancel.
A press on the wall console during the warning skips the rest of it and closes
at once.

### Safety sensors

If the photo‑eye (light curtain) is blocked, the opener refuses to close: it
flashes, and the cover briefly shows Closing, then Open. A close from the app
or Home Assistant still beeps for the full warning first. The **Close
refused** binary sensor turns on for 5–17 s when this happens, which you can
use in an automation to notify you. If the beam is broken while the door is
closing, or the door hits something, it reverses and opens; the hub reports
that only as the door opening, with no error.

### Wall console lockout

The lockout switch on the wall console blocks the **wireless remotes only**.
The phone app, and so Home Assistant, can still open and close the door while
it is on. If you want Home Assistant to respect it, go to
**Settings → Devices & Services → Orbit → Configure** and turn on **Respect the
wall console lockout**: open and close (and the Door button) are then refused
while the lockout is on. **Stop** always works.

## Installation (Marketplace)

1. In the Marketplace, add `https://github.com/joesilva101/ha-orbit` as a
   **custom repository**, category **Integration**.
2. Install **Orbit Garage Door Opener (Skylink)**, then restart Home Assistant.
3. **Settings → Devices & Services → Add Integration → Orbit** and sign in with
   your Orbit Home account (same email/password as the phone app).

## Manual installation

Copy `custom_components/orbit_gdo` into your Home Assistant `config/custom_components/`
directory and restart.

## Community and support

- **Discussion:** [Home Assistant Community forum thread](https://community.home-assistant.io/t/skylink-orbit-wi-fi-garage-door-opener-custom-integration-marketplace/1034662)
- **Bugs:** [open an issue](https://github.com/joesilva101/ha-orbit/issues). Include your
  hub model, its firmware, and a diagnostics download from the device page.

## Development

```bash
uv sync --group dev
uv run ruff check .
uv run pytest
```

The `orbitclient/` package under `custom_components/orbit_gdo/` has **no Home
Assistant dependency** and is unit‑tested standalone (REST via `aioresponses`,
MQTT via a fake transport, fixtures taken from real hub traffic).

## License

MIT — see [`LICENSE`](LICENSE).
