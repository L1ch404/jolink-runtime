"""Seeded, native-Java differential checks through the actual MCP debugger.

The corpus contains only bounded expressions. A failure retains the expression,
native outcome and both MCP response representations under pytest's tmp_path.
"""
from __future__ import annotations

import base64
import json
import math
import os
from pathlib import Path
import random
import struct
import subprocess
import time

import anyio
import pytest

from java_support import assert_ok, call_payload, open_mcp_session, require_real_mcp_java_e2e, reserve_local_port, source_line
from test_debugger_evaluate import _http_get

pytestmark = pytest.mark.mcp_java_e2e
SEED = 20261025


def expressions(seed=SEED):
    randomizer = random.Random(seed)
    cases = []

    def add(category, expression):
        if not any(item["expression"] == expression for item in cases):
            cases.append({"category": category, "expression": expression})

    def integer(depth):
        if depth == 0:
            return randomizer.choice(["a", "b", "c", "Integer.MAX_VALUE", "Integer.MIN_VALUE", str(randomizer.randint(-90, 90))])
        operator = randomizer.choice(["+", "-", "*", "^", "&", "|", "<<", ">>", ">>>", "/", "%"])
        right = str(randomizer.choice([1, 2, 3, 7, 16, 31, 32, 33])) if operator in {"/", "%", "<<", ">>", ">>>"} else integer(depth - 1)
        return f"({integer(depth - 1)} {operator} {right})"

    for _ in range(70):
        add("int", integer(randomizer.randint(1, 3)))
    for _ in range(25):
        operator = randomizer.choice(["+", "-", "*", "^", "&", "|", "<<", ">>", ">>>", "/", "%"])
        right = str(randomizer.choice([1, 2, 31, 32, 63, 64, 65])) if operator in {"/", "%", "<<", ">>", ">>>"} else randomizer.choice(["a", "b", "Long.MAX_VALUE", "Long.MIN_VALUE", "-39L"])
        add("long", f"wide {operator} {right}")
    for _ in range(25):
        left, right = integer(1), integer(1)
        comparison = randomizer.choice(["<", ">", "<=", ">=", "==", "!="])
        add("boolean", f"({left} {comparison} {right}) {randomizer.choice(['&&', '||', '^'])} flag")
    for expression in [
        "flag ? a : b", "absent == null || absent.isEmpty()", "absent != null && absent.isEmpty()",
        "(flag ? absent : text) == null", "java.util.Objects.equals(absent, text)",
        "x + a", "x / b", "(float) x / 3", "-0.0d", "Double.POSITIVE_INFINITY",
        "Double.NEGATIVE_INFINITY", "Double.NaN", "Float.NaN", "Float.POSITIVE_INFINITY",
        "1.0d / 0.0d", "0.0d / 0.0d", "Math.sqrt(-1.0)", "Math.copySign(0.0, -1.0)",
        "Math.nextUp(x)", "(short) a", "(byte) (a * 25)", "(char) (65 + (a & 15))",
        "text", "text.length()", "text.substring(1, 4)", "text.charAt(2)",
        "text.codePointAt(4)", 'text.replace("中", "文")', '"left\\0right"', '"\\n\\t\\r"',
        '"😀"', 'String.valueOf(absent)', "String.valueOf(wide)", 'text + ":" + a',
        "names.get(0)", "names.get(1).toUpperCase(java.util.Locale.ROOT)",
        "names.stream().map(String::length).reduce(0, Integer::sum)",
        'names.stream().filter(s -> s.startsWith("a")).count()',
        "numbers.stream().mapToInt(Integer::intValue).sum()",
        'values.get("a")', 'values.getOrDefault("missing", 17)', 'values.containsKey("b")',
        "java.util.Arrays.toString(ints)", "ints[1] + numbers.get(0)", "ints.length",
        "java.util.Optional.of(text).map(String::length).orElse(-1)",
        "java.util.Optional.ofNullable(absent).orElse(text)",
        "new java.math.BigDecimal(\"1.20\").add(new java.math.BigDecimal(\"2.30\")).toPlainString()",
        "((java.util.function.IntSupplier) () -> a + b).getAsInt()",
        "new java.util.function.IntSupplier(){ public int getAsInt(){return a ^ b;} }.getAsInt()",
        "this.bias + a", "this.twice(b)", "choose(boxed)", "choose((Integer) boxed)",
        "absent", "((String) null)",
    ]:
        add("mixed", expression)
    for expression in ["a / zero", "a % zero", "ints[99]", "names.get(-1)",
                       "absent.length()", "((String) boxed).length()", 'Integer.parseInt("bad")',
                       "java.util.Optional.empty().get()", "throwPublic()"]:
        add("expected_exception", expression)
    randomizer.shuffle(cases)
    return cases


SOURCE = r'''import com.sun.net.httpserver.HttpServer;
import java.net.InetSocketAddress;
import java.util.*;
import java.util.concurrent.Executors;
public class RandomEvaluationFixture {
 public int bias=7;
 public int twice(int value){return value*2;}
 public String choose(Object value){return "object";}
 public String choose(Integer value){return "integer";}
 public int throwPublic(){throw new IllegalArgumentException("native-oracle");}
 public Object observe(int index, boolean oracle) {
  int a=-17, b=5, c=29, zero=0;
  long wide=4000000001L;
  double x=2.25d;
  boolean flag=true;
  String text="ab中😀z", absent=null;
  Object boxed=Integer.valueOf(a);
  List<String> names=new ArrayList<String>(Arrays.asList("alice","bob","中"));
  List<Integer> numbers=Arrays.asList(a,b,c);
  Map<String,Integer> values=new LinkedHashMap<String,Integer>();
  values.put("a",a);values.put("b",b);
  int[] ints={a,b,c};
  if(oracle){ switch(index){ __CASES__ default: throw new AssertionError(index); } }
  int marker=a; // random expression breakpoint
  return a+":"+b+":"+names.size()+":"+values.size()+":"+ints[0];
 }
 static String encode(String value){
  byte[] units=new byte[value.length()*2];
  for(int i=0;i<value.length();i++){char c=value.charAt(i);units[i*2]=(byte)(c>>>8);units[i*2+1]=(byte)c;}
  return Base64.getEncoder().encodeToString(units);
 }
 public static void main(String[] args)throws Exception {
  RandomEvaluationFixture fixture=new RandomEvaluationFixture();
  if(args[0].equals("native")){
   for(int i=0;i<__COUNT__;i++){
    try {
     Object value=fixture.observe(i,true);
     String type=value==null?"null":value.getClass().getName();
     String text=String.valueOf(value);
     if(value instanceof Double)text=Long.toHexString(Double.doubleToRawLongBits((Double)value));
     if(value instanceof Float)text=Integer.toHexString(Float.floatToRawIntBits((Float)value));
     System.out.println(i+"\tV\t"+type+"\t"+encode(text));
    }catch(Throwable error){System.out.println(i+"\tE\t"+error.getClass().getName()+"\t"+encode(String.valueOf(error.getMessage())));}
   }
   return;
  }
  HttpServer server=HttpServer.create(new InetSocketAddress("127.0.0.1",Integer.parseInt(args[0])),0);
  server.createContext("/", exchange->{byte[] bytes=String.valueOf(fixture.observe(-1,false)).getBytes("UTF-8");exchange.sendResponseHeaders(200,bytes.length);exchange.getResponseBody().write(bytes);exchange.close();});
  server.setExecutor(Executors.newCachedThreadPool());server.start();
 }
}
'''


def native_value(kind, value):
    if kind == "null":
        return None
    if kind in {"java.lang.Double", "java.lang.Float"}:
        width, fmt = (8, ">d") if kind.endswith("Double") else (4, ">f")
        return struct.unpack(fmt, int(value, 16).to_bytes(width, "big"))[0]
    if kind in {"java.lang.Integer", "java.lang.Long", "java.lang.Short", "java.lang.Byte"}:
        return int(value)
    if kind == "java.lang.Boolean":
        return value == "true"
    return value


def mismatch(expected, actual):
    if expected["outcome"] == "E":
        exception = actual.get("exception", {})
        while exception.get("type") == "java.lang.reflect.InvocationTargetException" and exception.get("cause"):
            exception = exception["cause"]
        return None if actual.get("ok") is False and exception.get("type") == expected["type"] else "exception differs"
    if not actual.get("ok"):
        return "native succeeds but evaluate fails"
    value = actual.get("result", {}).get("value")
    kind = actual.get("result", {}).get("type")
    if isinstance(value, dict) and value.get("_kind") == "utf16_string":
        value = json.loads('"' + value["escaped"] + '"')
    if isinstance(value, dict) and value.get("_class") in {
        "Ljava/lang/Integer;", "Ljava/lang/Long;", "Ljava/lang/Boolean;", "Ljava/lang/Double;",
        "Ljava/lang/Float;", "Ljava/lang/Short;", "Ljava/lang/Byte;", "Ljava/lang/Character;",
    }:
        kind = value["_class"][1:-1].replace("/", ".")
        value = value.get("value")
    desired = native_value(expected["type"], expected["encoded_value"])
    if desired is None:
        return None if value is None else "null value differs"
    wrappers = {"int":"Integer", "long":"Long", "short":"Short", "byte":"Byte",
                "boolean":"Boolean", "double":"Double", "float":"Float", "char":"Character"}
    kind = "java.lang." + wrappers[kind] if kind in wrappers else kind
    if kind != expected["type"]:
        return f"type differs: {kind} != {expected['type']}"
    if isinstance(desired, float):
        if value in ("NaN", "Infinity", "-Infinity"):
            value = float(value)
        if not isinstance(value, (float, int)):
            return "numeric value missing or not numeric"
        if math.isnan(desired):
            return None if math.isnan(value) else "NaN value differs"
        if desired == 0:
            return None if value == 0 and math.copysign(1, value) == math.copysign(1, desired) else "signed zero differs"
    return None if value == desired else "value differs"


@pytest.mark.parametrize("java_major", [8, 17, 21])
def test_seeded_expression_differential(tmp_path: Path, java_major: int):
    require_real_mcp_java_e2e()
    home = os.environ.get(f"JOLINK_TEST_JAVA{java_major}_HOME")
    if not home:
        pytest.skip(f"set JOLINK_TEST_JAVA{java_major}_HOME")
    jdk = Path(home)
    suffix = ".exe" if os.name == "nt" else ""
    cases = expressions()
    source_text = SOURCE.replace("__COUNT__", str(len(cases))).replace(
        "__CASES__", "\n".join(f"case {i}: return (Object) ({case['expression']});" for i, case in enumerate(cases)))
    source = tmp_path / "RandomEvaluationFixture.java"
    source.write_text(source_text, encoding="utf-8")
    subprocess.run([str(jdk / f"bin/javac{suffix}"), "-g", "-encoding", "UTF-8", str(source)],
                   check=True, capture_output=True, text=True, timeout=45)
    native = subprocess.run([str(jdk / f"bin/java{suffix}"), "-cp", str(tmp_path), "RandomEvaluationFixture", "native"],
                            check=True, capture_output=True, text=True, encoding="utf-8", timeout=30)
    expected = []
    for line in native.stdout.splitlines():
        index, outcome, kind, data = line.split("\t")
        assert int(index) == len(expected)
        expected.append({"outcome": outcome, "type": kind, "encoded_value": base64.b64decode(data).decode("utf-16-be", errors="surrogatepass")})
    assert len(expected) == len(cases)
    report = {"seed": SEED, "java_major": java_major, "cases": [], "mismatches": [], "case_count": len(cases)}
    environment = {**os.environ, "JAVA_HOME": str(jdk), "PATH": str(jdk / "bin") + os.pathsep + os.environ.get("PATH", "")}
    port = reserve_local_port()

    async def scenario():
        with (tmp_path / "mcp-stderr.log").open("w+", encoding="utf-8") as stderr:
            async with open_mcp_session(stderr, environment=environment) as session:
                suspension = None
                try:
                    assert_ok(await call_payload(session, {"action":"run", "classpath":str(tmp_path), "main_class":"RandomEvaluationFixture",
                        "java_home":str(jdk), "app_args":[str(port)], "ready_port":port, "jdwp_port":reserve_local_port()}))
                    assert_ok(await call_payload(session, {"action":"breakpoint", "class_pattern":"RandomEvaluationFixture", "line":source_line(source_text,"// random expression breakpoint")}))
                    armed = assert_ok(await call_payload(session, {"action":"wait_event", "wait_mode":"arm", "timeout":20}))
                    response = {}
                    async def http():
                        response["body"] = await anyio.to_thread.run_sync(_http_get, port)
                    async with anyio.create_task_group() as group:
                        group.start_soon(http)
                        hit = assert_ok(await call_payload(session, {"action":"wait_event", "wait_mode":"await", "wait_handle":armed["wait_handle"], "timeout":20}))
                        assert hit["status"] == "breakpoint_hit", hit
                        suspension = hit["suspension_id"]
                        try:
                            for index, case in enumerate(cases):
                                started = time.monotonic()
                                reply = await session.call_tool("java_debugger", {"action":"evaluate", "expression":case["expression"], "suspension_id":suspension, "timeout":3})
                                actual = dict(reply.structuredContent or {})
                                text = json.loads(reply.content[0].text)
                                difference = mismatch(expected[index], actual)
                                if json.dumps(text, sort_keys=True) != json.dumps(actual, sort_keys=True) and not difference:
                                    difference = "MCP text and structured representations differ"
                                row = {"index":index, **case, "native":expected[index], "actual":actual, "raw_text":reply.content[0].text,
                                       "difference":difference, "elapsed_ms":round((time.monotonic()-started)*1000,1)}
                                report["cases"].append(row)
                                if difference:
                                    report["mismatches"].append(row)
                                if actual.get("error_code") == "EVALUATION_OUTCOME_UNKNOWN":
                                    break  # Do not retry potentially still-running target code.
                        finally:
                            if suspension:
                                assert_ok(await call_payload(session, {"action":"resume", "suspension_id":suspension}))
                                suspension = None
                    assert response["body"] == "-17:5:3:2:-17", response
                    report["resumed_state_correct"] = True
                finally:
                    await call_payload(session, {"action":"cleanup_debug_state"})
                    await call_payload(session, {"action":"stop"})
    try:
        anyio.run(scenario)
    finally:
        (tmp_path / "randomized-evidence.json").write_text(json.dumps(report, ensure_ascii=True, indent=2, allow_nan=False), encoding="utf-8")
    assert len(report["cases"]) == len(cases), "Corpus did not complete; inspect randomized-evidence.json"
    assert not report["mismatches"], [(row["expression"], row["difference"], row["actual"].get("error_code")) for row in report["mismatches"]]
