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

## Versioned Runtime Config

Provider config is now also part of the broader workspace/voicebot runtime
configuration:

- `PUT /workspaces/{workspace_id}/voicebots/{voicebot_id}/runtime-config`
- `GET /workspaces/{workspace_id}/voicebots/{voicebot_id}/runtime-config`

The runtime config is versioned with `config_version`. Every successful save
activates a new version and emits `runtime_config_updated` with
`workspace_id`, `voicebot_id`, `config_version`, and enabled state. Future
session-routing work should stamp this `config_version` on accepted sessions so
active calls keep the config version they started with while new calls use the
latest enabled version.

The schema includes:

- provider selections and secret references for STT, TTS, and communication agent
- greeting, system prompt, STT prompt, and language
- realtime audio thresholds, endpointing, reply limits, and TTS chunk size
- concurrency/provider quotas and enabled actions
- FlowHunt subagent binding fields
- enabled state

Local `.env` settings remain the development fallback for the current Docker
runtime. The versioned runtime config is the product/control-plane contract that
should move to FlowHunt DB plus FlowHunt secret storage before Kubernetes
deployment.

Runtime config responses redact by design: API keys are never part of this
payload. Providers reference secrets by `{name, workspace_id}` only.

Prompt config can also be managed independently from provider config:

- `GET /workspaces/{workspace_id}/voicebots/{voicebot_id}/prompts`
- `PUT /workspaces/{workspace_id}/voicebots/{voicebot_id}/prompts`
- `PATCH /workspaces/{workspace_id}/voicebots/{voicebot_id}/prompts`

The effective prompt config is cached in the runtime and included in
`/agent/tasks` context as `voicebot_prompts` and
`prompt_configs_by_call_id`. Communication-agent workers therefore do not need
to fetch prompt config on every turn. Prompt overrides take precedence over
prompts stored inside versioned runtime config; if neither exists, local `.env`
defaults are used.

For multilingual voicebots, configure prompt `language` as `auto`. The STT
runtime treats `auto`, `detect`, `multilingual`, and `any` as no fixed language
hint, while the communication agent mirrors the latest caller language. Use a
fixed language code only when the voicebot should intentionally prefer one
language.

The runtime keeps detected language as call/session context after accepted
caller transcripts. For `auto` prompt configs, `/agent/tasks` exposes the
detected language as the effective prompt language and marks
`language_source=session_detected`. TTS cache keys also include the effective
language, so reused phrases are partitioned by language instead of sharing a
single `auto` bucket.

Subagent prompt hooks are part of the versioned runtime config, not the
standalone communication prompt override. They are configured per provider kind
under `subagents.prompts`:

```json
{
  "subagents": {
    "flowhunt_workspace_id": "workspace-1",
    "flowhunt_flow_id": "flow-1",
    "prompts": {
      "flowhunt_flow": {
        "before_call_prompt": "I will ask the specialist now.",
        "after_call_prompt": "The specialist is checking it now.",
        "result_prompt": "Use this colleague result for the caller: {result}"
      }
    }
  }
}
```

These hooks let each voicebot/provider pair customize what is said before the
subagent call, what progress text is attached after the call is submitted, and
how the subagent answer is consumed by the communication agent. The result
prompt can reference `{result}`, `{provider}`, `{status}`, `{error}`,
`{call_id}`, `{task_id}`, `{external_task_id}`, and `{input_text}`.
