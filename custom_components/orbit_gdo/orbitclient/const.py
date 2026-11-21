"""Protocol constants for the Skylink Orbit Home cloud.

Recovered from the Orbit Home Android app (``com.mtqqdemo.skylink``, v3.7) and
confirmed against the live service with real hardware (Skylink GDO, firmware
``WF2012021040204``).

Architecture
------------
* **REST** (``skyhm_api_s.jsp``) is only needed for login. Every call is a signed
  ``POST ...?cmd=<cmd>``; the signature is an MD5 over timestamp/cmd/data plus a
  static app secret (see :func:`orbitclient.client.sign`).
* **MQTT over TLS** carries everything else: the device list, live state pushes,
  and door/light commands. Credentials are the account number (``acc_no``,
  returned by login) and the account password.
"""

from __future__ import annotations

from pathlib import Path

# ---- REST -------------------------------------------------------------------

API_URL = "https://iot.skyhm.net:8444/skylinkhub_crm/skyhm_api_s.jsp"
#: Static secret mixed into REQ-SIGNATURE by the official app.
API_SIGN_SECRET = "8uHDSF77ueRmLlKkl67"

CMD_LOGIN = "act_login"

#: ``result`` codes seen from the REST API.
RESULT_OK = 0
RESULT_LOGIN_NAME_NOT_FOUND = 11
RESULT_BAD_SIGNATURE = 25

# ---- MQTT -------------------------------------------------------------------

# The app connects to ssl://34.214.223.70:1899; that IP is iot.skyhm.net and the
# broker's certificate is issued for that name, so we use it and keep full
# hostname verification.
MQTT_HOST = "iot.skyhm.net"
MQTT_PORT = 1899
MQTT_KEEPALIVE = 30

#: The broker serves only its leaf certificate (no intermediate), so standard
#: clients cannot build a chain. This bundle holds the RapidSSL TLS RSA CA G1
#: intermediate and the DigiCert Global Root G2 it chains to.
CA_BUNDLE = Path(__file__).with_name("orbit_ca.pem")

TOPIC_BASE = "skylink/things/client/{acc_no}"
#: Publish "{}" here to request the full device list...
TOPIC_GET = TOPIC_BASE + "/get"
#: ...which arrives here as {"data": [hub, ...]}.
TOPIC_GET_RESULT = TOPIC_BASE + "/get/result"
#: Partial state pushes: {"data": {"hub_id": ..., "reported": {...}}}.
TOPIC_UPDATE_RESULT = TOPIC_BASE + "/update/result"
#: Commands are published here.
TOPIC_DESIRE = TOPIC_BASE + "/desire"

# ---- Commands (desired.mdev.ctrlgdo.cmd) --------------------------------------

#: Momentary "wall button" press. Observed cycle on real hardware:
#: closed -> opening -> (press) stopped -> (press) closing -> closed, i.e. from
#: "stopped" the door reverses its previous direction of travel.
#:
#: A remote (app/cloud) press that would close the door (fully open, or stopped
#: part-way after opening) does not close it at once: the opener beeps and
#: flashes for ~10 s (``autoclose_en`` = 1), then closes. A second remote press
#: during that warning CANCELS the close and the door stays where it is. (A
#: wireless remote press cancels it too; a wall press skips the rest and closes
#: at once.)
CTRL_PRESS = 0
#: How long after a remote press on an open door its close warning is treated
#: as running. Observed: ~10.8 s from the press to the door reporting
#: "closing"; the door leaving OPEN ends it sooner.
CLOSE_WARNING_WINDOW = 15.0
#: Reversing a moving door takes two presses (stop, then reverse). The hub
#: reports "stopped" ~0.8 s after the first; if it hasn't within this long,
#: the second press is not sent.
STOP_CONFIRM_TIMEOUT = 5.0
#: ``reported.link.online`` pushes are immediate, but the ``online`` flag in
#: the full device list lags them by minutes (observed 2-4 min). For this long
#: after a push, the pushed value wins over the device list.
LINK_PUSH_TRUST = 600.0

# ---- Error codes (reported.mdev.errno) ----------------------------------------

#: Set while the wall console lockout is on (with ``lockrf`` = 1).
ERRNO_LOCKOUT = 4
#: The photo-eye (light curtain) was blocked, so the opener refused to close:
#: the door reports "closing" for ~5 s, then "open", with this code for 5-17 s.
#: A reversal mid-travel (photo-eye or obstruction) reports no error code.
ERRNO_SAFETY_REFUSAL = 7
#: Light on/off - explicit (idempotent) commands, both confirmed on hardware.
#: GDO hubs do not report light state, so it cannot be read back (and the wall
#: console's single light button toggles it without telling the cloud).
CTRL_LIGHT_ON = 3
CTRL_LIGHT_OFF = 4

DEFAULT_TIMEOUT = 15.0
