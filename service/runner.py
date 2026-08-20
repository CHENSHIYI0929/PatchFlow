from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from agent.benchmark import build_run_manifest
from config.schema import load_config, merge_cli_overrides
from entry.cli import TaskCancelledError, TaskTimeoutError, _execute_run_with_task_timeout
from service.database import RunStore
from service.queue import JobQueue
from service.settings import ServiceSettings


class RunService:
    """Execute queued runs through the existing, benchmark-tested run path."""

    def __init__(self, settings: ServiceSettings, store: RunStore, queue: JobQueue) -> None:
        self.settings = settings
        self.store = store
        self.queue = queue

    def execute(self, run_id: str) -> None:
        record = self.store.get_run(run_id)
        if record is None:
            return
        if self.queue.is_cancelled(run_id):
            self.store.update_run(run_id, status="cancelled")
            return

        request = record["request"]
        repo_path = Path(record["repo_path"]).resolve()
        lock_ttl = max(self.settings.task_timeout_seconds + 60, 60)
        if not self.queue.acquire_repo_lock(str(repo_path), run_id, lock_ttl):
            self.store.append_event(run_id, "repository_busy", {"repo_path": str(repo_path)})
            self.queue.enqueue(run_id)
            return

        self.store.update_run(run_id, status="running")
        self.store.append_event(run_id, "worker_started", {"repo_path": str(repo_path)})

        config = load_config()
        config = merge_cli_overrides(
            config,
            provider=request.get("provider"),
            model=request.get("model"),
            max_steps=request.get("max_steps"),
        )
        config.agent.log_dir = str(self.settings.artifact_root.parent)
        manifest = build_run_manifest(
            task_id=run_id,
            task_file=None,
            task_repo=repo_path,
            source_repo=repo_path,
            workspace_repo=repo_path,
            config=config,
            grader=None,
            sandbox=bool(request.get("sandbox")),
        )
        mechanisms = request.get("mechanisms") or {}
        payload = {
            "config": config,
            "repo_path": repo_path,
            "description": record["description"],
            "source_repo_path": str(repo_path),
            "test_cmd": request.get("test_cmd"),
            "manifest": manifest,
            "stream": False,
            "confirm": False,
            "sandbox": bool(request.get("sandbox")),
            "verbose": False,
            "show_banner": False,
            "run_mode": request.get("run_mode", "auto"),
            "disable_failure_analyzer": not mechanisms.get("failure_analyzer", True),
            "disable_hybrid_retrieval": not mechanisms.get("hybrid_retrieval", True),
            "disable_edit_plan": not mechanisms.get("edit_plan", True),
            "disable_self_review": not mechanisms.get("self_review", True),
            "disable_long_memory": not mechanisms.get("long_memory", True),
            "disable_compression": not mechanisms.get("compression", True),
        }

        try:
            result, artifact_dir = _execute_run_with_task_timeout(
                self.settings.task_timeout_seconds,
                payload,
                cancel_check=lambda: self.queue.is_cancelled(run_id),
            )
            self._import_artifacts(run_id, Path(artifact_dir))
            cancelled = self.queue.is_cancelled(run_id)
            final_status = "cancelled" if cancelled else ("succeeded" if result.is_success() else "failed")
            self.store.update_run(
                run_id,
                status=final_status,
                agent_status=result.status.value,
                result=result.to_dict(),
                artifact_dir=str(Path(artifact_dir).resolve()),
                failure_type=result.failure_type,
                failure_stage=result.failure_stage,
                failure_message=result.failure_message,
            )
        except TaskCancelledError as exc:
            self.store.append_event(run_id, "run_cancelled", {"message": str(exc)})
            self.store.update_run(run_id, status="cancelled", failure_message=str(exc))
        except TaskTimeoutError as exc:
            self.store.append_event(run_id, "run_timed_out", {"message": str(exc)})
            self.store.update_run(
                run_id,
                status="timed_out",
                failure_type="timeout",
                failure_stage="agent_loop",
                failure_message=str(exc),
            )
        except Exception as exc:
            self.store.append_event(run_id, "run_failed", {"message": str(exc)})
            self.store.update_run(
                run_id,
                status="failed",
                failure_type="workspace_error",
                failure_stage="agent_loop",
                failure_message=str(exc),
            )
        finally:
            self.queue.release_repo_lock(str(repo_path), run_id)

    def _import_artifacts(self, run_id: str, artifact_dir: Path) -> None:
        events_path = artifact_dir / "events.json"
        if events_path.exists():
            for event in json.loads(events_path.read_text(encoding="utf-8")):
                self.store.append_event(run_id, event.get("event_type", "agent_event"), event)
        self.store.replace_artifacts(run_id, str(artifact_dir))
