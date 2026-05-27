from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import Any

from .core_processors import AgentRequestProcessor, EventLogProcessor, STTProcessor, TTSProcessor
from .events import EventStore
from .fanout import FanOutBranch, FanOutProcessor
from .processors import DropProcessor, FrameProcessorBase, PassthroughProcessor


@dataclass(frozen=True)
class ProcessorSpec:
    name: str
    options: dict[str, Any] = field(default_factory=dict)


ProcessorFactory = Callable[[dict[str, Any], "ProcessorDependencies"], FrameProcessorBase]


@dataclass
class ProcessorDependencies:
    events: EventStore | None = None
    stt: Any = None
    tts: Any = None


class ProcessorRegistry:
    def __init__(self) -> None:
        self._factories: dict[str, ProcessorFactory] = {}

    def register(self, name: str, factory: ProcessorFactory) -> None:
        self._factories[name] = factory

    def create(self, spec: ProcessorSpec, dependencies: ProcessorDependencies) -> FrameProcessorBase:
        factory = self._factories.get(spec.name)
        if factory is None:
            names = ", ".join(sorted(self._factories))
            raise ValueError(f"Unknown processor '{spec.name}'. Registered processors: {names}")
        return factory(spec.options, dependencies)

    def create_many(
        self,
        specs: Iterable[ProcessorSpec],
        dependencies: ProcessorDependencies,
    ) -> list[FrameProcessorBase]:
        return [self.create(spec, dependencies) for spec in specs]


def default_processor_registry() -> ProcessorRegistry:
    registry = ProcessorRegistry()
    registry.register("passthrough", lambda options, deps: PassthroughProcessor(options.get("name")))
    registry.register("drop", lambda options, deps: DropProcessor(options.get("name")))
    registry.register("event-log", _event_log_factory)
    registry.register("stt", _stt_factory)
    registry.register("agent-request", _agent_request_factory)
    registry.register("tts", _tts_factory)
    registry.register("fan-out", lambda options, deps: _fanout_factory(options, deps, registry))
    return registry


def _event_log_factory(options: dict[str, Any], dependencies: ProcessorDependencies) -> EventLogProcessor:
    if dependencies.events is None:
        raise ValueError("event-log processor requires EventStore dependency")
    return EventLogProcessor(dependencies.events)


def _stt_factory(options: dict[str, Any], dependencies: ProcessorDependencies) -> STTProcessor:
    if dependencies.stt is None:
        raise ValueError("stt processor requires STT dependency")
    return STTProcessor(dependencies.stt)


def _agent_request_factory(options: dict[str, Any], dependencies: ProcessorDependencies) -> AgentRequestProcessor:
    return AgentRequestProcessor(request_partials=bool(options.get("request_partials", False)))


def _tts_factory(options: dict[str, Any], dependencies: ProcessorDependencies) -> TTSProcessor:
    if dependencies.tts is None:
        raise ValueError("tts processor requires TTS dependency")
    return TTSProcessor(dependencies.tts)


def _fanout_factory(
    options: dict[str, Any],
    dependencies: ProcessorDependencies,
    registry: ProcessorRegistry,
) -> FanOutProcessor:
    branches_config = options.get("branches", [])
    if not isinstance(branches_config, list):
        raise ValueError("fan-out processor requires branches to be a list")

    branches: list[FanOutBranch] = []
    for index, branch_config in enumerate(branches_config, start=1):
        if not isinstance(branch_config, dict):
            raise ValueError("fan-out branch configuration must be an object")
        processor_specs = _processor_specs(branch_config.get("processors", []))
        branches.append(
            FanOutBranch(
                name=str(branch_config.get("name") or f"branch-{index}"),
                processors=registry.create_many(processor_specs, dependencies),
                include_outputs=bool(branch_config.get("include_outputs", False)),
            )
        )
    return FanOutProcessor(branches, name=str(options.get("name") or "fan-out"))


def _processor_specs(config: Any) -> list[ProcessorSpec]:
    if not isinstance(config, list):
        raise ValueError("fan-out branch processors must be a list")

    specs: list[ProcessorSpec] = []
    for item in config:
        if isinstance(item, ProcessorSpec):
            specs.append(item)
            continue
        if not isinstance(item, dict):
            raise ValueError("processor configuration must be an object")
        name = item.get("name")
        if not isinstance(name, str) or not name:
            raise ValueError("processor configuration requires a non-empty name")
        options = item.get("options", {})
        if not isinstance(options, dict):
            raise ValueError("processor options must be an object")
        specs.append(ProcessorSpec(name, options))
    return specs
