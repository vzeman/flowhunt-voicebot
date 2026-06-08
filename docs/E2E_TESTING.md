# Deployed E2E Testing

The normal validation suite does not depend on a live SIP/WebRTC deployment or
provider credentials. Deployed end-to-end tests are marked with `e2e` and run
only when `VOICEBOT_E2E_BASE_URL` points at a reachable voicebot API.

Run the regular suite without deployed checks:

```bash
python -m pytest -q -m "not e2e"
```

Run deployed smoke tests against a local or remote environment:

```bash
VOICEBOT_E2E_BASE_URL=http://127.0.0.1:8080 \
VOICEBOT_E2E_WORKSPACE_ID=workspace-1 \
VOICEBOT_E2E_VOICEBOT_ID=voicebot-1 \
python -m pytest -q -m e2e tests/e2e
```

Environment variables:

- `VOICEBOT_E2E_BASE_URL`: Required. Base URL for the deployed API.
- `VOICEBOT_E2E_WORKSPACE_ID`: Optional. Workspace used for scoped catalog checks.
- `VOICEBOT_E2E_VOICEBOT_ID`: Optional. Voicebot used for scoped catalog checks.
- `VOICEBOT_E2E_TIMEOUT_SECONDS`: Optional. HTTP timeout per deployed request.

The current smoke suite verifies readiness, the event catalog, configured
storage drivers, workspace transport discovery, event-stream readability,
worker queue lifecycle behavior, and session lease events over HTTP. The queue
and lease checks are deterministic process-boundary probes that do not require
provider credentials.
Provider-backed media scenarios should be added as separate `e2e` tests with
explicit environment requirements for SIP, WebRTC, STT, TTS, and agent
credentials.
