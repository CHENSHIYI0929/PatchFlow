"""
agent/grader.py

Grader abstraction for benchmark verification.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from tools.runtime import LocalRuntime, Runtime


@dataclass
class GraderResult:
    name: str
    success: bool
    stage: str = "grading"
    message: str = ""
    command: str | None = None
    output: str = ""
    returncode: int | None = None
    checks: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "success": self.success,
            "stage": self.stage,
            "message": self.message,
            "command": self.command,
            "output": self.output,
            "returncode": self.returncode,
            "checks": self.checks,
        }


class Grader:
    name = "grader"

    def run(
        self,
        repo_path: str | Path,
        *,
        runtime: Runtime | None = None,
        timeout: int = 120,
    ) -> GraderResult:
        raise NotImplementedError

    def describe(self) -> dict[str, Any]:
        return {"name": self.name}


class CommandGrader(Grader):
    def __init__(self, name: str, command: str) -> None:
        self.name = name
        self.command = command

    def run(
        self,
        repo_path: str | Path,
        *,
        runtime: Runtime | None = None,
        timeout: int = 120,
    ) -> GraderResult:
        exec_runtime = runtime or LocalRuntime()
        repo = Path(repo_path).resolve()
        result = exec_runtime.exec(self.command, cwd=str(repo), timeout=timeout)
        success = result.success
        message = "passed" if success else f"failed with exit code {result.returncode}"
        return GraderResult(
            name=self.name,
            success=success,
            stage="grading",
            message=message,
            command=self.command,
            output=result.output.strip(),
            returncode=result.returncode,
            checks=[
                {
                    "name": self.name,
                    "success": success,
                    "command": self.command,
                    "returncode": result.returncode,
                }
            ],
        )

    def describe(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "type": "command",
            "command": self.command,
        }


class CompositeGrader(Grader):
    name = "composite"

    def __init__(self, graders: list[Grader]) -> None:
        self.graders = graders

    def run(
        self,
        repo_path: str | Path,
        *,
        runtime: Runtime | None = None,
        timeout: int = 120,
    ) -> GraderResult:
        checks: list[dict[str, Any]] = []
        for grader in self.graders:
            result = grader.run(repo_path, runtime=runtime, timeout=timeout)
            checks.extend(result.checks or [result.to_dict()])
            if not result.success:
                return GraderResult(
                    name=self.name,
                    success=False,
                    stage=result.stage,
                    message=f"{grader.name}: {result.message}",
                    command=result.command,
                    output=result.output,
                    returncode=result.returncode,
                    checks=checks,
                )

        return GraderResult(
            name=self.name,
            success=True,
            stage="grading",
            message="all checks passed",
            checks=checks,
        )

    def describe(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "type": "composite",
            "checks": [grader.describe() for grader in self.graders],
        }
