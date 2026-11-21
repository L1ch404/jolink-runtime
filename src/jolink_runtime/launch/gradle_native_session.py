"""Run Gradle's configured compiler tasks."""
import os

from .native_compile_session import NativeCompileSession


class GradleNativeSession(NativeCompileSession):
    strategy = "gradle_native"
    operation_name = "gradle_joint_compile"

    def _command(self, inputs):
        wrapper = self.directory / ("gradlew.bat" if os.name == "nt" else "gradlew")
        args = [str(wrapper), "--console=plain", "--no-daemon"]
        if {"--offline", "-o"}.intersection(os.environ.get("GRADLE_ARGS", "").split()):
            args.append("--offline")
        return (*args, *self.tasks)
