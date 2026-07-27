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
    "Run, observe, and debug a local Java application. "
    "Launch or restart the target, inspect status and logs, and verify code "
    "changes against actual runtime behavior before making further assumptions. "
    "When repeated edits fail or a fix needs verification, obtain runtime "
    "evidence before applying another patch. "
    "For deeper investigation, attach to an existing JVM, set "
    "breakpoints or exception watches, inspect stack frames and variables, "
    "and resume suspended threads. "
    "For an HTTP application launched by run/restart, provide ready_port; "
    "if startup_state is starting, call status until TCP readiness is observed "
    "before arming an HTTP trigger. "
    "Treat runtime outputs as bounded observations; separate observed facts from "
    "interpretations and unverified conclusions. "
    "wait_event blocking can arm JDWP event requests, start an optional local "
    "HTTP request only after arming, and await an event in one call; use arm "
    "then await when an external action must occur between them. "
    "Always resume a suspended JVM or call cleanup_debug_state after inspection."
)

JAVA_RUNTIME_INPUT_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
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
            "enum": ["127.0.0.1", "localhost"],
            "default": "127.0.0.1",
            "description": "JDWP host; v0.1 accepts localhost only.",
        },
        "ready_port": {
            "type": "integer",
            "minimum": 1,
            "maximum": 65535,
            "description": (
                "Optional local application TCP port for run/restart readiness; "
                "must differ from jdwp_port."
            ),
        },
        "startup_wait_timeout_seconds": {
            "type": "number",
            "minimum": 0,
            "maximum": 60,
            "default": 30,
            "description": (
                "Maximum synchronous readiness wait for run/restart. Timeout "
                "leaves the process running with startup_state=starting."
            ),
        },
        "tail": {
            "type": "integer",
            "minimum": 1,
            "maximum": 500,
            "default": 50,
            "description": (
                "Lines from a bounded snapshot tail of the Runtime-captured "
                "launch log. The result reports truncation and scan metadata; "
                "attach output is unavailable."
            ),
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
            "description": (
                "Optional fallback selector for stack/variables. Omit it to use "
                "the active suspension's event-hit thread. Exact names are "
                "preferred; otherwise a unique prefix or substring must identify "
                "one JVM thread. The selected thread must be suspended for "
                "stack/variables to succeed."
            ),
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
        "wait_mode": {
            "type": "string",
            "enum": ["blocking", "arm", "await"],
            "default": "blocking",
            "description": (
                "blocking waits directly; with http_trigger it performs "
                "arm, trigger, and await in one call. Use arm then await with "
                "its wait_handle when an external action is needed after JDWP "
                "is armed. Resume every suspension."
            ),
        },
        "http_trigger": {
            "type": "object",
            "additionalProperties": False,
            "description": (
                "Optional loopback request started only after JDWP is armed. "
                "Use with blocking for one-call arm/trigger/await, or with arm "
                "when work must occur before a later await. "
                "It is rejected while configured application readiness is "
                "starting; unverified readiness is allowed with a warning. "
                "Never send the same request again."
            ),
            "properties": {
                "method": {
                    "type": "string",
                    "enum": ["GET", "POST", "PUT", "PATCH", "DELETE"],
                },
                "url": {
                    "type": "string",
                    "maxLength": 2048,
                    "description": "http://127.0.0.1 URL only.",
                },
                "headers": {
                    "type": "object",
                    "maxProperties": 32,
                    "additionalProperties": {"type": "string"},
                },
                "json_body": {},
                "timeout_seconds": {
                    "type": "number",
                    "minimum": 0.1,
                    "maximum": 120,
                    "default": 30,
                },
            },
            "required": ["method", "url"],
        },
        "wait_handle": {
            "type": "string",
            "description": (
                "Active observation handle returned by arm or a nonterminal "
                "blocking result."
            ),
        },
        "suspension_id": {
            "type": "string",
            "description": (
                "Active suspension id returned by wait_event/status. Pass it to "
                "stack, variables, and resume so stale observations are rejected; "
                "stack/variables use its event-hit thread when thread_name is omitted."
            ),
        },
    },
    "required": ["action"],
}

JAVA_PROCESSES_DESCRIPTION = (
    "List local Java processes so a JVM can be selected for java_runtime attach."
)

JAVA_PROCESSES_INPUT_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
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
