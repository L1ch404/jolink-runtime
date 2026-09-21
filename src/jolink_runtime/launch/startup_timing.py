"""A small local file per launch, storing its last successful startup duration."""

import hashlib
import json
import logging
import os
import uuid
from pathlib import Path

from .jdt_workspace_store import jolink_cache_root


logger = logging.getLogger(__name__)


class StartupTimings:
    def __init__(self, root: Path | None = None):
        self.root = root if root is not None else jolink_cache_root() / "startup-timings"

    def _file(self, key):
        identity = json.dumps(key, ensure_ascii=True, separators=(",", ":"))
        return self.root / (hashlib.sha256(identity.encode("utf-8")).hexdigest() + ".json")

    def save(self, key, duration_ms):
        path = self._file(key)
        temporary = path.with_suffix(f".{uuid.uuid4().hex}.tmp")
        try:
            self.root.mkdir(parents=True, exist_ok=True)
            temporary.write_text(json.dumps({"startup_ms": duration_ms}), encoding="utf-8")
            temporary.replace(path)
        except OSError as error:
            # This is an advisory measurement, not a prerequisite for launch.
            logger.warning("startup.timing.save_failed error_type=%s", type(error).__name__)
        finally:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass

    def load(self, key):
        try:
            return float(json.loads(self._file(key).read_text(encoding="utf-8"))["startup_ms"])
        except (OSError, ValueError, KeyError, TypeError):
            return None

    @staticmethod
    def _project_key(request, ready_port=None):
        return (
            "project", os.path.normcase(str(request.project_path)),
            request.launch_name or "", request.build_system,
            bool(request.ready_port if ready_port is None else ready_port),
        )

    @staticmethod
    def _direct_key(action, ready_port=None):
        return StartupTimings._direct_identity(
            action.jar_path, action.main_class, action.classpath,
            action.ready_port if ready_port is None else ready_port,
        )

    @staticmethod
    def _direct_identity(jar_path, main_class, classpath, ready_port):
        def absolute(value):
            return os.path.normcase(os.path.abspath(os.path.expanduser(value)))
        if jar_path:
            return ("jar", absolute(jar_path), bool(ready_port))
        return ("class", main_class, tuple(absolute(p or ".") for p in classpath.split(os.pathsep)), bool(ready_port))

    def previous(self, runtime, arguments):
        if arguments.get("project_path"):
            key = (
                "project", os.path.normcase(str(Path(arguments["project_path"]).expanduser().resolve())),
                arguments.get("launch_name") or "", arguments.get("build_system", ""),
                bool(arguments.get("ready_port", 0)),
            )
        elif arguments["action"] == "restart" and not arguments.get("jar_path") and not arguments.get("main_class"):
            request = runtime._last_project_request
            action = runtime._last_direct_action
            if request is not None:
                key = self._project_key(request, arguments.get("ready_port"))
            elif action is not None:
                key = self._direct_key(action, arguments.get("ready_port"))
            else:
                return None
        else:
            key = self._direct_identity(
                arguments.get("jar_path", ""), arguments.get("main_class", ""),
                arguments.get("classpath", "."), arguments.get("ready_port", 0),
            )
        return self.load(key)
