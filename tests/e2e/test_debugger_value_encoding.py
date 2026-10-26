"""Java edge values survive both real MCP representations without data loss."""
from __future__ import annotations

import base64
import json
import math
import os
from pathlib import Path
import struct
import subprocess

import anyio
import pytest

from java_support import assert_ok, call_payload, open_mcp_session, require_real_mcp_java_e2e, reserve_local_port, source_line
from test_debugger_evaluate import _http_get


pytestmark = pytest.mark.mcp_java_e2e

SOURCE = r'''import com.sun.net.httpserver.HttpServer;
import java.net.InetSocketAddress;
import java.util.Base64;
import java.util.concurrent.Executors;
public class ValueEncodingFixture {
 public String fail(String message){throw new IllegalStateException(message);}
 public Object[] observe() {
  double nan=Double.NaN, positive=Double.POSITIVE_INFINITY, negative=Double.NEGATIVE_INFINITY;
  double minusZero=-0.0d, finite=1.25d;
  float floatNan=Float.NaN, floatPositive=Float.POSITIVE_INFINITY, floatNegative=Float.NEGATIVE_INFINITY;
  String full="中文😀", high=full.substring(2,3), low=full.substring(3,4), nul="left\0right";
  String mixed="\t\"\\"+high+"中"+low, literal="\\ud83d";
  char highChar=full.charAt(2), lowChar=full.charAt(3);
  double[] numbers={nan,positive,negative,minusZero,finite};
  String[] strings={full,high,low,nul,mixed,literal};
  Object[] output={nan,positive,negative,minusZero,finite,floatNan,floatPositive,floatNegative,full,high,low,nul,mixed,literal,highChar,lowChar};
  int marker=1; // value encoding breakpoint
  return output;
 }
 static String encode(String text){
  byte[] units=new byte[text.length()*2];
  for(int i=0;i<text.length();i++){char c=text.charAt(i);units[2*i]=(byte)(c>>>8);units[2*i+1]=(byte)c;}
  return Base64.getEncoder().encodeToString(units);
 }
 public static void main(String[] args)throws Exception {
  ValueEncodingFixture fixture=new ValueEncodingFixture();
  if(args[0].equals("native")){
   for(Object value:fixture.observe()){
    String text=String.valueOf(value);
    if(value instanceof Double)text=Long.toHexString(Double.doubleToRawLongBits((Double)value));
    if(value instanceof Float)text=Integer.toHexString(Float.floatToRawIntBits((Float)value));
    System.out.println(value.getClass().getName()+"\t"+encode(text));
   }
   return;
  }
  HttpServer server=HttpServer.create(new InetSocketAddress("127.0.0.1",Integer.parseInt(args[0])),0);
  server.createContext("/", exchange->{fixture.observe();byte[] bytes="resumed".getBytes("UTF-8");exchange.sendResponseHeaders(200,bytes.length);exchange.getResponseBody().write(bytes);exchange.close();});
  server.setExecutor(Executors.newCachedThreadPool());server.start();
 }
}
'''
NAMES = ["nan", "positive", "negative", "minusZero", "finite", "floatNan", "floatPositive", "floatNegative",
         "full", "high", "low", "nul", "mixed", "literal", "highChar", "lowChar"]


def _assert_native_value(actual, kind, native_text):
    if kind in {"java.lang.Double", "java.lang.Float"}:
        width, fmt = (8, ">d") if kind.endswith("Double") else (4, ">f")
        native = struct.unpack(fmt, int(native_text, 16).to_bytes(width, "big"))[0]
        if math.isnan(native):
            assert actual == "NaN"
        elif math.isinf(native):
            assert actual == ("Infinity" if native > 0 else "-Infinity")
        else:
            assert isinstance(actual, (float, int))
            assert actual == native
            assert math.copysign(1, actual) == math.copysign(1, native)
    else:
        if isinstance(actual, dict):
            assert actual["_kind"] == "utf16_string"
            restored = json.loads('"' + actual["escaped"] + '"')
            assert restored.encode("utf-16-be", errors="surrogatepass") == native_text.encode("utf-16-be", errors="surrogatepass")
        else:
            assert actual == native_text


@pytest.mark.parametrize("java_major", [8, 17, 21])
def test_evaluate_and_variables_preserve_nonfinite_and_utf16(tmp_path: Path, java_major: int):
    require_real_mcp_java_e2e()
    home = os.environ.get(f"JOLINK_TEST_JAVA{java_major}_HOME")
    if not home:
        pytest.skip(f"set JOLINK_TEST_JAVA{java_major}_HOME")
    jdk, suffix = Path(home), ".exe" if os.name == "nt" else ""
    source = tmp_path / "ValueEncodingFixture.java"
    source.write_text(SOURCE, encoding="utf-8")
    subprocess.run([str(jdk / f"bin/javac{suffix}"), "-g", "-encoding", "UTF-8", str(source)], check=True, capture_output=True, timeout=30)
    native = subprocess.run([str(jdk / f"bin/java{suffix}"), "-cp", str(tmp_path), "ValueEncodingFixture", "native"], check=True, capture_output=True, text=True, timeout=30)
    expected = [(kind, base64.b64decode(text).decode("utf-16-be", errors="surrogatepass"))
                for kind, text in (line.split("\t") for line in native.stdout.splitlines())]
    assert len(expected) == len(NAMES)
    environment = {**os.environ, "JAVA_HOME": str(jdk), "PATH": str(jdk / "bin") + os.pathsep + os.environ.get("PATH", "")}
    port, report = reserve_local_port(), []

    async def scenario():
        with (tmp_path / "mcp-stderr.log").open("w+", encoding="utf-8") as stderr:
            async with open_mcp_session(stderr, environment=environment) as session:
                suspension = None
                async def debug(arguments):
                    result = await session.call_tool("java_debugger", arguments)
                    payload = result.structuredContent
                    assert payload is not None
                    # Reject non-JSON NaN tokens, and assert actual transport
                    # equality rather than comparing pre-serialization objects.
                    def invalid_constant(value):
                        raise AssertionError(f"Non-JSON numeric token: {value}")
                    assert json.loads(result.content[0].text, parse_constant=invalid_constant) == payload
                    json.dumps(payload, ensure_ascii=False, allow_nan=False).encode("utf-8")
                    report.append({"request": arguments, "result": payload})
                    return payload
                try:
                    assert_ok(await call_payload(session, {"action":"run", "classpath":str(tmp_path), "main_class":"ValueEncodingFixture",
                        "java_home":str(jdk), "app_args":[str(port)], "ready_port":port, "jdwp_port":reserve_local_port()}))
                    assert_ok(await call_payload(session, {"action":"breakpoint", "class_pattern":"ValueEncodingFixture", "line":source_line(SOURCE,"// value encoding breakpoint")}))
                    armed = assert_ok(await call_payload(session, {"action":"wait_event", "wait_mode":"arm", "timeout":20}))
                    response = {}
                    async def request():
                        response["body"] = await anyio.to_thread.run_sync(_http_get, port)
                    async with anyio.create_task_group() as group:
                        group.start_soon(request)
                        hit = assert_ok(await call_payload(session, {"action":"wait_event", "wait_mode":"await", "wait_handle":armed["wait_handle"], "timeout":20}))
                        assert hit["status"] == "breakpoint_hit", hit
                        suspension = hit["suspension_id"]
                        try:
                            for name, (kind, text) in zip(NAMES, expected):
                                payload = assert_ok(await debug({"action":"evaluate", "expression":name, "suspension_id":suspension}))
                                assert payload["result"]["type"] == {
                                    "java.lang.Double": "double", "java.lang.Float": "float",
                                    "java.lang.Character": "char", "java.lang.String": "java.lang.String",
                                }[kind]
                                _assert_native_value(payload["result"]["value"], kind, text)
                            variables = assert_ok(await debug({"action":"variables", "suspension_id":suspension, "max_value_depth":2, "item_limit":32}))
                            values = {value["name"]: value["value"] for value in variables["variables"]}
                            for name, (kind, text) in zip(NAMES, expected):
                                _assert_native_value(values[name], kind, text)
                            for index, (kind, text) in enumerate(expected[:5]):
                                _assert_native_value(values["numbers"]["items"][index], kind, text)
                            for index, (kind, text) in enumerate(expected[8:14]):
                                _assert_native_value(values["strings"]["items"][index], kind, text)
                            thrown = await debug({"action":"evaluate", "expression":"fail(high)", "suspension_id":suspension})
                            assert thrown.get("error_code") == "EVALUATION_EXCEPTION", thrown
                            # Error messages also retain the exact code unit.
                            assert "\\ud83d" in json.dumps(thrown), thrown
                            normal = assert_ok(await debug({"action":"evaluate", "expression":"finite + 1", "suspension_id":suspension}))
                            assert normal["result"]["value"] == 2.25
                        finally:
                            if suspension:
                                assert_ok(await call_payload(session, {"action":"resume", "suspension_id":suspension}))
                                suspension = None
                    assert response["body"] == "resumed"
                finally:
                    await call_payload(session, {"action":"cleanup_debug_state"})
                    await call_payload(session, {"action":"stop"})
    try:
        anyio.run(scenario)
    finally:
        (tmp_path / "value-encoding-evidence.json").write_text(json.dumps(report, ensure_ascii=True, allow_nan=False, indent=2), encoding="utf-8")
