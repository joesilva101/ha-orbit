"""Async client for the Skylink Orbit Home cloud (REST login + MQTT)."""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import time
from collections import deque
from collections.abc import Callable
from typing import Any

import aiohttp

from . import const
from .exceptions import (
    OrbitAuthError,
    OrbitCannotConnect,
    OrbitCommandError,
    OrbitError,
    OrbitTimeout,
)
from .models import DoorState, OrbitAccount, OrbitDevice
from .transport import MqttTransport, build_default_transport

_LOGGER = logging.getLogger(__name__)

#: Called (on the event loop) with the merged device after every state push.
DeviceCallback = Callable[[OrbitDevice], None]


def sign(timestamp: str, cmd: str, req_data: str) -> str:
    """REQ-SIGNATURE as computed by the app: md5(ts+cmd+data+secret), "+"-joined."""
    raw = f"{timestamp}+{cmd}+{req_data}+{const.API_SIGN_SECRET}"
    return hashlib.md5(raw.encode()).hexdigest()  # noqa: S324 - vendor protocol


def parse_api_body(text: str) -> dict[str, Any]:
    """Parse a REST body. The server emits the invalid literal ``"result":00``."""
    text = text.strip().replace('"result":00', '"result":0')
    data = json.loads(text)
    if not isinstance(data, dict):
        raise ValueError("expected a JSON object")
    return data


class OrbitClient:
    """Talks to the Orbit cloud on behalf of the Home Assistant integration.

    Usage: :meth:`async_connect` (login + MQTT), then :meth:`async_get_devices`
    and the command helpers. State pushes are delivered to ``on_update``.
    Never imports Home Assistant, so it can be tested standalone.
    """

    def __init__(
        self,
        email: str,
        password: str,
        *,
        session: aiohttp.ClientSession,
        timeout: float = const.DEFAULT_TIMEOUT,
        transport_factory: Callable[[], MqttTransport] | None = None,
    ) -> None:
        self._email = email.strip()
        self._password = password
        self._session = session
        self._timeout = timeout
        self._transport_factory = transport_factory or build_default_transport

        self.account: OrbitAccount | None = None
        self._transport: MqttTransport | None = None
        self._devices: dict[str, OrbitDevice] = {}
        self._pending_get: asyncio.Future[list[dict[str, Any]]] | None = None
        self._get_lock = asyncio.Lock()
        self.on_update: DeviceCallback | None = None
        #: hub_id -> (monotonic time, door state) of the remote press that
        #: started a close warning (by us or by the phone app, seen on /desire).
        self._close_warning: dict[str, tuple[float, DoorState]] = {}
        #: ``ts`` of commands we sent, so their /desire echoes are not
        #: mistaken for presses from the phone app.
        self._sent_ts: deque[str] = deque(maxlen=32)
        #: hub_id -> (monotonic time, online) of the last reported.link push.
        self._link_push: dict[str, tuple[float, bool]] = {}
        #: hub_id -> [(door state when registered, future)]: resolved with
        #: the new state once the door reports something else.
        self._door_waiters: dict[
            str, list[tuple[DoorState, asyncio.Future[DoorState]]]
        ] = {}
        self._clock: Callable[[], float] = time.monotonic

    # ---- Properties -------------------------------------------------------

    @property
    def acc_no(self) -> str | None:
        return self.account.acc_no if self.account else None

    @property
    def devices(self) -> dict[str, OrbitDevice]:
        return self._devices

    @property
    def is_connected(self) -> bool:
        return self._transport is not None and self._transport.is_connected

    # ---- REST -------------------------------------------------------------

    async def async_login(self) -> OrbitAccount:
        """Validate credentials via REST and return the account number."""
        data = await self._api(
            const.CMD_LOGIN,
            self._email,
            {
                "username": self._email,
                "password": self._password,
                "app_sys": "gcm",
                "app_brand": "00",
                "pn_token": "",
            },
        )
        result = data.get("result")
        if result != const.RESULT_OK:
            message = data.get("message") or "login failed"
            if result == const.RESULT_BAD_SIGNATURE:
                raise OrbitCannotConnect(f"API rejected request signature: {message}")
            raise OrbitAuthError(f"{message} (result={result})")
        acc_no = data.get("acc_no")
        if not acc_no:
            raise OrbitError("login succeeded but returned no account number")
        self.account = OrbitAccount(
            acc_no=str(acc_no), alias_name=data.get("alias_name")
        )
        _LOGGER.debug("Orbit login OK (acc_no=%s)", acc_no)
        return self.account

    async def _api(
        self, cmd: str, req_data: str, body: dict[str, Any]
    ) -> dict[str, Any]:
        ts = str(int(time.time()) * 1000)
        headers = {
            "Content-Type": "application/json; charset=utf-8",
            "Accept": "application/json",
            "REQ-TIMESTAMP": ts,
            "REQ-CMD": cmd,
            "REQ-DATA": req_data,
            "REQ-SIGNATURE": sign(ts, cmd, req_data),
        }
        try:
            async with self._session.post(
                const.API_URL,
                params={"cmd": cmd},
                data=json.dumps(body),
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=self._timeout),
            ) as resp:
                if resp.status >= 400:
                    raise OrbitCannotConnect(f"API HTTP {resp.status} for {cmd}")
                text = await resp.text()
        except TimeoutError as err:
            raise OrbitTimeout(f"timeout calling {cmd}") from err
        except aiohttp.ClientError as err:
            raise OrbitCannotConnect(f"connection error calling {cmd}: {err}") from err
        try:
            return parse_api_body(text)
        except ValueError as err:
            raise OrbitCannotConnect(
                f"unparseable response to {cmd}: {text[:200]!r}"
            ) from err

    # ---- MQTT lifecycle ---------------------------------------------------

    def _topic(self, template: str) -> str:
        assert self.acc_no is not None
        return template.format(acc_no=self.acc_no)

    async def async_connect(self) -> None:
        """Log in (if needed) and open the MQTT session."""
        if self.account is None:
            await self.async_login()
        if self._transport is not None:
            return
        transport = self._transport_factory()
        await transport.async_connect(
            username=self.acc_no,
            password=self._password,
            topics=[
                self._topic(const.TOPIC_GET_RESULT),
                self._topic(const.TOPIC_UPDATE_RESULT),
                # The phone app's commands, to know when it started a close.
                self._topic(const.TOPIC_DESIRE),
            ],
            on_message=self._handle_message,
            on_connect=self._handle_connect,
        )
        self._transport = transport

    async def async_disconnect(self) -> None:
        transport, self._transport = self._transport, None
        if transport is not None:
            await transport.async_disconnect()

    def _handle_connect(self) -> None:
        # After a reconnect, resync the full list so no push is missed.
        if self._devices:
            asyncio.ensure_future(self._safe_refresh())

    async def _safe_refresh(self) -> None:
        try:
            await self.async_get_devices()
        except OrbitError as err:
            _LOGGER.debug("Resync after reconnect failed: %s", err)

    # ---- Inbound ------------------------------------------------------------

    def _handle_message(self, topic: str, payload: bytes) -> None:
        try:
            message = json.loads(payload)
        except (ValueError, TypeError):
            _LOGGER.debug("Ignoring non-JSON MQTT payload on %s", topic)
            return
        if not isinstance(message, dict):
            return
        data = message.get("data")

        if topic == self._topic(const.TOPIC_GET_RESULT):
            hubs = (
                [d for d in data if isinstance(d, dict)]
                if isinstance(data, list)
                else []
            )
            fut = self._pending_get
            if fut is not None and not fut.done():
                fut.set_result(hubs)
            else:
                self._replace_devices(hubs, notify=True)
        elif topic == self._topic(const.TOPIC_UPDATE_RESULT) and isinstance(data, dict):
            self._apply_update(data)
        elif topic == self._topic(const.TOPIC_DESIRE) and isinstance(data, dict):
            self._handle_desire(data)

    def _handle_desire(self, data: dict[str, Any]) -> None:
        """A command on /desire: ours (echoed back) or from the phone app."""
        ctrl = (((data.get("desired") or {}).get("mdev") or {}).get("ctrlgdo")) or {}
        if not isinstance(ctrl, dict) or ctrl.get("cmd") != const.CTRL_PRESS:
            return
        if str(ctrl.get("ts")) in self._sent_ts:
            return  # our own command, already accounted for when sent
        hub_id = data.get("hub_id")
        if hub_id:
            self._note_remote_press(str(hub_id))

    def _note_remote_press(self, hub_id: str) -> None:
        """Track the close warning a remote press starts (or cancels).

        Any remote press that would close the door (fully open, or stopped
        part-way after opening) starts the warning; pressing again cancels it.
        """
        device = self._devices.get(hub_id)
        was_active = self.close_warning_active(hub_id)
        if was_active:
            self._close_warning.pop(hub_id, None)  # second press cancels
        elif device is not None and self.press_would_move(hub_id) is DoorState.CLOSING:
            warning = (self._clock(), device.door_state)
            self._close_warning[hub_id] = warning
            self._schedule_warning_expiry(hub_id, warning)
        else:
            self._close_warning.pop(hub_id, None)
        if device is not None and was_active != self.close_warning_active(hub_id):
            self._notify(device)  # the cover shows "closing" during the warning

    def _schedule_warning_expiry(
        self, hub_id: str, warning: tuple[float, DoorState]
    ) -> None:
        """Re-notify when the window lapses, in case no state push ends it."""

        def expire() -> None:
            if self._close_warning.get(hub_id) is warning:
                self._close_warning.pop(hub_id, None)
                if (device := self._devices.get(hub_id)) is not None:
                    self._notify(device)

        # No running loop (sync use): the window check still ends it.
        with contextlib.suppress(RuntimeError):
            asyncio.get_running_loop().call_later(const.CLOSE_WARNING_WINDOW, expire)

    def _end_close_warning_if_over(
        self, previous: OrbitDevice | None, device: OrbitDevice
    ) -> None:
        warning = self._close_warning.get(device.device_id)
        if warning is None:
            return
        if device.door_state is not warning[1] or (
            previous is not None
            and previous.autoclose_pending
            and device.autoclose_pending is False
        ):
            self._close_warning.pop(device.device_id, None)

    def close_warning_active(self, device_id: str) -> bool:
        """Whether a remote close is in its ~10 s warning (door not moving yet).

        Only known when the close came from us or the phone app. A timer-to-
        close countdown (``autoclose_pending`` with no recent remote press) is
        NOT a warning: a press then starts a close rather than cancelling one.
        """
        warning = self._close_warning.get(device_id)
        device = self._devices.get(device_id)
        return (
            warning is not None
            and device is not None
            and device.door_state is warning[1]
            and self._clock() - warning[0] < const.CLOSE_WARNING_WINDOW
        )

    def _replace_devices(self, hubs: list[dict[str, Any]], *, notify: bool) -> None:
        devices: dict[str, OrbitDevice] = {}
        for hub in hubs:
            try:
                device = OrbitDevice.from_dict(hub)
            except (KeyError, TypeError, ValueError) as err:
                _LOGGER.warning("Skipping unparseable Orbit hub: %s", err)
                continue
            previous = self._devices.get(device.device_id)
            if previous is not None and device.last_motion is None:
                device.last_motion = previous.last_motion
            pushed = self._link_push.get(device.device_id)
            if pushed is not None and self._clock() - pushed[0] < const.LINK_PUSH_TRUST:
                device.online = pushed[1]  # the list's "online" lags the push
            self._end_close_warning_if_over(previous, device)
            devices[device.device_id] = device
        self._devices = devices
        if notify:
            for device in devices.values():
                self._notify(device)

    def _apply_update(self, data: dict[str, Any]) -> None:
        hub_id = data.get("hub_id")
        existing = self._devices.get(str(hub_id)) if hub_id else None
        if existing is None:
            _LOGGER.debug("Push for unknown hub %s; requesting full list", hub_id)
            asyncio.ensure_future(self._safe_refresh())
            return
        updated = existing.merged_with(data)
        link = (data.get("reported") or {}).get("link")
        if isinstance(link, dict) and "online" in link:
            self._link_push[updated.device_id] = (self._clock(), updated.online)
        self._end_close_warning_if_over(existing, updated)
        self._devices[updated.device_id] = updated
        self._notify(updated)

    def _notify(self, device: OrbitDevice) -> None:
        for from_state, fut in self._door_waiters.get(device.device_id, []):
            if not fut.done() and device.door_state is not from_state:
                fut.set_result(device.door_state)
        if self.on_update is not None:
            self.on_update(device)

    # ---- Requests -----------------------------------------------------------

    async def async_get_devices(self) -> dict[str, OrbitDevice]:
        """Request and return the full device list."""
        await self.async_connect()
        async with self._get_lock:
            loop = asyncio.get_running_loop()
            self._pending_get = loop.create_future()
            try:
                await self._publish(self._topic(const.TOPIC_GET), "{}")
                async with asyncio.timeout(self._timeout):
                    hubs = await self._pending_get
            except TimeoutError as err:
                raise OrbitTimeout("no device list received from Orbit cloud") from err
            finally:
                self._pending_get = None
        self._replace_devices(hubs, notify=False)
        return self._devices

    async def async_send_ctrl(self, device_id: str, cmd: int) -> None:
        """Publish a raw ``ctrlgdo`` command for a hub."""
        await self.async_connect()
        ts = str(int(time.time() * 1000))
        payload = {
            "data": {
                "hub_id": device_id,
                "desired": {"mdev": {"ctrlgdo": {"cmd": cmd, "ts": ts}}},
            }
        }
        self._sent_ts.append(ts)
        await self._publish(
            self._topic(const.TOPIC_DESIRE), json.dumps(payload, separators=(",", ":"))
        )
        if cmd == const.CTRL_PRESS:
            self._note_remote_press(device_id)

    async def _publish(self, topic: str, payload: str) -> None:
        assert self._transport is not None
        await self._transport.async_publish(topic, payload)

    # ---- High-level commands -------------------------------------------------

    async def async_press_button(self, device_id: str) -> None:
        """Equivalent to pressing the wall button (toggle)."""
        await self.async_send_ctrl(device_id, const.CTRL_PRESS)

    def press_would_move(self, device_id: str) -> DoorState | None:
        """Predict the motion one button press starts, or None if unknowable.

        Observed: CLOSED -> OPENING, OPEN -> CLOSING, a moving door stops, and
        from STOPPED the door reverses its last direction of travel.
        """
        device = self._devices.get(device_id)
        if device is None:
            return None
        state = device.door_state
        if state is DoorState.CLOSED:
            return DoorState.OPENING
        if state is DoorState.OPEN:
            return DoorState.CLOSING
        if state is DoorState.STOPPED:
            if device.last_motion is DoorState.OPENING:
                return DoorState.CLOSING
            if device.last_motion is DoorState.CLOSING:
                return DoorState.OPENING
        return None

    async def _async_move(self, device_id: str, target: DoorState) -> None:
        device = self._devices.get(device_id)
        state = device.door_state if device else DoorState.UNKNOWN
        if self.close_warning_active(device_id):
            # The door hasn't moved yet but is about to close. Another press
            # (the app's single button, tapped again) cancels that close: send
            # it for "open", never for "close". The door then stays where it is.
            if target is DoorState.OPENING:
                await self.async_press_button(device_id)
            return
        done = (
            (DoorState.OPEN, DoorState.OPENING)
            if target is DoorState.OPENING
            else (DoorState.CLOSED, DoorState.CLOSING)
        )
        if state in done:
            return
        if state in (DoorState.OPENING, DoorState.CLOSING):
            # Moving the other way: stop it, then reverse it. That's the
            # app's button pressed twice; the second press waits until the
            # hub reports the door stopped, so it can't land as a third.
            await self._async_press_and_wait(device_id, state)
            return
        # Open, closed, stopped part-way or unknown: one press, exactly like
        # the app's single button. From part-way the opener reverses its last
        # direction, so this can go the other way; the cover then shows that
        # at once, and another press (or Stop, during a close warning) acts as
        # it would in the app.
        await self.async_press_button(device_id)

    async def _async_press_and_wait(self, device_id: str, moving: DoorState) -> None:
        loop = asyncio.get_running_loop()
        fut: asyncio.Future[DoorState] = loop.create_future()
        waiters = self._door_waiters.setdefault(device_id, [])
        entry = (moving, fut)
        waiters.append(entry)  # before the press, so no push is missed
        try:
            await self.async_press_button(device_id)
            try:
                async with asyncio.timeout(const.STOP_CONFIRM_TIMEOUT):
                    new_state = await fut
            except TimeoutError as err:
                raise OrbitCommandError(
                    "the door didn't report stopping, so it wasn't reversed. "
                    "Check it and try again"
                ) from err
        finally:
            waiters.remove(entry)
            if not waiters:
                self._door_waiters.pop(device_id, None)
        # Anything but "stopped" (e.g. it reached the end of travel just as
        # the press arrived) is ambiguous: don't add a press.
        if new_state is DoorState.STOPPED:
            await self.async_press_button(device_id)

    async def async_open_door(self, device_id: str) -> None:
        """Open the door, the way the app's single button would.

        No-op if already open/opening. A closing door is stopped, then
        reversed. From part-way it sends one press, which reverses the last
        direction of travel. During a remote close's warning it sends the
        second press that cancels the close; the door stays where it is.
        It cannot stop a timer-to-close countdown (that is indistinguishable
        from an open door that is simply waiting).
        """
        await self._async_move(device_id, DoorState.OPENING)

    async def async_close_door(self, device_id: str) -> None:
        """Close the door, the way the app's single button would.

        No-op if already closed/closing or in a close warning. An opening
        door is stopped, then reversed. From part-way it sends one press.
        """
        await self._async_move(device_id, DoorState.CLOSING)

    async def async_stop_door(self, device_id: str) -> None:
        """Stop a moving door, or cancel a close warning; no-op otherwise."""
        device = self._devices.get(device_id)
        if self.close_warning_active(device_id) or (
            device and device.door_state in (DoorState.OPENING, DoorState.CLOSING)
        ):
            await self.async_press_button(device_id)

    async def async_set_light(self, device_id: str, on: bool) -> None:
        """Turn the opener light on/off (state is not reported by the hub)."""
        await self.async_send_ctrl(
            device_id, const.CTRL_LIGHT_ON if on else const.CTRL_LIGHT_OFF
        )
