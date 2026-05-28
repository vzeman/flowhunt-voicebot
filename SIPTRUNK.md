# SIP Trunk AudioSocket Example

Yes, this can work, but Python should not implement the SIP trunk directly for
this demo. Use Asterisk for SIP registration and RTP, then stream call audio to
Python with Asterisk `AudioSocket()`.

The Python side is `sip_audiosocket_bridge.py`. It receives 8 kHz signed-linear
PCM from Asterisk, detects speech, transcribes with Whisper, synthesizes with
Supertonic 3, and streams audio back into the call.

## Security

Do not commit the SIP password. The password shared in chat should be considered
exposed if the transcript is stored anywhere outside your control. Rotate it in
the SIP provider UI if needed.

## Install Asterisk

On macOS:

```bash
brew install asterisk
```

Make sure Asterisk has `res_pjsip` and `app_audiosocket` available:

```bash
asterisk -rvvv
module show like pjsip
module show like audiosocket
```

## Configure Asterisk

Use these templates:

- `asterisk/pjsip.conf.example`
- `asterisk/extensions.conf.example`

Copy or include them in your Asterisk config directory, then replace
`REPLACE_WITH_SIP_PASSWORD` locally.

The template is configured for:

- SIP phone ID: `876`
- Display name: `ViktorSIPTrunk`
- Host: `qla-prod-ec1-la-opensips-03.prod-ec1.live-agent.net`
- User: `u262625_876`

After editing, reload:

```asterisk
pjsip reload
dialplan reload
pjsip show registrations
```

## Start Python Bridge

In this project:

```bash
source .venv/bin/activate
python sip_audiosocket_bridge.py --host 127.0.0.1 --port 9019 --whisper-model base
```

Then call the SIP trunk. Asterisk should answer and connect the call to the
Python AudioSocket bridge.

## Dockerized Asterisk

The repository includes `docker-compose.yml` for running Asterisk locally in
Docker. The current Docker stack manages SIP trunks dynamically through the
voicebot API. Asterisk includes `/data/asterisk/pjsip-trunks.conf`, which is
generated from `/data/sip_trunks.json` by the voicebot service.

Start the Python bridge first when using only the low-level demo:

```bash
source .venv/bin/activate
python sip_audiosocket_bridge.py --host 0.0.0.0 --port 9019 --whisper-model base
```

Then start Asterisk. Supplying `SIP_HOST`, `SIP_USER`, and `SIP_PASSWORD` is
only a local-development seed when no dynamic trunk file exists:

```bash
SIP_HOST='sip.example.com' \
SIP_USER='customer_user' \
SIP_PASSWORD='your-password-here' \
docker compose up -d asterisk
```

Check registration:

```bash
docker exec supertronic-asterisk asterisk -rx 'pjsip show registrations'
```

The full Docker stack runs both Asterisk and the voicebot service:

```bash
docker compose up -d --build
```

Add a trunk through the API:

```bash
curl -X POST http://127.0.0.1:8080/sip-trunks \
  -H 'Content-Type: application/json' \
  -d '{
    "trunk_id": "customer-1",
    "host": "sip.example.com",
    "user": "customer_user",
    "password": "your-password-here"
  }'
```

The agent API is exposed on `http://127.0.0.1:8080`.

## Notes

AudioSocket’s dialplan app sends 16-bit, 8 kHz, mono PCM over TCP and receives
the same format back. The script resamples to 16 kHz for Whisper and resamples
Supertonic output back to 8 kHz for the call.

For production, use a real SIP/PBX deployment with TLS/SRTP if supported by the
provider, secret management, call logging policy, retry handling, and stronger
turn-taking/barge-in logic.
