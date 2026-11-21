"""Client tests: REST signing/login via aioresponses, MQTT via a fake transport."""

from __future__ import annotations

import asyncio
import hashlib
import json
import re

import pytest
from aioresponses import aioresponses

from orbitclient import (
    DoorState,
    OrbitAuthError,
    OrbitCannotConnect,
    OrbitClient,
    OrbitCommandError,
    OrbitTimeout,
    const,
)
from orbitclient.client import parse_api_body, sign

API = re.compile(r"^https://iot\.skyhm\.net:8444/skylinkhub_crm/skyhm_api_s\.jsp.*$")
ACC = "8000000001"
HUB = {
    "hub_id": "HUB1",
    "hub_name": "Single Door",
    "type": "GDO",
    "online": True,
    "reported": {"mdev": {"door": 4, "rssi": -60}},
}


class FakeTransport:
    """In-memory broker: records publishes, answers /get with a hub list."""

    def __init__(self, hubs=None, *, answer_get=True):
        self.hubs = hubs if hubs is not None else [HUB]
        self.answer_get = answer_get
        self.published: list[tuple[str, dict]] = []
        self.topics: list[str] = []
        self.username = self.password = None
        self._on_message = None
        self.connected = False

    @property
    def is_connected(self):
        return self.connected

    async def async_connect(
        self, *, username, password, topics, on_message, on_connect
    ):
        self.username, self.password, self.topics = username, password, topics
        self._on_message = on_message
        self.connected = True

    async def async_publish(self, topic, payload):
        self.published.append((topic, json.loads(payload)))
        if topic.endswith("/get") and self.answer_get:
            body = json.dumps({"data": self.hubs}).encode()
            asyncio.get_running_loop().call_soon(
                self._on_message, f"skylink/things/client/{ACC}/get/result", body
            )
        elif topic.endswith("/desire"):
            # Like the real broker, we are subscribed to /desire and get our
            # own commands echoed back.
            asyncio.get_running_loop().call_soon(
                self._on_message, topic, payload.encode()
            )

    async def async_disconnect(self):
        self.connected = False

    def push(self, data):
        self._on_message(
            f"skylink/things/client/{ACC}/update/result",
            json.dumps({"data": data}).encode(),
        )

    def app_press(self, hub_id="HUB1", ts="1795220882352"):
        """A button press sent by the phone app, as seen on /desire."""
        ctrl = {"cmd": 0, "ts": ts}
        self._on_message(
            f"skylink/things/client/{ACC}/desire",
            json.dumps(
                {"data": {"hub_id": hub_id, "desired": {"mdev": {"ctrlgdo": ctrl}}}}
            ).encode(),
        )


def _login_ok(m):
    m.post(
        API,
        # Byte-for-byte shape of a real reply, incl. the invalid "result":00.
        body='\n\r\n{"result":00,"message":"Success","acc_no":"'
        + ACC
        + '","alias_name":"Test User"}',
    )


def _client(session, transport):
    return OrbitClient(
        " user@example.com ", "pw", session=session, transport_factory=lambda: transport
    )


# ---- REST -------------------------------------------------------------------


def test_sign_matches_app_algorithm():
    expected = hashlib.md5(
        b"1700000000000+act_login+a@b.c+8uHDSF77ueRmLlKkl67"
    ).hexdigest()
    assert sign("1700000000000", "act_login", "a@b.c") == expected


def test_parse_api_body_fixes_invalid_result_literal():
    assert parse_api_body('\n{"result":00,"x":1}\n') == {"result": 0, "x": 1}


async def test_login_sends_signed_request(session):
    with aioresponses() as m:
        _login_ok(m)
        client = _client(session, FakeTransport())
        account = await client.async_login()
        assert account.acc_no == ACC and account.alias_name == "Test User"

        ((key, calls),) = m.requests.items()
        assert key[1].query["cmd"] == "act_login"
        headers = calls[0].kwargs["headers"]
        body = json.loads(calls[0].kwargs["data"])
        assert body["username"] == "user@example.com"  # stripped
        assert body["app_sys"] == "gcm" and body["app_brand"] == "00"
        assert headers["REQ-CMD"] == "act_login"
        assert headers["REQ-DATA"] == "user@example.com"
        assert headers["REQ-SIGNATURE"] == sign(
            headers["REQ-TIMESTAMP"], "act_login", "user@example.com"
        )


async def test_login_rejected_is_auth_error(session):
    with aioresponses() as m:
        m.post(API, body='{"result":11,"message":"Login Name Does Not Exist"}')
        with pytest.raises(OrbitAuthError, match="Does Not Exist"):
            await _client(session, FakeTransport()).async_login()


async def test_bad_signature_is_connect_error(session):
    with aioresponses() as m:
        m.post(API, body='{"result":25,"message":"Invalid Request Signature"}')
        with pytest.raises(OrbitCannotConnect):
            await _client(session, FakeTransport()).async_login()


async def test_http_error_is_connect_error(session):
    with aioresponses() as m:
        m.post(API, status=502)
        with pytest.raises(OrbitCannotConnect):
            await _client(session, FakeTransport()).async_login()


# ---- MQTT -------------------------------------------------------------------


async def test_connect_and_get_devices(session):
    t = FakeTransport()
    with aioresponses() as m:
        _login_ok(m)
        client = _client(session, t)
        devices = await client.async_get_devices()
    assert t.username == ACC and t.password == "pw"
    assert t.topics == [
        f"skylink/things/client/{ACC}/get/result",
        f"skylink/things/client/{ACC}/update/result",
        f"skylink/things/client/{ACC}/desire",
    ]
    assert t.published == [(f"skylink/things/client/{ACC}/get", {})]
    assert devices["HUB1"].door_state is DoorState.CLOSED


async def test_get_devices_timeout(session):
    t = FakeTransport(answer_get=False)
    with aioresponses() as m:
        _login_ok(m)
        client = OrbitClient(
            "u", "p", session=session, timeout=0.05, transport_factory=lambda: t
        )
        with pytest.raises(OrbitTimeout):
            await client.async_get_devices()


async def _ready(session, t):
    with aioresponses() as m:
        _login_ok(m)
        client = _client(session, t)
        await client.async_get_devices()
    t.published.clear()
    return client


def _cmds(t):
    return [p["data"]["desired"]["mdev"]["ctrlgdo"]["cmd"] for _, p in t.published]


def _mdev(**mdev):
    return {"hub_id": "HUB1", "reported": {"mdev": mdev}}


async def _open_door(session, t, now):
    """A client whose door is OPEN, with a controllable clock."""
    client = await _ready(session, t)
    client._clock = lambda: now[0]
    t.push(_mdev(door=1, autoclose_en=0))
    return client


async def _settle():
    """Let the fake broker deliver /desire echoes."""
    await asyncio.sleep(0)


async def test_push_updates_and_notifies(session):
    t = FakeTransport()
    client = await _ready(session, t)
    seen = []
    client.on_update = seen.append
    t.push({"hub_id": "HUB1", "reported": {"mdev": {"door": 0}}})
    assert client.devices["HUB1"].door_state is DoorState.OPENING
    assert seen and seen[-1].device_id == "HUB1"


async def test_command_payload_shape(session):
    t = FakeTransport()
    client = await _ready(session, t)
    await client.async_open_door("HUB1")
    ((topic, payload),) = t.published
    assert topic == f"skylink/things/client/{ACC}/desire"
    assert payload["data"]["hub_id"] == "HUB1"
    ctrl = payload["data"]["desired"]["mdev"]["ctrlgdo"]
    assert ctrl["cmd"] == const.CTRL_PRESS and ctrl["ts"].isdigit()


async def test_open_close_are_idempotent(session):
    t = FakeTransport()
    client = await _ready(session, t)
    await client.async_close_door("HUB1")  # already closed
    assert t.published == []
    t.push({"hub_id": "HUB1", "reported": {"mdev": {"door": 1}}})
    await client.async_open_door("HUB1")  # already open
    assert t.published == []
    await client.async_close_door("HUB1")
    assert _cmds(t) == [0]


@pytest.mark.parametrize("last", [0, 3])  # stopped after opening / closing
@pytest.mark.parametrize("action", ["async_open_door", "async_close_door"])
async def test_partway_door_gets_one_press_either_way(session, last, action):
    """Like the app's single button: never refused, the opener picks the way."""
    t = FakeTransport()
    client = await _ready(session, t)
    t.push(_mdev(door=last))
    t.push(_mdev(door=2))  # stopped part-way
    await getattr(client, action)("HUB1")
    assert _cmds(t) == [0]


async def test_partway_with_unknown_direction_gets_one_press(session):
    t = FakeTransport(hubs=[{**HUB, "reported": {"mdev": {"door": 2}}}])
    client = await _ready(session, t)
    await client.async_open_door("HUB1")
    assert _cmds(t) == [0]


async def test_open_from_partway_after_opening_starts_close_warning(session):
    """One press from there closes (with the warning); Stop then cancels it."""
    t, now = FakeTransport(), [100.0]
    client = await _ready(session, t)
    client._clock = lambda: now[0]
    t.push(_mdev(door=0))
    t.push(_mdev(door=2))
    await client.async_open_door("HUB1")
    await _settle()
    assert client.close_warning_active("HUB1")
    await client.async_stop_door("HUB1")
    assert _cmds(t) == [0, 0]
    assert not client.close_warning_active("HUB1")


async def _reverse(client, t, action, *, reply):
    """Run a reversing command, answering its first press with ``reply``."""
    task = asyncio.create_task(getattr(client, action)("HUB1"))
    await _settle()
    assert _cmds(t) == [0]  # the stop press; the reverse waits for the hub
    if reply is not None:
        t.push(_mdev(door=reply))
    await task


async def test_open_while_closing_stops_then_reverses(session):
    t = FakeTransport()
    client = await _ready(session, t)
    t.push(_mdev(door=3))  # closing
    await _reverse(client, t, "async_open_door", reply=2)
    assert _cmds(t) == [0, 0]


async def test_close_while_opening_stops_then_reverses_with_warning(session):
    t, now = FakeTransport(), [100.0]
    client = await _ready(session, t)
    client._clock = lambda: now[0]
    t.push(_mdev(door=0))  # opening
    await _reverse(client, t, "async_close_door", reply=2)
    await _settle()
    assert _cmds(t) == [0, 0]
    assert client.close_warning_active("HUB1")  # 2nd press closes, with warning


async def test_reverse_needs_the_stop_to_be_reported(session, monkeypatch):
    monkeypatch.setattr(const, "STOP_CONFIRM_TIMEOUT", 0.01)
    t = FakeTransport()
    client = await _ready(session, t)
    t.push(_mdev(door=3))
    with pytest.raises(OrbitCommandError, match="didn't report stopping"):
        await _reverse(client, t, "async_open_door", reply=None)
    assert _cmds(t) == [0]  # never a blind second press
    assert client._door_waiters == {}


async def test_reverse_skips_second_press_if_door_reached_the_end(session):
    t = FakeTransport()
    client = await _ready(session, t)
    t.push(_mdev(door=3))
    await _reverse(client, t, "async_open_door", reply=4)  # closed meanwhile
    assert _cmds(t) == [0]


async def test_same_direction_while_moving_is_noop(session):
    t = FakeTransport()
    client = await _ready(session, t)
    t.push(_mdev(door=0))
    await client.async_open_door("HUB1")
    t.push(_mdev(door=3))
    await client.async_close_door("HUB1")
    assert t.published == []


async def test_stop_while_moving(session):
    t = FakeTransport()
    client = await _ready(session, t)
    t.push(_mdev(door=0))
    await client.async_stop_door("HUB1")
    assert _cmds(t) == [0]


# ---- Online / offline ----------------------------------------------------------


async def test_online_push_updates_device(session):
    t = FakeTransport()
    client = await _ready(session, t)
    t.push({"hub_id": "HUB1", "reported": {"link": {"online": False}}})
    assert client.devices["HUB1"].online is False
    t.push({"hub_id": "HUB1", "reported": {"link": {"online": True}}})
    assert client.devices["HUB1"].online is True


async def test_stale_device_list_does_not_undo_online_push(session):
    """Captured: the list still said offline 4 min after the hub came back."""
    t, now = FakeTransport(), [100.0]
    client = await _ready(session, t)
    client._clock = lambda: now[0]
    t.push({"hub_id": "HUB1", "reported": {"link": {"online": True}}})
    t.hubs = [{**HUB, "online": False}]
    now[0] += 240
    await client.async_get_devices()
    assert client.devices["HUB1"].online is True
    now[0] += const.LINK_PUSH_TRUST  # long after: the list wins again
    await client.async_get_devices()
    assert client.devices["HUB1"].online is False


async def test_stop_is_noop_when_idle(session):
    t = FakeTransport()
    client = await _ready(session, t)
    await client.async_stop_door("HUB1")
    assert t.published == []


async def test_light_on_off(session):
    t = FakeTransport()
    client = await _ready(session, t)
    await client.async_set_light("HUB1", True)
    await client.async_set_light("HUB1", False)
    assert _cmds(t) == [3, 4]  # 4 = off confirmed on hardware


# ---- Close warning (remote close of an open door) -----------------------------
#
# Real hardware: a remote press on an OPEN door beeps/flashes ~10 s
# (autoclose_en=1) before closing, and a second remote press during that
# warning CANCELS the close.


async def test_close_twice_does_not_cancel_the_close(session):
    t, now = FakeTransport(), [100.0]
    client = await _open_door(session, t, now)
    await client.async_close_door("HUB1")
    await _settle()  # our own /desire echo must not count as a second press
    t.push(_mdev(autoclose_en=1))  # warning starts; door still reports open
    now[0] += 5
    await client.async_close_door("HUB1")
    assert _cmds(t) == [0]
    assert client.close_warning_active("HUB1")


async def test_open_during_warning_cancels_the_close(session):
    t, now = FakeTransport(), [100.0]
    client = await _open_door(session, t, now)
    await client.async_close_door("HUB1")
    await _settle()
    now[0] += 4
    await client.async_open_door("HUB1")
    await _settle()
    assert _cmds(t) == [0, 0]
    assert not client.close_warning_active("HUB1")
    await client.async_open_door("HUB1")  # cancelled; door just open now
    assert _cmds(t) == [0, 0]


async def test_app_close_is_seen_on_desire(session):
    t, now = FakeTransport(), [100.0]
    client = await _open_door(session, t, now)
    t.app_press()
    assert client.close_warning_active("HUB1")
    await client.async_close_door("HUB1")  # already closing: no second press
    assert _cmds(t) == []
    await client.async_open_door("HUB1")  # cancels the app's close
    assert _cmds(t) == [0]


async def test_warning_from_partway_open(session):
    """Stopped part-way after opening: a press closes it, with the warning."""
    t, now = FakeTransport(), [100.0]
    client = await _ready(session, t)
    client._clock = lambda: now[0]
    t.push(_mdev(door=0))  # opening
    t.push(_mdev(door=2))  # stopped part-way
    await client.async_close_door("HUB1")
    await _settle()
    assert client.close_warning_active("HUB1")
    await client.async_close_door("HUB1")  # must not cancel it
    assert _cmds(t) == [0]
    await client.async_open_door("HUB1")  # second press: cancels
    assert _cmds(t) == [0, 0]
    assert not client.close_warning_active("HUB1")
    t.push(_mdev(door=3))
    assert not client.close_warning_active("HUB1")


async def test_app_close_from_partway_open(session):
    t, now = FakeTransport(), [100.0]
    client = await _ready(session, t)
    client._clock = lambda: now[0]
    t.push(_mdev(door=0))
    t.push(_mdev(door=2))
    t.app_press()
    assert client.close_warning_active("HUB1")
    t.push(_mdev(door=3))  # warning over: door moving
    assert not client.close_warning_active("HUB1")


async def test_press_that_opens_starts_no_warning(session):
    """Stopped part-way after closing: a press opens it, no warning."""
    t, now = FakeTransport(), [100.0]
    client = await _ready(session, t)
    client._clock = lambda: now[0]
    t.push(_mdev(door=3))  # closing
    t.push(_mdev(door=2))  # stopped part-way
    t.app_press()
    assert not client.close_warning_active("HUB1")


async def test_stop_during_warning_cancels_the_close(session):
    """HA's card keeps Stop enabled during the warning; it sends the cancel."""
    t, now = FakeTransport(), [100.0]
    client = await _open_door(session, t, now)
    await client.async_close_door("HUB1")
    await _settle()
    await client.async_stop_door("HUB1")
    assert _cmds(t) == [0, 0]
    assert not client.close_warning_active("HUB1")
    await client.async_stop_door("HUB1")  # nothing to stop now
    assert _cmds(t) == [0, 0]


async def test_warning_start_and_cancel_notify(session):
    """The cover shows "closing" during the warning, so HA must hear about it."""
    t, now = FakeTransport(), [100.0]
    client = await _open_door(session, t, now)
    seen = []
    client.on_update = seen.append
    await client.async_close_door("HUB1")
    assert len(seen) == 1
    await client.async_stop_door("HUB1")
    assert len(seen) == 2


async def test_warning_expiry_notifies(session, monkeypatch):
    """With timer-to-close on, autoclose_en is already 1: no push may come."""
    monkeypatch.setattr(const, "CLOSE_WARNING_WINDOW", 0.01)
    t = FakeTransport()
    client = await _ready(session, t)
    t.push(_mdev(door=1, autoclose_en=1))
    seen = []
    client.on_update = seen.append
    await client.async_close_door("HUB1")
    await asyncio.sleep(0.05)
    assert not client.close_warning_active("HUB1")
    assert len(seen) == 2  # start, then expiry


async def test_app_second_press_cancels_tracked_warning(session):
    t, now = FakeTransport(), [100.0]
    client = await _open_door(session, t, now)
    t.app_press(ts="1")
    t.app_press(ts="2")
    assert not client.close_warning_active("HUB1")


async def test_warning_ends_when_door_starts_closing(session):
    t, now = FakeTransport(), [100.0]
    client = await _open_door(session, t, now)
    await client.async_close_door("HUB1")
    # Captured order: autoclose_en drops, then door -> closing (3).
    t.push(_mdev(autoclose_en=1))
    t.push(_mdev(autoclose_en=0))
    assert not client.close_warning_active("HUB1")
    t.push(_mdev(door=3))
    t.published.clear()
    await _reverse(client, t, "async_open_door", reply=2)  # now a normal reverse
    assert _cmds(t) == [0, 0]


async def test_warning_expires(session):
    t, now = FakeTransport(), [100.0]
    client = await _open_door(session, t, now)
    await client.async_close_door("HUB1")
    now[0] += const.CLOSE_WARNING_WINDOW + 1  # no push arrived (e.g. lost)
    assert not client.close_warning_active("HUB1")
    await client.async_close_door("HUB1")
    assert _cmds(t) == [0, 0]


async def test_timer_countdown_is_not_a_warning(session):
    """Timer-to-close: autoclose_en=1 for minutes with no remote press.

    A press then STARTS a close (captured), so close must press and open must
    not (it cannot cancel the timer).
    """
    t, now = FakeTransport(), [100.0]
    client = await _open_door(session, t, now)
    t.push(_mdev(autoclose_en=1))
    assert not client.close_warning_active("HUB1")
    await client.async_open_door("HUB1")
    assert _cmds(t) == []
    await client.async_close_door("HUB1")
    assert _cmds(t) == [0]


async def test_desire_for_light_is_not_a_press(session):
    t, now = FakeTransport(), [100.0]
    client = await _open_door(session, t, now)
    await client.async_set_light("HUB1", True)
    t._on_message(
        f"skylink/things/client/{ACC}/desire",
        json.dumps(
            {"data": {"hub_id": "HUB1", "desired": {"mdev": {"ctrlgdo": {"cmd": 3}}}}}
        ).encode(),
    )
    assert not client.close_warning_active("HUB1")


async def test_last_motion_survives_full_resync(session):
    t = FakeTransport()
    client = await _ready(session, t)
    t.push({"hub_id": "HUB1", "reported": {"mdev": {"door": 0}}})
    t.hubs = [{**HUB, "reported": {"mdev": {"door": 2}}}]
    await client.async_get_devices()
    assert client.devices["HUB1"].last_motion is DoorState.OPENING
