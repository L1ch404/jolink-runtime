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
    "update",
)

JAVA_RUNTIME_DESCRIPTION = (
    "Run, observe, and debug a local Java application. "
    "Launch directly or from a supported IntelliJ IDEA Maven/Gradle project, "
    "restart the "
    "target, inspect status and logs, and verify code "
    "changes against actual runtime behavior before making further assumptions. "
    "When repeated edits fail or a fix needs verification, obtain runtime "
    "evidence before applying another patch. "
    "For deeper investigation, attach to an existing JVM, set "
    "breakpoints or exception watches, inspect stack frames and variables, "
    "and resume suspended threads. "
    "For an HTTP application launched by run/restart, provide ready_port; "
    "if startup_state is starting, call status until TCP readiness is observed "
    "before arming an HTTP trigger. "
    "After editing existing Java method bodies in a project_path launch, "
    "update accepts a background reload Attempt and returns reload_started with "
    "a reload_id; call status until last_reload is terminal, then verify with a "
    "fresh request. "
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
            "description": (
                "Classpath for direct launch/restart with main_class; omit with "
                "project_path."
            ),
        },
        "main_class": {
            "type": "string",
            "description": (
                "Fully qualified main class for direct classpath launch; "
                "omit with project_path."
            ),
        },
        "jar_path": {
            "type": "string",
            "description": (
                "Executable JAR for direct launch/restart; omit with project_path."
            ),
        },
        "project_path": {
            "type": "string",
            "description": (
                "Local Maven or supported Gradle Wrapper project root for "
                "launch or Fast Test. Maven launch imports an IntelliJ IDEA "
                "Application or Spring Boot configuration; Gradle G4 currently "
                "accepts the verified built-in Java/Application plugin world. "
                "Maven/Gradle only export the Build World; JDT compiles before "
                "the JVM starts. A matching persisted Build World skips the "
                "build tool and uses workspace_source_changes for incremental "
                "startup. Do not combine with "
                "classpath, main_class, jar_path, app_args, or vm_args. "
                "Fast Test can use a supported single-Project Gradle Java build, "
                "a headless Maven jar project, or one "
                "selector-identified jar module in a standard Reactor and "
                "does not require a running application. Restart never accepts "
                "project_path; it reuses the current sealed Generation."
            ),
        },
        "launch_name": {
            "type": "string",
            "description": (
                "Exact case-sensitive IntelliJ IDEA launch configuration "
                "name. Required only when multiple supported launches match; "
                "requires project_path."
            ),
        },
        "app_args": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Application arguments for launch/restart.",
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
            "description": "Additional JVM arguments for launch/restart.",
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
                "Local application TCP port for launch/restart readiness; "
                "must differ from jdwp_port."
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
        "source_files": {
            "type": "array",
            "minItems": 1,
            "maxItems": 16,
            "uniqueItems": True,
            "items": {
                "type": "string",
                "minLength": 1,
            },
            "description": (
                "Explicit Java source paths changed for reload or Fast Test. "
                "For Fast Test this may include added, edited, or deleted main/test "
                "sources and may be "
                "omitted on the initial unchanged baseline; paths are relative "
                "to project_path, including a Reactor module prefix. For reload, paths are "
                "in the selected "
                "build module. joLink compiles them in a persistent private JDT "
                "session and applies compatible loaded class definitions with "
                "HotSwap."
            ),
        },
        "hotswap": {
            "type": "boolean",
            "default": True,
            "description": (
                "Reload applies compatible classes only when true. False is "
                "retained for request compatibility and returns "
                "RELOAD_REQUIRES_RELAUNCH; reload never restarts the JVM."
            ),
        },
        "tests": {
            "type": "array",
            "minItems": 1,
            "maxItems": 64,
            "uniqueItems": True,
            "items": {"type": "string", "minLength": 1},
            "description": (
                "Explicit Fast Test selectors as fully qualified Class or "
                "Class#method. Fast Test v1 supports JUnit 4/5 and TestNG."
            ),
        },
        "build_system": {
            "type": "string",
            "enum": ["maven", "gradle"],
            "description": (
                "Optional authoritative build system for project launch or Fast Test. Set this "
                "when a project contains both Maven and Gradle builds; omit it "
                "when exactly one supported build is present."
            ),
        },
        "test_run_id": {
            "type": "string",
            "description": "Active Fast Test id required by cancel_test.",
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

PUBLIC_APPLICATION_ACTIONS = (
    "launch",
    "attach",
    "restart",
    "stop",
    "detach",
)
PUBLIC_FAST_TEST_ACTIONS = ("run", "cancel")
PUBLIC_STATUS_ACTIONS = ("processes", "status", "logs")
PUBLIC_DEBUGGER_ACTIONS = (
    "breakpoint",
    "exception",
    "wait_event",
    "threads",
    "stack",
    "variables",
    "resume",
    "cleanup_debug_state",
)


def _schema_for_actions(
    actions: tuple[str, ...],
    properties: tuple[str, ...],
) -> dict:
    selected = {
        name: deepcopy(JAVA_RUNTIME_INPUT_SCHEMA["properties"][name])
        for name in ("action", *properties)
    }
    selected["action"]["enum"] = list(actions)
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": selected,
        "required": ["action"],
    }


JAVA_APPLICATION_INPUT_SCHEMA = _schema_for_actions(
    PUBLIC_APPLICATION_ACTIONS,
    (
        "classpath",
        "main_class",
        "jar_path",
        "project_path",
        "launch_name",
        "app_args",
        "jdwp_port",
        "vm_args",
        "pid",
        "host",
        "ready_port",
        "source_files",
        "hotswap",
        "build_system",
        "timeout",
    ),
)
JAVA_APPLICATION_INPUT_SCHEMA["properties"]["project_path"]["description"] = (
    "Maven or Gradle project directory for an IDEA-derived application launch. "
    "Use instead of direct jar_path, classpath or main_class launch arguments."
)
JAVA_APPLICATION_INPUT_SCHEMA["properties"]["source_files"]["description"] = (
    "Optional edited-source hints for restart. Normally omit: restart detects all changed "
    "Java sources in the project and required upstream modules automatically."
)
JAVA_APPLICATION_INPUT_SCHEMA["properties"]["hotswap"]["description"] = (
    "For restart, default true: incrementally compile edits and prefer HotSwap; "
    "if changes cannot be hot-swapped, restart the JVM with those compiled outputs. "
    "False forces a JVM restart after compilation. Use false to reinitialize application "
    "state or reload framework/startup configuration. No pending code changes also "
    "restart the JVM without recompiling."
)
JAVA_APPLICATION_INPUT_SCHEMA["properties"]["build_system"]["description"] = (
    "Optional authoritative build system for project launch; specify maven or gradle when both exist."
)
JAVA_APPLICATION_INPUT_SCHEMA["properties"]["timeout"] = {
    "type": "number",
    "minimum": 0,
    "default": 30,
    "description": (
        "Seconds to wait for the launch/restart result in this call. "
        "Defaults to 30; values above 30 wait only 30 seconds without error. "
        "Zero returns immediately after submission. On expiry the same task "
        "continues in the background."
    ),
}
JAVA_FAST_TEST_INPUT_SCHEMA = _schema_for_actions(
    PUBLIC_FAST_TEST_ACTIONS,
    ("project_path", "tests", "source_files", "build_system", "timeout", "test_run_id"),
)
JAVA_FAST_TEST_INPUT_SCHEMA["required"] = []
JAVA_FAST_TEST_INPUT_SCHEMA["properties"]["action"]["default"] = "run"
JAVA_FAST_TEST_INPUT_SCHEMA["properties"]["action"]["description"] = (
    "Omit or use run to execute tests; use cancel with a returned test_run_id to stop that test run."
)
JAVA_FAST_TEST_INPUT_SCHEMA["properties"]["project_path"]["description"] = (
    "Maven project or Gradle Wrapper project directory containing the selected tests. "
    "No IDEA launch configuration or running application is required."
)
JAVA_FAST_TEST_INPUT_SCHEMA["properties"]["source_files"]["description"] = (
    "Optional edited source paths. Normally omit: the persistent workspace "
    "automatically detects changed Java sources."
)
JAVA_FAST_TEST_INPUT_SCHEMA["properties"]["build_system"]["description"] = (
    "Optional authoritative build system for tests; specify maven or gradle when both exist."
)
JAVA_FAST_TEST_INPUT_SCHEMA["properties"]["timeout"] = deepcopy(
    JAVA_APPLICATION_INPUT_SCHEMA["properties"]["timeout"]
)
JAVA_FAST_TEST_INPUT_SCHEMA["properties"]["timeout"]["description"] = (
    "Seconds to wait for this test result: default 30, values above 30 wait only 30, "
    "zero submits immediately. Expiry leaves the same task running. "
    "This does not change the test Runner's separate execution time limit."
)
JAVA_FAST_TEST_INPUT_SCHEMA["properties"]["test_run_id"]["description"] = (
    "Test run ID returned by java_fast_test; required for action='cancel'."
)
JAVA_FAST_TEST_INPUT_SCHEMA["allOf"] = [{
    "if": {"properties": {"action": {"const": "cancel"}}, "required": ["action"]},
    "then": {"required": ["test_run_id"]},
    "else": {"required": ["project_path", "tests"]},
}]
JAVA_STATUS_INPUT_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "action": {
            "type": "string",
            "enum": list(PUBLIC_STATUS_ACTIONS),
            "description": "Status operation to perform.",
        },
        "filter": deepcopy(JAVA_PROCESSES_INPUT_SCHEMA["properties"]["filter"]),
        "full": deepcopy(JAVA_PROCESSES_INPUT_SCHEMA["properties"]["full"]),
        "tail": deepcopy(JAVA_RUNTIME_INPUT_SCHEMA["properties"]["tail"]),
    },
    "required": ["action"],
}
JAVA_DEBUGGER_INPUT_SCHEMA = _schema_for_actions(
    PUBLIC_DEBUGGER_ACTIONS,
    tuple(
        name
        for name in JAVA_RUNTIME_INPUT_SCHEMA["properties"]
        if name
        not in {
            "action",
            "classpath",
            "main_class",
            "jar_path",
            "project_path",
            "launch_name",
            "app_args",
            "vm_args",
            "pid",
            "ready_port",
            "tail",
            "source_files",
            "hotswap",
            "build_system",
            "tests",
            "test_run_id",
        }
    ),
)

JAVA_APPLICATION_DESCRIPTION = (
    "Launch, attach, restart, stop, or detach Java applications. "
    "Use java_fast_test to run tests without launching an application. "
    "Launch and restart wait up to timeout (at most 30 seconds), returning the "
    "result if finished or the original background task if still running. "
    "After editing a managed Maven/Gradle project, use restart: it detects changed "
    "sources, incrementally compiles in the persistent JDT workspace, and prefers "
    "HotSwap (hotswap=true by default). Incompatible changes use the same compiled "
    "outputs to restart the JVM. Set hotswap=false for a real process restart and "
    "application reinitialization. apply_method reports hotswap or restart; HotSwap "
    "does not refresh framework state. A pending restart returns reload_id; observe "
    "active_operation and last_reload using java_status. Direct JAR/classpath "
    "launches restart their existing artifact without source compilation."
)
JAVA_FAST_TEST_DESCRIPTION = (
    "Run selected Java tests in Maven or Gradle projects using persistent "
    "incremental compilation. Supports JUnit 4/5 and TestNG. No application "
    "launch is required, and an existing application is left running. "
    "Provide project_path and tests (Class or Class#method); action defaults to run. "
    "The first build-model preparation and JDT compilation can take minutes; "
    "subsequent calls reuse them and compile changed sources. "
    "Waits up to timeout (maximum 30 seconds); unfinished work returns its "
    "test_run_id and continues in the background. Observe it with java_status "
    "or cancel it here using action='cancel' and the same test_run_id. "
    "This is not the complete Maven/Gradle verification or packaging lifecycle."
)
JAVA_STATUS_DESCRIPTION = (
    "Discover local Java processes, inspect joLink application/build state, or "
    "read a bounded captured-log tail."
)
JAVA_DEBUGGER_DESCRIPTION = (
    "Observe executed paths and runtime state with JDWP breakpoints, exception "
    "events, stacks, and variables. Always resume or clean up every suspension."
)


def get_mcp_tools() -> list[types.Tool]:
    """Build fresh MCP tool models from the compact v0.1 schemas."""
    return [
        types.Tool(
            name="java_application",
            description=JAVA_APPLICATION_DESCRIPTION,
            inputSchema=deepcopy(JAVA_APPLICATION_INPUT_SCHEMA),
        ),
        types.Tool(
            name="java_fast_test",
            description=JAVA_FAST_TEST_DESCRIPTION,
            inputSchema=deepcopy(JAVA_FAST_TEST_INPUT_SCHEMA),
        ),
        types.Tool(
            name="java_status",
            description=JAVA_STATUS_DESCRIPTION,
            inputSchema=deepcopy(JAVA_STATUS_INPUT_SCHEMA),
        ),
        types.Tool(
            name="java_debugger",
            description=JAVA_DEBUGGER_DESCRIPTION,
            inputSchema=deepcopy(JAVA_DEBUGGER_INPUT_SCHEMA),
        ),
    ]


__all__ = [
    "JAVA_APPLICATION_DESCRIPTION",
    "JAVA_APPLICATION_INPUT_SCHEMA",
    "JAVA_DEBUGGER_DESCRIPTION",
    "JAVA_DEBUGGER_INPUT_SCHEMA",
    "JAVA_FAST_TEST_DESCRIPTION",
    "JAVA_FAST_TEST_INPUT_SCHEMA",
    "JAVA_PROCESSES_DESCRIPTION",
    "JAVA_PROCESSES_INPUT_SCHEMA",
    "JAVA_RUNTIME_DESCRIPTION",
    "JAVA_RUNTIME_INPUT_SCHEMA",
    "JAVA_STATUS_DESCRIPTION",
    "JAVA_STATUS_INPUT_SCHEMA",
    "PUBLIC_APPLICATION_ACTIONS",
    "PUBLIC_FAST_TEST_ACTIONS",
    "PUBLIC_DEBUGGER_ACTIONS",
    "PUBLIC_RUNTIME_ACTIONS",
    "PUBLIC_STATUS_ACTIONS",
    "get_mcp_tools",
]
