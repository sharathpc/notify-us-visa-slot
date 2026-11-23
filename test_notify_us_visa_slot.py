import os
import tempfile
import unittest
from pathlib import Path
from urllib.error import HTTPError
from unittest.mock import patch

from notify_us_visa_slot import (
    AuthorizationError,
    Config,
    MonitorError,
    SlotClient,
    SlotSnapshot,
    _availability_message,
    _login_required_message,
    _request_json,
    _send_ntfy_notification,
    load_env_file,
    main,
    run_monitor,
    _zero_slots_message,
)


class ConfigTests(unittest.TestCase):
    def test_loads_env_file_without_overriding_existing_environment(self):
        with tempfile.TemporaryDirectory() as directory:
            env_path = Path(directory) / ".env"
            env_path.write_text(
                "# monitor settings\nVISA_API_KEY='file key'\nVISA_POLL_INTERVAL_SECONDS=30\n",
                encoding="utf-8",
            )
            with patch.dict(os.environ, {"VISA_API_KEY": "existing key"}, clear=True):
                load_env_file(env_path)
                self.assertEqual(os.environ["VISA_API_KEY"], "existing key")
                self.assertEqual(os.environ["VISA_POLL_INTERVAL_SECONDS"], "30")

    def test_rejects_malformed_env_file_entry(self):
        with tempfile.TemporaryDirectory() as directory:
            env_path = Path(directory) / ".env"
            env_path.write_text("not-an-assignment\n", encoding="utf-8")
            with self.assertRaisesRegex(MonitorError, "line 1"):
                load_env_file(env_path)

    def test_http_401_is_reported_as_authorization_error(self):
        error = HTTPError("https://example.test/slots/v3", 401, "Unauthorized", {}, None)
        with patch("notify_us_visa_slot.urlopen", side_effect=error):
            with self.assertRaisesRegex(AuthorizationError, "HTTP 401"):
                _request_json("https://example.test/slots/v3", "secret", "1")

    def test_request_includes_captured_browser_headers_and_api_key(self):
        class FakeResponse:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                pass

            def read(self):
                return b'{"ok": true}'

        with patch("notify_us_visa_slot.urlopen", return_value=FakeResponse()) as urlopen:
            _request_json("https://example.test/slots/v3", "test-key", "5.1.5")
        request = urlopen.call_args.args[0]
        self.assertEqual(request.get_header("X-api-key"), "test-key")
        self.assertEqual(request.get_header("User-agent"), (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/154.0.0.0 Safari/537.36"
        ))
        self.assertEqual(request.get_header("Sec-fetch-mode"), "cors")

    def test_ntfy_sends_plain_text_with_optional_bearer_token(self):
        class FakeResponse:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                pass

            def getcode(self):
                return 200

        with patch("notify_us_visa_slot.urlopen", return_value=FakeResponse()) as urlopen, \
             self.assertLogs("notify_us_visa_slot", level="INFO") as logs:
            _send_ntfy_notification(
                "https://ntfy.sh/private-topic",
                "Visa appointment slots are available - HYDERABAD: 2",
                priority=5,
            )
        request = urlopen.call_args.args[0]
        self.assertEqual(request.get_method(), "POST")
        self.assertEqual(
            request.data,
            b"Visa appointment slots are available - HYDERABAD: 2",
        )
        self.assertEqual(request.get_header("Content-type"), "text/plain; charset=utf-8")
        self.assertEqual(request.get_header("Priority"), "5")
        self.assertIn("ntfy accepted notification (HTTP 200)", logs.output[0])

    def test_requires_api_key(self):
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(MonitorError, "VISA_API_KEY is required"):
                Config.from_env()

    def test_rejects_poll_interval_below_documented_minimum(self):
        with patch.dict(
            os.environ,
            {"VISA_API_KEY": "test-key", "VISA_POLL_INTERVAL_SECONDS": "9"},
            clear=True,
        ):
            with self.assertRaisesRegex(MonitorError, "at least 10 seconds"):
                Config.from_env()

    def test_accepts_minimum_interval_and_defaults(self):
        with patch.dict(
            os.environ,
            {"VISA_API_KEY": "test-key", "VISA_POLL_INTERVAL_SECONDS": "10"},
            clear=True,
        ):
            config = Config.from_env()
        self.assertEqual(config.poll_interval_seconds, 10)
        self.assertEqual(config.api_base_url, "https://app.checkvisaslots.com")
        self.assertIsNone(config.ntfy_topic_url)
        self.assertEqual(config.zero_slots_heartbeat_minutes, 30)
        self.assertEqual(config.ntfy_availability_priority, 5)
        self.assertEqual(config.ntfy_heartbeat_priority, 1)

    def test_reads_separate_ntfy_priorities(self):
        with patch.dict(
            os.environ,
            {
                "VISA_API_KEY": "test-key",
                "VISA_NTFY_AVAILABILITY_PRIORITY": "5",
                "VISA_NTFY_HEARTBEAT_PRIORITY": "1",
            },
            clear=True,
        ):
            config = Config.from_env()
        self.assertEqual(config.ntfy_availability_priority, 5)
        self.assertEqual(config.ntfy_heartbeat_priority, 1)

    def test_rejects_ntfy_priority_outside_supported_range(self):
        with patch.dict(
            os.environ,
            {"VISA_API_KEY": "test-key", "VISA_NTFY_AVAILABILITY_PRIORITY": "6"},
            clear=True,
        ):
            with self.assertRaisesRegex(MonitorError, "from 1 to 5"):
                Config.from_env()

    def test_reads_zero_slots_heartbeat_minutes(self):
        with patch.dict(
            os.environ,
            {
                "VISA_API_KEY": "test-key",
                "VISA_ZERO_SLOTS_HEARTBEAT_MINUTES": "45",
            },
            clear=True,
        ):
            config = Config.from_env()
        self.assertEqual(config.zero_slots_heartbeat_minutes, 45)

    def test_rejects_non_positive_zero_slots_heartbeat(self):
        with patch.dict(
            os.environ,
            {
                "VISA_API_KEY": "test-key",
                "VISA_ZERO_SLOTS_HEARTBEAT_MINUTES": "0",
            },
            clear=True,
        ):
            with self.assertRaisesRegex(MonitorError, "at least 1"):
                Config.from_env()

    def test_reads_ntfy_topic(self):
        with patch.dict(
            os.environ,
            {
                "VISA_API_KEY": "test-key",
                "VISA_NTFY_TOPIC_URL": "https://ntfy.sh/private-topic",
            },
            clear=True,
        ):
            config = Config.from_env()
        self.assertEqual(config.ntfy_topic_url, "https://ntfy.sh/private-topic")

    def test_rejects_non_https_api_url(self):
        with patch.dict(
            os.environ,
            {"VISA_API_KEY": "test-key", "VISA_API_BASE_URL": "http://example.test"},
            clear=True,
        ):
            with self.assertRaisesRegex(MonitorError, "must be an HTTPS URL"):
                Config.from_env()


class AvailabilityMessageTests(unittest.TestCase):
    def test_message_is_deterministic(self):
        self.assertEqual(
            _availability_message({"MUMBAI": 2, "CHENNAI": 1}),
            "Visa appointment slots are available - CHENNAI: 1, MUMBAI: 2",
        )

    def test_slot_client_returns_positive_counts_for_all_locations(self):
        config = Config("test-key", "https://example.test", "1", 10, None)
        client = SlotClient(config)
        response = {
            "slotDetails": [
                {"visa_location": "HYDERABAD", "slots": 2},
                {"visa_location": "HYDERABAD VAC", "slots": 0},
                {"visa_location": "CHENNAI", "slots": 8},
            ],
            "userActivity": {"remaining": 500},
        }
        with patch("notify_us_visa_slot._request_json", return_value=response):
            self.assertEqual(
                client.fetch_snapshot(),
                SlotSnapshot({"HYDERABAD": 2, "CHENNAI": 8}, 500),
            )

    def test_slot_client_returns_availability_for_every_city(self):
        config = Config("test-key", "https://example.test", "1", 10, None)
        client = SlotClient(config)
        response = {
            "slotDetails": [
                {"visa_location": "CHENNAI", "slots": 12},
                {"visa_location": "MUMBAI", "slots": 4},
                {"visa_location": "HYDERABAD", "slots": 0},
                {"visa_location": "HYDERABAD VAC", "slots": 0},
            ],
            "userActivity": {"remaining": 500},
        }
        with patch("notify_us_visa_slot._request_json", return_value=response):
            self.assertEqual(
                client.fetch_snapshot(),
                SlotSnapshot({"CHENNAI": 12, "MUMBAI": 4}, 500),
            )

    def test_slot_client_requires_valid_remaining_activity(self):
        config = Config("test-key", "https://example.test", "1", 10, None)
        client = SlotClient(config)
        response = {
            "slotDetails": [],
            "userActivity": {"remaining": True},
        }
        with patch("notify_us_visa_slot._request_json", return_value=response):
            with self.assertRaisesRegex(MonitorError, "invalid userActivity.remaining"):
                client.fetch_snapshot()

    def test_authorize_requires_matching_api_key_in_response(self):
        config = Config("test-key", "https://example.test", "1", 10, None)
        client = SlotClient(config)
        with patch("notify_us_visa_slot._request_json", return_value={"apikey": "test-key"}):
            client.authorize()
        with patch("notify_us_visa_slot._request_json", return_value={"message": "invalid key"}):
            with self.assertRaisesRegex(MonitorError, "did not validate"):
                client.authorize()

    def test_monitor_notifies_only_when_available_counts_change(self):
        class FakeClient:
            snapshots = [
                {"HYDERABAD": 1},
                {"HYDERABAD": 1},
                {"MUMBAI": 2},
            ]

            def __init__(self):
                self.calls = 0

            def authorize(self):
                pass

            def fetch_snapshot(self):
                result = self.snapshots[self.calls]
                self.calls += 1
                return SlotSnapshot(result, 500)

        class FakeNotifier:
            def __init__(self):
                self.messages = []
                self.priorities = []

            def notify(self, message, priority):
                self.messages.append(message)
                self.priorities.append(priority)

        client = FakeClient()
        notifier = FakeNotifier()
        with patch("notify_us_visa_slot.time.sleep"):
            run_monitor(client, notifier, 10, should_stop=lambda: client.calls == 3)
        self.assertEqual(
            notifier.messages,
            [
                "Visa appointment slots are available - HYDERABAD: 1",
                "Visa appointment slots are available - MUMBAI: 2",
            ],
        )
        self.assertEqual(notifier.priorities, [5, 5])

    def test_low_remaining_activity_sends_priority_four_and_stops_polling(self):
        class FakeClient:
            def __init__(self):
                self.calls = 0

            def authorize(self):
                pass

            def fetch_snapshot(self):
                self.calls += 1
                return SlotSnapshot({"HYDERABAD": 2}, 199)

        class FakeNotifier:
            def __init__(self):
                self.notifications = []

            def notify(self, message, priority):
                self.notifications.append((message, priority))

        client = FakeClient()
        notifier = FakeNotifier()
        with patch("notify_us_visa_slot.time.sleep") as sleep:
            run_monitor(client, notifier, 10)

        self.assertEqual(client.calls, 1)
        sleep.assert_not_called()
        self.assertEqual(
            notifier.notifications,
            [(_login_required_message(199), 4)],
        )

    def test_remaining_activity_at_threshold_does_not_stop_polling(self):
        class FakeClient:
            def __init__(self):
                self.calls = 0

            def authorize(self):
                pass

            def fetch_snapshot(self):
                self.calls += 1
                return SlotSnapshot({}, 200)

        class FakeNotifier:
            def __init__(self):
                self.priorities = []

            def notify(self, _message, priority):
                self.priorities.append(priority)

        client = FakeClient()
        notifier = FakeNotifier()
        with patch("notify_us_visa_slot.time.sleep"):
            run_monitor(
                client,
                notifier,
                10,
                should_stop=lambda: client.calls == 1,
            )
        self.assertEqual(client.calls, 1)
        self.assertEqual(notifier.priorities, [1])

    def test_zero_slots_notification_is_sent_immediately_then_at_heartbeat(self):
        class FakeClient:
            def __init__(self):
                self.calls = 0

            def authorize(self):
                pass

            def fetch_snapshot(self):
                self.calls += 1
                return SlotSnapshot({}, 500)

        class FakeNotifier:
            def __init__(self):
                self.messages = []
                self.priorities = []

            def notify(self, message, priority):
                self.messages.append(message)
                self.priorities.append(priority)

        client = FakeClient()
        notifier = FakeNotifier()
        with patch("notify_us_visa_slot.time.sleep"), patch(
            "notify_us_visa_slot.time.monotonic",
            side_effect=[100.0, 110.0, 1899.0, 1900.0],
        ):
            run_monitor(
                client,
                notifier,
                10,
                should_stop=lambda: client.calls == 4,
                zero_slots_heartbeat_minutes=30,
                availability_priority=5,
                heartbeat_priority=1,
            )
        self.assertEqual(
            notifier.messages,
            [_zero_slots_message(), _zero_slots_message()],
        )
        self.assertEqual(notifier.priorities, [1, 1])

    def test_monitor_stops_on_slot_endpoint_authorization_error(self):
        class FakeClient:
            def authorize(self):
                pass

            def fetch_snapshot(self):
                raise AuthorizationError("API rejected access with HTTP 401.")

        class FakeNotifier:
            def notify(self, message, priority):
                pass

        with self.assertRaises(AuthorizationError):
            run_monitor(FakeClient(), FakeNotifier(), 10)

    def test_ctrl_c_is_handled_as_a_clean_exit(self):
        config = Config("test-key", "https://example.test", "1", 10, None)
        with patch("notify_us_visa_slot.load_env_file"), \
             patch("notify_us_visa_slot.Config.from_env", return_value=config), \
             patch("notify_us_visa_slot.signal.signal"), \
             patch("notify_us_visa_slot.run_monitor", side_effect=KeyboardInterrupt):
            self.assertEqual(main(), 0)

    def test_stop_event_interrupts_the_poll_wait(self):
        from threading import Event

        class FakeClient:
            def authorize(self):
                pass

            def fetch_snapshot(self):
                return SlotSnapshot({}, 500)

        class FakeNotifier:
            def notify(self, message, priority):
                pass

        stop_event = Event()
        with patch("notify_us_visa_slot.Event.wait", side_effect=lambda _timeout: stop_event.set()):
            run_monitor(
                FakeClient(),
                FakeNotifier(),
                3600,
                stop_event=stop_event,
            )
        self.assertTrue(stop_event.is_set())


if __name__ == "__main__":
    unittest.main()
