# Embeddable Voicebot Widget

The public widget lets a website visitor start a WebRTC voice session with a
configured voicebot. It is intentionally separate from the internal dashboard
WebRTC inference console.

## Install

For a custom voicebot route such as `https://voice.example.com/support`, expose
the route through the public ingress and add:

```html
<script src="https://voice.example.com/widget.js" async></script>
```

Inline mode is available for pages that want to place the button inside their
own layout:

```html
<script src="https://voice.example.com/widget.js" data-inline="true" async></script>
```

Optional visitor metadata can be passed as JSON. The runtime treats it as
untrusted visitor data, strips reserved routing keys, limits it to 2048 bytes,
and stores it under `visitor_metadata`.

```html
<script
  src="https://voice.example.com/widget.js"
  data-visitor-metadata='{"page":"pricing","plan":"trial"}'
  async></script>
```

For a direct link or iframe-style integration, use:

```html
<iframe src="https://voice.example.com/widget" title="Voice support"></iframe>
```

## Public Runtime Flow

1. The widget loads `/.well-known/flowhunt-voicebot`.
2. The runtime resolves the public route from the forwarded host/path.
3. The widget receives caller-safe display settings, WebRTC ICE servers, public
   limits, and the session endpoint.
4. The visitor clicks the button and grants microphone access.
5. The widget creates an SDP offer and posts it to `POST /webrtc/sessions`.
6. The runtime validates origin allow-list, SDP size, rate limit, concurrent
   session capacity, and visitor metadata size before allocating the session.
7. Remote voicebot audio is played in the browser.
8. The widget calls `DELETE /webrtc/sessions/{session_id}` when the visitor ends
   the call.

The widget never receives internal API keys, prompts, provider config, task
queues, event logs, diagnostics, or dashboard APIs.

The internal dashboard WebRTC test console includes a `Widget Chat Preview` tab
for administrator testing. It reads caller transcript events and voicebot chat
payloads from the same event stream shape planned for the public widget, so
admins can verify voice plus parallel readable chat before publishing an embed.

## Route Configuration

Widget display settings are read from the public route metadata:

- `launcher_label`
- `welcome_label`
- `locale`
- `theme.primary_color`
- `theme.placement`: `bottom-right` or `bottom-left`
- `show_captions`
- `recording_visible_to_visitor`

Voice/chat capability is controlled by the voicebot runtime config rather than
by the browser script. The `channels` section defines whether the bot supports
voice, parallel chat output, typed chat input, visitor-visible transcripts, and
rich chat content. The `prompts.chat` section defines how the communication
agent should behave when chat is enabled:

- `disabled`: do not generate chat messages.
- `mirror_voice`: show the spoken answer as text.
- `expanded_chat`: keep voice concise and put extra detail in chat.
- `chat_only_when_useful`: send chat messages only for useful supplements such
  as links, images, summaries, or cards.

Public widget sessions must treat chat as optional. Voice-only sessions should
continue without chat events, while voice+chat sessions should render the same
chat schema that the internal WebRTC test preview uses.

Allowed origins are configured on the public route. If `allowed_origins` is
non-empty, browser session creation must include a matching `Origin` header.
The public runtime also answers CORS preflight requests for allowed origins so
the widget can post JSON SDP offers from customer websites. The browser script
itself is public and cacheable; any token or route id in an embed is an
identifier, not a secret.

## Security Notes

- Use only public ingress for `/.well-known/flowhunt-voicebot`, `/widget.js`,
  `/widget`, `POST /webrtc/sessions`, and caller-safe session deletion.
- Keep internal OpenAPI, dashboard, event logs, transcripts, call-control APIs,
  diagnostics, and task queues on private services with internal auth.
- Public admission decisions are emitted as `session_admission_decided` events.
- Visitor recording playback stays disabled unless a future voicebot/channel
  configuration explicitly enables a visitor-safe recording surface.
