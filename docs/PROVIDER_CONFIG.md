# Provider Configuration Model

Provider configuration is scoped by FlowHunt workspace and voicebot.

## Scope

`VoicebotProviderConfig` contains:

- `workspace_id`
- `voicebot_id`
- independent STT, TTS, and agent provider choices

This lets one workspace run multiple voicebots with different provider stacks.

## Secret References

Runtime config should use `SecretReference`, not raw API keys:

- secret name
- workspace id

Secrets are resolved by the FlowHunt secret system at runtime. Missing required
secrets are validation errors before enabling a voicebot/channel.

## Validation

`validate_provider_config()` checks:

- provider is registered for the family
- required credentials have a secret reference
- fallback provider exists

Validation should run when saving provider config and before enabling a channel.

## Runtime Plan

`provider_selection_plan()` converts product config into normalized provider,
fallback, and model selections that runtime workers can use per session.

## Fallback Policy

Each family can define a fallback provider. The first implementation only models
fallback selection; runtime retry and failover behavior should be added at the
provider call boundary with metrics and typed provider-error events.
