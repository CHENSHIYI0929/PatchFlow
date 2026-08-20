from __future__ import annotations

import queue
import hashlib
from typing import Protocol

import redis


class JobQueue(Protocol):
    def enqueue(self, run_id: str) -> None: ...
    def dequeue(self, timeout: int = 5) -> str | None: ...
    def cancel(self, run_id: str) -> None: ...
    def is_cancelled(self, run_id: str) -> bool: ...
    def acquire_repo_lock(self, repo_path: str, owner: str, ttl_seconds: int) -> bool: ...
    def release_repo_lock(self, repo_path: str, owner: str) -> None: ...
    def ping(self) -> bool: ...


class RedisJobQueue:
    queue_key = "patchflow:runs"

    def __init__(self, redis_url: str) -> None:
        self.client = redis.Redis.from_url(redis_url, decode_responses=True, socket_timeout=10)

    def enqueue(self, run_id: str) -> None:
        self.client.rpush(self.queue_key, run_id)

    def dequeue(self, timeout: int = 5) -> str | None:
        item = self.client.blpop(self.queue_key, timeout=timeout)
        return item[1] if item else None

    def cancel(self, run_id: str) -> None:
        self.client.setex(f"patchflow:run:{run_id}:cancel", 86400, "1")

    def is_cancelled(self, run_id: str) -> bool:
        return bool(self.client.exists(f"patchflow:run:{run_id}:cancel"))

    @staticmethod
    def _repo_lock_key(repo_path: str) -> str:
        digest = hashlib.sha256(repo_path.encode("utf-8")).hexdigest()
        return f"patchflow:repo:{digest}:lock"

    def acquire_repo_lock(self, repo_path: str, owner: str, ttl_seconds: int) -> bool:
        return bool(self.client.set(self._repo_lock_key(repo_path), owner, nx=True, ex=ttl_seconds))

    def release_repo_lock(self, repo_path: str, owner: str) -> None:
        self.client.eval(
            "if redis.call('get', KEYS[1]) == ARGV[1] then return redis.call('del', KEYS[1]) else return 0 end",
            1,
            self._repo_lock_key(repo_path),
            owner,
        )

    def ping(self) -> bool:
        return bool(self.client.ping())


class InMemoryJobQueue:
    def __init__(self) -> None:
        self.items: queue.Queue[str] = queue.Queue()
        self.cancelled: set[str] = set()
        self.repo_locks: dict[str, str] = {}

    def enqueue(self, run_id: str) -> None:
        self.items.put(run_id)

    def dequeue(self, timeout: int = 5) -> str | None:
        try:
            return self.items.get(timeout=timeout)
        except queue.Empty:
            return None

    def cancel(self, run_id: str) -> None:
        self.cancelled.add(run_id)

    def is_cancelled(self, run_id: str) -> bool:
        return run_id in self.cancelled

    def acquire_repo_lock(self, repo_path: str, owner: str, ttl_seconds: int) -> bool:
        del ttl_seconds
        current = self.repo_locks.get(repo_path)
        if current is not None and current != owner:
            return False
        self.repo_locks[repo_path] = owner
        return True

    def release_repo_lock(self, repo_path: str, owner: str) -> None:
        if self.repo_locks.get(repo_path) == owner:
            self.repo_locks.pop(repo_path, None)

    def ping(self) -> bool:
        return True
