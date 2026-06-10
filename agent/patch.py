"""
agent/patch.py

结构化编辑对象。

目标：
- 让“编辑意图”先变成内部 Patch 对象，再交给工具执行
- 先支持几种稳定的文本级操作，后续再扩展到 AST / IR
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
import uuid


@dataclass
class Patch:
    """
    一次结构化编辑。

    patch_type:
    - replace_file: 全量替换文件内容
    - search_replace: 在文本中搜索并替换
    - replace_range: 按行号替换一段内容
    """

    patch_type: str
    path: str
    content: str | None = None
    search: str | None = None
    replace: str | None = None
    start_line: int | None = None
    end_line: int | None = None
    all_occurrences: bool = False
    expected_content: str | None = None
    expected_lines: str | None = None
    expected_occurrences: int | None = None
    patch_id: str = field(default_factory=lambda: f"patch-{uuid.uuid4().hex[:8]}")

    def to_dict(self) -> dict[str, Any]:
        return {
            "patch_id": self.patch_id,
            "patch_type": self.patch_type,
            "path": self.path,
            "content": self.content,
            "search": self.search,
            "replace": self.replace,
            "start_line": self.start_line,
            "end_line": self.end_line,
            "all_occurrences": self.all_occurrences,
            "expected_content": self.expected_content,
            "expected_lines": self.expected_lines,
            "expected_occurrences": self.expected_occurrences,
        }

    @classmethod
    def from_params(cls, params: dict[str, Any]) -> "Patch":
        patch_type = str(params.get("patch_type", "")).strip()
        path = str(params.get("path", "")).strip()
        if not patch_type:
            raise ValueError("patch_type is required")
        if not path:
            raise ValueError("path is required")
        return cls(
            patch_type=patch_type,
            path=path,
            content=params.get("content"),
            search=params.get("search"),
            replace=params.get("replace"),
            start_line=int(params["start_line"]) if params.get("start_line") is not None else None,
            end_line=int(params["end_line"]) if params.get("end_line") is not None else None,
            all_occurrences=bool(params.get("all_occurrences", False)),
            expected_content=params.get("expected_content"),
            expected_lines=params.get("expected_lines"),
            expected_occurrences=(
                int(params["expected_occurrences"])
                if params.get("expected_occurrences") is not None
                else None
            ),
        )

    def target_path(self) -> Path:
        return Path(self.path)
