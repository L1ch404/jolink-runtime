"""Compact MCP v0.1 tool definitions.

These schemas are intentionally separate from the frozen Runtime 2.4.0
lineage schemas in ``adapters.java.tool_schema``.
"""

from __future__ import annotations

from copy import deepcopy

import mcp.types as types


PUBLIC_RUNTIME_ACTIONS = (
    "run",
    "stop",
    "restart",
    "attach",
    "detach",
    "status",
    "logs",
    "breakpoint",
    "exception",
    "wait_event",
    "threads",
    "stack",
    "variables",
    "resume",
    "cleanup_debug_state",
)

JAVA_RUNTIME_DESCRIPTION = (
    "Observe and control a local JVM when source code, logs, or tests cannot "
    "determine the executed path or runtime state. Supports launch/attach, "
    "breakpoints, exception events, stack frames, variables, and resume. "
    "After wait_event returns a suspension_id, always call resume or "
    "cleanup_debug_state after inspection."
)

JAVA_RUNTIME_INPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "action": {
            "type": "string",
            "enum": list(PUBLIC_RUNTIME_ACTIONS),
            "description": "Runtime operation to perform.",
        },
        "classpath": {
            "type": "string",
            "default": ".",
            "description": "Classpath for run/restart with main_class.",
        },
        "main_class": {
            "type": "string",
            "description": "Fully qualified main class for classpath launch.",
        },
        "jar_path": {
            "type": "string",
            "description": "Executable JAR for run/restart; overrides classpath mode.",
        },
        "app_args": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Application arguments for run/restart.",
        },
        "jdwp_port": {
            "type": "integer",
            "minimum": 1024,
            "maximum": 65535,
            "default": 5005,
            "description": "Local JDWP port for launch or attach.",
        },
        "vm_args": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Additional JVM arguments for run/restart.",
        },
        "pid": {
            "type": "integer",
            "minimum": 1,
            "description": "Local Java PID required by attach.",
        },
        "host": {
            "type": "string",
            "default": "127.0.0.1",
            "description": "JDWP host; v0.1 accepts localhost only.",
        },
        "tail": {
            "type": "integer",
            "minimum": 1,
            "maximum": 500,
            "default": 50,
            "description": "Launch-log lines for logs; attach output is unavailable.",
        },
        "bp_action": {
            "type": "string",
            "enum": ["set", "remove", "list"],
            "default": "set",
            "description": "Breakpoint operation.",
        },
        "exception_action": {
            "type": "string",
            "enum": ["set", "remove", "list"],
            "default": "set",
            "description": "Exception-watch operation.",
        },
        "breakpoint_id": {
            "type": "string",
            "description": "Breakpoint identifier returned by set/list.",
        },
        "request_id": {
            "type": "integer",
            "minimum": 1,
            "description": "Exception-watch identifier returned by set/list.",
        },
        "class_pattern": {
            "type": "string",
            "description": "Class name/pattern for breakpoint set or removal.",
        },
        "include_proxy": {
            "type": "boolean",
            "default": False,
            "description": "Allow proxy classes in breakpoint matching.",
        },
        "include_generated": {
            "type": "boolean",
            "default": False,
            "description": "Allow generated classes in breakpoint matching.",
        },
        "exception_class": {
            "type": "string",
            "description": "Exception class in Java name, JVM path, or signature form.",
        },
        "caught": {
            "type": "boolean",
            "default": True,
            "description": "Watch caught exceptions.",
        },
        "uncaught": {
            "type": "boolean",
            "default": True,
            "description": "Watch uncaught exceptions.",
        },
        "allow_broad_caught": {
            "type": "boolean",
            "default": False,
            "description": "Allow noisy broad caught-exception watches.",
        },
        "line": {
            "type": "integer",
            "minimum": 1,
            "description": "Source line for breakpoint set/removal.",
        },
        "thread_name": {
            "type": "string",
            "description": "Suspended thread-name substring for stack/variables.",
        },
        "frame_index": {
            "type": "integer",
            "minimum": 0,
            "default": 0,
            "description": "Frame index for variables.",
        },
        "max_frames": {
            "type": "integer",
            "minimum": 1,
            "maximum": 100,
            "default": 20,
            "description": "Maximum stack frames.",
        },
        "include_this": {
            "type": "boolean",
            "default": False,
            "description": "Include this in variables.",
        },
        "max_value_depth": {
            "type": "integer",
            "minimum": 0,
            "maximum": 5,
            "default": 1,
            "description": "Object expansion depth.",
        },
        "semantic_collections": {
            "type": "boolean",
            "default": True,
            "description": "Render supported Java collections logically.",
        },
        "item_limit": {
            "type": "integer",
            "minimum": 0,
            "maximum": 64,
            "default": 16,
            "description": "Maximum list/set/array items.",
        },
        "map_entry_limit": {
            "type": "integer",
            "minimum": 0,
            "maximum": 64,
            "default": 16,
            "description": "Maximum map entries.",
        },
        "timeout": {
            "type": "number",
            "minimum": 0.1,
            "maximum": 300,
            "default": 30,
            "description": "Seconds to wait for an event.",
        },
        "suspension_id": {
            "type": "string",
            "description": "Active suspension id returned by wait_event/status.",
        },
    },
    "required": ["action"],
}

JAVA_PROCESSES_DESCRIPTION = (
    "List local Java processes so a JVM can be selected for java_runtime attach."
)

JAVA_PROCESSES_INPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "filter": {
            "type": "string",
            "description": "Optional case-insensitive process filter.",
        },
        "full": {
            "type": "boolean",
            "default": False,
            "description": "Include JVM arguments; slower and more verbose.",
        },
    },
}


def get_mcp_tools() -> list[types.Tool]:
    """Build fresh MCP tool models from the compact v0.1 schemas."""
    return [
        types.Tool(
            name="java_runtime",
            description=JAVA_RUNTIME_DESCRIPTION,
            inputSchema=deepcopy(JAVA_RUNTIME_INPUT_SCHEMA),
        ),
        types.Tool(
            name="java_processes",
            description=JAVA_PROCESSES_DESCRIPTION,
            inputSchema=deepcopy(JAVA_PROCESSES_INPUT_SCHEMA),
        ),
    ]


__all__ = [
    "JAVA_PROCESSES_DESCRIPTION",
    "JAVA_PROCESSES_INPUT_SCHEMA",
    "JAVA_RUNTIME_DESCRIPTION",
    "JAVA_RUNTIME_INPUT_SCHEMA",
    "PUBLIC_RUNTIME_ACTIONS",
    "get_mcp_tools",
]
