from __future__ import annotations

from .agent_tasks import AgentTaskTracker, JsonAgentTaskTracker
from .config import Settings
from .call_state import CallStateStore, JsonCallStateStore
from .events import EventStore, JsonEventStore
from .provider_config import JsonProviderConfigStore, ProviderConfigStore
from .scaling import JsonWorkerQueueStore, JsonWorkerRegistry, WorkerQueueStore, WorkerRegistry
from .session_leases import JsonSessionLeaseStore, SessionLeaseStore
from .sip_trunks import SipTrunkStore
from .storage import (
    FilesystemArtifactStore,
    S3ArtifactStore,
    RedisAgentTaskTracker,
    RedisCallStateStore,
    RedisSessionLeaseStore,
    RedisSubagentTaskStore,
    RedisWorkerQueueStore,
    RedisWorkerRegistry,
    SQLiteEventStore,
    SQLiteProviderConfigStore,
    SQLiteSipTrunkStore,
    SQLiteTranscriptStore,
    SQLiteVoicebotSessionStore,
    StorageDriverDefinition,
    StorageDriverSelection,
    StorageRegistry,
    attach_storage_driver,
    normalize_driver_name,
)
from .subagents import JsonSubagentTaskStore, SubagentTaskStore
from .transcripts import TranscriptStore
from .workspace_model import JsonVoicebotSessionStore, VoicebotSessionStore


def default_storage_registry() -> StorageRegistry:
    return StorageRegistry(
        [
            _definition("events", "memory", "process", False, True, False, "in-memory append-only event list"),
            _definition("events", "jsonl", "node", False, True, False, "append-only JSONL event log"),
            _definition("events", "sqlite", "node", False, True, False, "SQLite event table with workspace indexes"),
            _definition("events", "postgres", "shared", True, False, True, "PostgreSQL event table with workspace indexes", implemented=False),
            _definition("events", "flowhunt_db", "shared", True, False, True, "workspace-scoped durable DB event rows", implemented=False),
            _definition("events", "append_only_event_log", "shared", True, False, True, "managed append-only event log", implemented=False),
            _definition("transcripts", "jsonl", "node", False, True, False, "per-call JSONL transcript files"),
            _definition("transcripts", "sqlite", "node", False, True, False, "SQLite transcript metadata and text index"),
            _definition("transcripts", "postgres", "shared", True, False, True, "PostgreSQL transcript metadata and text index", implemented=False),
            _definition("transcripts", "flowhunt_db", "shared", True, False, True, "workspace-scoped transcript rows", implemented=False),
            _definition("voicebot_sessions", "memory", "process", False, True, False, "in-memory routed session records"),
            _definition("voicebot_sessions", "json", "node", False, True, False, "local JSON session records"),
            _definition("voicebot_sessions", "sqlite", "node", False, True, False, "SQLite session records"),
            _definition("voicebot_sessions", "postgres", "shared", True, False, True, "PostgreSQL session records", implemented=False),
            _definition("voicebot_sessions", "flowhunt_db", "shared", True, False, True, "workspace-scoped session table", implemented=False),
            _definition("session_leases", "memory", "process", False, True, False, "best-effort in-memory leases"),
            _definition("session_leases", "json", "node", False, True, False, "local JSON leases"),
            _definition("session_leases", "redis", "shared", True, False, True, "atomic lease-capable KV", runtime_dependencies=("redis",)),
            _definition("agent_tasks", "memory", "process", False, True, False, "in-memory task claims/responded ids"),
            _definition("agent_tasks", "json", "node", False, True, False, "local JSON task claims/responded ids"),
            _definition("agent_tasks", "redis", "shared", True, False, True, "shared claim and responded-id state", runtime_dependencies=("redis",)),
            _definition("agent_tasks", "flowhunt_db", "shared", True, False, True, "durable task response table", implemented=False),
            _definition("worker_queue", "memory", "process", False, True, False, "in-memory queue"),
            _definition("worker_queue", "json", "node", False, True, False, "local JSON queue"),
            _definition("worker_queue", "redis", "shared", True, False, True, "shared Redis queue state", runtime_dependencies=("redis",)),
            _definition("worker_queue", "redis_streams", "shared", True, False, True, "Redis Streams queue", implemented=False),
            _definition("worker_queue", "nats_jetstream", "shared", True, False, True, "NATS JetStream queue", implemented=False),
            _definition("worker_queue", "rabbitmq", "shared", True, False, True, "RabbitMQ queue", implemented=False),
            _definition("worker_queue", "flowhunt_queue", "shared", True, False, True, "FlowHunt managed queue", implemented=False),
            _definition("worker_registry", "memory", "process", False, True, False, "in-memory worker heartbeats"),
            _definition("worker_registry", "json", "node", False, True, False, "local JSON worker heartbeats"),
            _definition("worker_registry", "redis", "shared", True, False, True, "shared heartbeat registry", runtime_dependencies=("redis",)),
            _definition("worker_registry", "flowhunt_db", "shared", True, False, True, "durable worker records", implemented=False),
            _definition("call_states", "memory", "process", False, True, False, "in-memory active call snapshots"),
            _definition("call_states", "json", "node", False, True, False, "local JSON active call snapshots"),
            _definition("call_states", "redis", "shared", True, False, True, "shared active call snapshots", runtime_dependencies=("redis",)),
            _definition("call_states", "flowhunt_db", "shared", True, False, True, "durable call state snapshots", implemented=False),
            _definition("provider_config", "memory", "process", False, True, False, "in-memory provider config"),
            _definition("provider_config", "json", "node", False, True, False, "local JSON provider config records"),
            _definition("provider_config", "sqlite", "node", False, True, False, "SQLite provider config records"),
            _definition("provider_config", "postgres", "shared", True, False, True, "PostgreSQL provider config records", implemented=False),
            _definition("provider_config", "flowhunt_db", "shared", True, False, True, "versioned provider config records", implemented=False),
            _definition("sip_trunks", "json", "node", False, True, False, "local trunk registry plus generated PJSIP include"),
            _definition("sip_trunks", "sqlite", "node", False, True, False, "SQLite trunk records plus generated PJSIP include"),
            _definition("sip_trunks", "postgres", "shared", True, False, True, "PostgreSQL trunk records with secret references", implemented=False),
            _definition("sip_trunks", "flowhunt_db", "shared", True, False, True, "workspace-scoped trunk records", implemented=False),
            _definition("subagent_tasks", "memory", "process", False, True, False, "in-memory delegated task lifecycle"),
            _definition("subagent_tasks", "json", "node", False, True, False, "local JSON delegated task lifecycle"),
            _definition("subagent_tasks", "flowhunt_db", "shared", True, False, True, "durable delegated task records", implemented=False),
            _definition("subagent_tasks", "redis", "shared", True, False, True, "shared delegated task coordination", runtime_dependencies=("redis",)),
            _definition("subagent_tasks", "flowhunt_queue", "shared", True, False, True, "FlowHunt task queue handoff", implemented=False),
            _definition("audio_artifacts", "filesystem", "node", False, True, False, "local filesystem artifacts/cache"),
            _definition("audio_artifacts", "object_storage", "shared", True, False, True, "managed object storage", runtime_dependencies=("boto3",)),
            _definition("audio_artifacts", "s3", "shared", True, False, True, "S3-compatible object storage", runtime_dependencies=("boto3",)),
        ]
    )


def _definition(
    family: str,
    driver: str,
    scope: str,
    managed: bool,
    supports_local_dev: bool,
    supports_production: bool,
    consistency: str,
    *,
    implemented: bool = True,
    runtime_dependencies: tuple[str, ...] = (),
) -> StorageDriverDefinition:
    return StorageDriverDefinition(
        family=family,
        driver=driver,
        scope=scope,
        managed=managed,
        supports_local_dev=supports_local_dev,
        supports_production=supports_production,
        consistency=consistency,
        implemented=implemented,
        runtime_dependencies=runtime_dependencies,
    )


def build_event_store(settings: Settings, transcripts: TranscriptStore) -> EventStore:
    driver = normalize_driver_name(settings.event_store_provider)
    if driver == "json":
        driver = "jsonl"
    selection = storage_driver_selection("events", driver, settings.event_store_provider, settings.event_store_path)
    if driver == "jsonl":
        return attach_storage_driver(
            JsonEventStore(settings.event_store_path, settings.max_context_events, transcript_store=transcripts),
            selection,
        )
    if driver == "sqlite":
        return attach_storage_driver(
            SQLiteEventStore(settings.relational_database_url, settings.max_context_events, transcript_store=transcripts),
            storage_driver_selection(
                "events",
                driver,
                settings.event_store_provider,
                None,
                {"database_url": settings.relational_database_url},
            ),
        )
    if driver == "memory":
        return attach_storage_driver(EventStore(settings.max_context_events, transcript_store=transcripts), selection)
    raise_unsupported_storage("VOICEBOT_EVENT_STORE_PROVIDER", settings.event_store_provider, selection)


def build_voicebot_session_store(settings: Settings) -> VoicebotSessionStore:
    driver = json_object_driver(settings.voicebot_session_store_provider)
    selection = storage_driver_selection(
        "voicebot_sessions",
        driver,
        settings.voicebot_session_store_provider,
        settings.voicebot_session_store_path,
    )
    if driver == "json":
        return attach_storage_driver(JsonVoicebotSessionStore(settings.voicebot_session_store_path), selection)
    if driver == "memory":
        return attach_storage_driver(VoicebotSessionStore(), selection)
    if driver == "sqlite":
        return attach_storage_driver(
            SQLiteVoicebotSessionStore(settings.relational_database_url),
            storage_driver_selection(
                "voicebot_sessions",
                driver,
                settings.voicebot_session_store_provider,
                None,
                {"database_url": settings.relational_database_url},
            ),
        )
    raise_unsupported_storage("VOICEBOT_SESSION_STORE_PROVIDER", settings.voicebot_session_store_provider, selection)


def build_session_lease_store(settings: Settings) -> SessionLeaseStore:
    driver = json_object_driver(settings.session_lease_store_provider)
    selection = storage_driver_selection(
        "session_leases",
        driver,
        settings.session_lease_store_provider,
        settings.session_lease_store_path,
    )
    if driver == "json":
        return attach_storage_driver(JsonSessionLeaseStore(settings.session_lease_store_path), selection)
    if driver == "memory":
        return attach_storage_driver(SessionLeaseStore(), selection)
    if driver == "redis":
        return attach_storage_driver(
            RedisSessionLeaseStore(settings.redis_url),
            storage_driver_selection(
                "session_leases",
                driver,
                settings.session_lease_store_provider,
                None,
                {"redis_url": settings.redis_url},
            ),
        )
    raise_unsupported_storage("VOICEBOT_SESSION_LEASE_STORE_PROVIDER", settings.session_lease_store_provider, selection)


def build_agent_task_tracker(settings: Settings) -> AgentTaskTracker:
    driver = json_object_driver(settings.agent_task_store_provider)
    selection = storage_driver_selection("agent_tasks", driver, settings.agent_task_store_provider, settings.agent_task_store_path)
    if driver == "json":
        return attach_storage_driver(
            JsonAgentTaskTracker(
                settings.agent_task_store_path,
                max_responded_event_ids=settings.agent_task_responded_event_retention,
            ),
            selection,
        )
    if driver == "memory":
        return attach_storage_driver(AgentTaskTracker(settings.agent_task_responded_event_retention), selection)
    if driver == "redis":
        return attach_storage_driver(
            RedisAgentTaskTracker(
                settings.redis_url,
                max_responded_event_ids=settings.agent_task_responded_event_retention,
            ),
            storage_driver_selection(
                "agent_tasks",
                driver,
                settings.agent_task_store_provider,
                None,
                {"redis_url": settings.redis_url},
            ),
        )
    raise_unsupported_storage("VOICEBOT_AGENT_TASK_STORE_PROVIDER", settings.agent_task_store_provider, selection)


def build_call_state_store(settings: Settings) -> CallStateStore:
    driver = json_object_driver(settings.call_state_store_provider)
    selection = storage_driver_selection("call_states", driver, settings.call_state_store_provider, settings.call_state_store_path)
    if driver == "json":
        return attach_storage_driver(JsonCallStateStore(settings.call_state_store_path), selection)
    if driver == "memory":
        return attach_storage_driver(CallStateStore(), selection)
    if driver == "redis":
        return attach_storage_driver(
            RedisCallStateStore(settings.redis_url),
            storage_driver_selection(
                "call_states",
                driver,
                settings.call_state_store_provider,
                None,
                {"redis_url": settings.redis_url},
            ),
        )
    raise_unsupported_storage("VOICEBOT_CALL_STATE_STORE_PROVIDER", settings.call_state_store_provider, selection)


def build_worker_queue_store(settings: Settings) -> WorkerQueueStore:
    driver = json_object_driver(settings.worker_queue_store_provider)
    selection = storage_driver_selection("worker_queue", driver, settings.worker_queue_store_provider, settings.worker_queue_store_path)
    if driver == "json":
        return attach_storage_driver(JsonWorkerQueueStore(settings.worker_queue_store_path), selection)
    if driver == "memory":
        return attach_storage_driver(WorkerQueueStore(), selection)
    if driver == "redis":
        return attach_storage_driver(
            RedisWorkerQueueStore(settings.redis_url),
            storage_driver_selection(
                "worker_queue",
                driver,
                settings.worker_queue_store_provider,
                None,
                {"redis_url": settings.redis_url},
            ),
        )
    raise_unsupported_storage("VOICEBOT_WORKER_QUEUE_STORE_PROVIDER", settings.worker_queue_store_provider, selection)


def build_worker_registry(settings: Settings) -> WorkerRegistry:
    driver = json_object_driver(settings.worker_registry_store_provider)
    selection = storage_driver_selection(
        "worker_registry",
        driver,
        settings.worker_registry_store_provider,
        settings.worker_registry_store_path,
    )
    if driver == "json":
        return attach_storage_driver(
            JsonWorkerRegistry(
                settings.worker_registry_store_path,
                heartbeat_ttl_seconds=settings.worker_registry_heartbeat_ttl_seconds,
            ),
            selection,
        )
    if driver == "memory":
        return attach_storage_driver(
            WorkerRegistry(heartbeat_ttl_seconds=settings.worker_registry_heartbeat_ttl_seconds),
            selection,
        )
    if driver == "redis":
        return attach_storage_driver(
            RedisWorkerRegistry(
                settings.redis_url,
                heartbeat_ttl_seconds=settings.worker_registry_heartbeat_ttl_seconds,
            ),
            storage_driver_selection(
                "worker_registry",
                driver,
                settings.worker_registry_store_provider,
                None,
                {"redis_url": settings.redis_url},
            ),
        )
    raise_unsupported_storage("VOICEBOT_WORKER_REGISTRY_STORE_PROVIDER", settings.worker_registry_store_provider, selection)


def build_transcript_store(settings: Settings) -> TranscriptStore:
    driver = normalize_driver_name(settings.transcript_store_provider)
    if driver == "json":
        driver = "jsonl"
    selection = storage_driver_selection("transcripts", driver, settings.transcript_store_provider, settings.transcript_dir)
    if driver == "jsonl":
        return attach_storage_driver(TranscriptStore(settings.transcript_dir), selection)
    if driver == "sqlite":
        return attach_storage_driver(
            SQLiteTranscriptStore(settings.relational_database_url),
            storage_driver_selection(
                "transcripts",
                driver,
                settings.transcript_store_provider,
                None,
                {"database_url": settings.relational_database_url},
            ),
        )
    raise_unsupported_storage("VOICEBOT_TRANSCRIPT_STORE_PROVIDER", settings.transcript_store_provider, selection)


def build_provider_config_store(settings: Settings) -> ProviderConfigStore:
    driver = json_object_driver(settings.provider_config_store_provider)
    selection = storage_driver_selection(
        "provider_config",
        driver,
        settings.provider_config_store_provider,
        settings.provider_config_store_path,
    )
    if driver == "json":
        return attach_storage_driver(JsonProviderConfigStore(settings.provider_config_store_path), selection)
    if driver == "memory":
        return attach_storage_driver(ProviderConfigStore(), selection)
    if driver == "sqlite":
        return attach_storage_driver(
            SQLiteProviderConfigStore(settings.relational_database_url),
            storage_driver_selection(
                "provider_config",
                driver,
                settings.provider_config_store_provider,
                None,
                {"database_url": settings.relational_database_url},
            ),
        )
    raise_unsupported_storage("VOICEBOT_PROVIDER_CONFIG_STORE_PROVIDER", settings.provider_config_store_provider, selection)


def build_sip_trunk_store(settings: Settings) -> SipTrunkStore:
    driver = normalize_driver_name(settings.sip_trunk_store_provider)
    selection = storage_driver_selection("sip_trunks", driver, settings.sip_trunk_store_provider, settings.sip_trunk_registry_path)
    if driver == "json":
        return attach_storage_driver(
            SipTrunkStore(settings.sip_trunk_registry_path, settings.sip_trunk_pjsip_include_path),
            selection,
        )
    if driver == "sqlite":
        return attach_storage_driver(
            SQLiteSipTrunkStore(settings.relational_database_url, settings.sip_trunk_pjsip_include_path),
            storage_driver_selection(
                "sip_trunks",
                driver,
                settings.sip_trunk_store_provider,
                None,
                {"database_url": settings.relational_database_url},
            ),
        )
    raise_unsupported_storage("VOICEBOT_SIP_TRUNK_STORE_PROVIDER", settings.sip_trunk_store_provider, selection)


def build_subagent_task_store(settings: Settings) -> SubagentTaskStore:
    driver = json_object_driver(settings.subagent_task_store_provider)
    selection = storage_driver_selection(
        "subagent_tasks",
        driver,
        settings.subagent_task_store_provider,
        settings.subagent_task_store_path,
    )
    if driver == "json":
        return attach_storage_driver(JsonSubagentTaskStore(settings.subagent_task_store_path), selection)
    if driver == "memory":
        return attach_storage_driver(SubagentTaskStore(), selection)
    if driver == "redis":
        return attach_storage_driver(
            RedisSubagentTaskStore(settings.redis_url),
            storage_driver_selection(
                "subagent_tasks",
                driver,
                settings.subagent_task_store_provider,
                None,
                {"redis_url": settings.redis_url},
            ),
        )
    raise_unsupported_storage("VOICEBOT_SUBAGENT_TASK_STORE_PROVIDER", settings.subagent_task_store_provider, selection)


def build_audio_artifact_store(settings: Settings) -> FilesystemArtifactStore | S3ArtifactStore:
    driver = normalize_driver_name(settings.audio_artifact_store_provider)
    selection = storage_driver_selection(
        "audio_artifacts",
        driver,
        settings.audio_artifact_store_provider,
        settings.tts_cache_dir,
        {"debug_audio_dir": settings.debug_audio_dir},
    )
    if driver == "filesystem":
        return attach_storage_driver(FilesystemArtifactStore(settings.tts_cache_dir), selection)
    if driver in {"s3", "object_storage"}:
        return attach_storage_driver(
            S3ArtifactStore(
                settings.object_storage_bucket,
                endpoint_url=settings.object_storage_endpoint,
                region_name=settings.object_storage_region,
            ),
            storage_driver_selection(
                "audio_artifacts",
                driver,
                settings.audio_artifact_store_provider,
                None,
                {
                    "bucket": settings.object_storage_bucket,
                    "endpoint": settings.object_storage_endpoint,
                    "region": settings.object_storage_region,
                },
            ),
        )
    raise_unsupported_storage("VOICEBOT_AUDIO_ARTIFACT_STORE_PROVIDER", settings.audio_artifact_store_provider, selection)


def selected_storage_drivers(settings: Settings) -> dict[str, StorageDriverSelection]:
    selections = {
        "events": storage_driver_selection(
            "events",
            "jsonl" if normalize_driver_name(settings.event_store_provider) == "json" else normalize_driver_name(settings.event_store_provider),
            settings.event_store_provider,
            settings.event_store_path if normalize_driver_name(settings.event_store_provider) not in {"sqlite", "postgres"} else None,
            {"database_url": settings.relational_database_url}
            if normalize_driver_name(settings.event_store_provider) in {"sqlite", "postgres"}
            else None,
        ),
        "transcripts": storage_driver_selection(
            "transcripts",
            "jsonl" if normalize_driver_name(settings.transcript_store_provider) == "json" else normalize_driver_name(settings.transcript_store_provider),
            settings.transcript_store_provider,
            settings.transcript_dir
            if normalize_driver_name(settings.transcript_store_provider) not in {"sqlite", "postgres"}
            else None,
            {"database_url": settings.relational_database_url}
            if normalize_driver_name(settings.transcript_store_provider) in {"sqlite", "postgres"}
            else None,
        ),
        "voicebot_sessions": storage_driver_selection(
            "voicebot_sessions",
            json_object_driver(settings.voicebot_session_store_provider),
            settings.voicebot_session_store_provider,
            settings.voicebot_session_store_path
            if json_object_driver(settings.voicebot_session_store_provider) not in {"sqlite", "postgres"}
            else None,
            {"database_url": settings.relational_database_url}
            if json_object_driver(settings.voicebot_session_store_provider) in {"sqlite", "postgres"}
            else None,
        ),
        "session_leases": storage_driver_selection(
            "session_leases",
            json_object_driver(settings.session_lease_store_provider),
            settings.session_lease_store_provider,
            settings.session_lease_store_path if json_object_driver(settings.session_lease_store_provider) != "redis" else None,
            {"redis_url": settings.redis_url} if json_object_driver(settings.session_lease_store_provider) == "redis" else None,
        ),
        "agent_tasks": storage_driver_selection(
            "agent_tasks",
            json_object_driver(settings.agent_task_store_provider),
            settings.agent_task_store_provider,
            settings.agent_task_store_path if json_object_driver(settings.agent_task_store_provider) != "redis" else None,
            {"redis_url": settings.redis_url} if json_object_driver(settings.agent_task_store_provider) == "redis" else None,
        ),
        "worker_queue": storage_driver_selection(
            "worker_queue",
            json_object_driver(settings.worker_queue_store_provider),
            settings.worker_queue_store_provider,
            settings.worker_queue_store_path if json_object_driver(settings.worker_queue_store_provider) != "redis" else None,
            {"redis_url": settings.redis_url} if json_object_driver(settings.worker_queue_store_provider) == "redis" else None,
        ),
        "worker_registry": storage_driver_selection(
            "worker_registry",
            json_object_driver(settings.worker_registry_store_provider),
            settings.worker_registry_store_provider,
            settings.worker_registry_store_path if json_object_driver(settings.worker_registry_store_provider) != "redis" else None,
            {"redis_url": settings.redis_url} if json_object_driver(settings.worker_registry_store_provider) == "redis" else None,
        ),
        "call_states": storage_driver_selection(
            "call_states",
            json_object_driver(settings.call_state_store_provider),
            settings.call_state_store_provider,
            settings.call_state_store_path if json_object_driver(settings.call_state_store_provider) != "redis" else None,
            {"redis_url": settings.redis_url} if json_object_driver(settings.call_state_store_provider) == "redis" else None,
        ),
        "provider_config": storage_driver_selection(
            "provider_config",
            json_object_driver(settings.provider_config_store_provider),
            settings.provider_config_store_provider,
            settings.provider_config_store_path
            if json_object_driver(settings.provider_config_store_provider) not in {"sqlite", "postgres"}
            else None,
            {"database_url": settings.relational_database_url}
            if json_object_driver(settings.provider_config_store_provider) in {"sqlite", "postgres"}
            else None,
        ),
        "sip_trunks": storage_driver_selection(
            "sip_trunks",
            normalize_driver_name(settings.sip_trunk_store_provider),
            settings.sip_trunk_store_provider,
            settings.sip_trunk_registry_path
            if normalize_driver_name(settings.sip_trunk_store_provider) not in {"sqlite", "postgres"}
            else None,
            {"database_url": settings.relational_database_url}
            if normalize_driver_name(settings.sip_trunk_store_provider) in {"sqlite", "postgres"}
            else None,
        ),
        "subagent_tasks": storage_driver_selection(
            "subagent_tasks",
            json_object_driver(settings.subagent_task_store_provider),
            settings.subagent_task_store_provider,
            settings.subagent_task_store_path if json_object_driver(settings.subagent_task_store_provider) != "redis" else None,
            {"redis_url": settings.redis_url} if json_object_driver(settings.subagent_task_store_provider) == "redis" else None,
        ),
        "audio_artifacts": storage_driver_selection(
            "audio_artifacts",
            normalize_driver_name(settings.audio_artifact_store_provider),
            settings.audio_artifact_store_provider,
            settings.tts_cache_dir if normalize_driver_name(settings.audio_artifact_store_provider) == "filesystem" else None,
            {"debug_audio_dir": settings.debug_audio_dir}
            if normalize_driver_name(settings.audio_artifact_store_provider) == "filesystem"
            else {
                "bucket": settings.object_storage_bucket,
                "endpoint": settings.object_storage_endpoint,
                "region": settings.object_storage_region,
            },
        ),
    }
    return selections


def storage_driver_selection(
    family: str,
    driver: str,
    configured_driver: str,
    path: str | None = None,
    options: dict | None = None,
) -> StorageDriverSelection:
    definition = default_storage_registry().resolve(family, driver)
    return StorageDriverSelection(
        family=family,
        driver=driver,
        configured_driver=configured_driver,
        path=path,
        definition=definition,
        options=options or {},
    )


def json_object_driver(configured_provider: str) -> str:
    driver = normalize_driver_name(configured_provider)
    return "json" if driver == "jsonl" else driver


def storage_drivers_payload(settings: Settings) -> dict:
    return {
        "registry": default_storage_registry().to_dict(),
        "selected": {
            family: selection.to_dict()
            for family, selection in selected_storage_drivers(settings).items()
        },
    }


def raise_unsupported_storage(env_name: str, configured: str, selection: StorageDriverSelection):
    definitions = default_storage_registry().definitions_for_family(selection.family)
    implemented = sorted(definition.driver for definition in definitions if definition.implemented)
    planned = sorted(definition.driver for definition in definitions if not definition.implemented)
    planned_text = f" Planned drivers not yet selectable: {planned}." if planned else ""
    raise ValueError(
        f"Unsupported {env_name}: {configured}. "
        f"Implemented drivers for {selection.family}: {implemented}.{planned_text}"
    )
