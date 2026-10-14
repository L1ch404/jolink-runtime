"""Direct classpath/JAR restart settings, independent of project compilation."""

from copy import copy

from ..core.models import RuntimeAction


def direct_restart_action(action: RuntimeAction, previous, process) -> RuntimeAction:
    """Inherit the last direct target and preserve MCP-only metadata when copying."""
    selected = copy(
        previous
        if not action.jar_path and not action.main_class and previous
        else action
    )
    selected.action = "restart"
    for name in getattr(action, "_launch_overrides", ()):
        if name in {"app_args", "vm_args", "jdwp_port"}:
            setattr(selected, name, getattr(action, name))
    home = getattr(action, "java_home", None) or getattr(previous, "java_home", None)
    if home is not None:
        selected.java_home = home
    ready_port = (
        action.ready_port or getattr(process, "ready_port", 0) or selected.ready_port
    )
    wait_timeout = (
        action.startup_wait_timeout_seconds
        if action.startup_wait_timeout_provided
        else getattr(
            process,
            "startup_wait_timeout_seconds",
            selected.startup_wait_timeout_seconds,
        )
    )
    selected.configure_startup_readiness(
        ready_port=ready_port,
        wait_timeout_seconds=wait_timeout,
        wait_timeout_provided=action.startup_wait_timeout_provided,
        config_source="explicit"
        if action.ready_port
        else ("previous_run" if ready_port else "not_configured"),
    )
    return selected
