from __future__ import annotations

from types import SimpleNamespace

import pytest

from jolink_runtime.adapters.java import debug_evaluation as evaluation_module
from jolink_runtime.adapters.java.debug_evaluation import DebugEvaluation
from jolink_runtime.adapters.java.evaluation_vm import EvaluationThrown, EvaluationVM, Value
from jolink_runtime.adapters.java.jdwp_client import (
    Cmd, IDSizes, JDWPCommandOutcomeUnknown, JDWPError,
)
from jolink_runtime.core.dispatcher import parse_runtime_action


def _evaluation(monkeypatch, outcome):
    ids = IDSizes(8, 8, 8, 8, 8)
    frame = {"frame_id": 44, "class_id": 11, "method_id": 12, "location_index": 3,
             "method": "observe()V", "is_native": False}
    frames_read = []
    invalidations = []

    def frames(*args, **kwargs):
        # Re-entering the suspended stack after invocation needs a fresh ID.
        frames_read.append(True)
        return [{**frame, "frame_id": 44 + len(frames_read)}]

    jdwp = SimpleNamespace(ids=ids, classes_by_signature=lambda name: [])
    runtime = SimpleNamespace(
        _code_revision=0,
        _require_suspension=lambda action: object(),
        _connect=lambda: jdwp,
        _resolve_thread_id=lambda *args: 9,
        _read_frames=frames,
        _snapshot_context=lambda snapshot: {
            "suspension_id": "susp_1", "valid_while_suspended": True, "debug_state": "suspended",
        },
        _public_frame=lambda value: {"method": value["method"]},
        _invalidate_suspension=lambda: invalidations.append(True),
    )

    class VM(EvaluationVM):
        def __init__(self, *args):
            super().__init__(*args)
            self.field_writes = []
            self.local_writes = []
            self.instances = {}
            self.definition_calls = []
            self.pin_commands = []
            self.run_receivers = []
            self.outcome = outcome
            self.closed = False
            self.inputs = {
                "record": self.obj(0x111), "array": self.obj(0x222), "count": self.integer(3),
            }

        def command(self, group, command, data=b""):
            if group == Cmd.OBJ_REF and command in {7, 8}:
                self.pin_commands.append((command, int.from_bytes(data, "big")))
                return b""
            assert (group, command) == (Cmd.STACK, 3)
            return self.obj(0).tagged()

        def locals(self, frame):
            return [dict(name=name, type="Ljava/lang/Object;" if name != "count" else "I",
                         signature="Ljava/lang/Object;" if name != "count" else "I",
                         slot=index, value=self.pin(value))
                    for index, (name, value) in enumerate(self.inputs.items())]

        def loader(self, type_id):
            return 7

        def signature(self, type_id):
            return "Lexample/App;"

        def class_id(self, signature):
            assert signature == "Ljava/lang/Class;"
            return 70

        def define(self, loader, name, data):
            self.definition_calls.append((loader, name))
            assert name == "Snippet"
            return 30

        def fields(self, type_id):
            return {
                "val$__jolink_alias_record": (101, "Ljava/lang/Object;"),
                "val$__jolink_alias_array": (103, "[I"),
                "val$__jolink_alias_count": (102, "I"),
                "resultValue": (201, "Ljava/lang/Object;"),
                "resultType": (202, "Ljava/lang/Class;"),
            }

        def set_fields(self, receiver, values):
            self.field_writes.append((receiver, dict(values)))
            self.instances[receiver].update(values)

        def get_fields(self, receiver, fields):
            return {name: self.pin(self.instances[receiver].get(field, self.obj(0)))
                    for name, (field, _) in fields.items()}

        def write_locals(self, frame_id, locals_, updated):
            self.local_writes.append((frame_id, updated))

        def object_type(self, reference):
            raise JDWPError(20, "Cannot inspect exception after target disconnect")

        def invoke(self, type_id, name, descriptor, arguments=(), **kwargs):
            if name == "<init>":
                assert kwargs == {"constructor": True}
                reference = 0x333 + len(self.instances)
                # Core initializes this to void.class before expression execution.
                self.instances[reference] = {202: self.obj(0x777)}
                return self.pin(self.obj(reference))
            if name == "getName":
                assert type_id == 70
                assert kwargs == {"receiver": 0x777}
                return self.pin(self.obj(0x778))
            assert name == "run"
            receiver = kwargs["receiver"]
            self.run_receivers.append(receiver)
            assert self.instances[receiver][202] == self.obj(0)
            if self.outcome == "unknown":
                raise JDWPCommandOutcomeUnknown(
                    packet_id=1, command_set=Cmd.OBJ_REF, command=6,
                    operation="Evaluation run", cause=TimeoutError(),
                )
            if self.outcome == "rejected":
                raise JDWPError(13, "Invocation was rejected")
            self.instances[receiver][102] = self.integer(8)
            if self.outcome == "exception":
                raise EvaluationThrown(self.pin(self.obj(0x444)))
            if self.outcome != "no_result":
                self.instances[receiver][202] = self.obj(0x777)
            return Value(ord("V"), b"")

        def close(self):
            super().close()
            self.closed = True

    vm = VM(jdwp, 9, 2)
    monkeypatch.setattr(evaluation_module, "EvaluationVM", lambda *args: vm)
    compiled = {
        "ok": True, "classes": [{"name": "Snippet", "bytes_base64": ""}],
        "snippet_class": "Snippet", "local_var_prefix": "val$__jolink_alias_",
    }
    vm.compile_calls = []

    def compile_snippet(*args, **kwargs):
        vm.compile_calls.append((args, kwargs))
        return compiled

    compiler = SimpleNamespace(compile_snippet=compile_snippet)
    evaluation = DebugEvaluation(runtime)
    monkeypatch.setattr(evaluation, "_compiler", lambda vm: compiler)
    monkeypatch.setattr(evaluation, "_display",
                        lambda vm, value, action: "void" if value.reference == 0x778 else None)
    action = parse_runtime_action({"action": "evaluate", "expression": "count = 8"})
    return evaluation, action, vm, invalidations


@pytest.mark.parametrize("outcome", ["success", "exception", "rejected"])
def test_terminal_evaluation_keeps_instance_values_and_releases_pins(monkeypatch, outcome):
    evaluation, action, vm, invalidations = _evaluation(monkeypatch, outcome)
    result = evaluation.evaluate(action)

    assert vm.closed is True
    assert vm.pinned == set()
    assert sorted(reference for command, reference in vm.pin_commands if command == 7) == sorted(
        reference for command, reference in vm.pin_commands if command == 8
    )
    assert invalidations == []
    assert len(vm.field_writes) == 1
    assert vm.field_writes[0][0] == 0x333
    assert vm.instances[0x333][101].reference == 0x111
    assert vm.instances[0x333][103].reference == 0x222
    if outcome != "rejected":
        assert vm.local_writes[0][0] == 46
        assert vm.local_writes[0][1]["count"] == vm.integer(8)
    if outcome == "exception":
        # Failure to read exception metadata must not hide the actual throw.
        assert result.data["error_code"] == "EVALUATION_EXCEPTION"
        assert result.data["exception"]["_ref"] == "0x444"
        assert "details_error" in result.data["exception"]
    elif outcome == "rejected":
        assert result.data["error_code"] == "EVALUATION_FAILED"
        assert vm.local_writes == []
    else:
        assert result.ok is True
        assert result.data["result"] == {"type": "void", "value": None}


def test_no_result_is_not_reported_as_default_void_and_releases_pins(monkeypatch):
    evaluation, action, vm, invalidations = _evaluation(monkeypatch, "no_result")
    result = evaluation.evaluate(action)

    assert result.ok is False
    assert result.data["error_code"] == "EVALUATION_NO_RESULT"
    assert "result" not in result.data
    assert "incomplete" in result.error
    assert "Inspect application state" in result.data["suggested_next_step"]
    assert invalidations == []
    assert vm.run_receivers == [0x333]
    # A partial run can already have changed locals. Keep the actual changes
    # and captured inputs, but do not mistake inherited void.class for success.
    assert vm.local_writes[0][0] == 46
    assert vm.local_writes[0][1]["count"] == vm.integer(8)
    assert len(vm.field_writes) == 1
    assert vm.field_writes[0][1][202] == vm.obj(0)
    assert vm.instances[0x333][101].reference == 0x111
    assert vm.instances[0x333][103].reference == 0x222
    assert vm.instances[0x333][202] == vm.obj(0)
    assert vm.closed is True
    assert vm.pinned == set()
    assert sorted(reference for command, reference in vm.pin_commands if command == 7) == sorted(
        reference for command, reference in vm.pin_commands if command == 8
    )


def test_constructor_frame_is_not_an_explicit_constructor_invocation(monkeypatch):
    evaluation, action, vm, _ = _evaluation(monkeypatch, "success")
    read_frames = evaluation.runtime._read_frames

    def constructor_frames(*args, **kwargs):
        return [{**frame, "method": "<init>(I)V"} for frame in read_frames(*args, **kwargs)]

    evaluation.runtime._read_frames = constructor_frames
    assert evaluation.evaluate(action).ok
    assert vm.compile_calls[0][1].get("is_constructor", False) is False


def test_unknown_execution_preserves_live_instance_and_invalidates_snapshot(monkeypatch):
    evaluation, action, vm, invalidations = _evaluation(monkeypatch, "unknown")
    result = evaluation.evaluate(action)

    assert result.ok is False
    assert result.data["error_code"] == "EVALUATION_OUTCOME_UNKNOWN"
    assert result.data["execution_completed"] is None
    assert result.data["valid_while_suspended"] is False
    assert result.data["debug_state"] == "unknown"
    assert invalidations == [True]
    assert vm.closed is True
    assert vm.pinned == set()
    assert sorted(reference for command, reference in vm.pin_commands if command == 7) == sorted(
        reference for command, reference in vm.pin_commands if command == 8
    )
    assert vm.local_writes == []
    # The method can still be using these slots after the caller stops waiting.
    assert len(vm.field_writes) == 1
    assert vm.instances[0x333][101].reference == 0x111
    assert vm.instances[0x333][103].reference == 0x222
    assert vm.run_receivers == [0x333]


def test_cached_expression_reuses_class_but_owns_new_instance_each_time(monkeypatch):
    evaluation, action, vm, invalidations = _evaluation(monkeypatch, "success")
    first = evaluation.evaluate(action)
    vm.inputs["record"] = vm.obj(0x555)
    vm.inputs["array"] = vm.obj(0x666)
    vm.inputs["count"] = vm.integer(40)
    second = evaluation.evaluate(action)

    assert first.ok is second.ok is True
    assert first.data["compile_reused"] is False
    assert second.data["compile_reused"] is True
    assert len(vm.compile_calls) == 1
    assert vm.definition_calls == [(7, "Snippet")]
    assert vm.run_receivers == [0x333, 0x334]
    assert [receiver for receiver, _ in vm.field_writes] == [0x333, 0x334]
    # An escaped closure may still reference the first instance. A later call
    # must neither clear its references nor replace them with new frame inputs.
    assert vm.instances[0x333][101].reference == 0x111
    assert vm.instances[0x333][103].reference == 0x222
    assert vm.instances[0x334][101].reference == 0x555
    assert vm.instances[0x334][103].reference == 0x666
    assert [frame_id for frame_id, _ in vm.local_writes] == [46, 48]
    assert all(updated["count"] == vm.integer(8) for _, updated in vm.local_writes)
    assert invalidations == []
    assert vm.pinned == set()
    assert sorted(reference for command, reference in vm.pin_commands if command == 7) == sorted(
        reference for command, reference in vm.pin_commands if command == 8
    )
