"""Small shared pieces for runtime installation, not project compilation."""

from __future__ import annotations

import contextvars
import os
import re
from pathlib import Path

progress_context = contextvars.ContextVar("runtime_install_progress", default=None)


class PreparationCancelled(RuntimeError):
    pass


def report_progress(**values) -> None:
    progress = progress_context.get()
    if progress is not None:
        progress.update(**values)


def check_cancelled() -> None:
    progress = progress_context.get()
    if progress is not None and progress.cancelled.is_set():
        raise PreparationCancelled("Runtime preparation was cancelled.")


def directory_mode() -> int:
    # Windows inherits the user's cache ACL. CPython's special 0700 ACL can
    # deny the same user's non-elevated process after elevated installation.
    return 0o777 if os.name == "nt" else 0o700


def create_directory(path: Path, *, parents=False, exist_ok=False) -> None:
    path.mkdir(parents=parents, exist_ok=exist_ok, mode=directory_mode())


def error_details(error: BaseException) -> dict:
    detail = {"type": type(error).__name__, "message": str(error)}
    cause = error
    while cause.__cause__ is not None and cause.__cause__ is not cause:
        cause = cause.__cause__
    if cause is not error:
        detail.update(cause_type=type(cause).__name__, reason=str(cause))
    for key in ("reason", "code", "errno", "winerror"):
        value = getattr(cause, key, None)
        if value is not None:
            detail[key] = value if isinstance(value, (int, float)) else str(value)
    for key, value in detail.items():
        if isinstance(value, str):
            value = re.sub(r"(?i)(https?://)[^/\s@]+@", r"\1<redacted>@", value)
            detail[key] = re.sub(
                r"(?i)(https?://[^\s?#]+)[?#][^\s]*", r"\1?<redacted>", value
            )
    return detail
