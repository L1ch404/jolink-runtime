"""Seeded mutation/capture sequences compared with native javac and real MCP.

The same generated steps run first as ordinary Java methods, then as debugger
evaluations. Every mutation is followed by a full observable-state comparison;
escaped closures are also invoked after resume and across the next suspension.
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
SEED = 281047


def _steps(seed: int) -> list[list[dict[str, str]]]:
    random_ = random.Random(seed)
    rounds = []
    for round_ in range(2):
        kinds = ["lambda", "lambda", "anonymous", "rebind", "rebind",
                 "mutate", "append", "count", "count", "field", "throw", "read"]
        random_.shuffle(kinds)
        steps = []
        for index, kind in enumerate(kinds):
            label = f"s{seed}r{round_}i{index}"
            amount = random_.randint(-8, 9)
            prefix = ""
            if kind == "lambda":
                expression = ('this.saved.add(() -> names.get(0) + ":" + count '
                              '+ ":" + this.bias)')
            elif kind == "anonymous":
                expression = ('this.saved.add(new java.util.function.Supplier<String>() { '
                              'public String get() { return names.get(0) + ":" + count; } })')
            elif kind == "rebind":
                expression = ("(names = new java.util.ArrayList<String>("
                              f'java.util.Arrays.asList("{label}"))).get(0)')
            elif kind == "mutate":
                expression = f'names.set(0, "{label}")'
            elif kind == "append":
                expression = f'names.add("{label}")'
            elif kind == "count":
                expression = f"count += {amount}"
            elif kind == "field":
                expression = f"this.bias += {amount}"
            elif kind == "throw":
                expression = (f"this.failAfter(count += {amount}, "
                              f'names.set(0, "{label}"))')
            else:
                expression = 'names.get(0) + ":" + count + ":" + this.bias'
            steps.append({"kind": kind, "prefix": prefix, "expression": expression})
        rounds.append(steps)
    return rounds


SOURCE = """\
import com.sun.net.httpserver.HttpServer;
import java.net.InetSocketAddress;
import java.util.ArrayList;
import java.util.Arrays;
import java.util.List;
import java.util.concurrent.Executors;
import java.util.function.Supplier;

public class StateSequenceFixture {
    public List<Supplier<String>> saved = new ArrayList<Supplier<String>>();
    public int bias = 7;
    public int requestCount;
    public int lastCount;
    public List<String> lastNames;
    public static class State {
        public int count;
        public List<String> names;
        public State(int count, List<String> names) { this.count = count; this.names = names; }
    }
    public int failAfter(int value, String old) {
        throw new IllegalStateException("step-failure");
    }
    public String snapshot(int count, List<String> names) {
        List<String> captures = new ArrayList<String>();
        for (Supplier<String> supplier : saved) captures.add(supplier.get());
        return count + "|" + names + "|" + bias + "|" + captures;
    }
    public String observe() {
        int round = requestCount++;
        int count = 4 + round * 10;
        List<String> names = new ArrayList<String>(Arrays.asList("alice" + round));
        int marker = count; // state-sequence breakpoint
        lastCount = count;
        lastNames = names;
        return snapshot(count, names);
    }
    __METHODS__
    public void nativeRun() {
        __NATIVE__
    }
    public static void main(String[] args) throws Exception {
        Class.forName("java.lang.IllegalStateException");
        final StateSequenceFixture fixture = new StateSequenceFixture();
        if (args[0].equals("native")) { fixture.nativeRun(); return; }
        HttpServer server = HttpServer.create(
            new InetSocketAddress("127.0.0.1", Integer.parseInt(args[0])), 0);
        server.createContext("/", exchange -> {
            byte[] body = fixture.observe().getBytes("UTF-8");
            exchange.sendResponseHeaders(200, body.length);
            exchange.getResponseBody().write(body); exchange.close();
        });
        server.createContext("/snapshot", exchange -> {
            byte[] body = fixture.snapshot(fixture.lastCount, fixture.lastNames).getBytes("UTF-8");
            exchange.sendResponseHeaders(200, body.length);
            exchange.getResponseBody().write(body); exchange.close();
        });
        server.setExecutor(Executors.newCachedThreadPool()); server.start();
    }
}
"""


def _source(rounds: list[list[dict[str, str]]]) -> str:
    methods, native = [], []
    for round_, steps in enumerate(rounds):
        native.append(f'{{ State state = new State({4 + round_ * 10}, '
                      f'new ArrayList<String>(Arrays.asList("alice{round_}")));')
        for index, step in enumerate(steps):
            name = f"step{round_}_{index}"
            methods.append(f"""
    public void {name}(State state) {{
        int count = state.count;
        List<String> names = state.names;
        String status = "ok", result;
        try {{
            {step['prefix']}
            Object value = ({step['expression']});
            result = String.valueOf(value);
        }} catch (Throwable error) {{
            status = "exception";
            result = error.getClass().getName() + ":" + error.getMessage();
        }}
        state.count = count; state.names = names;
        System.out.println("{round_}\\t{index}\\t" + status + "\\t" + result
            + "\\t" + snapshot(count, names));
    }}
""")
            native.append(f"{name}(state);")
        native.append("}")
    return SOURCE.replace("__METHODS__", "\n".join(methods)).replace("__NATIVE__", "\n".join(native))


@pytest.mark.parametrize("java_major", [8, 17, 21])
def test_seeded_state_sequences_match_native_java(tmp_path: Path, java_major: int) -> None:
    require_real_mcp_java_e2e()
    home = os.environ.get(f"JOLINK_TEST_JAVA{java_major}_HOME")
    if not home:
        pytest.skip(f"set JOLINK_TEST_JAVA{java_major}_HOME")
    java_home = Path(home)
    suffix = ".exe" if os.name == "nt" else ""
    rounds = _steps(SEED)
    text = _source(rounds)
    source = tmp_path / "StateSequenceFixture.java"
    source.write_text(text, encoding="utf-8")
    subprocess.run([str(java_home / f"bin/javac{suffix}"), "-g", "-encoding", "UTF-8", str(source)],
                   cwd=tmp_path, capture_output=True, text=True, check=True, timeout=30)
    native = subprocess.run([str(java_home / f"bin/java{suffix}"), "-cp", str(tmp_path),
                             "StateSequenceFixture", "native"], cwd=tmp_path,
                            capture_output=True, text=True, check=True, timeout=30).stdout.splitlines()
    assert len(native) == sum(map(len, rounds)), native
    expected = {}
    for line in native:
        round_, index, status, value, state = line.split("\t", 4)
        expected[int(round_), int(index)] = {"status": status, "value": value, "state": state}
    records: list[dict[str, Any]] = []
    mismatches: list[dict[str, Any]] = []
    evidence = tmp_path / "state-sequence-evidence.json"
    port = reserve_local_port()
    before = _class_snapshot(str(tmp_path))
    environment = {**os.environ, "JAVA_HOME": str(java_home),
                   "PATH": str(java_home / "bin") + os.pathsep + os.environ.get("PATH", "")}

    def save() -> None:
        evidence.write_text(json.dumps({"seed": SEED, "java_major": java_major,
            "evaluation_count": len(records) * 2, "records": records,
            "mismatches": mismatches}, indent=2, ensure_ascii=False), encoding="utf-8")

    async def scenario() -> None:
        with (tmp_path / "mcp-stderr.log").open("w+", encoding="utf-8") as stderr:
            async with open_mcp_session(stderr, environment=environment) as session:
                try:
                    launch = assert_ok(await call_payload(session, {"action": "run",
                        "classpath": str(tmp_path), "main_class": "StateSequenceFixture",
                        "java_home": str(java_home), "app_args": [str(port)],
                        "ready_port": port, "jdwp_port": reserve_local_port()}))
                    assert launch["startup_state"] == "ready", launch
                    assert_ok(await call_payload(session, {"action": "breakpoint", "bp_action": "set",
                        "class_pattern": "StateSequenceFixture",
                        "line": source_line(text, "// state-sequence breakpoint")}))
                    for round_, steps in enumerate(rounds):
                        response = {}
                        armed = assert_ok(await call_payload(session, {"action": "wait_event",
                            "wait_mode": "arm", "timeout": 20}))

                        async def request() -> None:
                            response["body"] = await anyio.to_thread.run_sync(_http_get, port)

                        async with anyio.create_task_group() as group:
                            group.start_soon(request)
                            hit = assert_ok(await call_payload(session, {"action": "wait_event",
                                "wait_mode": "await", "wait_handle": armed["wait_handle"], "timeout": 20}))
                            assert hit["status"] == "breakpoint_hit", hit
                            suspension = hit["suspension_id"]
                            try:
                                for index, step in enumerate(steps):
                                    code = step["prefix"] + step["expression"]
                                    args = {"action": "evaluate", "expression": code,
                                            "suspension_id": suspension, "frame_index": 0}
                                    actual = await call_payload(session, args)
                                    state_args = {**args, "expression": "this.snapshot(count, names)"}
                                    state = await call_payload(session, state_args)
                                    oracle = expected[round_, index]
                                    record = {"round": round_, "index": index, "kind": step["kind"],
                                              "request": args, "actual": actual,
                                              "state_request": state_args, "state": state, "native": oracle}
                                    records.append(record)
                                    if oracle["status"] == "exception":
                                        valid_result = (actual.get("error_code") == "EVALUATION_EXCEPTION"
                                            and "step-failure" in json.dumps(actual.get("exception")))
                                    else:
                                        value = actual.get("result", {}).get("value")
                                        normalized = str(value).lower() if isinstance(value, bool) else str(value)
                                        valid_result = actual.get("ok") is True and normalized == oracle["value"]
                                    valid_state = state.get("ok") is True and state.get("result", {}).get("value") == oracle["state"]
                                    if not valid_result or not valid_state:
                                        mismatches.append({"round": round_, "index": index,
                                            "kind": step["kind"], "result_match": valid_result,
                                            "state_match": valid_state})
                                    save()
                            finally:
                                assert_ok(await call_payload(session, {"action": "resume", "suspension_id": suspension}))
                        wanted = expected[round_, len(steps) - 1]["state"]
                        for route, observed in [("resume", response["body"]),
                            ("post-resume-snapshot", await anyio.to_thread.run_sync(_http_get, port, "/snapshot"))]:
                            if observed != wanted:
                                mismatches.append({"round": round_, "route": route,
                                                   "actual": observed, "expected": wanted})
                        save()
                finally:
                    await call_payload(session, {"action": "cleanup_debug_state"})
                    await call_payload(session, {"action": "stop"})

    anyio.run(scenario)
    assert _class_snapshot(str(tmp_path)) == before
    assert source.read_text(encoding="utf-8") == text
    assert not mismatches, f"{len(mismatches)} mismatches; full requests/results: {evidence}: {mismatches}"
