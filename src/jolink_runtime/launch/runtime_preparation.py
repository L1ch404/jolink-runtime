"""One background runtime preparation per stdio MCP process."""

from __future__ import annotations

import logging
import threading
from concurrent.futures import Future, TimeoutError

from .runtime_install import PreparationCancelled, error_details, progress_context

logger = logging.getLogger(__name__)
_active = None


class RuntimePreparation:
    def __init__(self):
        self._lock = threading.Lock()
        self.cancelled = threading.Event()
        self._thread = None
        self._future = None
        self._progress = {}
        self._requested = False

    def start(self, *, retry=False):
        global _active
        with self._lock:
            if self.cancelled.is_set():
                raise PreparationCancelled("Runtime preparation was cancelled.")
            if self._future is not None and not (
                retry and self._future.done() and self._future.exception() is not None
            ):
                return self._future
            _active = self
            self._progress = {"state": "preparing"}
            future = self._future = Future()
            self._thread = threading.Thread(
                target=self._run,
                args=(future,),
                name="jolink-runtime-prepare",
                daemon=True,
            )
            self._thread.start()
            return future

    def _run(self, future):
        from .jdt_compile_session import JdtCandidate

        token = progress_context.set(self)
        logger.info("jdt.preparation.started")
        try:
            candidate = JdtCandidate.load_product()
            runtime = candidate.select_worker_java()
            if self.cancelled.is_set():
                raise PreparationCancelled("Runtime preparation was cancelled.")
            with self._lock:
                self._progress = {"state": "ready"}
            future.set_result((candidate, runtime))
            logger.info("jdt.preparation.ready")
        except PreparationCancelled as error:
            future.set_exception(error)
            logger.info("jdt.preparation.cancelled")
        except Exception as error:
            with self._lock:
                self._progress.update(state="failed", error=error_details(error))
            future.set_exception(error)
            logger.exception("jdt.preparation.failed")
        finally:
            progress_context.reset(token)

    def update(self, **values):
        with self._lock:
            for key, value in values.items():
                if value is None:
                    self._progress.pop(key, None)
                else:
                    self._progress[key] = value

    def result(self, check_request=None):
        if check_request is not None:
            check_request()
        future = self.start(retry=True)
        with self._lock:
            self._requested = True
        while True:
            if check_request is not None:
                check_request()
            if self.cancelled.is_set():
                raise PreparationCancelled("Runtime preparation was cancelled.")
            try:
                return future.result(timeout=0.1)
            except TimeoutError:
                if future.done():
                    return future.result()

    def snapshot(self, *, requested=False):
        with self._lock:
            state = self._progress.get("state")
            if self.cancelled.is_set() or state == "ready":
                return None
            if state == "preparing" and self._progress.get("phase"):
                return dict(self._progress)
            if state == "failed" and (requested or self._requested):
                return dict(self._progress)
        return None

    def close(self):
        global _active
        self.cancelled.set()
        if _active is self:
            _active = None
        # No join: network reads may still be waiting; the daemon cannot delay
        # MCP exit. Completed JARs already live outside the temporary assembly.


def prepared_runtime(preferred=(), *, check_request=None):
    if check_request is not None:
        check_request()
    preparation = _active
    if preparation is not None:
        return preparation.result(check_request)
    from .jdt_compile_session import JdtCandidate

    candidate = JdtCandidate.load_product()
    return candidate, candidate.select_worker_java(preferred)
