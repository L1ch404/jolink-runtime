"""Escaping evaluated closures must behave like native javac closures.

Each case compiles/runs an independent Java oracle, then repeats the same
operations through the public MCP debugger and executes the escaped objects
after resume via HTTP. No user project or pre-existing application is touched.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Any

import anyio
import pytest

from java_support import (
    assert_ok,
    call_payload,
    open_mcp_session,
    require_real_mcp_java_e2e,
    reserve_local_port,
    source_line,
)
from test_debugger_evaluate import _class_snapshot, _http_get


pytestmark = pytest.mark.mcp_java_e2e


class KnownJdtNamedLocalDefect(RuntimeError):
    """Only the independently reproduced JDT missing-result defect is expected."""


STORES = {
    "anonymous": """this.saved.add(new java.util.function.Supplier<String>() {
        public String get() { return names.get(0) + ":" + count; }
    });""",
    "lambda": "this.saved.add(() -> names.get(0) + \":\" + count);",
    "stream": """this.streams.add(java.util.stream.Stream.of("x")
        .map(item -> names.get(0) + ":" + count + ":" + item));""",
    "callback": """this.callbacks.add(() ->
        this.callbackResults.add(names.get(0) + ":" + count));""",
    "exception": """this.failAfter(this.saved.add(new java.util.function.Supplier<String>() {
        public String get() { return names.get(0) + ":" + count; }
    }));""",
    "named_local": """this.saved.add(((java.util.function.Supplier<java.util.function.Supplier<String>>) () -> {
        class Local implements java.util.function.Supplier<String> {
            public String get() { return names.get(0) + ":" + count; }
        }
        return new Local();
    }).get());""",
    "nested_lambda": """this.saved.add(((java.util.function.Supplier<java.util.function.Supplier<String>>) () ->
        () -> names.get(0) + ":" + count).get());""",
    "anonymous_lambda": """this.saved.add(new java.util.function.Supplier<java.util.function.Supplier<String>>() {
        public java.util.function.Supplier<String> get() {
            return () -> names.get(0) + ":" + count;
        }
    }.get());""",
}


OWN_THIS = """new java.util.function.Supplier<String>() {
    public String own = "anon";
    public String get() {
        return ((java.util.function.Supplier<String>) () -> this.own).get();
    }
}.get()"""


SOURCE = """\
import com.sun.net.httpserver.HttpServer;
import java.net.InetSocketAddress;
import java.util.ArrayList;
import java.util.Arrays;
import java.util.List;
import java.util.concurrent.Executors;
import java.util.function.Supplier;
import java.util.stream.Collectors;
import java.util.stream.Stream;

public class CaptureFixture {
    public List<Supplier<String>> saved = new ArrayList<Supplier<String>>();
    public List<Stream<String>> streams = new ArrayList<Stream<String>>();
    public List<Runnable> callbacks = new ArrayList<Runnable>();
    public List<String> callbackResults = new ArrayList<String>();
    public List<String> streamResults = new ArrayList<String>();
    public List<String> scratch = new ArrayList<String>(Arrays.asList("scratch"));
    public int requestCount;

    public void nativeStore(List<String> names, int count) {
        __STORE__
    }

    public boolean failAfter(boolean savedSuccessfully) {
        throw new IllegalStateException("after-save");
    }

    public String nativeOwnThis() {
        return __OWN_THIS__;
    }

    public String snapshot() {
        List<String> values = new ArrayList<String>();
        try {
            for (Supplier<String> supplier : saved) values.add(supplier.get());
            for (Stream<String> stream : streams) {
                streamResults.add(stream.collect(Collectors.joining(",")));
            }
            streams.clear();
            values.addAll(streamResults);
            for (Runnable callback : callbacks) callback.run();
            callbacks.clear();
            values.addAll(callbackResults);
            return String.join("|", values);
        } catch (Throwable error) {
            return "ERROR:" + error.getClass().getName() + ":" + error.getMessage();
        }
    }

    public String observe() {
        int round = requestCount++;
        int count = 4 + round * 10;
        List<String> names = new ArrayList<String>(Arrays.asList("alice" + round));
        int marker = count; // capture breakpoint
        return count + "|" + names.get(0);
    }

    public static void main(String[] args) throws Exception {
        Class.forName("java.lang.IllegalStateException");
        final CaptureFixture fixture = new CaptureFixture();
        if (args[0].equals("native")) {
            if (!"anon".equals(fixture.nativeOwnThis())) throw new AssertionError("anonymous this");
            fixture.scratch.clear();
            if (!fixture.scratch.isEmpty()) throw new AssertionError("void invocation");
            for (int round = 0; round < 2; round++) {
                List<String> names = new ArrayList<String>(Arrays.asList("alice" + round));
                int count = 4 + round * 10;
                try { fixture.nativeStore(names, count); }
                catch (IllegalStateException expected) {
                    if (!"after-save".equals(expected.getMessage())) throw expected;
                }
                names.set(0, "mutated" + round);
                names = new ArrayList<String>(Arrays.asList("second" + round));
                count = 9 + round * 10;
                try { fixture.nativeStore(names, count); }
                catch (IllegalStateException expected) {
                    if (!"after-save".equals(expected.getMessage())) throw expected;
                }
                System.out.println(fixture.snapshot());
            }
            return;
        }
        HttpServer server = HttpServer.create(
            new InetSocketAddress("127.0.0.1", Integer.parseInt(args[0])), 0);
        server.createContext("/", exchange -> {
            byte[] body = fixture.observe().getBytes("UTF-8");
            exchange.sendResponseHeaders(200, body.length);
            exchange.getResponseBody().write(body);
            exchange.close();
        });
        server.createContext("/snapshot", exchange -> {
            byte[] body = fixture.snapshot().getBytes("UTF-8");
            exchange.sendResponseHeaders(200, body.length);
            exchange.getResponseBody().write(body);
            exchange.close();
        });
        server.setExecutor(Executors.newCachedThreadPool());
        server.start();
    }
}
"""


@pytest.mark.parametrize("java_major", [8, 17, 21])
@pytest.mark.parametrize("kind", [
    pytest.param(kind, marks=pytest.mark.xfail(
        raises=KnownJdtNamedLocalDefect, strict=True,
        reason="JDT snippet emission drops an expression containing a named local class",
    )) if kind == "named_local" else kind
    for kind in STORES
])
def test_evaluated_capture_survives_reuse_and_resume(
    tmp_path: Path, java_major: int, kind: str,
) -> None:
    require_real_mcp_java_e2e()
    home = os.environ.get(f"JOLINK_TEST_JAVA{java_major}_HOME")
    if not home:
        pytest.skip(f"set JOLINK_TEST_JAVA{java_major}_HOME")
    java_home = Path(home)
    suffix = ".exe" if os.name == "nt" else ""
    expression = STORES[kind]
    saved_supplier = kind not in {"stream", "callback"}
    source_text = SOURCE.replace("__STORE__", expression).replace("__OWN_THIS__", OWN_THIS)
    source = tmp_path / "CaptureFixture.java"
    source.write_text(source_text, encoding="utf-8")
    subprocess.run(
        [str(java_home / f"bin/javac{suffix}"), "-g", "-encoding", "UTF-8", str(source)],
        cwd=tmp_path, capture_output=True, text=True, check=True, timeout=30,
    )
    native = subprocess.run(
        [str(java_home / f"bin/java{suffix}"), "-cp", str(tmp_path), "CaptureFixture", "native"],
        cwd=tmp_path, capture_output=True, text=True, check=True, timeout=30,
    ).stdout.splitlines()
    tail = ":x" if kind == "stream" else ""
    expected = [f"mutated0:4{tail}|second0:9{tail}"]
    expected.append(expected[0] + f"|mutated1:14{tail}|second1:19{tail}")
    assert native == expected, "The native javac/JVM control must establish the expected semantics"
    mutable_capture = "new Object[] { (java.util.function.IntSupplier) () -> count, count++ }"
    if kind == "lambda":
        invalid = tmp_path / "CaptureNotFinal.java"
        invalid.write_text(
            "public class CaptureNotFinal { public Object[] capture(int count) { return "
            + mutable_capture + "; } }", encoding="utf-8",
        )
        rejected = subprocess.run(
            [str(java_home / f"bin/javac{suffix}"), "-g", "-encoding", "UTF-8", str(invalid)],
            cwd=tmp_path, capture_output=True, text=True, timeout=30,
        )
        assert rejected.returncode != 0 and "count" in rejected.stderr, rejected.stderr
        assert not (tmp_path / "CaptureNotFinal.class").exists()
    before = _class_snapshot(str(tmp_path))
    environment = {
        **os.environ,
        "JAVA_HOME": str(java_home),
        "PATH": str(java_home / "bin") + os.pathsep + os.environ.get("PATH", ""),
    }
    port = reserve_local_port()

    async def scenario() -> None:
        with (tmp_path / "mcp-stderr.log").open("w+", encoding="utf-8") as stderr:
            async with open_mcp_session(stderr, environment=environment) as session:
                try:
                    started = assert_ok(await call_payload(session, {
                        "action": "run", "classpath": str(tmp_path),
                        "main_class": "CaptureFixture", "java_home": str(java_home),
                        "app_args": [str(port)], "ready_port": port,
                        "jdwp_port": reserve_local_port(),
                    }))
                    assert started["startup_state"] == "ready", started
                    assert_ok(await call_payload(session, {
                        "action": "breakpoint", "bp_action": "set",
                        "class_pattern": "CaptureFixture",
                        "line": source_line(source_text, "// capture breakpoint"),
                    }))
                    for round_ in range(2):
                        response: dict[str, str] = {}
                        armed = assert_ok(await call_payload(session, {
                            "action": "wait_event", "wait_mode": "arm", "timeout": 20,
                        }))

                        async def request() -> None:
                            response["body"] = await anyio.to_thread.run_sync(_http_get, port)

                        async with anyio.create_task_group() as task_group:
                            task_group.start_soon(request)
                            hit = assert_ok(await call_payload(session, {
                                "action": "wait_event", "wait_mode": "await",
                                "wait_handle": armed["wait_handle"], "timeout": 20,
                            }))
                            assert hit["status"] == "breakpoint_hit", hit
                            suspension = hit["suspension_id"]

                            async def evaluate(code: str) -> dict[str, Any]:
                                return await call_payload(session, {
                                    "action": "evaluate", "expression": code,
                                    "suspension_id": suspension, "frame_index": 0,
                                })

                            async def value(code: str) -> Any:
                                return assert_ok(await evaluate(code))["result"]["value"]

                            async def store(*, reused: bool) -> None:
                                result = await evaluate(expression)
                                if (kind == "named_local" and result.get("ok") is False
                                        and result.get("error_code") == "EVALUATION_NO_RESULT"):
                                    raise KnownJdtNamedLocalDefect(json.dumps(result))
                                if kind == "exception":
                                    assert result.get("ok") is False, result
                                    assert result["error_code"] == "EVALUATION_EXCEPTION", result
                                    assert "after-save" in json.dumps(result.get("exception")), result
                                else:
                                    assert_ok(result)
                                    assert result["compile_reused"] is reused, result
                                    assert result["result"] == {"type": "boolean", "value": True}, result

                            try:
                                if kind == "anonymous" and round_ == 0:
                                    # A lambda nested in an anonymous object's body
                                    # captures that object's this, not the target frame.
                                    assert await value(OWN_THIS) == "anon"
                                    assert await value("this.scratch.size()") == 1
                                    cleared = assert_ok(await evaluate("this.scratch.clear()"))
                                    assert cleared["result"] == {"type": "void", "value": None}, cleared
                                    assert await value("this.scratch.size()") == 0
                                    # The known compiler defect must be an explicit
                                    # error, not a fake void success or a poisoned frame.
                                    assert await value("this.saved.size()") == 0
                                    missing = await evaluate(STORES["named_local"])
                                    assert missing.get("ok") is False, missing
                                    assert missing["error_code"] == "EVALUATION_NO_RESULT", missing
                                    assert await value("this.saved.size()") == 0
                                    assert await value("count") == 4
                                if kind == "lambda" and round_ == 0:
                                    # Captured variables must be real Java locals,
                                    # not mutable fields disguised as locals.
                                    rejected = await evaluate(mutable_capture)
                                    assert rejected.get("ok") is False, rejected
                                    assert rejected["error_code"] == "EVALUATION_COMPILE_FAILED", rejected
                                    assert any("effectively final" in message for message in rejected["diagnostics"])
                                    assert await value("count") == 4
                                await store(reused=round_ > 0)
                                if saved_supplier:
                                    assert await value(f"this.saved.get({round_ * 2}).get()") == (
                                        f"alice{round_}:{4 + round_ * 10}"
                                    )
                                assert await value(f'names.set(0, "mutated{round_}")') == f"alice{round_}"
                                if saved_supplier:
                                    assert await value(f"this.saved.get({round_ * 2}).get()") == (
                                        f"mutated{round_}:{4 + round_ * 10}"
                                    )
                                assert_ok(await evaluate(
                                    "names = new java.util.ArrayList<String>("
                                    f'java.util.Arrays.asList("second{round_}"))'
                                ))
                                assert await value(f"count = {9 + round_ * 10}") == 9 + round_ * 10
                                # Same source/compiled class, fresh invocation locals:
                                # neither new values nor cleanup may alter old captures.
                                await store(reused=True)
                                if saved_supplier:
                                    assert await value(f"this.saved.get({round_ * 2}).get()") == (
                                        f"mutated{round_}:{4 + round_ * 10}"
                                    )
                                    assert await value(f"this.saved.get({round_ * 2 + 1}).get()") == (
                                        f"second{round_}:{9 + round_ * 10}"
                                    )
                            finally:
                                assert_ok(await call_payload(session, {
                                    "action": "resume", "suspension_id": suspension,
                                }))
                        assert response["body"] == f"{9 + round_ * 10}|second{round_}"
                        # Execute the escaped values as application code after the
                        # original frame has returned, not inside another evaluate.
                        assert await anyio.to_thread.run_sync(_http_get, port, "/snapshot") == native[round_]
                    assert _class_snapshot(str(tmp_path)) == before
                    assert source.read_text(encoding="utf-8") == source_text
                finally:
                    await call_payload(session, {"action": "cleanup_debug_state"})
                    await call_payload(session, {"action": "stop"})

    try:
        anyio.run(scenario)
    except ExceptionGroup as errors:
        # MCP/AnyIO wraps a test failure in nested task groups. Only unwrap this
        # precise known defect; cleanup failures or unrelated errors remain red.
        known, unexpected = errors.split(KnownJdtNamedLocalDefect)
        if known is not None and unexpected is None:
            raise KnownJdtNamedLocalDefect(str(known)) from errors
        raise
