# Ablation Report

- Artifact root: `logs/artifacts`
- Scope: `all-runs`
- Verification columns are total observed calls/rejections across each profile.
- Older artifacts without these metrics are backfilled from `events.jsonl` / `events.json` when available.

| Profile | Runs | Success Rate | First-pass | Avg Steps | Avg Tools | Avg Tokens | Patch Success | Verify Task | Targeted Tests | Broad Rejections | Failure Analyses | Edit Plans | Patch Review Fails |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| baseline | 15 | 93.33% | 0.00% | 4.8 | 4.733 | 38859.733 | 93.33% | 0 | 22 | 0 | 0 | 16 | 0 |
| full | 30 | 100.00% | 3.33% | 5.133 | 4.833 | 39643.5 | 96.67% | 10 | 33 | 0 | 13 | 31 | 0 |
| partial | 15 | 86.67% | 0.00% | 4.467 | 4.133 | 34180.933 | 89.66% | 0 | 18 | 0 | 6 | 16 | 0 |
