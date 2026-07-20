# SWE-bench Lite Benchmark Results (30 Tasks)

Date: 2026-06-22

This document records the current 30-task PatchFlow evaluation set and its outcomes.

## Summary

- Total tasks evaluated: `30`
- Passed / fixed: `24`
- Failed: `3`
- Evaluation-blocked: `3`

Status definitions:

- `passed`: task was fixed and the target verification passed
- `failed`: task reached a clear negative outcome
- `evaluation-blocked`: task was exercised, but the final result was not fair to count as a normal pass/fail because of dataset-target mismatch or interpreter/runtime compatibility issues

## Batch Summary

| Batch | Tasks | Passed | Failed | Blocked | Notes |
| --- | ---: | ---: | ---: | ---: | --- |
| `lite_5` (`astropy`) | 5 | 4 | 1 | 0 | Official SWE-bench Docker smoke test completed |
| `django_4_official` | 4 | 4 | 0 | 0 | Official SWE-bench Docker evaluation completed |
| `next_requests_sympy_6` | 5 | 5 | 0 | 0 | Local/Docker targeted verification |
| `next_requests_pytest_8` | 8 | 4 | 1 | 3 | `pytest` old-version compatibility issues under Python 3.13 |
| `next_sympy_8` | 8 | 7 | 1 | 0 | Local targeted verification |

## Task Results

### 1. `lite_5` (`astropy`)

| Instance | Status | Notes |
| --- | --- | --- |
| `astropy__astropy-12907` | passed | Official report resolved |
| `astropy__astropy-14182` | failed | Local run exhausted steps; official report recorded `empty_patch` |
| `astropy__astropy-14365` | passed | Official report resolved |
| `astropy__astropy-14995` | passed | Official report resolved |
| `astropy__astropy-6938` | passed | Official report resolved |

Official report:
- [patchflow-deepseek-v4-flash.patchflow-lite5-official-retry12.json](../SWE-bench/patchflow-deepseek-v4-flash.patchflow-lite5-official-retry12.json)

### 2. `django_4_official`

| Instance | Status | Notes |
| --- | --- | --- |
| `django__django-10914` | passed | Official report resolved |
| `django__django-11001` | passed | Official report resolved |
| `django__django-11039` | passed | Official report resolved |
| `django__django-11049` | passed | Official report resolved |

Official report:
- [patchflow-deepseek-v4-flash.patchflow-django4-official.json](../SWE-bench/patchflow-deepseek-v4-flash.patchflow-django4-official.json)

### 3. `next_requests_sympy_6`

| Instance | Status | Notes |
| --- | --- | --- |
| `psf__requests-1963` | passed | Redirect method propagation fixed |
| `psf__requests-2148` | passed | `socket.error` wrapped as `ConnectionError` in streaming path |
| `psf__requests-2317` | passed | Native string handling fixed in session method path |
| `sympy__sympy-11400` | passed | `ccode` printer fixed for `sinc` and relational printing |
| `sympy__sympy-11870` | passed | `sinc(...).rewrite(sin)` fixed to preserve the zero case |

### 4. `next_requests_pytest_8`

| Instance | Status | Notes |
| --- | --- | --- |
| `psf__requests-2674` | passed | `PoolError` wrapped into `ConnectionError` |
| `psf__requests-3362` | failed | Model stopped with no content during run |
| `psf__requests-863` | passed | Hook registration fixed for list-valued hooks |
| `pytest-dev__pytest-5413` | evaluation-blocked | Lite test patch behavior did not align cleanly with the issue goal |
| `pytest-dev__pytest-5495` | evaluation-blocked | Old `pytest` assertion rewrite incompatible with Python 3.13 AST validation |
| `pytest-dev__pytest-6116` | evaluation-blocked | Old `pytest` assertion rewrite incompatible with Python 3.13 AST validation |
| `pytest-dev__pytest-8906` | passed | Module-level `pytest.skip` guidance improved |
| `pytest-dev__pytest-9359` | passed | Statement-range detection fixed to include decorators |

### 5. `next_sympy_8`

| Instance | Status | Notes |
| --- | --- | --- |
| `sympy__sympy-11897` | passed | LaTeX multiplication now wraps `Piecewise` factors |
| `sympy__sympy-12171` | passed | Mathematica printer gained derivative support |
| `sympy__sympy-12419` | passed | `Identity._entry()` now preserves symbolic indices via `KroneckerDelta` |
| `sympy__sympy-12454` | passed | Upper-triangular / upper-Hessenberg checks fixed for tall matrices |
| `sympy__sympy-13043` | passed | `decompose(..., separate=True)` returns a set as expected |
| `sympy__sympy-13647` | passed | `col_insert` index calculation fixed |
| `sympy__sympy-13773` | passed | `__matmul__` / `__rmatmul__` now return `NotImplemented` for non-matrix operands |
| `sympy__sympy-14308` | failed | Pretty-print alignment for basis-dependent expressions still incorrect |

## Evaluation-Blocked Tasks

These tasks were explored and partially adapted, but were not counted as normal pass/fail outcomes.

### `pytest-dev__pytest-5413`

- Problem type: dataset target mismatch
- Reason:
  - The issue discussion suggests changing `str(excinfo)` behavior.
  - The Lite test patch in the cached dataset still expected the old representation behavior.
  - Because the issue target and the effective test target were not aligned cleanly, this sample was not counted as a fair agent failure.

### `pytest-dev__pytest-5495`

- Problem type: interpreter/runtime compatibility
- Reason:
  - The logic fix itself was straightforward and was patched locally.
  - However, old `pytest 4.6` assertion rewriting hit Python 3.13 AST validation failures such as invalid line metadata.
  - The final outcome was dominated by old-runtime incompatibility rather than task reasoning quality.

### `pytest-dev__pytest-6116`

- Problem type: interpreter/runtime compatibility
- Reason:
  - The task itself is small (`--collect-only` short option).
  - Validation was blocked by old `pytest 5.2` assertion rewrite behavior under Python 3.13.
  - This was treated as an evaluation-platform limitation, not a clean agent pass/fail.

## Key Takeaways

- PatchFlow now has meaningful evaluation coverage across:
  - `astropy`
  - `django`
  - `requests`
  - `sympy`
  - `pytest`
- The most reliable official SWE-bench results currently are:
  - `astropy` 5-task smoke test: `4/5`
  - `django` 4-task official run: `4/4`
- The main structural limitation in the current setup is not the agent loop itself, but:
  - old `pytest` repository/runtime compatibility under Python 3.13
  - occasional dataset-target mismatch in individual Lite samples

## Artifact Locations

- Official SWE-bench reports:
  - [patchflow-deepseek-v4-flash.patchflow-lite5-official-retry12.json](../SWE-bench/patchflow-deepseek-v4-flash.patchflow-lite5-official-retry12.json)
  - [patchflow-deepseek-v4-flash.patchflow-django4-official.json](../SWE-bench/patchflow-deepseek-v4-flash.patchflow-django4-official.json)
- Local task sets:
  - [lite_5](../swebench_runs/lite_5)
  - [django_4_official](../swebench_runs/django_4_official)
  - [next_requests_sympy_6](../swebench_runs/next_requests_sympy_6)
  - [next_requests_pytest_8](../swebench_runs/next_requests_pytest_8)
  - [next_sympy_8](../swebench_runs/next_sympy_8)
