Closes #

## What changed


## Verification

- [ ] `BASE=http://localhost:8016 bash tests/smoke.sh`
- [ ] `BASE=http://localhost:8016 pytest tests/test_queries.py tests/test_importance.py -v`
- [ ] `URL=http://localhost:8016 node tests/e2e_smoke.mjs`
- [ ] New/updated automated test added for this change
- [ ] CiC script run against :8016 — transcript pasted below (or `n/a`, backend only)

<details>
<summary>CiC transcript</summary>

```
```

</details>

## Deploy notes

- [ ] `sw.js` CACHE version bumped (required for any static asset change)
- [ ] Dashboard restart required (any template change — Jinja caches templates in prod)
- [ ] Rollback path confirmed, as stated in the issue

## Risk

<!-- Anything touching DuckDB write locks, the ingest cycle, or auth deserves a sentence here. -->
