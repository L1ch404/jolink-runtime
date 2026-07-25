"""Runtime request, observation, and result models."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


# ============================================================================
# Observation — what the LLM sees (never protocol details)
# ============================================================================


@dataclass
class RuntimeObservation:
    """Structured observation returned by ``status()``."""
    running: bool = False
    pid: int | None = None
    message: str = ""
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class StackFrame:
    """A single stack frame."""
    index: int = 0
    class_name: str = ""
    method_name: str = ""
    line: int = 0
    is_native: bool = False


@dataclass
class Variable:
    """A local variable with its value."""
    name: str = ""
    type_name: str = ""
    value: Any = None
    value_observed: bool = False
    error: str = ""
    slot: int = 0


# ============================================================================
# Action — what the LLM requests
# ============================================================================


@dataclass
class RuntimeAction:
    """Parsed action from LLM tool call args."""
    action: str

    # lifecycle
    classpath: str = "."
    main_class: str = ""
    jar_path: str = ""
    app_args: list[str] | None = None
    jdwp_port: int = 5005
    vm_args: list[str] | None = None
    pid: int = 0
    host: str = "127.0.0.1"

    # observation
    tail: int = 50

    # debug
    bp_action: str = "set"
    exception_action: str = "set"
    breakpoint_id: str = ""
    request_id: int = 0
    class_pattern: str = ""
    include_proxy: bool = False
    include_generated: bool = False
    exception_class: str = ""
    caught: bool = True
    uncaught: bool = True
    allow_broad_caught: bool = False
    line: int = 0
    thread_name: str = ""
    frame_index: int = 0
    max_frames: int = 20
    include_this: bool = False
    max_value_depth: int = 1
    semantic_collections: bool = True
    item_limit: int = 16
    map_entry_limit: int = 16
    timeout: float = 30.0
    suspension_id: str = ""

    def configure_startup_readiness(
        self,
        *,
        ready_port: int = 0,
        wait_timeout_seconds: float = 30.0,
        wait_timeout_provided: bool = False,
        config_source: str = "",
    ) -> None:
        """Attach MCP-only launch metadata without changing lineage fields."""
        self._ready_port = int(ready_port)
        self._startup_wait_timeout_seconds = float(wait_timeout_seconds)
        self._startup_wait_timeout_provided = bool(wait_timeout_provided)
        self._readiness_config_source = str(config_source)

    @property
    def ready_port(self) -> int:
        return int(getattr(self, "_ready_port", 0))

    @property
    def startup_wait_timeout_seconds(self) -> float:
        return float(
            getattr(self, "_startup_wait_timeout_seconds", 30.0)
        )

    @property
    def startup_wait_timeout_provided(self) -> bool:
        return bool(
            getattr(self, "_startup_wait_timeout_provided", False)
        )

    @property
    def readiness_config_source(self) -> str:
        return str(getattr(self, "_readiness_config_source", ""))


# ============================================================================
# RuntimeResult — what the handler turns into JSON for the LLM
# ============================================================================


@dataclass
class RuntimeResult:
    """Structured result for the LLM tool callback."""
    ok: bool = True
    data: dict[str, Any] = field(default_factory=dict)
    error: str = ""

    def to_json(self) -> str:
        import json
        data = {
            key: value
            for key, value in self.data.items()
            if key not in {"ok", "error"}
        }
        if self.error:
            return json.dumps(
                {**data, "ok": False, "error": self.error},
                ensure_ascii=False,
            )
        return json.dumps({**data, "ok": self.ok}, ensure_ascii=False)
