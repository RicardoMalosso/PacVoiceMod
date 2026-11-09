import json
import time
import webbrowser

import requests
from websockets.exceptions import ConnectionClosed
from websockets.sync.client import connect

CLIENT_ID = "sorb998iik1sbba6xfbqfr6e7pn9s1"
IDENTITY = "https://id.twitch.tv/oauth2"
EVENTSUB = "wss://eventsub.wss.twitch.tv/ws"
SUBSCRIPTIONS = "https://api.twitch.tv/helix/eventsub/subscriptions"


def authorize(stop, status):
    response = requests.post(IDENTITY + "/device", timeout=10,
                             data={"client_id": CLIENT_ID, "scopes": "user:read:chat"})
    response.raise_for_status()
    device = response.json()
    status(f"Enter {device['user_code']} in the Twitch browser tab.")
    webbrowser.open(device["verification_uri"])
    interval = device["interval"]
    deadline = time.monotonic() + device["expires_in"]
    while time.monotonic() < deadline and not stop.wait(interval):
        response = requests.post(IDENTITY + "/token", timeout=10, data={
            "client_id": CLIENT_ID, "scopes": "user:read:chat",
            "device_code": device["device_code"],
            "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
        })
        data = response.json()
        if response.ok:
            return data
        message = data.get("message")
        if message == "slow_down":
            interval += 5
        elif message != "authorization_pending":
            raise RuntimeError(f"Twitch sign-in failed: {message}")
    if not stop.is_set():
        raise RuntimeError("Twitch sign-in expired.")


def validate(token):
    response = requests.get(IDENTITY + "/validate", timeout=10,
                            headers={"Authorization": f"OAuth {token}"})
    response.raise_for_status()
    identity = response.json()
    if identity["client_id"] != CLIENT_ID or "user:read:chat" not in identity["scopes"]:
        raise RuntimeError("Unexpected Twitch account permissions.")
    return identity


def is_command(event, user_id, command, restricted):
    if event.get("cheer") or event.get("source_broadcaster_user_id"):
        return False
    if event["message"]["text"].strip().casefold() != command.casefold():
        return False
    return (not restricted or event["chatter_user_id"] == user_id or
            any(badge["set_id"] == "moderator" for badge in event.get("badges", [])))


def listen(stop, status, trigger):
    credentials = authorize(stop, status)
    if not credentials or stop.is_set():
        return
    token = credentials["access_token"]
    identity = validate(token)
    user_id = identity["user_id"]
    validate_at = time.monotonic() + min(3600, max(0, identity["expires_in"] - 60))
    seen = {}
    while not stop.is_set():
        try:
            with connect(EVENTSUB, open_timeout=10, close_timeout=1) as socket:
                welcome = json.loads(socket.recv(timeout=10))["payload"]["session"]
                response = requests.post(SUBSCRIPTIONS, timeout=8, headers={
                    "Authorization": f"Bearer {token}", "Client-Id": CLIENT_ID,
                }, json={"type": "channel.chat.message", "version": "1",
                         "condition": {"broadcaster_user_id": user_id, "user_id": user_id},
                         "transport": {"method": "websocket", "session_id": welcome["id"]}})
                response.raise_for_status()
                status(f"Listening to #{identity['login']}")
                timeout = welcome.get("keepalive_timeout_seconds") or 30
                last_message = time.monotonic()
                while not stop.is_set():
                    if time.monotonic() >= validate_at:
                        if validate(token)["expires_in"] <= 60:
                            response = requests.post(IDENTITY + "/token", timeout=10, data={
                                "client_id": CLIENT_ID, "grant_type": "refresh_token",
                                "refresh_token": credentials["refresh_token"],
                            })
                            response.raise_for_status()
                            credentials = response.json()
                            token = credentials["access_token"]
                        identity = validate(token)
                        validate_at = time.monotonic() + min(3600, max(0, identity["expires_in"] - 60))
                    try:
                        message = json.loads(socket.recv(timeout=1))
                    except TimeoutError:
                        if time.monotonic() - last_message > timeout + 2:
                            raise ConnectionError("Twitch keepalive timed out.")
                        continue
                    last_message = time.monotonic()
                    metadata = message["metadata"]
                    kind = metadata["message_type"]
                    if kind == "session_reconnect":
                        # Reopening the canonical endpoint creates a fresh subscription.
                        # No events are replayed during the gap.
                        raise ConnectionError("Twitch requested a reconnect.")
                    if kind == "revocation":
                        raise RuntimeError("Twitch revoked chat access. Reconnect Twitch.")
                    if kind == "notification" and metadata.get("subscription_type") == "channel.chat.message":
                        event = message["payload"]["event"]
                        message_id = event["message_id"]
                        if message_id not in seen:
                            seen[message_id] = None
                            if len(seen) > 2048:
                                del seen[next(iter(seen))]
                            trigger(event, user_id)
        except requests.HTTPError:
            raise
        except (ConnectionClosed, OSError, TimeoutError) as exc:
            status(f"Twitch disconnected. Retrying ({exc}).")
            stop.wait(2)
