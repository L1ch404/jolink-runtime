"""Compile a JDT snippet and execute it against the current JDWP stack frame."""

from __future__ import annotations

import base64
from pathlib import Path
import re
import tempfile
import time

import psutil

from ...core.models import RuntimeResult
from ...launch.jdt_compile_session import (
    JdtCandidate, JdtCompileError, PersistentJdtCompileSession,
    discover_target_system_entries, select_target_system_home,
)
from .evaluation_vm import EvaluationThrown, EvaluationVM, Reader
from .jdwp_client import Cmd, JDWPCommandOutcomeUnknown, JDWPError, Tag


def java_type(signature: str) -> str:
    dimensions = len(signature) - len(signature.lstrip("["))
    base = signature[dimensions:]
    name = {"B": "byte", "C": "char", "D": "double", "F": "float",
            "I": "int", "J": "long", "S": "short", "Z": "boolean", "V": "void"}.get(base)
    return (name or base.removeprefix("L").removesuffix(";").replace("/", ".")) + "[]" * dimensions


class DebugEvaluation:
    def __init__(self, runtime):
        self.runtime = runtime
        self.compiler = None
        self.temporary = None
        self.compiled = {}
        self.deployed = {}
        self.connection = None

    def close(self):
        if self.compiler is not None:
            self.compiler.close()
            self.compiler = None
        if self.temporary is not None:
            self.temporary.cleanup()
            self.temporary = None
        self.compiled.clear()
        self.deployed.clear()
        self.connection = None

    def _compiler(self, vm):
        runtime = self.runtime
        current = runtime._proc.current
        with runtime._project_state_lock:
            sessions = [session for key, session in runtime._project_sessions.items()
                        if runtime._project_processes.get(key) is current]
        for session in sessions:
            compiler = session.compile_session
            if compiler is not None and compiler.ready:
                return compiler
        if self.compiler is not None:
            return self.compiler

        # Direct launch/attach needs only a binary Java project, not Maven or a
        # full business-source build. Obtain paths from the actual application.
        reader = Reader(vm.command(Cmd.VM, 13))
        base = Path(reader.string())
        classpath = [reader.string() for _ in range(reader.number())]
        bootpath = [reader.string() for _ in range(reader.number())]
        version = Reader(vm.command(Cmd.VM, 1))
        version.string()
        version.take(8)
        version_string = version.string()
        match = re.match(r"(?:1\.)?(\d+)", version_string)
        if match is None:
            raise JdtCompileError("EVALUATION_PLATFORM_UNAVAILABLE", "Cannot determine application Java version")
        major = int(match.group(1))
        preferred = []
        if current is not None and current.pid:
            try:
                preferred.append(Path(psutil.Process(current.pid).exe()).resolve().parent.parent)
            except (psutil.Error, OSError):
                pass
        home = select_target_system_home(preferred, major)
        system = discover_target_system_entries(home, major)
        paths = list(system)
        for item in (*classpath, *bootpath):
            path = Path(item) if item else base
            if not path.is_absolute():
                path = base / path
            if path.exists():
                paths.append(path)
        candidate = JdtCandidate.load_product()
        self.temporary = tempfile.TemporaryDirectory(prefix="jolink-evaluate-")
        compiler = PersistentJdtCompileSession(
            root=Path(self.temporary.name) / "compiler", candidate=candidate,
            worker_java_home=candidate.select_worker_java_home(), source_roots=(),
            classpath_entries=tuple(dict.fromkeys(paths)), source_encoding="UTF-8", source_level=major,
            max_heap_mb=512,
        )
        try:
            result = compiler.start()
            if not result.compile_ok:
                raise JdtCompileError("EVALUATION_COMPILE_FAILED", "Cannot initialize expression type environment",
                                      context={"diagnostics": list(result.diagnostics)})
            compiler.accept_baseline()
        except Exception:
            compiler.close()
            self.temporary.cleanup()
            self.temporary = None
            raise
        self.compiler = compiler
        return compiler

    def _display(self, vm, value, action):
        if value.tag == ord("V"):
            return None
        return self.runtime._read_value(
            vm.jdwp, vm.ids, value.tag, value.data, 0,
            depth=self.runtime._value_depth(action.max_value_depth), visited=set(),
            semantic_collections=action.semantic_collections,
            item_limit=self.runtime._collection_item_limit(action.item_limit),
            map_entry_limit=self.runtime._collection_item_limit(action.map_entry_limit),
        )[0]

    def _exception(self, vm, value, action, seen=None):
        seen = set() if seen is None else seen
        seen.add(value.reference)
        result = {"_ref": f"0x{value.reference:x}"}
        try:
            type_id = vm.object_type(value.reference)
            result["type"] = java_type(vm.signature(type_id))
            fields = vm.fields(type_id)
            observed = vm.get_fields(value.reference, {name: field for name, field in fields.items()
                                                       if name in {"detailMessage", "cause", "target"}})
            message = observed.get("detailMessage")
            result["message"] = self._display(vm, message, action) if message else None
            # Reflection wraps the user's exception in InvocationTargetException.
            # Read the chain without invoking user-overridable Throwable methods.
            cause = observed.get("target") or observed.get("cause")
            if cause and cause.reference and cause.reference not in seen and len(seen) < 5:
                result["cause"] = self._exception(vm, cause, action, seen)
        except (JDWPError, OSError) as error:
            result["details_error"] = str(error)
        return result

    def evaluate(self, action):
        runtime = self.runtime
        vm = None
        started = time.monotonic()
        context = {}
        try:
            snapshot = runtime._require_suspension(action)
            jdwp = runtime._connect()
            if self.connection is not jdwp:
                self.close()
                self.connection = jdwp
            thread = runtime._resolve_thread_id(jdwp, snapshot, action.thread_name)
            vm = EvaluationVM(jdwp, thread, action.timeout)
            frames = runtime._read_frames(jdwp, thread, 1, start_index=action.frame_index)
            if not frames or frames[0]["is_native"]:
                raise JDWPError(-1, "Evaluation requires a Java stack frame")
            frame = frames[0]
            context = {**runtime._snapshot_context(snapshot), "frame": runtime._public_frame(frame),
                       "expression": action.expression}
            locals_ = vm.locals(frame)
            this = vm.pin(vm.value(Reader(vm.command(Cmd.STACK, 3,
                vm.ids.pack_obj(thread) + vm.ids.pack_frame(frame["frame_id"])))))
            loader = vm.loader(frame["class_id"])
            if not loader:
                # A bootstrap frame still needs somewhere to deploy helper classes.
                loader = vm.invoke(vm.class_id("Ljava/lang/ClassLoader;"), "getSystemClassLoader",
                                   "()Ljava/lang/ClassLoader;").reference
            vm.pin(vm.obj(loader, Tag.CLASS_LOADER))
            compiler = self._compiler(vm)
            local_names = [item["name"] for item in locals_]
            local_types = [item["type"] for item in locals_]
            key = (id(compiler), runtime._code_revision, frame["class_id"], frame["method_id"],
                   frame["location_index"], tuple(zip(local_names, local_types)), action.expression)
            compile_started = time.monotonic()
            compiled = self.compiled.get(key)
            reused = compiled is not None
            if compiled is None:
                # A suspended <init> frame is not an explicit this()/super()
                # invocation expression. Its supplied receiver remains usable.
                compiled = compiler.compile_snippet(
                    action.expression, declaring_type=java_type(vm.signature(frame["class_id"])),
                    local_types=local_types, local_names=local_names, is_static=not this.reference,
                )
                if not compiled.get("ok"):
                    diagnostics = compiled.get("diagnostics") or [
                        compiled.get("error") or compiled.get("message") or "Expression compilation failed"
                    ]
                    return RuntimeResult(ok=False, error="Java expression could not be compiled", data={
                        **context, "error_code": "EVALUATION_COMPILE_FAILED",
                        "diagnostics": diagnostics,
                    })
                self.compiled[key] = compiled
            compile_ms = (time.monotonic() - compile_started) * 1000
            type_ids = {}
            for entry in compiled["classes"]:
                name = entry["name"]
                deployment_key = (loader, name)
                type_id = self.deployed.get(deployment_key)
                if type_id is None:
                    existing = [item.reference_type_id for item in jdwp.classes_by_signature("L" + name.replace(".", "/") + ";")
                                if vm.loader(item.reference_type_id) == loader]
                    type_id = existing[0] if existing else vm.define(loader, name, base64.b64decode(entry["bytes_base64"]))
                    self.deployed[deployment_key] = type_id
                type_ids[name] = type_id
            snippet_type = type_ids[compiled["snippet_class"]]
            instance = vm.invoke(snippet_type, "<init>", "()V", constructor=True)
            fields = vm.fields(snippet_type)
            prefix = compiled.get("local_var_prefix", "val$")
            this_field = compiled.get("delegate_this", "val$this")
            value_field = compiled.get("result_value_field", "resultValue")
            type_field = compiled.get("result_type_field", "resultType")
            incoming = {fields[prefix + item["name"]][0]: item["value"] for item in locals_}
            # Core initializes resultType to void, even when a parser defect drops
            # the expression body. A real result (including void) must replace null.
            incoming[fields[type_field][0]] = vm.obj(0)
            vm.set_fields(instance.reference, incoming)
            if this.reference and this_field in fields:
                vm.set_fields(instance.reference, {fields[this_field][0]: this})
            thrown = None
            try:
                vm.invoke(snippet_type, compiled.get("run_method", "run"), "()V", receiver=instance.reference)
            except EvaluationThrown as error:
                thrown = error
            # Invoking target code invalidates frame IDs, even if execution returns
            # to the same suspended location. Reacquire before writing back locals.
            frame = runtime._read_frames(jdwp, thread, 1, start_index=action.frame_index)[0]
            selected = {prefix + item["name"]: fields[prefix + item["name"]] for item in locals_}
            outgoing = vm.get_fields(instance.reference, selected)
            vm.write_locals(frame["frame_id"], locals_, {name[len(prefix):]: value for name, value in outgoing.items()})
            if thrown is not None:
                raise thrown
            result = vm.get_fields(instance.reference, {name: fields[name] for name in (value_field, type_field)})
            value, type_value = result[value_field], result[type_field]
            if not type_value.reference:
                return RuntimeResult(ok=False, error="The evaluator returned without producing an expression result; execution may be incomplete.",
                    data={**context, "error_code": "EVALUATION_NO_RESULT",
                          "suggested_next_step": "Inspect application state before retrying. This expression could not be evaluated reliably."})
            # Class.getName also handles primitive Class objects, which have
            # no ReferenceType ID in JDWP.
            type_name = vm.invoke(vm.class_id("Ljava/lang/Class;"), "getName", "()Ljava/lang/String;", receiver=type_value.reference)
            result_type = self._display(vm, type_name, action)
            if result_type in {"boolean", "byte", "char", "short", "int", "long", "float", "double"}:
                value = vm.get_fields(value.reference, {"value": vm.fields(vm.object_type(value.reference))["value"]})["value"]
            return RuntimeResult(data={**context, "status": "evaluated", "result": {
                "type": result_type, "value": self._display(vm, value, action),
            }, "compile_reused": reused, "compile_ms": round(compile_ms, 1),
                "elapsed_ms": round((time.monotonic() - started) * 1000, 1)})
        except EvaluationThrown as error:
            return RuntimeResult(ok=False, error="The expression threw an exception in the application", data={
                **context, "error_code": "EVALUATION_EXCEPTION", "exception": self._exception(vm, error.value, action),
            })
        except JDWPCommandOutcomeUnknown as error:
            runtime._invalidate_suspension()
            return RuntimeResult(ok=False, error=str(error), data={**context,
                "error_code": "EVALUATION_OUTCOME_UNKNOWN", "execution_completed": None,
                "valid_while_suspended": False, "debug_state": "unknown",
                "suggested_next_step": "Evaluation may still be running. Do not automatically retry it; inspect or clean up the debug session.",
            })
        except JdtCompileError as error:
            return RuntimeResult(ok=False, error=str(error), data={**context,
                "error_code": error.error_code, **error.context})
        except (JDWPError, RuntimeError, OSError) as error:
            return RuntimeResult(ok=False, error=str(error), data={**context, "error_code": "EVALUATION_FAILED"})
        finally:
            if vm is not None:
                # Each invocation owns its instance. Escaped closures keep their
                # Java captures; otherwise the VM can collect the whole instance.
                vm.close()
