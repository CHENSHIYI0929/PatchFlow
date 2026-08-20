from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class ServiceSettings:
    database_url: str = "sqlite:///./patchflow.db"
    redis_url: str = "redis://localhost:6379/0"
    repo_root: Path = Path.cwd()
    artifact_root: Path = Path("./logs/artifacts")
    api_token: str = ""
    task_timeout_seconds: int = 900

    @classmethod
    def from_env(cls) -> "ServiceSettings":
        return cls(
            database_url=os.getenv("PATCHFLOW_DATABASE_URL", cls.database_url),
            redis_url=os.getenv("PATCHFLOW_REDIS_URL", cls.redis_url),
            repo_root=Path(os.getenv("PATCHFLOW_REPO_ROOT", str(Path.cwd()))).resolve(),
            artifact_root=Path(os.getenv("PATCHFLOW_ARTIFACT_ROOT", "./logs/artifacts")).resolve(),
            api_token=os.getenv("PATCHFLOW_API_TOKEN", ""),
            task_timeout_seconds=int(os.getenv("PATCHFLOW_TASK_TIMEOUT_SECONDS", "900")),
        )
