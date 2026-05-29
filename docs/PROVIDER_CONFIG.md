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

Provider config value objects reject blank workspace, voicebot, provider,
secret, model, and fallback fields before validation reaches runtime provider
descriptors.

`validate_provider_config()` checks:

- provider choices include a provider id
- provider choice family matches the config slot
- provider is registered for the family
- required credentials have a secret reference
- secret references belong to the same workspace as the voicebot config
- fallback provider exists
- fallback provider is different from the primary provider
- fallback providers that require credentials have a usable secret reference

Validation should run when saving provider config and before enabling a channel.

## Runtime Plan

`provider_selection_plan()` converts product config into normalized provider,
fallback, and model selections that runtime workers can use per session.

## Runtime API

`PUT /workspaces/{workspace_id}/voicebots/{voicebot_id}/providers` validates and
saves STT, TTS, and communication-agent provider choices for a voicebot.

`GET /workspaces/{workspace_id}/voicebots/{voicebot_id}/providers` returns the
saved config plus the normalized runtime selection plan.

This first store is process-local. In FlowHunt production the same payload
should be backed by workspace-scoped database rows and secret references should
resolve through FlowHunt's secret store.

If a provider descriptor declares a fixed model list, validation rejects models
outside that list. Providers without a descriptor model catalog can still accept
workspace-specific model names.

## Fallback Policy

Each family can define a fallback provider. The first implementation only models
fallback selection; runtime retry and failover behavior should be added at the
provider call boundary with metrics and typed provider-error events.
