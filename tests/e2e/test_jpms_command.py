"""Exercise the Java9+ argument-file launch branch with a real modular JVM."""

import os
import subprocess
from pathlib import Path

import pytest

from java_support import require_real_mcp_java_e2e, reserve_local_port
from jolink_runtime.launch.contracts import JvmLaunchPlan
from jolink_runtime.launch.java_command import JavaCommandMaterializer


@pytest.mark.mcp_java_e2e
def test_module_argument_file_keeps_names_paths_and_quotes(tmp_path):
    require_real_mcp_java_e2e()
    home = os.environ.get("JOLINK_TEST_JAVA17_HOME")
    if not home:
        pytest.skip("JDK17 is required")
    java = Path(home)
    suffix = ".exe" if os.name == "nt" else ""
    sources = tmp_path / "module source 中文"
    sources.mkdir()
    descriptor = sources / "module-info.java"
    descriptor.write_text("module example.launch {}", encoding="utf-8")
    main = sources / "App.java"
    main.write_text('''package example;
public class App {public static void main(String[] args) {
System.out.print(App.class.getModule().getName()+"|"+System.getProperty("proof")+"|"+args[0]);
}}''', encoding="utf-8")
    classes = tmp_path / "module output 中文"
    subprocess.run([str(java / f"bin/javac{suffix}"), "-encoding", "UTF-8", "-d", str(classes),
                    str(descriptor), str(main)], check=True, capture_output=True, timeout=30)
    value = '空格 and "quotes" with \\backslash'
    plan = JvmLaunchPlan(java_executable=java / f"bin/java{suffix}", classpath=(),
                        main_class="example.App", working_directory=tmp_path,
                        module_name="example.launch", module_path=(classes,),
                        jvm_args=("-Dproof=" + value,), program_args=("argument 中文",))
    command = JavaCommandMaterializer().materialize(plan, jdwp_port=reserve_local_port(),
        attempt_directory=tmp_path / "attempt", force_pathing_jar=True)
    assert command.materialization == "module_argfile"
    completed = subprocess.run(command.argv, cwd=tmp_path, check=True, capture_output=True,
                               text=True, encoding="utf-8", timeout=30)
    assert "example.launch|" + value + "|argument 中文" in completed.stdout
