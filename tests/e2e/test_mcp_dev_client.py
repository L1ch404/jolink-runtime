from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path


def test_dev_client_starts_current_worktree_and_calls_real_mcp(
    tmp_path: Path,
) -> None:
    repository = Path(__file__).resolve().parents[2]
    commands = "\n".join(
        (
            json.dumps(
                {
                    "tool": "java_status",
                    "arguments": {"action": "status"},
                }
            ),
            json.dumps({"command": "quit"}),
            "",
        )
    )
    completed = subprocess.run(
        [
            sys.executable,
            str(repository / "scripts/jolink_mcp_dev_client.py"),
            "--repository",
            str(repository),
            "--stderr-log",
            str(tmp_path / "stderr.log"),
        ],
        input=commands,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=30,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    lines = completed.stdout.splitlines()
    ready = json.loads(lines[0].removeprefix("READY "))
    result = json.loads(lines[1].removeprefix("RESULT "))

    assert ready["name"] == "jolink-runtime"
    assert ready["repository"] == str(repository)
    assert len(ready["source_fingerprint"]) == 64
    assert result["process_state"] == "absent"
    assert result["server_diagnostics"]["status"] == "active"
    assert (tmp_path / "stderr.log").is_file()
