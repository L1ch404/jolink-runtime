from __future__ import annotations

import json

from jolink_runtime_debugger.core.models import RuntimeResult


def test_runtime_result_data_cannot_override_reserved_fields() -> None:
    success = json.loads(
        RuntimeResult(
            ok=True,
            data={"ok": False, "error": "fake error", "status": "ready"},
        ).to_json()
    )
    failure = json.loads(
        RuntimeResult(
            ok=False,
            error="real error",
            data={"ok": True, "error": "fake error", "error_code": "FAILED"},
        ).to_json()
    )

    assert success == {"ok": True, "status": "ready"}
    assert failure == {
        "ok": False,
        "error": "real error",
        "error_code": "FAILED",
    }


def test_runtime_result_ok_false_without_formal_error_drops_data_error() -> None:
    payload = json.loads(
        RuntimeResult(
            ok=False,
            data={"error": "not authoritative", "retryable": True},
        ).to_json()
    )

    assert payload == {"ok": False, "retryable": True}
