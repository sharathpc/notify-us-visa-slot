#!/usr/bin/env python3
"""Poll the authorized visa-slot API and notify when slots become available."""

from __future__ import annotations

import json
import logging
import os
import re
import shlex
import signal
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from threading import Event
from typing import Any, Callable, Optional, Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen


LOG = logging.getLogger("notify_us_visa_slot")
MIN_POLL_INTERVAL_SECONDS = 10
LOGIN_REQUIRED_REMAINING_THRESHOLD = 200
LOGIN_REQUIRED_PRIORITY = 4
DEFAULT_API_BASE_URL = "https://app.checkvisaslots.com"
DEFAULT_EXT_VERSION = "5.1.5"
ENV_FILE = Path(__file__).with_name(".env")


class MonitorError(Exception):
    """Raised when API configuration or responses are invalid."""


class AuthorizationError(MonitorError):
    """Raised when the API rejects access to a protected endpoint."""


@dataclass(frozen=True)
class SlotSnapshot:
    available_slots: dict[str, int]
    remaining_activity: int


def load_env_file(path: Path = ENV_FILE) -> None:
    if not path.exists():
        return
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise MonitorError(f"Unable to read environment file: {path.name}.") from exc

    for line_number, line in enumerate(lines, start=1):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if stripped.startswith("export "):
            stripped = stripped[len("export ") :].lstrip()
        key, separator, raw_value = stripped.partition("=")
        key = key.strip()
        if not separator or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
            raise MonitorError(f"Invalid environment file entry on line {line_number}.")
        try:
            values = shlex.split(raw_value, comments=True, posix=True)
        except ValueError as exc:
            raise MonitorError(f"Invalid value in environment file on line {line_number}.") from exc
        if len(values) > 1:
            raise MonitorError(f"Quote values containing spaces on line {line_number}.")
        if key not in os.environ:
            os.environ[key] = values[0] if values else ""


@dataclass(frozen=True)
class Config:
    api_key: str
    api_base_url: str
    ext_version: str
    poll_interval_seconds: int
    zero_slots_heartbeat_minutes: int = 30
    ntfy_topic_url: Optional[str] = None
    ntfy_availability_priority: int = 5
    ntfy_heartbeat_priority: int = 1

    @classmethod
    def from_env(cls) -> Config:
        api_key = os.environ.get("VISA_API_KEY", "").strip()
        if not api_key:
            raise MonitorError("VISA_API_KEY is required.")

        base_url = os.environ.get("VISA_API_BASE_URL", DEFAULT_API_BASE_URL).rstrip("/")
        _require_https_url(base_url, "VISA_API_BASE_URL")

        raw_interval = os.environ.get("VISA_POLL_INTERVAL_SECONDS", "60")
        try:
            interval = int(raw_interval)
        except ValueError as exc:
            raise MonitorError("VISA_POLL_INTERVAL_SECONDS must be an integer.") from exc
        if interval < MIN_POLL_INTERVAL_SECONDS:
            raise MonitorError(
                f"VISA_POLL_INTERVAL_SECONDS must be at least "
                f"{MIN_POLL_INTERVAL_SECONDS} seconds."
            )

        raw_heartbeat = os.environ.get("VISA_ZERO_SLOTS_HEARTBEAT_MINUTES", "30")
        try:
            heartbeat_minutes = int(raw_heartbeat)
        except ValueError as exc:
            raise MonitorError(
                "VISA_ZERO_SLOTS_HEARTBEAT_MINUTES must be an integer."
            ) from exc
        if heartbeat_minutes < 1:
            raise MonitorError("VISA_ZERO_SLOTS_HEARTBEAT_MINUTES must be at least 1.")

        ntfy_topic_url = os.environ.get("VISA_NTFY_TOPIC_URL", "").strip() or None
        if ntfy_topic_url:
            _require_https_url(ntfy_topic_url, "VISA_NTFY_TOPIC_URL")
        ntfy_availability_priority = _ntfy_priority_from_env(
            "VISA_NTFY_AVAILABILITY_PRIORITY", "5"
        )
        ntfy_heartbeat_priority = _ntfy_priority_from_env(
            "VISA_NTFY_HEARTBEAT_PRIORITY", "1"
        )

        return cls(
            api_key=api_key,
            api_base_url=base_url,
            ext_version=os.environ.get("VISA_EXT_VERSION", DEFAULT_EXT_VERSION),
            poll_interval_seconds=interval,
            zero_slots_heartbeat_minutes=heartbeat_minutes,
            ntfy_topic_url=ntfy_topic_url,
            ntfy_availability_priority=ntfy_availability_priority,
            ntfy_heartbeat_priority=ntfy_heartbeat_priority,
        )


def _ntfy_priority_from_env(name: str, default: str) -> int:
    raw_priority = os.environ.get(name, default)
    try:
        priority = int(raw_priority)
    except ValueError as exc:
        raise MonitorError(f"{name} must be an integer from 1 to 5.") from exc
    if not 1 <= priority <= 5:
        raise MonitorError(f"{name} must be from 1 to 5.")
    return priority


def _require_https_url(value: str, setting: str) -> None:
    parsed = urlsplit(value)
    if parsed.scheme != "https" or not parsed.netloc:
        raise MonitorError(f"{setting} must be an HTTPS URL.")


def _request_json(
    url: str,
    api_key: Optional[str],
    ext_version: Optional[str],
    *,
    timeout: int = 20,
) -> Any:
    headers = {
        "Accept": "*/*",
        "Accept-Language": "en-US,en;q=0.9",
        "Cache-Control": "no-cache",
        "Pragma": "no-cache",
        "Priority": "u=1, i",
        "Sec-CH-UA": '"Chromium";v="154", "Google Chrome";v="154", "Not(A:Brand";v="99"',
        "Sec-CH-UA-Mobile": "?0",
        "Sec-CH-UA-Platform": '"macOS"',
        "Sec-Fetch-Dest": "empty",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Site": "none",
        "Sec-Fetch-Storage-Access": "none",
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/154.0.0.0 Safari/537.36"
        ),
    }
    if ext_version is not None:
        headers["extversion"] = ext_version
    if api_key is not None:
        headers["x-api-key"] = api_key
    request = Request(url, headers=headers, method="GET")

    try:
        with urlopen(request, timeout=timeout) as response:
            response_body = response.read()
    except HTTPError as exc:
        if exc.code in (401, 403):
            raise AuthorizationError(
                f"API rejected access with HTTP {exc.code}; check the API key and account permissions."
            ) from exc
        raise MonitorError(f"Request failed with HTTP status {exc.code}.") from exc
    except (URLError, TimeoutError) as exc:
        reason = exc.reason if isinstance(exc, URLError) else exc
        raise MonitorError(f"Request failed: {reason}") from exc

    try:
        return json.loads(response_body)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise MonitorError("API returned invalid JSON.") from exc


def _send_ntfy_notification(
    topic_url: str,
    message: str,
    priority: int = 4,
) -> None:
    headers = {
        "Content-Type": "text/plain; charset=utf-8",
        "Title": "Notify-US-Visa-Slot appointment available",
        "Priority": str(priority),
    }
    request = Request(
        topic_url,
        data=message.encode("utf-8"),
        headers=headers,
        method="POST",
    )
    try:
        with urlopen(request, timeout=20) as response:
            LOG.info("ntfy accepted notification (HTTP %s).", response.getcode())
    except HTTPError as exc:
        raise MonitorError(f"ntfy notification failed with HTTP status {exc.code}.") from exc
    except (URLError, TimeoutError) as exc:
        reason = exc.reason if isinstance(exc, URLError) else exc
        raise MonitorError(f"ntfy notification failed: {reason}") from exc


class SlotClient:
    def __init__(self, config: Config) -> None:
        self.config = config

    def authorize(self) -> None:
        response = _request_json(
            f"{self.config.api_base_url}/validate/v3",
            self.config.api_key,
            self.config.ext_version,
        )
        if not isinstance(response, dict) or response.get("apikey") != self.config.api_key:
            raise MonitorError("Authorization endpoint did not validate the configured API key.")
        LOG.info("API authorization validated.")

    def fetch_snapshot(self) -> SlotSnapshot:
        response = _request_json(
            f"{self.config.api_base_url}/slots/v3",
            self.config.api_key,
            self.config.ext_version,
        )
        if not isinstance(response, dict) or not isinstance(response.get("slotDetails"), list):
            raise MonitorError("Slots endpoint returned an unexpected response.")
        user_activity = response.get("userActivity")
        if not isinstance(user_activity, dict):
            raise MonitorError("Slots endpoint response is missing userActivity.")
        remaining_activity = user_activity.get("remaining")
        if (
            isinstance(remaining_activity, bool)
            or not isinstance(remaining_activity, int)
            or remaining_activity < 0
        ):
            raise MonitorError("Slots endpoint returned an invalid userActivity.remaining value.")

        available: dict[str, int] = {}
        for item in response["slotDetails"]:
            if not isinstance(item, dict):
                raise MonitorError("Slots endpoint returned a malformed slot entry.")
            location = item.get("visa_location")
            if not isinstance(location, str) or not location.strip():
                raise MonitorError("Slots endpoint returned an entry without a location.")
            slots = item.get("slots")
            if isinstance(slots, bool) or not isinstance(slots, int) or slots < 0:
                raise MonitorError(f"Slots endpoint returned an invalid slot count for {location}.")
            if slots > 0:
                available[location.strip()] = slots
        return SlotSnapshot(available, remaining_activity)


class Notifier(Protocol):
    def notify(self, message: str, priority: int) -> None: ...


class AlertNotifier:
    def __init__(
        self,
        ntfy_topic_url: Optional[str] = None,
        ntfy_availability_priority: int = 5,
        ntfy_heartbeat_priority: int = 1,
    ) -> None:
        self.ntfy_topic_url = ntfy_topic_url
        self.ntfy_availability_priority = ntfy_availability_priority
        self.ntfy_heartbeat_priority = ntfy_heartbeat_priority

    def notify(self, message: str, priority: Optional[int] = None) -> None:
        LOG.warning("%s", message)
        if self.ntfy_topic_url is not None:
            try:
                _send_ntfy_notification(
                    self.ntfy_topic_url,
                    message,
                    priority if priority is not None else self.ntfy_availability_priority,
                )
            except MonitorError:
                LOG.exception("ntfy notification failed.")
        else:
            LOG.warning("ntfy is not configured; notification was only logged locally.")


def _availability_message(available: dict[str, int]) -> str:
    details = ", ".join(f"{location}: {count}" for location, count in sorted(available.items()))
    return f"Visa appointment slots are available - {details}"


def _zero_slots_message() -> str:
    return "Notify-US-Visa-Slot is running - no slots currently available in any location."


def _login_required_message(remaining_activity: int) -> str:
    return (
        "Notify-US-Visa-Slot stopped because API activity remaining is "
        f"{remaining_activity} (below {LOGIN_REQUIRED_REMAINING_THRESHOLD}). "
        "Please log in to refresh access, then restart the monitor."
    )


def run_monitor(
    client: SlotClient,
    notifier: Notifier,
    poll_interval_seconds: int,
    *,
    should_stop: Optional[Callable[[], bool]] = None,
    stop_event: Optional[Event] = None,
    zero_slots_heartbeat_minutes: int = 30,
    availability_priority: int = 5,
    heartbeat_priority: int = 1,
) -> None:
    client.authorize()
    previous_available: dict[str, int] = {}
    last_zero_notification: Optional[float] = None
    heartbeat_seconds = zero_slots_heartbeat_minutes * 60
    stop_requested = lambda: (
        (stop_event is not None and stop_event.is_set())
        or (should_stop is not None and should_stop())
    )

    while not stop_requested():
        try:
            snapshot = client.fetch_snapshot()
            if snapshot.remaining_activity < LOGIN_REQUIRED_REMAINING_THRESHOLD:
                message = _login_required_message(snapshot.remaining_activity)
                LOG.error("%s", message)
                notifier.notify(message, LOGIN_REQUIRED_PRIORITY)
                return

            available = snapshot.available_slots
            if available and available != previous_available:
                notifier.notify(_availability_message(available), availability_priority)
            if available:
                LOG.info("Slot check completed; available slots: %s", available)
                last_zero_notification = None
            else:
                LOG.info("Slot check completed; no slots currently available.")
                now = time.monotonic()
                if (
                    last_zero_notification is None
                    or now - last_zero_notification >= heartbeat_seconds
                ):
                    notifier.notify(_zero_slots_message(), heartbeat_priority)
                    last_zero_notification = now
            previous_available = available
        except AuthorizationError:
            raise
        except MonitorError:
            LOG.exception("Slot check or notification failed; retrying after the configured interval.")

        if not stop_requested():
            if stop_event is not None:
                stop_event.wait(poll_interval_seconds)
            else:
                time.sleep(poll_interval_seconds)


def main() -> int:
    try:
        load_env_file()
    except MonitorError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 2
    logging.basicConfig(
        level=os.environ.get("VISA_LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(message)s",
    )
    try:
        config = Config.from_env()
    except MonitorError as exc:
        LOG.error("%s", exc)
        return 2

    stop_event = Event()

    def request_stop(_signum: int, _frame: Any) -> None:
        stop_event.set()

    signal.signal(signal.SIGTERM, request_stop)

    try:
        run_monitor(
            SlotClient(config),
            AlertNotifier(
                config.ntfy_topic_url,
                config.ntfy_availability_priority,
                config.ntfy_heartbeat_priority,
            ),
            config.poll_interval_seconds,
            stop_event=stop_event,
            zero_slots_heartbeat_minutes=config.zero_slots_heartbeat_minutes,
            availability_priority=config.ntfy_availability_priority,
            heartbeat_priority=config.ntfy_heartbeat_priority,
        )
    except KeyboardInterrupt:
        LOG.info("Notify-US-Visa-Slot interrupted.")
        return 0
    except AuthorizationError as exc:
        LOG.error("API access denied: %s", exc)
        return 1
    except MonitorError:
        LOG.exception("Monitor startup failed.")
        return 1
    LOG.info("Notify-US-Visa-Slot stopped.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
