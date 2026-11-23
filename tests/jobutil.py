"""Helpers to drive async generate/install slots from tests."""

from __future__ import annotations

import json
import time
from typing import Any, Callable

from tests.qtutil import ensure_qt_app


def wait_theme_job(
    be,
    method: str = "installTheme",
    *args,
    timeout: float = 60.0,
    app=None,
    start: Callable[..., str] | None = None,
) -> dict[str, Any]:
    """Start a theme job and wait for jobFinished. The slot itself must return quickly."""
    app = app or ensure_qt_app()
    results: list[str] = []

    def on_fin(raw: str) -> None:
        results.append(raw)

    be.jobFinished.connect(on_fin)
    try:
        fn = start or getattr(be, method)
        t0 = time.monotonic()
        raw = fn(*args) if args else fn()
        start_sec = time.monotonic() - t0
        started = json.loads(raw)
        started["_start_sec"] = start_sec
        if not started.get("started"):
            return started
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            app.processEvents()
            if results:
                data = json.loads(results[0])
                data["_start_sec"] = start_sec
                for _ in range(8):
                    app.processEvents()
                    time.sleep(0.004)
                return data
            time.sleep(0.005)
        raise TimeoutError(
            f"{method} did not finish in {timeout}s (label={getattr(be, 'jobLabel', '')})"
        )
    finally:
        try:
            be.jobFinished.disconnect(on_fin)
        except Exception:
            pass
