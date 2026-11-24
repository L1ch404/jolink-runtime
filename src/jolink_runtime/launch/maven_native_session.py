"""Maven owns compiler/plugin ordering for scopes that cannot use JDT outputs."""
from pathlib import Path
from functools import lru_cache
import hashlib
import os
import re
import shutil
from stat import S_ISREG
from tempfile import TemporaryDirectory
from urllib.parse import quote, unquote
from xml.etree import ElementTree as ET

from .native_compile_session import NativeCompileSession
from .maven_probe import ProductMavenProbe
from .jdt_compile_session import JdtCompileError


def _deletion_refresh(modules, previous, current, config_roots=()):
    removed = [Path(p) for p in previous.keys() - current.keys() if p.endswith(".java")]
    if not removed and not config_roots:
        return {}
    scopes = {}
    for m in modules:
        output = m["output_directory"]
        scopes[output] = (m["source_roots"], set(m["classpath"]))
        if "test_compiler" in m:
            scopes[m["test_output_directory"]] = (m["test_source_roots"],
                {output, *m["classpath"], *m["test_classpath"], *m["test_compiler"].get("classpath", ())})
    owners = {out for out, (roots, _) in scopes.items() if any(p.is_relative_to(r) for r in roots for p in removed)}
    affected = owners | {out for out, (roots, _) in scopes.items() if config_roots and any(Path(r) in config_roots for r in roots)}
    while True:
        dependents = {out for out, (_, deps) in scopes.items() if deps & affected}
        if dependents <= affected:
            break
        affected.update(dependents)
    current_java = [Path(p) for p in current if p.endswith(".java")]
    return {out: (out in owners, any(p.is_relative_to(r) for r in scopes[out][0] for p in current_java))
            for out in affected}


@lru_cache(maxsize=256)
def _lombok_imports(path, stamp):
    # Only collect invalidation inputs. Lombok itself reads the untouched
    # configuration and decides all values, precedence and list semantics.
    return re.findall(r"^[ \t]*import[ \t]+(.+?)[ \t\r]*$",
                      path.read_text(encoding="utf-8", errors="replace"), re.MULTILINE)


def _config_inputs(directories):
    pending = {a / "lombok.config" for directory in directories for a in (directory, *directory.parents)}
    inputs = {}
    while pending:
        path = pending.pop()
        if str(path) in inputs:
            continue
        try:
            st = path.stat()
        except (FileNotFoundError, NotADirectoryError):
            inputs[str(path)] = None
            continue
        inputs[str(path)] = [st.st_mtime_ns, st.st_size]
        if not S_ISREG(st.st_mode) or path.suffix in {".jar", ".zip"}:
            continue  # Archive members resolve inside this same physical file.
        for name in _lombok_imports(path, (st.st_mtime_ns, st.st_size)):
            name = name.split("!", 1)[0]
            if name.startswith("~"):
                name = str(Path.home()) + name[1:]
            name = re.sub(r"<(.+?)>", lambda m: os.environ.get(m[1], m[0]), name)
            if name:
                imported = Path(name) if Path(name).is_absolute() else path.parent / unquote(name)
                pending.add(Path(os.path.abspath(imported)))
    return inputs


def _config_stub(dest, original):
    if original is None:
        contents = "config.stopBubbling = true\n"  # Never inherit config from joLink's cache.
    elif not original.is_file():
        dest.unlink(missing_ok=True)
        return
    else:
        try:
            # Lombok resolves relative imports as URIs, including escaped spaces/!.
            target = quote(Path(os.path.relpath(original, dest.parent)).as_posix(), safe="/")
        except ValueError:  # Cross-drive Windows paths must be absolute.
            target = original.as_posix()
        contents = f"import {target}\n"
    if not dest.is_file() or dest.read_text(encoding="utf-8") != contents:
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(contents, encoding="utf-8")


def attach_maven_native(modules, *, target, build_root, maven_argv, settings, local_repository, offline, profiles, tests):
    if not any(m.get("native_main_compile") or (tests and (m.get("native_test_compile") or m.get("native_test_phase"))) for m in modules):
        return
    for m in modules:
        # Maven will run the actual compiler configuration, including checks
        # that would have been skipped on the JDT path.
        m["skipped_compiler_checks"] = []
        if tests and "test_compiler" in m:
            m["test_compiler"]["skipped_compiler_checks"] = []
        outputs = [m["output_directory"]]
        if tests and "test_compiler" in m:
            outputs.append(m["test_output_directory"])
        m["native_compile"] = {
            "build_root": str(build_root), "outputs": outputs,
            "resource_inputs": m.get("post_test_inputs", ()),
        }
        if Path(m["module_root"]) == target:
            m["native_compile"]["maven"] = {
                "command": list(maven_argv), "settings": str(settings) if settings else None,
                "profiles": list(profiles), "repository": str(local_repository), "offline": offline,
                "scope": "test" if tests else "runtime",
            }


class MavenNativeSession(NativeCompileSession):
    strategy = "maven_native"
    operation_name = "maven_native_compile"
    source_suffixes = (".java",)

    def __init__(self, root, modules, target_module, build_java_home):
        super().__init__(root, modules, target_module, build_java_home)
        # A selected upstream test-jar has sources too, not just the leaf module.
        self.test_source_roots = tuple(dict.fromkeys(Path(p) for m in modules for p in m["test_source_roots"]))
        self.modules, self.target = modules, target_module
        self.options = next(m["native_compile"]["maven"] for m in modules if Path(m["module_root"]) == target_module)
        self._lombok_roots = {
            Path(r) for m in modules for roots, compiler in (("source_roots", m), ("test_source_roots", m.get("test_compiler", {})))
            if compiler.get("lombok_entries") for r in m[roots]
            if any(Path(p).is_relative_to(r) for p in m.get("prepared_source_files", {}))}
        self._config_roots_changed = set()
        self.product_files = {p for m in modules for p in m.get("post_test_products", ())}
        self.aliases = {}
        for m in modules:
            for test in ("", "test_"):
                for resource in m.get(test + "resource_roots", ()):
                    self.aliases[Path(resource)] = Path(m[test + "output_directory"])

    def runtime_classpath(self, entries):
        return super().runtime_classpath(self.aliases.get(Path(p), p) for p in entries)

    def _committed_inputs(self, before):
        # Tests may open a generated file relative to the project directory
        # instead of using their private classpath. Observe only named files,
        # not every generated class or an entire target tree.
        result = dict(before)
        for name in self.product_files:
            path = Path(name)
            if path.is_file():
                st = path.stat()
                result[name] = [st.st_mtime_ns, st.st_size]
            else:
                result.pop(name, None)
        return result

    def _inputs(self):
        inputs = self._committed_inputs(super()._inputs())
        self._config_roots_changed.clear()
        if self._lombok_roots:
            directories = {Path(p).parent for p in inputs if p.endswith(".java")}
            directories.update(Path(p).parent for m in self.modules for p in m.get("prepared_source_files", {}))
            for root in self._lombok_roots:
                config = _config_inputs(d for d in directories if d.is_relative_to(root))
                if any(p not in self.stamps or self.stamps[p] != stamp for p, stamp in config.items()):
                    self._config_roots_changed.add(root)
                inputs.update(config)
        return inputs

    def _source_view(self, root, overrides):
        """Only roots containing a generated replacement need a private compiler view."""
        root = Path(root)
        replacements = {Path(p).relative_to(root): Path(src) for p, src in overrides.items()
                        if Path(p).is_relative_to(root)}
        if not replacements:
            return None
        view = self.root / "sources" / hashlib.sha256(os.fsencode(root)).hexdigest()
        with_lombok = root in self._lombok_roots
        if with_lombok:
            _config_stub(view / "lombok.config", None)
            for ancestor in reversed(root.parents):
                view = view / "p"
                _config_stub(view / "lombok.config", ancestor / "lombok.config")
            view = view / "p"
        view.mkdir(parents=True, exist_ok=True)
        files = {}
        for folder, dirs, names in os.walk(root):
            parent = Path(folder)
            dirs[:] = [d for d in dirs if d != ".git" and parent / d != self.root]
            for name in names:
                src = parent / name
                if src.is_file():
                    files[src.relative_to(root)] = src
        files.update(replacements)
        # Remove obsolete entries first: a file may have become a directory or vice versa.
        for folder, _, names in os.walk(view, topdown=False):
            parent = Path(folder)
            for name in names:
                old = parent / name
                if old.relative_to(view) not in files:
                    old.unlink()
            if parent != view and not any(parent.iterdir()):
                parent.rmdir()
        for rel, src in files.items():
            dest = view / rel
            if with_lombok and rel.name == "lombok.config":
                _config_stub(dest, src)
                continue
            # Generated files are content addressed. Source mtimes alone do
            # not distinguish two different versions of a replacement.
            if dest.is_file():
                if rel in replacements:
                    if src.read_bytes() == dest.read_bytes():
                        continue
                else:
                    old, new = dest.stat(), src.stat()
                    if (old.st_mtime_ns, old.st_size) == (new.st_mtime_ns, new.st_size):
                        continue
            dest.parent.mkdir(parents=True, exist_ok=True)
            if rel in replacements:
                shutil.copyfile(src, dest)
            else:
                shutil.copy2(src, dest)
        return view

    def _command(self, inputs):
        plan = self.options
        directory = self._attempt_directory
        probe = ProductMavenProbe.load().prepare(attempt_directory=directory,
            source_settings=Path(plan["settings"]) if plan["settings"] else None,
            local_repository=Path(plan["repository"]), offline=plan["offline"])
        probe.output_directory.mkdir(parents=True, exist_ok=True)
        self._clean_reason = probe.output_directory / "source-removal-requires-clean.txt"
        manifest = ET.Element("modules")
        refresh = _deletion_refresh(self.modules, self.stamps, inputs, self._config_roots_changed)
        for m in self.modules:
            module = ET.SubElement(manifest, "module", root=m["module_root"])
            for goal, output in (("compile", m["output_directory"]), ("testCompile", m["test_output_directory"])):
                if output in refresh:
                    owner, has_sources = refresh[output]
                    ET.SubElement(module, "refresh", goal=goal, removed=str(owner).lower(), sources=str(has_sources).lower())
            for key, tag in (("source_roots","main"), ("test_source_roots","test"), ("post_test_executions","post")):
                for value in m.get(key, ()):
                    view = self._source_view(value, m.get("prepared_source_files", {})) if tag != "post" else None
                    item = ET.SubElement(module, tag)
                    if view is not None:
                        item.set("replaces", value)
                    item.text = str(view) if view is not None else value
        ET.ElementTree(manifest).write(probe.output_directory/"compile-inputs.xml", encoding="utf-8")
        return (*plan["command"], "--batch-mode", "-f", str(self.directory/"pom.xml"), "-s", str(probe.settings_file),
                "-Dmaven.repo.local=" + plan["repository"], *(("--offline",) if plan["offline"] else ()),
                *(("-P", ",".join(plan["profiles"])) if plan["profiles"] else ()),
                probe.goal.replace("export-build-world", "export-reactor-world"), "-Djolink.probe.nativeCompile=true",
                "-Djolink.probe.scope=" + plan["scope"], "-Djolink.probe.targetDirectory=" + str(self.target),
                "-Djolink.probe.outputDirectory=" + str(probe.output_directory))

    def _build(self, run_operation=None):
        with TemporaryDirectory(prefix="native-maven-", dir=self.root) as directory:
            self._attempt_directory = Path(directory)
            self._clean_reason = None
            try:
                return super()._build(run_operation)
            except JdtCompileError as error:
                if self._clean_reason and self._clean_reason.is_file():
                    raise JdtCompileError("MAVEN_NATIVE_SOURCE_REMOVAL_REQUIRES_CLEAN",
                                          self._clean_reason.read_text(encoding="utf-8"), context=error.context) from error
                raise
