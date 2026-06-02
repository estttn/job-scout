# -*- coding: utf-8 -*-
"""In-memory state for background cover-letter generation."""
from __future__ import annotations

import threading
from typing import Any, Callable

_lock = threading.Lock()
_jobs: dict[str, dict[str, Any]] = {}


def job_key(user_id: int, resume_id: int) -> str:
    return f"{user_id}:{resume_id}"


def get_progress(user_id: int, resume_id: int) -> dict[str, Any]:
    with _lock:
        job = _jobs.get(job_key(user_id, resume_id))
    if not job:
        return {
            "running": False,
            "phase": "idle",
            "letters_done": 0,
            "letters_failed": 0,
            "letters_total": 0,
            "letters_processed": 0,
        }
    return {
        "running": bool(job.get("running")),
        "phase": job.get("phase", "idle"),
        "letters_done": int(job.get("letters_done", 0)),
        "letters_failed": int(job.get("letters_failed", 0)),
        "letters_total": int(job.get("letters_total", 0)),
        "letters_processed": int(job.get("letters_processed", 0)),
        "error": job.get("error"),
    }


def start_letter_job(
    user_id: int,
    resume_id: int,
    worker: Callable[[Callable[[int, int, int, int], None]], dict[str, Any]],
) -> bool:
    """Start background letter generation. Returns False if already running."""
    key = job_key(user_id, resume_id)
    with _lock:
        cur = _jobs.get(key)
        if cur and cur.get("running"):
            return False
        _jobs[key] = {
            "running": True,
            "phase": "letters",
            "letters_done": 0,
            "letters_failed": 0,
            "letters_total": 0,
            "error": None,
        }

    def progress(processed: int, total: int, failed: int, succeeded: int) -> None:
        with _lock:
            j = _jobs.get(key)
            if j:
                j["letters_processed"] = processed
                j["letters_done"] = succeeded
                j["letters_total"] = total
                j["letters_failed"] = failed

    def run() -> None:
        try:
            stats = worker(progress)
            with _lock:
                j = _jobs.get(key)
                if j:
                    j.update(stats)
        except Exception as e:
            with _lock:
                j = _jobs.get(key)
                if j:
                    j["error"] = str(e)[:500]
        finally:
            with _lock:
                j = _jobs.get(key)
                if j:
                    j["running"] = False
                    j["phase"] = "done"

    threading.Thread(target=run, daemon=True).start()
    return True
