"""Differentially evaluate seeded values in real Java stack-frame contexts.

The same source contains a javac/JVM oracle and HTTP breakpoint fixtures. Each
case records its exact expression, frame, request and MCP result before the
final assertion, so one failure does not prevent the remaining observations.
"""

from __future__ import annotations

import json
import os
import random
import subprocess
from pathlib import Path
from typing import Any

import anyio
import pytest

from java_support import (
    assert_ok, call_payload, open_mcp_session, require_real_mcp_java_e2e,
    reserve_local_port, source_line,
)
from test_debugger_evaluate import _class_snapshot, _http_get


pytestmark = pytest.mark.mcp_java_e2e
SEED = 20261025
VALUES = random.Random(SEED).sample(range(-24, 25), 3)
EXPRESSIONS = {
    "static": ["count", "count * 7 + label.length()", "staticBias + count",
               "ContextFixture.staticBias + count", "items.get(1).intValue()",
               "String.valueOf(count).length()", "label.substring(1)",
               "((java.util.function.IntSupplier) () -> count + 1).getAsInt()"],
    "constructor": ["count", "this.own + count", "super.base + count",
                    "this.base", "inherited(count)", "this.getClass().getSimpleName()",
                    "label.length()", "((java.util.function.IntSupplier) () -> count).getAsInt()"],
    "constructor_first": ["count", "this.own", "this.base", "inherited(count)",
                          "this.getClass().getSimpleName()"],
    "constructor_parent": ["count", "this.base", "parentName", "inherited(count)",
                           "this.getClass().getSimpleName()"],
    "constructor_chained": ["count", "this.own + count", "this.base", "inherited(count)",
                            "this.getClass().getSimpleName()", "label.length()"],
    "constructor_delegating": ["count", "this.own + count", "this.base", "inherited(count)",
                               "this.getClass().getSimpleName()", "label.length()"],
    "inherited": ["count", "base", "this.base", "super.base", "inherited(count)",
                  "super.inherited(count)", "parentName + label", "((Parent)this).base",
                  "((ContextFixture.Parent)this).base"],
    "generic_class": ["count", "value.intValue() + count", "this.item.doubleValue()",
                      "values.get(0).longValue()", "value.getClass().getSimpleName()",
                      "String.valueOf(value)", "values.stream().mapToInt(Number::intValue).sum()",
                      "((java.util.function.IntSupplier) () -> value.intValue()).getAsInt()",
                      "nested.get(0).get(1).intValue()",
                      "values.set(0, value).intValue()",
                      "java.util.Collections.<T>singletonList(value).get(0).intValue()"],
    "generic_method": ["count", "text.length() + count", "text.charAt(1)",
                       "text.subSequence(1, 3).toString()", "texts.get(0).length()",
                       "text.getClass().getSimpleName()", "String.valueOf(text)",
                       "((java.util.function.IntSupplier) () -> text.length()).getAsInt()",
                       "text.compareTo(text)", "texts.get(0).compareTo(text)",
                       "java.util.Collections.<U>singletonList(text).get(0).length()"],
    "generic_shadow": ["count", "text.length() + count", "nested.get(0).get(0).length()",
                       "this.item.doubleValue()", "String.valueOf(text)",
                       "((java.util.function.IntSupplier) () -> text.length()).getAsInt()",
                       "java.util.Collections.<T>singletonList(text).get(0).length()"],
    "inner": ["count", "this.own + count", "outerBias + count",
              "ContextFixture.this.outerBias + count", "outerLabel + label",
              "ContextFixture.this.outerMethod(count)", "this.getClass().getSimpleName()",
              "((java.util.function.IntSupplier) () -> count + this.own).getAsInt()"],
    "parent_frame": ["count", "label", "count + parentBias", "this.outerBias + count",
                     "parentNames.get(0)", "parentNames.size()", "label.length()",
                     "((java.util.function.IntSupplier) () -> count).getAsInt()"],
}
BREAKPOINT_CLASSES = {
    "static": "ContextFixture", "constructor": "ContextFixture$Constructed",
    "constructor_first": "ContextFixture$FirstStatement",
    "constructor_parent": "ContextFixture$Parent",
    "constructor_chained": "ContextFixture$ConstructorChain",
    "constructor_delegating": "ContextFixture$ConstructorChain",
    "inherited": "ContextFixture$Child", "generic_class": "ContextFixture$GenericBox",
    "generic_method": "ContextFixture$GenericBox", "generic_shadow": "ContextFixture$GenericBox",
    "inner": "ContextFixture$Inner",
    "parent_frame": "ContextFixture",
}


SOURCE = """\
import com.sun.net.httpserver.HttpServer;
import java.net.InetSocketAddress;
import java.util.Arrays;
import java.util.List;
import java.util.concurrent.Executors;

public class ContextFixture {
    private static int staticBias = 17;
    private int outerBias = 29;
    private String outerLabel = "outer";
    public String outerMethod(int count) { return "outer:" + count; }
    static void emit(String context, int count, int index, Object value) {
        System.out.println(context + ":" + count + ":" + index + "\\t" + String.valueOf(value));
    }
    static String staticObserve(int input, boolean oracle) {
        int count = input;
        String label = "static" + input;
        List<Integer> items = Arrays.asList(count - 1, count + 2);
        if (oracle) { __static__ }
        int marker = count; // static breakpoint
        return "static:" + marker;
    }
    public static class Parent {
        protected int base = 13;
        protected String parentName = "parent:";
        Parent() {}
        Parent(int count, boolean oracle, String context) {
            if (oracle && context.equals("constructor_parent")) { __constructor_parent__ }
            int marker = count; // constructor_parent breakpoint
        }
        public String inherited(int count) { return "inherited:" + count; }
    }
    public static class Constructed extends Parent {
        public int own;
        public String result;
        Constructed(int input, boolean oracle) {
            super();
            this.own = 7;
            int count = input;
            String label = "constructor" + count;
            if (oracle) { __constructor__ }
            int marker = count; // constructor breakpoint
            result = "constructor:" + marker;
        }
    }
    public static class FirstStatement extends Parent {
        public int own;
        public String result;
        FirstStatement(int count, boolean oracle) {
            super();
            if (oracle) { __constructor_first__ }
            this.own = 7; // constructor_first breakpoint
            result = "constructor_first:" + count;
        }
    }
    public static class ConstructorChain extends Parent {
        public int own;
        public String result;
        ConstructorChain(int input, boolean oracle, String context) {
            this(input, oracle, context, 7);
            int count = input;
            String label = "delegating" + count;
            if (oracle && context.equals("constructor_delegating")) { __constructor_delegating__ }
            int marker = count; // constructor_delegating breakpoint
            result = context + ":" + marker;
        }
        ConstructorChain(int input, boolean oracle, String context, int initial) {
            super(input, oracle, context);
            this.own = initial;
            int count = input;
            String label = "chained" + count;
            if (oracle && context.equals("constructor_chained")) { __constructor_chained__ }
            int marker = count; // constructor_chained breakpoint
        }
    }
    public static class Child extends Parent {
        public int base = 31;
        String observe(int input, boolean oracle) {
            int count = input;
            String label = "child" + count;
            if (oracle) { __inherited__ }
            int marker = count; // inherited breakpoint
            return "inherited:" + marker;
        }
    }
    public static class GenericBox<T extends Number> {
        public T item;
        GenericBox(T value) { item = value; }
        String observe(int input, boolean oracle) {
            int count = input;
            T value = item;
            List<T> values = Arrays.asList(value, value);
            List<List<T>> nested = Arrays.asList(values);
            if (oracle) { __generic_class__ }
            int marker = count; // generic_class breakpoint
            return "generic_class:" + marker;
        }
        <U extends CharSequence & Comparable<U>> String methodObserve(int input, U text, boolean oracle) {
            int count = input;
            List<U> texts = Arrays.asList(text);
            if (oracle) { __generic_method__ }
            int marker = count; // generic_method breakpoint
            return "generic_method:" + marker;
        }
        <T extends CharSequence> String shadowObserve(int input, T text, boolean oracle) {
            int count = input;
            List<T> texts = Arrays.asList(text);
            List<List<T>> nested = Arrays.asList(texts);
            if (oracle) { __generic_shadow__ }
            int marker = count; // generic_shadow breakpoint
            return "generic_shadow:" + marker;
        }
    }
    public class Inner {
        public int own = 37;
        String observe(int input, boolean oracle) {
            int count = input;
            String label = "inner" + count;
            if (oracle) { __inner__ }
            int marker = count; // inner breakpoint
            return "inner:" + marker;
        }
    }
    String parentObserve(int input, boolean oracle) {
        int count = input;
        int parentBias = 41;
        String label = "parent" + count;
        List<String> parentNames = Arrays.asList("first" + count, "second");
        if (oracle) { __parent_frame__ }
        leaf(count);
        return "parent_frame:" + count;
    }
    void leaf(int childValue) {
        int leafOnly = childValue + 99; // parent_frame breakpoint
        if (leafOnly == Integer.MIN_VALUE) throw new AssertionError("unreachable");
    }
    String dispatch(String context, int count, boolean oracle) {
        if (context.equals("static")) return staticObserve(count, oracle);
        if (context.equals("constructor")) return new Constructed(count, oracle).result;
        if (context.equals("constructor_first")) return new FirstStatement(count, oracle).result;
        if (context.equals("constructor_parent") || context.equals("constructor_chained") || context.equals("constructor_delegating"))
            return new ConstructorChain(count, oracle, context).result;
        if (context.equals("inherited")) return new Child().observe(count, oracle);
        if (context.equals("generic_class")) return new GenericBox<Integer>(count + 3).observe(count, oracle);
        if (context.equals("generic_method")) return new GenericBox<Integer>(count).methodObserve(count, "text" + count, oracle);
        if (context.equals("generic_shadow")) return new GenericBox<Integer>(count).shadowObserve(count, "text" + count, oracle);
        if (context.equals("inner")) return new Inner().observe(count, oracle);
        if (context.equals("parent_frame")) return parentObserve(count, oracle);
        throw new IllegalArgumentException(context);
    }
    public static void main(String[] args) throws Exception {
        final ContextFixture fixture = new ContextFixture();
        for (String nested : new String[] {"Parent", "Constructed", "FirstStatement", "ConstructorChain", "Child", "GenericBox", "Inner"})
            Class.forName("ContextFixture$" + nested);
        if (args[0].equals("native")) {
            for (String context : new String[] {__CONTEXTS__})
                for (int count : new int[] {__VALUES__}) fixture.dispatch(context, count, true);
            return;
        }
        HttpServer server = HttpServer.create(new InetSocketAddress("127.0.0.1", Integer.parseInt(args[0])), 0);
        server.createContext("/", exchange -> {
            String[] parts = exchange.getRequestURI().getPath().substring(1).split("/");
            byte[] body = fixture.dispatch(parts[0], Integer.parseInt(parts[1]), false).getBytes("UTF-8");
            exchange.sendResponseHeaders(200, body.length);
            exchange.getResponseBody().write(body);
            exchange.close();
        });
        server.setExecutor(Executors.newCachedThreadPool());
        server.start();
    }
}
"""


def _source() -> str:
    source = SOURCE.replace("__CONTEXTS__", ",".join(json.dumps(key) for key in EXPRESSIONS))
    source = source.replace("__VALUES__", ",".join(str(value) for value in VALUES))
    for context, expressions in EXPRESSIONS.items():
        oracle = "\n".join(
            f'emit("{context}", count, {index}, {expression});'
            for index, expression in enumerate(expressions)
        )
        source = source.replace(f"__{context}__", oracle)
    return source


def _java_string(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return str(value).lower()
    return str(value)


def run_frame_contexts(
    tmp_path: Path, java_major: int, expressions_by_context: dict[str, list[str]],
    *, check_generic_writes: bool = True,
) -> None:
    require_real_mcp_java_e2e()
    home = os.environ.get(f"JOLINK_TEST_JAVA{java_major}_HOME")
    if not home:
        pytest.skip(f"set JOLINK_TEST_JAVA{java_major}_HOME")
    java_home = Path(home)
    suffix = ".exe" if os.name == "nt" else ""
    source_text = _source()
    source = tmp_path / "ContextFixture.java"
    source.write_text(source_text, encoding="utf-8")
    subprocess.run(
        [str(java_home / f"bin/javac{suffix}"), "-g", "-encoding", "UTF-8", str(source)],
        capture_output=True, text=True, check=True, timeout=30,
    )
    native = subprocess.run(
        [str(java_home / f"bin/java{suffix}"), "-cp", str(tmp_path), "ContextFixture", "native"],
        capture_output=True, text=True, check=True, timeout=30,
    )
    expected = dict(line.split("\t", 1) for line in native.stdout.splitlines())
    assert len(expected) == sum(map(len, EXPRESSIONS.values())) * len(VALUES)
    # Verify the rejection oracle too. First-bound substitution must not turn
    # T into arbitrary Number for writes, even though both erase to Number.
    invalid_generic_expressions = ("values.add(1.5)", "value = 1.5") if check_generic_writes else ()
    native_rejections: dict[str, Any] = {}
    for index, expression in enumerate(invalid_generic_expressions):
        rejected_dir = tmp_path / f"native-rejected-{index}"
        rejected_dir.mkdir()
        rejected_source = rejected_dir / "ContextFixture.java"
        rejected_source.write_text(source_text.replace(
            "int marker = count; // generic_class breakpoint",
            expression + ";\nint marker = count; // generic_class breakpoint",
        ), encoding="utf-8")
        rejected = subprocess.run(
            [str(java_home / f"bin/javac{suffix}"), "-g", "-encoding", "UTF-8", str(rejected_source)],
            capture_output=True, text=True, check=False, timeout=30,
        )
        assert rejected.returncode != 0 and expression in rejected.stderr, rejected.stderr
        native_rejections[expression] = {"exit_code": rejected.returncode, "stderr": rejected.stderr}
    before = _class_snapshot(str(tmp_path))
    environment = {**os.environ, "JAVA_HOME": str(java_home),
                   "PATH": str(java_home / "bin") + os.pathsep + os.environ.get("PATH", "")}
    report: dict[str, Any] = {
        "seed": SEED, "java_major": java_major, "values": VALUES,
        "native_rejections": native_rejections, "observations": [],
    }
    evidence = tmp_path / "context-evidence.json"
    failures: list[dict[str, Any]] = []
    port = reserve_local_port()

    def record(observation: dict[str, Any], matched: bool) -> None:
        observation["matched"] = matched
        report["observations"].append(observation)
        if not matched:
            failures.append(observation)
        evidence.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    async def scenario() -> None:
        with (tmp_path / "mcp-stderr.log").open("w+", encoding="utf-8") as stderr:
            async with open_mcp_session(stderr, environment=environment) as session:
                try:
                    started = assert_ok(await call_payload(session, {
                        "action": "run", "classpath": str(tmp_path), "main_class": "ContextFixture",
                        "java_home": str(java_home), "app_args": [str(port)], "ready_port": port,
                        "jdwp_port": reserve_local_port(),
                    }))
                    assert started["startup_state"] == "ready", started
                    for context, expressions in expressions_by_context.items():
                        assert_ok(await call_payload(session, {"action": "cleanup_debug_state"}))
                        assert_ok(await call_payload(session, {
                            "action": "breakpoint", "bp_action": "set", "class_pattern": BREAKPOINT_CLASSES[context],
                            "line": source_line(source_text, f"// {context} breakpoint"),
                        }))
                        for count in VALUES:
                            response: dict[str, str] = {}
                            armed = assert_ok(await call_payload(session, {
                                "action": "wait_event", "wait_mode": "arm", "timeout": 20,
                            }))

                            async def request() -> None:
                                response["body"] = await anyio.to_thread.run_sync(_http_get, port, f"/{context}/{count}")

                            async with anyio.create_task_group() as group:
                                group.start_soon(request)
                                hit = assert_ok(await call_payload(session, {
                                    "action": "wait_event", "wait_mode": "await", "wait_handle": armed["wait_handle"],
                                    "timeout": 20,
                                }))
                                assert hit["status"] == "breakpoint_hit", hit
                                suspension = hit["suspension_id"]
                                frame = 1 if context == "parent_frame" else 0

                                async def evaluate(expression: str) -> tuple[dict[str, Any], dict[str, Any]]:
                                    arguments = {"action": "evaluate", "expression": expression,
                                                 "suspension_id": suspension, "frame_index": frame}
                                    return arguments, await call_payload(session, arguments)

                                try:
                                    for expression in expressions:
                                        index = EXPRESSIONS[context].index(expression)
                                        arguments, actual = await evaluate(expression)
                                        wanted = expected[f"{context}:{count}:{index}"]
                                        matched = actual.get("ok") is True and _java_string(actual.get("result", {}).get("value")) == wanted
                                        record({"context": context, "count": count, "request": arguments,
                                                "expected_native": wanted, "actual": actual}, matched)
                                    if context == "generic_class":
                                        for expression in invalid_generic_expressions:
                                            arguments, actual = await evaluate(expression)
                                            record({"context": context, "count": count, "phase": "invalid_generic_write",
                                                    "request": arguments, "native_javac_rejected": True,
                                                    "expected_error": "EVALUATION_COMPILE_FAILED", "actual": actual},
                                                   actual.get("ok") is False and actual.get("error_code") == "EVALUATION_COMPILE_FAILED")
                                            arguments, actual = await evaluate("value.getClass().getName()")
                                            record({"context": context, "count": count, "phase": "rejected_write_preserves_local",
                                                    "request": arguments, "expected_value": "java.lang.Integer", "actual": actual},
                                                   actual.get("ok") is True and actual.get("result", {}).get("value") == "java.lang.Integer")
                                        if invalid_generic_expressions:
                                            arguments, actual = await evaluate("(value = values.get(0)).intValue()")
                                            record({"context": context, "count": count, "phase": "restore_generic_local",
                                                    "request": arguments, "expected_value": count + 3, "actual": actual},
                                                   actual.get("ok") is True and actual.get("result", {}).get("value") == count + 3)
                                    for expression, expected_error, wanted in (
                                        ("count +", "EVALUATION_COMPILE_FAILED", None),
                                        ("((String)null)", None, None),
                                        ("count", None, count),
                                    ):
                                        arguments, actual = await evaluate(expression)
                                        matched = (actual.get("ok") is False and actual.get("error_code") == expected_error
                                                   if expected_error else actual.get("ok") is True and actual.get("result", {}).get("value") == wanted)
                                        record({"context": context, "count": count, "phase": "recovery",
                                                "request": arguments, "expected_error": expected_error,
                                                "expected_value": wanted, "actual": actual}, matched)
                                finally:
                                    assert_ok(await call_payload(session, {"action": "resume", "suspension_id": suspension}))
                            assert response["body"] == f"{context}:{count}", response
                    assert _class_snapshot(str(tmp_path)) == before
                    assert source.read_text(encoding="utf-8") == source_text
                finally:
                    await call_payload(session, {"action": "cleanup_debug_state"})
                    await call_payload(session, {"action": "stop"})

    anyio.run(scenario)
    assert not failures, f"{len(failures)} context observations disagreed; exact requests/results: {evidence}\n" + json.dumps([
        {"context": item["context"], "expression": item["request"]["expression"],
         "count": item["count"], "error": item["actual"].get("error_code"),
         "value": item["actual"].get("result")}
        for item in failures
    ], ensure_ascii=False)


@pytest.mark.parametrize("java_major", [8, 17, 21])
def test_evaluate_seeded_frame_contexts(tmp_path: Path, java_major: int) -> None:
    # The full differential suite deliberately retains unresolved generic
    # scope, super, outer-instance and nested-type-name goals as failures,
    # not XFAILs. A compile error is not completed compatibility support.
    run_frame_contexts(tmp_path, java_major, EXPRESSIONS)


@pytest.mark.parametrize("java_major", [8, 17, 21])
def test_evaluate_reported_constructor_regressions(tmp_path: Path, java_major: int) -> None:
    expressions = {
        context: EXPRESSIONS[context] for context in (
            "static", "constructor_first", "constructor_parent", "constructor_chained", "constructor_delegating",
            "parent_frame",
        )
    }
    expressions["constructor"] = [
        expression for expression in EXPRESSIONS["constructor"] if expression != "super.base + count"
    ]
    # Generic scope work was set aside, rather than broadening T to its bound.
    # Every generic expression remains asserted in the full differential suite.
    run_frame_contexts(tmp_path, java_major, expressions, check_generic_writes=False)
