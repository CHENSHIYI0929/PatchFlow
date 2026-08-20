from __future__ import annotations

from pathlib import Path
import json
import multiprocessing
import time
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from integrations.mcp import MCPToolAdapter, MCPToolSpec, register_mcp_tools
from config.schema import load_config
from agent.task import RunResult, RunStatus
from entry.cli import TaskCancelledError, TaskTimeoutError, _execute_run_with_task_timeout
from service.api import create_app
from service.database import RunStore
from service.queue import InMemoryJobQueue
from service.runner import RunService
from service.settings import ServiceSettings
from tools.base import ToolRegistry


def _service(tmp_path: Path):
    store = RunStore(f"sqlite:///{tmp_path / 'service.db'}")
    queue = InMemoryJobQueue()
    settings = ServiceSettings(
        database_url=f"sqlite:///{tmp_path / 'service.db'}",
        redis_url="redis://unused",
        repo_root=tmp_path,
        artifact_root=tmp_path / "logs" / "artifacts",
        api_token="secret",
        task_timeout_seconds=10,
    )
    return create_app(settings, store=store, job_queue=queue), store, queue


def _runner(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir(exist_ok=True)
    database_url = f"sqlite:///{tmp_path / 'runner.db'}"
    store = RunStore(database_url)
    store.init_schema()
    queue = InMemoryJobQueue()
    settings = ServiceSettings(
        database_url=database_url,
        redis_url="redis://unused",
        repo_root=tmp_path,
        artifact_root=tmp_path / "logs" / "artifacts",
        task_timeout_seconds=10,
    )
    run_id = "run-1"
    store.create_run(run_id, "fix it", str(repo), {"repo_path": str(repo), "mechanisms": {}})
    return RunService(settings, store, queue), store, queue, run_id, repo


def _artifact(tmp_path: Path) -> Path:
    artifact = tmp_path / "artifact"
    artifact.mkdir()
    (artifact / "events.json").write_text(
        json.dumps([{"event_type": "task_complete", "payload": {"summary": "ok"}}]),
        encoding="utf-8",
    )
    (artifact / "result.json").write_text("{}", encoding="utf-8")
    return artifact


def _slow_worker(result_queue, progress_queue, payload):
    del result_queue, progress_queue, payload
    time.sleep(30)


def test_create_and_get_run(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    app, _, queue = _service(tmp_path)
    headers = {"Authorization": "Bearer secret"}

    with TestClient(app) as client:
        assert client.get("/health/ready").json() == {"status": "ready"}
        response = client.post("/v1/runs", headers=headers, json={"repo_path": "repo", "task": "fix it"})
        assert response.status_code == 202
        run = response.json()
        assert run["status"] == "queued"
        assert queue.dequeue(timeout=0) == run["id"]

        fetched = client.get(f"/v1/runs/{run['id']}", headers=headers)
        assert fetched.status_code == 200
        assert fetched.json()["description"] == "fix it"


def test_api_rejects_unauthorized_and_outside_repo(tmp_path):
    app, _, _ = _service(tmp_path)
    outside = tmp_path.parent / "outside-repo"
    outside.mkdir(exist_ok=True)

    with TestClient(app) as client:
        assert client.post("/v1/runs", json={"repo_path": ".", "task": "x"}).status_code == 401
        response = client.post(
            "/v1/runs",
            headers={"Authorization": "Bearer secret"},
            json={"repo_path": str(outside), "task": "x"},
        )
        assert response.status_code == 400


def test_cancel_queued_run(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    app, _, queue = _service(tmp_path)
    headers = {"Authorization": "Bearer secret"}

    with TestClient(app) as client:
        run = client.post("/v1/runs", headers=headers, json={"repo_path": "repo", "task": "x"}).json()
        cancelled = client.post(f"/v1/runs/{run['id']}/cancel", headers=headers)
        assert cancelled.json()["status"] == "cancelled"
        assert queue.is_cancelled(run["id"])


def test_cancel_running_run_requests_hard_stop(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    app, store, queue = _service(tmp_path)
    headers = {"Authorization": "Bearer secret"}

    with TestClient(app) as client:
        run = client.post("/v1/runs", headers=headers, json={"repo_path": "repo", "task": "x"}).json()
        store.update_run(run["id"], status="running")
        response = client.post(f"/v1/runs/{run['id']}/cancel", headers=headers)
        assert response.json()["status"] == "cancellation_requested"
        assert queue.is_cancelled(run["id"])


def test_artifact_download_and_event_stream(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    app, store, _ = _service(tmp_path)
    headers = {"Authorization": "Bearer secret"}

    with TestClient(app) as client:
        run = client.post("/v1/runs", headers=headers, json={"repo_path": "repo", "task": "x"}).json()
        artifact = _artifact(tmp_path)
        store.replace_artifacts(run["id"], str(artifact))
        store.append_event(run["id"], "task_complete", {"summary": "ok"})
        store.update_run(run["id"], status="succeeded")

        listing = client.get(f"/v1/runs/{run['id']}/artifacts", headers=headers)
        assert {item["name"] for item in listing.json()} == {"events.json", "result.json"}
        downloaded = client.get(f"/v1/runs/{run['id']}/artifacts/result.json", headers=headers)
        assert downloaded.text == "{}"
        events = client.get(f"/v1/runs/{run['id']}/events", headers=headers)
        assert "event: task_complete" in events.text


def test_worker_success_imports_artifacts(tmp_path):
    runner, store, queue, run_id, repo = _runner(tmp_path)
    artifact = _artifact(tmp_path)
    result = RunResult(run_id, RunStatus.SUCCESS, "done", 1)

    with patch("service.runner._execute_run_with_task_timeout", return_value=(result, artifact)):
        runner.execute(run_id)

    record = store.get_run(run_id)
    assert record["status"] == "succeeded"
    assert record["agent_status"] == "success"
    assert {item["name"] for item in store.list_artifacts(run_id)} == {"events.json", "result.json"}
    assert str(repo) not in queue.repo_locks


def test_worker_preserves_agent_failure(tmp_path):
    runner, store, _, run_id, _ = _runner(tmp_path)
    artifact = _artifact(tmp_path)
    result = RunResult(
        run_id,
        RunStatus.FAILED,
        "tests failed",
        2,
        failure_type="verification_failed",
        failure_stage="grading",
    )

    with patch("service.runner._execute_run_with_task_timeout", return_value=(result, artifact)):
        runner.execute(run_id)

    record = store.get_run(run_id)
    assert record["status"] == "failed"
    assert record["failure_type"] == "verification_failed"


def test_worker_records_timeout(tmp_path):
    runner, store, _, run_id, _ = _runner(tmp_path)
    with patch(
        "service.runner._execute_run_with_task_timeout",
        side_effect=TaskTimeoutError("Timed out after 10s"),
    ):
        runner.execute(run_id)
    assert store.get_run(run_id)["status"] == "timed_out"


def test_worker_records_running_cancellation(tmp_path):
    runner, store, _, run_id, _ = _runner(tmp_path)
    with patch(
        "service.runner._execute_run_with_task_timeout",
        side_effect=TaskCancelledError("Run cancelled by request"),
    ):
        runner.execute(run_id)
    assert store.get_run(run_id)["status"] == "cancelled"


def test_worker_records_unexpected_failure(tmp_path):
    runner, store, _, run_id, _ = _runner(tmp_path)
    with patch("service.runner._execute_run_with_task_timeout", side_effect=RuntimeError("boom")):
        runner.execute(run_id)
    record = store.get_run(run_id)
    assert record["status"] == "failed"
    assert record["failure_type"] == "workspace_error"


def test_busy_repository_is_requeued(tmp_path):
    runner, store, queue, run_id, repo = _runner(tmp_path)
    assert queue.acquire_repo_lock(str(repo), "other-run", 60)
    runner.execute(run_id)
    assert store.get_run(run_id)["status"] == "queued"
    assert queue.dequeue(timeout=0) == run_id


@pytest.mark.skipif("fork" not in multiprocessing.get_all_start_methods(), reason="requires fork")
def test_parent_cancellation_terminates_worker():
    started = time.monotonic()
    with patch("entry.cli._benchmark_run_worker", new=_slow_worker):
        with pytest.raises(TaskCancelledError):
            _execute_run_with_task_timeout(30, {}, cancel_check=lambda: True)
    assert time.monotonic() - started < 5


class FakeMCPClient:
    def list_tools(self):
        return [MCPToolSpec("echo", "Echo input", {"type": "object", "properties": {"text": {"type": "string"}}})]

    def call_tool(self, name, arguments):
        return {"content": [{"type": "text", "text": arguments["text"]}], "isError": False}


def test_mcp_adapter_registers_and_executes():
    registry = register_mcp_tools(ToolRegistry(), "demo server", FakeMCPClient())
    assert registry.tool_names == ["mcp__demo_server__echo"]
    result = registry.execute_tool("mcp__demo_server__echo", {"text": "hello"})
    assert result.success
    assert result.output == "hello"


def test_mcp_adapter_normalizes_failure():
    class FailingClient(FakeMCPClient):
        def call_tool(self, name, arguments):
            raise RuntimeError("offline")

    tool = MCPToolAdapter("demo", FailingClient().list_tools()[0], FailingClient())
    result = tool.execute({"text": "hello"})
    assert not result.success
    assert result.failure_type == "tool_failure"


def test_mcp_config_parses_server(tmp_path):
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        "mcp:\n  servers:\n    demo:\n      transport: stdio\n      command: python\n      args: ['server.py']\n",
        encoding="utf-8",
    )
    server = load_config(config_path).mcp.servers[0]
    assert server.name == "demo"
    assert server.command == "python"
