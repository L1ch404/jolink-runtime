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
        if self.error:
            return json.dumps(
                {"ok": False, "error": self.error, **self.data},
                ensure_ascii=False,
            )
        return json.dumps({"ok": self.ok, **self.data}, ensure_ascii=False)

