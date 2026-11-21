"""Shared warm reuse and private publication for a build tool's native compiler."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import time
import uuid

from .contracts import BuildOperationSpec
from .build_log import redacted_build_log_tail
from .gradle_module_world import LANGUAGE_SUFFIXES
from .source_selection import selected_files as _producer_files
from .jdt_compile_session import JdtCompileError, JdtCompileResult
from .process_supervisor import AttemptToken, ProcessSupervisor




class NativeCompileSession:
    source_suffixes = LANGUAGE_SUFFIXES
    module_metadata = {}

    def __init__(self, root, modules, target_module, build_java_home):
        self.root = Path(root)
        self.home = Path(build_java_home)
        target = next(m for m in modules if Path(m["module_root"]) == target_module)
        self.directory = Path(target["native_compile"]["build_root"])
        self.tasks = tuple(dict.fromkeys(t for m in modules for t in m["native_compile"].get("tasks", ())))
        self.source_roots = tuple(dict.fromkeys(Path(p) for m in modules for p in m["source_roots"]))
        self.test_source_roots = tuple(Path(p) for p in target["test_source_roots"])
        self.resources = tuple(dict.fromkeys(Path(p) for m in modules for p in m["native_compile"]["resource_inputs"]))
        outputs = dict.fromkeys(p for m in modules for p in m["native_compile"]["outputs"])
        self.archives = {Path(p) for m in modules for p in m["native_compile"].get("archives", ())}
        self._outputs = {Path(p): self.root / "workspace" / str(i) for i, p in enumerate(outputs)}
        # Track original inputs, not the class/resource/intermediate directories
        # these same tasks will rewrite. Test and application have separate baselines.
        produced = {Path(p) for m in modules for p in m["native_compile"].get("extraGeneratedPaths", ())}
        generated = set(self._outputs) | produced
        self.producer_inputs = []
        for m in modules:
            plan = m["native_compile"]
            # A producer can own only a child of a registered output root.
            # Watch its static siblings, never its generated destination.
            for p in plan.get("extraUnownedDirs", ()):
                self.producer_inputs.append((Path(p), {}, produced))
            for spec in [*plan.get("extraInputTrees", ()), *({"directory": p} for p in plan.get("extraInputFiles", ()))]:
                path = Path(spec["directory"])
                if not any(path.is_relative_to(out) for out in generated):
                    self.producer_inputs.append((path, spec, generated))
        self.output_directory = self._outputs[Path(target["output_directory"])]
        self.test_output_directory = self._outputs.get(Path(target["test_output_directory"]), self.root / "unused-test")
        self.index = self.root / "native-state.json"
        self.compile_log = self.root / (self.strategy.split("_")[0] + "-compile.log")
        self.supervisor = ProcessSupervisor()
        self.ready = False
        self.last_close_clean = False
        self.working_compile_state = "unknown"
        self.stamps = {}
        self.digests = {}
        self.pending = {}
        self._index_dirty = False

    def runtime_classpath(self, entries):
        return tuple(dict.fromkeys(self._outputs[p] / p.name if p in self.archives else self._outputs.get(p, p)
                                   for p in map(Path, entries)))

    def workspace_source_roots(self):
        return (*self.source_roots, *self.test_source_roots)

    def _inputs(self):
        result = {}
        for roots, sources in ((self.workspace_source_roots(), True), (self.resources, False)):
            for root in roots:
                for path in (root.rglob("*") if root.is_dir() else (root,)):
                    if (not sources or path.suffix in LANGUAGE_SUFFIXES) and path.is_file():
                        stat = path.stat()
                        result[str(path)] = [stat.st_mtime_ns, stat.st_size]
        for root, spec, excluded in self.producer_inputs:
            for path in _producer_files(root, spec, excluded):
                stat = path.stat()
                result[str(path)] = [stat.st_mtime_ns, stat.st_size]
        return result

    def workspace_source_changes(self):
        now = self._inputs()
        return tuple(Path(p) for p in self.stamps.keys() | now.keys() if self.stamps.get(p) != now.get(p))

    def _committed_inputs(self, before):
        return before

    def start(self, *, reuse_workspace=False, run_operation=None, probe_refreshed=False):
        self.root.mkdir(parents=True, exist_ok=True)
        if reuse_workspace and self.index.is_file():
            saved = json.loads(self.index.read_text(encoding="utf-8"))
            self.stamps, self.digests = saved["inputs"], saved["outputs"]
            self.ready = True
            self.working_compile_state = "valid"
            if not probe_refreshed and not self.workspace_source_changes():
                return self._result(None, 0)
        return self._build(run_operation)

    def compile(self, source_files):
        if not source_files and self.working_compile_state == "valid":
            return self._result(None, 0)
        return self._build()

    def _build(self, run_operation=None):
        before = self._inputs()
        started = time.monotonic()
        log = self.compile_log
        log.unlink(missing_ok=True)
        spec = BuildOperationSpec(
            argv=self._command(), cwd=self.directory,
            environment={"JAVA_HOME": str(self.home), "PATH": str(self.home / "bin") + os.pathsep + os.environ.get("PATH", "")},
            output_capture=log, operation_name=self.operation_name, timeout_seconds=900,
            max_output_bytes=16 * 1024 * 1024,
        )
        result = (run_operation(spec) if run_operation else
                  self.supervisor.run(spec, owner=AttemptToken(uuid.uuid4().hex, 1)))
        if not result.succeeded:
            self.working_compile_state = "failed"
            self.last_compile_diagnostics = ({"message": "\n".join(redacted_build_log_tail(log))},)
            self.last_compile_error_count = len(self.last_compile_diagnostics)
            raise JdtCompileError(self.strategy.upper() + "_COMPILE_FAILED", f"{self.strategy.split('_')[0].title()} compilation did not complete.",
                                  context={"diagnostics": list(self.last_compile_diagnostics), "compile_backend": self.strategy,
                                           "compile_log": str(log), "return_code": result.return_code})
        # Gradle owns its task caches. Test and Runtime read separate, stable
        # directories and only receive changed bytes after their own build.
        digests = {}
        for original, private in self._outputs.items():
            private.mkdir(parents=True, exist_ok=True)
            archive = original in self.archives
            for path in ((original,) if archive else original.rglob("*")):
                if not path.is_file():
                    continue
                rel = Path(path.name) if archive else path.relative_to(original)
                dest = private / rel
                key = private.name + "/" + rel.as_posix()
                # Output tasks (including cache restores) may preserve a file's
                # timestamp. Compare bytes after a real build; no warm scan.
                data = path.read_bytes()
                digest = hashlib.sha256(data).hexdigest()
                digests[key] = digest
                if self.digests.get(key) != digest or not dest.is_file():
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    temp = dest.with_name("." + dest.name + ".tmp")
                    temp.write_bytes(data)
                    temp.replace(dest)
                    self.pending[key] = False
        for key in self.digests.keys() - digests.keys():
            private = self.root / "workspace"
            stale = private / key
            if not stale.resolve().is_relative_to(private.resolve()):
                raise JdtCompileError("GRADLE_OUTPUT_INDEX_INVALID", "Invalid path in the private Gradle output index.")
            stale.unlink(missing_ok=True)
            self.pending[key] = True
        self.stamps, self.digests = self._committed_inputs(before), digests
        self.working_compile_state = "valid"
        self.ready = True
        self._index_dirty = True
        self.save_source_index()
        return self._result(self.strategy.upper(), (time.monotonic() - started) * 1000)

    def _result(self, kind, elapsed):
        def paths(deleted, classes):
            return tuple(sorted(p for p, d in self.pending.items() if d == deleted and p.endswith(".class") == classes))
        # Gradle does not report an exact count of compiled source units.
        return JdtCompileResult(compile_ok=True, actual_build_kind=kind, compiled_source_count=None if kind else 0,
            compiled_source_units=(), changed_classes=(), deleted_classes=(), changed_resources=(), deleted_resources=(),
            error_count=0, warning_count=0, diagnostics=(), diagnostics_truncated=False, elapsed_ms=elapsed,
            source_changes_pending=False, output_directory=self.output_directory,
            runtime_changed_classes=paths(False, True), runtime_deleted_classes=paths(True, True),
            runtime_changed_resources=paths(False, False), runtime_deleted_resources=paths(True, False),
            test_output_directory=self.test_output_directory)

    def class_file(self, key):
        return key.split("/", 1)[1], self.root / "workspace" / key

    def mark_published(self):
        self.pending.clear()

    accept_baseline = mark_published

    def reset_publication_baseline(self, _):
        self.mark_published()

    def save_source_index(self):
        if not self._index_dirty:
            return
        temp = self.index.with_suffix(".tmp")
        temp.write_text(json.dumps({"inputs": self.stamps, "outputs": self.digests}), encoding="utf-8")
        temp.replace(self.index)
        self._index_dirty = False

    def interrupt(self, reason):
        return self.close()

    def close(self):
        self.ready = False
        self.last_close_clean = self.supervisor.close(deadline=time.monotonic() + 5).settled
        return self.last_close_clean
