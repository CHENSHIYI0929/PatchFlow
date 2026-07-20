from __future__ import annotations

import argparse
import ast
import contextlib
import json
import re
import shutil
import tarfile
import time
import subprocess
import tempfile
import urllib.request
from pathlib import Path
from typing import Any, Iterable


DEFAULT_DATASET = "princeton-nlp/SWE-bench_Lite"
SOURCE_CACHE_ROOT = Path(__file__).resolve().parent.parent / "swebench_source_repos"


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Prepare SWE-bench Lite instances for PatchFlow experiments.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    prepare = subparsers.add_parser("prepare", help="Export N Lite instances as PatchFlow task files")
    prepare.add_argument("--dataset-name", default=DEFAULT_DATASET)
    prepare.add_argument("--split", default="test")
    prepare.add_argument("--limit", type=int, default=5)
    prepare.add_argument("--output-dir", default="swebench_runs/lite_5")
    prepare.add_argument("--prepare-repos", action="store_true", help="Clone/fetch repos and checkout base commits")
    prepare.add_argument("--repo-root", default=None, help="Where task repos should be prepared")
    prepare.add_argument(
        "--instances-jsonl",
        default=None,
        help="Reuse a local SWE-bench instances.jsonl instead of loading from Hugging Face",
    )

    collect = subparsers.add_parser("collect-predictions", help="Build SWE-bench predictions from PatchFlow artifacts")
    collect.add_argument("--instances-jsonl", required=True)
    collect.add_argument("--artifacts-dir", default="logs/artifacts")
    collect.add_argument("--output", required=True)
    collect.add_argument("--model-name", default="patchflow")

    args = parser.parse_args()
    if args.command == "prepare":
        prepare_instances(
            dataset_name=args.dataset_name,
            split=args.split,
            limit=args.limit,
            output_dir=Path(args.output_dir),
            prepare_repos=args.prepare_repos,
            repo_root=Path(args.repo_root) if args.repo_root else None,
            instances_jsonl=Path(args.instances_jsonl) if args.instances_jsonl else None,
        )
    elif args.command == "collect-predictions":
        collect_predictions(
            instances_jsonl=Path(args.instances_jsonl),
            artifacts_dir=Path(args.artifacts_dir),
            output=Path(args.output),
            model_name=args.model_name,
        )


def prepare_instances(
    *,
    dataset_name: str,
    split: str,
    limit: int,
    output_dir: Path,
    prepare_repos: bool,
    repo_root: Path | None,
    instances_jsonl: Path | None,
) -> None:
    rows = _load_rows(
        dataset_name=dataset_name,
        split=split,
        limit=limit,
        instances_jsonl=instances_jsonl,
    )

    output_dir.mkdir(parents=True, exist_ok=True)
    tasks_dir = output_dir / "tasks"
    tasks_dir.mkdir(parents=True, exist_ok=True)
    repos_dir = repo_root or output_dir / "repos"
    repos_dir.mkdir(parents=True, exist_ok=True)

    instances_path = output_dir / "instances.jsonl"
    ids_path = output_dir / "instance_ids.txt"
    empty_predictions_path = output_dir / "predictions.empty.jsonl"
    gold_predictions_path = output_dir / "predictions.gold.jsonl"

    _write_jsonl(instances_path, rows)
    ids_path.write_text("\n".join(row["instance_id"] for row in rows) + "\n", encoding="utf-8")
    _write_jsonl(empty_predictions_path, [_prediction(row, "") for row in rows])
    _write_jsonl(gold_predictions_path, [_prediction(row, row.get("patch") or "", "gold") for row in rows])

    for row in rows:
        repo_dir = repos_dir / row["instance_id"]
        if prepare_repos and not _repo_is_prepared(repo_dir):
            prepare_repo(row, repo_dir)
        if repo_dir.exists():
            _ensure_repo_runtime_compat(row, repo_dir)
        task_path = tasks_dir / f"{row['instance_id']}.txt"
        task_path.write_text(_render_task(row, repo_dir), encoding="utf-8")

    print(f"Wrote {len(rows)} instances to {output_dir}")
    print(f"PatchFlow tasks: {tasks_dir}")
    print(f"SWE-bench local dataset: {instances_path}")
    print(f"Gold predictions: {gold_predictions_path}")


def _load_rows(
    *,
    dataset_name: str,
    split: str,
    limit: int,
    instances_jsonl: Path | None,
) -> list[dict[str, Any]]:
    if instances_jsonl is not None:
        rows = [
            json.loads(line)
            for line in instances_jsonl.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        return rows[:limit]

    from datasets import load_dataset

    dataset = load_dataset(dataset_name, split=split)
    return [dict(row) for row in dataset.select(range(limit))]


def prepare_repo(row: dict[str, Any], repo_dir: Path) -> None:
    if row["repo"] == "django/django":
        prepare_repo_from_archive(row, repo_dir)
        _apply_test_patch(repo_dir, row.get("test_patch") or "")
        return

    repo_url = f"https://github.com/{row['repo']}.git"
    source_repo_dir = SOURCE_CACHE_ROOT / row["repo"].split("/")[-1]
    try:
        if source_repo_dir.exists() and (source_repo_dir / ".git").exists():
            if not _git_has_commit(source_repo_dir, row["base_commit"]):
                _git(["fetch", "--depth", "1", "origin", row["base_commit"]], cwd=source_repo_dir)
        else:
            source_repo_dir.parent.mkdir(parents=True, exist_ok=True)
            if source_repo_dir.exists():
                subprocess.run(["rm", "-rf", str(source_repo_dir)], check=True)
            _git(["init", str(source_repo_dir)])
            _git(["remote", "add", "origin", repo_url], cwd=source_repo_dir)
            _git(["fetch", "--depth", "1", "origin", row["base_commit"]], cwd=source_repo_dir)

        repo_dir.parent.mkdir(parents=True, exist_ok=True)
        if repo_dir.exists():
            subprocess.run(["rm", "-rf", str(repo_dir)], check=True)
        _git(["worktree", "prune"], cwd=source_repo_dir)
        _git(["worktree", "add", "--detach", "--force", str(repo_dir), row["base_commit"]], cwd=source_repo_dir)
        _git(["reset", "--hard", row["base_commit"]], cwd=repo_dir)
        _apply_test_patch(repo_dir, row.get("test_patch") or "")
        _ensure_repo_runtime_compat(row, repo_dir)
    except subprocess.CalledProcessError:
        prepare_repo_from_archive(row, repo_dir)
        _apply_test_patch(repo_dir, row.get("test_patch") or "")
        _ensure_repo_runtime_compat(row, repo_dir)


def prepare_repo_from_archive(row: dict[str, Any], repo_dir: Path) -> None:
    commit = row["base_commit"]
    archive_url = f"https://codeload.github.com/{row['repo']}/tar.gz/{commit}"
    repo_dir.parent.mkdir(parents=True, exist_ok=True)
    if repo_dir.exists():
        shutil.rmtree(repo_dir)

    with tempfile.TemporaryDirectory() as tmpdir:
        archive_path = Path(tmpdir) / f"{commit}.tar.gz"
        _download_with_retries(archive_url, archive_path)
        extract_dir = Path(tmpdir) / "extract"
        extract_dir.mkdir(parents=True, exist_ok=True)
        with tarfile.open(archive_path, "r:gz") as tar:
            tar.extractall(extract_dir)
        extracted_roots = [p for p in extract_dir.iterdir() if p.is_dir()]
        if len(extracted_roots) != 1:
            raise RuntimeError(f"Unexpected archive layout for {row['instance_id']}")
        shutil.move(str(extracted_roots[0]), str(repo_dir))

    _git(["init"], cwd=repo_dir)
    _git(["config", "user.email", "setup@swebench.local"], cwd=repo_dir)
    _git(["config", "user.name", "SWE-bench Local"], cwd=repo_dir)
    _git(["add", "."], cwd=repo_dir)
    _git(["commit", "-m", f"baseline {commit}"], cwd=repo_dir)


def _repo_is_prepared(repo_dir: Path) -> bool:
    return repo_dir.exists() and (repo_dir / ".git").exists()


def _ensure_repo_runtime_compat(row: dict[str, Any], repo_dir: Path) -> None:
    if row.get("repo") != "pytest-dev/pytest":
        return

    src_root = repo_dir / "src" / "_pytest"
    version_file = src_root / "_version.py"
    version = _pytest_repo_version(row)
    expected_version_text = _render_pytest_version_file(version)
    if not version_file.exists() or version_file.read_text(encoding="utf-8") != expected_version_text:
        version_file.write_text(expected_version_text, encoding="utf-8")

    compat_src = Path("/opt/miniconda3/envs/patchflow-swebench/lib/python3.10/site-packages/_pytest/_py")
    compat_dst = src_root / "_py"
    if compat_src.exists() and not compat_dst.exists():
        shutil.copytree(compat_src, compat_dst)


def _pytest_repo_version(row: dict[str, Any]) -> str:
    version = str(row.get("version") or "").strip()
    return version or "0.0.0"


def _render_pytest_version_file(version: str) -> str:
    parts = []
    for chunk in version.split("."):
        parts.append(int(chunk) if chunk.isdigit() else repr(chunk))
    tuple_repr = ", ".join(str(part) for part in parts)
    return (
        f'version = "{version}"\n'
        f"version_tuple = ({tuple_repr})\n"
    )


def _apply_test_patch(repo_dir: Path, patch_text: str) -> None:
    if not patch_text.strip():
        return
    subprocess.run(
        ["git", "apply", "--whitespace=nowarn", "-"],
        cwd=repo_dir,
        input=patch_text.encode("utf-8"),
        check=True,
    )


def _download_with_retries(url: str, destination: Path, retries: int = 5, delay_seconds: int = 5) -> None:
    last_error: Exception | None = None
    for attempt in range(1, retries + 1):
        with contextlib.suppress(FileNotFoundError):
            destination.unlink()
        try:
            with urllib.request.urlopen(url) as response, destination.open("wb") as out:
                shutil.copyfileobj(response, out)
            return
        except Exception as exc:
            last_error = exc
            if attempt == retries:
                break
            print(f"Download failed ({attempt}/{retries}) for {url}: {exc}; retrying...")
            time.sleep(delay_seconds)
    if last_error is None:
        raise RuntimeError(f"Download failed for {url}")
    raise last_error


def collect_predictions(
    *,
    instances_jsonl: Path,
    artifacts_dir: Path,
    output: Path,
    model_name: str,
) -> None:
    instances = [json.loads(line) for line in instances_jsonl.read_text(encoding="utf-8").splitlines()]
    artifact_roots = sorted(p for p in artifacts_dir.glob("*") if p.is_dir())
    predictions = []
    for row in instances:
        patch = _find_patch_for_instance(row["instance_id"], artifact_roots)
        predictions.append(_prediction(row, patch or "", model_name))
    _write_jsonl(output, predictions)
    print(f"Wrote {len(predictions)} predictions to {output}")


def _find_patch_for_instance(instance_id: str, artifact_roots: Iterable[Path]) -> str | None:
    for artifact in reversed(list(artifact_roots)):
        result_path = artifact / "result.json"
        patch_path = artifact / "final_diff.patch"
        manifest_path = artifact / "run_manifest.json"
        if not patch_path.exists():
            continue
        haystacks = [artifact.name]
        if result_path.exists():
            haystacks.append(result_path.read_text(encoding="utf-8", errors="ignore"))
        if manifest_path.exists():
            haystacks.append(manifest_path.read_text(encoding="utf-8", errors="ignore"))
        if any(instance_id in text for text in haystacks):
            return patch_path.read_text(encoding="utf-8")
    return None


def _render_task(row: dict[str, Any], repo_dir: Path) -> str:
    target_files = sorted(_patch_files(row.get("patch") or "") | _patch_files(row.get("test_patch") or ""))
    test_cmd = _test_cmd(row, repo_dir)
    target_files_value = ", ".join(target_files) if target_files else ""
    return (
        "---\n"
        f"repo: {repo_dir}\n"
        f"category: swebench_lite\n"
        f"difficulty: real_world\n"
        f"target_files: {target_files_value}\n"
        f"test_cmd: {test_cmd}\n"
        "finish_if_verified: true\n"
        "skip_preverified: false\n"
        "max_steps: 40\n"
        "---\n"
        f"SWE-bench Lite instance: {row['instance_id']}\n\n"
        f"Repository: {row['repo']}\n"
        f"Base commit: {row['base_commit']}\n"
        f"Version: {row.get('version')}\n\n"
        "Problem statement:\n"
        f"{row['problem_statement'].strip()}\n\n"
        "Fix the issue with the smallest reasonable code change. "
        "Do not edit tests unless the issue explicitly requires it. "
        "After editing, use verify_task in benchmark mode.\n"
    )


def _test_cmd(row: dict[str, Any], repo_dir: Path) -> str:
    fail_to_pass = _coerce_test_list(row.get("FAIL_TO_PASS"))
    repo = row.get("repo")
    compat_prefix = _pytest_env_prefix(repo)
    if fail_to_pass:
        if repo == "django/django":
            django_tests = [_django_test_label(item) for item in fail_to_pass]
            return "PYTHONPATH=. python tests/runtests.py " + " ".join(django_tests)
        if repo == "psf/requests":
            focused_targets = _nodeids_for_added_tests(repo_dir, row.get("test_patch") or "")
            if focused_targets:
                pytest_args = " ".join(focused_targets)
                command = f"{compat_prefix}python -m pytest --rootdir=. {pytest_args} -q".strip()
                if _requests_targets_need_httpbin(focused_targets):
                    command = _wrap_with_mock_httpbin(command)
                return command
        if repo == "sympy/sympy":
            test_files = sorted(_patch_files(row.get("test_patch") or ""))
            if test_files:
                fail_to_pass = [
                    item if "::" in item or item.endswith(".py") else _resolve_sympy_nodeid(repo_dir, test_files, item)
                    for item in fail_to_pass
                ]
        pytest_args = " ".join(fail_to_pass)
        return f"{compat_prefix}python -m pytest --rootdir=. {pytest_args} -q".strip()
    test_files = sorted(_patch_files(row.get("test_patch") or ""))
    if test_files:
        return f"{compat_prefix}python -m pytest --rootdir=. " + " ".join(test_files) + " -q"
    return f"{compat_prefix}python -m pytest --rootdir=. -q".strip()


def _pytest_env_prefix(repo: str | None) -> str:
    prefixes: list[str] = []
    if repo in {"psf/requests", "sympy/sympy"}:
        compat_dir = (Path(__file__).resolve().parent.parent / "swebench_runs" / "py_compat").resolve()
        prefixes.append(str(compat_dir))
    if repo == "pytest-dev/pytest":
        py_compat_dir = (Path(__file__).resolve().parent.parent / "swebench_runs" / "py_compat").resolve()
        compat_dir = (Path(__file__).resolve().parent.parent / "swebench_runs" / "pytest_compat").resolve()
        prefixes.append("src")
        prefixes.append(str(compat_dir))
        prefixes.append(str(py_compat_dir))
    if repo == "pallets/flask":
        prefixes.append("src")
    if not prefixes:
        return ""
    return f"PYTHONPATH={':'.join(prefixes)}:$PYTHONPATH PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 "


def _wrap_with_mock_httpbin(command: str) -> str:
    helper = (Path(__file__).resolve().parent / "run_with_mock_httpbin.py").resolve()
    return f"python {helper} -- {command}"


def _resolve_sympy_nodeid(repo_path: Path, test_files: list[str], test_name: str) -> str:
    needle = f"def {test_name}("
    matches = []
    for rel_path in test_files:
        path = repo_path / rel_path
        if path.exists() and needle in path.read_text(encoding="utf-8", errors="ignore"):
            matches.append(rel_path)
    if len(matches) == 1:
        return f"{matches[0]}::{test_name}"
    if test_files:
        return f"{test_files[0]}::{test_name}"
    return test_name


def _requests_targets_need_httpbin(nodeids: list[str]) -> bool:
    return any(nodeid.endswith("::test_encoded_methods") for nodeid in nodeids)


def _nodeids_for_added_tests(repo_dir: Path, test_patch: str) -> list[str]:
    nodeids: list[str] = []
    for test_file, test_name in _added_test_functions(test_patch):
        path = repo_dir / test_file
        if not path.exists():
            continue
        for candidate_name, class_name in _test_definitions(path).items():
            if candidate_name != test_name:
                continue
            if class_name:
                nodeids.append(f"{test_file}::{class_name}::{test_name}")
            else:
                nodeids.append(f"{test_file}::{test_name}")
            break
    return nodeids


def _added_test_functions(test_patch: str) -> list[tuple[str, str]]:
    added: list[tuple[str, str]] = []
    current_file: str | None = None
    for line in test_patch.splitlines():
        if line.startswith("+++ b/"):
            current_file = line[6:]
            continue
        if not current_file:
            continue
        match = re.match(r"\+\s*def\s+(test_[^(]+)", line)
        if match:
            added.append((current_file, match.group(1)))
    return added


def _test_definitions(path: Path) -> dict[str, str | None]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found: dict[str, str | None] = {}
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name.startswith("test_"):
            found[node.name] = None
        elif isinstance(node, ast.ClassDef):
            for child in node.body:
                if isinstance(child, ast.FunctionDef) and child.name.startswith("test_"):
                    found[child.name] = node.name
    return found


def _coerce_test_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(item) for item in value]
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return [value] if value.strip() else []
        if isinstance(parsed, list):
            return [str(item) for item in parsed]
    return []


def _django_test_label(value: str) -> str:
    match = re.fullmatch(r"\s*([^\(]+?)\s+\(([^)]+)\)\s*", value)
    if not match:
        return value.strip()
    test_name = match.group(1).strip()
    test_case = match.group(2).strip()
    return f"{test_case}.{test_name}"


def _patch_files(patch: str) -> set[str]:
    files: set[str] = set()
    for match in re.finditer(r"^\+\+\+\s+b/(.+)$", patch, re.MULTILINE):
        path = match.group(1).strip()
        if path and path != "/dev/null":
            files.add(path)
    return files


def _prediction(row: dict[str, Any], patch: str, model_name: str = "patchflow") -> dict[str, str]:
    return {
        "instance_id": row["instance_id"],
        "model_name_or_path": model_name,
        "model_patch": patch,
    }


def _write_jsonl(path: Path, rows: Iterable[dict[str, Any]]) -> None:
    path.write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows),
        encoding="utf-8",
    )


def _run(cmd: list[str], cwd: Path | None = None) -> None:
    subprocess.run(cmd, cwd=cwd, check=True)


def _git(args: list[str], cwd: Path | None = None) -> None:
    if args and args[0] in {"clone", "fetch"}:
        last_error: subprocess.CalledProcessError | None = None
        for attempt in range(1, 6):
            try:
                _run(["git", *args], cwd=cwd)
                return
            except subprocess.CalledProcessError as exc:
                last_error = exc
                if attempt == 5:
                    raise
                time.sleep(5)
        if last_error is not None:
            raise last_error
        return
    _run(["git", *args], cwd=cwd)


def _git_has_commit(repo_dir: Path, commit: str) -> bool:
    result = subprocess.run(
        ["git", "cat-file", "-e", f"{commit}^{{commit}}"],
        cwd=repo_dir,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return result.returncode == 0


if __name__ == "__main__":
    main()
