# Notify-US-Visa-Slot

Notify-US-Visa-Slot is a Python 3.9+ monitor that validates your CheckVisaSlots API
key, repeatedly checks the authorized US visa appointment endpoint, and sends
ntfy notifications when appointment availability changes.

The application uses only the Python standard library and can log to the
console, send push notifications through ntfy, or do both.

## Configure and run

Create a local `.env` file next to `notify_us_visa_slot.py`:

```dotenv
VISA_API_KEY=your-rotated-api-key
VISA_POLL_INTERVAL_SECONDS=60
VISA_ZERO_SLOTS_HEARTBEAT_MINUTES=30
VISA_NTFY_TOPIC_URL=https://ntfy.sh/replace-with-a-long-random-topic
```

You can copy `.env.example` as a starting point. The `.env` file is ignored by
Git and must not be committed. Environment variables already set by the process
take precedence over values in `.env`.

Start Notify-US-Visa-Slot with:

```sh
python3 notify_us_visa_slot.py
```

Use `Ctrl+C` or send `SIGTERM` for a clean shutdown.

## Notification behavior

- The monitor validates the API key once during startup.
- The first slot check runs immediately after startup.
- The polling interval defaults to 60 seconds and cannot be lower than 10 seconds.
- Available slots are logged and sent as an ntfy notification.
- Repeated notifications are sent only when the available locations or counts change.
- When every location has zero slots, an “application is running” notification
  is sent immediately and then every 30 minutes by default.
- When API activity remaining is below 200, Notify-US-Visa-Slot sends a login reminder,
  stops polling, and requires a restart after the API access is refreshed.

## ntfy notification setup

### 1. Install ntfy

- On iPhone, install **ntfy** from the App Store.
- Open ntfy and allow notifications when prompted.
- On Android or desktop, use the ntfy app or a supported web client.

### 2. Create a private topic name

Generate a long, unpredictable topic name. For example, use a password manager
or run:

```sh
python3 -c 'import secrets; print(secrets.token_urlsafe(24))'
```

Treat the topic name as a secret. Anyone who knows the full topic URL can publish
to an unrestricted ntfy server topic.

### 3. Subscribe on the phone

1. In the ntfy app, tap **Subscribe** or the add-topic button.
2. Enter `https://ntfy.sh/<your-topic-name>` or the topic name after `ntfy.sh/`.
3. Confirm the subscription.
4. Send a test notification from a terminal:

```sh
curl -d "Notify-US-Visa-Slot test notification" https://ntfy.sh/<your-topic-name>
```

The notification should appear on the phone. If it does not, verify notification
permissions and that the topic name is identical on both devices.

### 4. Configure Notify-US-Visa-Slot

Add the same HTTPS topic publish URL to `.env`:

```dotenv
VISA_NTFY_TOPIC_URL=https://ntfy.sh/replace-with-a-long-random-topic
VISA_NTFY_AVAILABILITY_PRIORITY=5
VISA_NTFY_HEARTBEAT_PRIORITY=1
```

Restart Notify-US-Visa-Slot after changing `.env`. If the topic is on ntfy.sh, HTTPS
is required. For a server that requires authentication or access control, configure
the protected topic on that server using its supported authentication mechanism.

Notify-US-Visa-Slot does not support or require a `VISA_NTFY_TOKEN` setting.

## ntfy priorities

- `5`: Highest priority, for urgent available-slot notifications.
- `3`: Normal priority.
- `1`: Minimum priority, suitable for no-slot heartbeat notifications.

The configured priorities use the `1`–`5` range. Only a topic URL is needed;
there is no built-in username/password or token field.

## Configuration reference

| Variable | Default | Description |
|---|---|---|
| `VISA_API_KEY` | Required | CheckVisaSlots API key |
| `VISA_POLL_INTERVAL_SECONDS` | `60` | Delay between slot checks (minimum 10) |
| `VISA_ZERO_SLOTS_HEARTBEAT_MINUTES` | `30` | Repeat interval for no-slots status notifications (minimum 1) |
| `VISA_NTFY_TOPIC_URL` | None | HTTPS ntfy topic publish URL |
| `VISA_NTFY_AVAILABILITY_PRIORITY` | `5` | Priority for available-slot alerts (1–5) |
| `VISA_NTFY_HEARTBEAT_PRIORITY` | `1` | Priority for no-slot heartbeat notifications (1–5) |
| `VISA_API_BASE_URL` | `https://app.checkvisaslots.com` | Authorized API base URL (HTTPS only) |
| `VISA_EXT_VERSION` | `5.1.5` | API client version header |
| `VISA_LOG_LEVEL` | `INFO` | Python logging level |

## Error handling

API and ntfy errors are logged. Slot-check failures are retried after the
configured polling interval. HTTP 401 or 403 responses stop the monitor rather
than retrying; verify that `.env` contains the current API key and that the
account is permitted to access the slots endpoint.

A successful validation response alone does not guarantee slots-endpoint access.

## Tests

```sh
python3 -m unittest -v test_notify_us_visa_slot.py
```
