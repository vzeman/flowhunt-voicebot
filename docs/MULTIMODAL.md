# Multimodal Extension Points

The runtime remains voice-first. This layer only defines extension points so
future image, video, file, chat, visual card, and avatar output support can be
added without changing session orchestration.

## Content Parts

`MultimodalContent` represents one input or output part:

- modality: audio, text, image, video, screen, file, chat, visual card, avatar
  video
- direction: input or output
- optional MIME type
- optional URI
- optional text
- metadata

`MultimodalContext` packages these parts with `workspace_id`, `voicebot_id`,
`session_id`, and `call_id` for the communication agent.

## Capabilities

`ModalityCapabilities` declares supported input and output modalities. Transport
capabilities now include these modality flags. Provider capabilities can also
declare future multimodal agent support, such as image input or visual output.

## Design Rule

Media/session orchestration should not know how a model consumes images or
renders visual cards. It should move normalized content parts and let provider
adapters translate them into provider-specific payloads.

## Future Work

- WebRTC chat and visual card events
- image/file upload handling
- screen/video frame sampling
- avatar/video output transport
- provider adapters for multimodal LLM input
- UI timeline rendering for non-audio events
