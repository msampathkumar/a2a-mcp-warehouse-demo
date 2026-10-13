# Changelog

All notable changes to this project. Versions follow [Semantic Versioning](https://semver.org/).

## [0.2.0-beta] — 2026-10-13

First public release. Beta: the method and numbers may change as more trials run.

### Added

- `demo-app/`: FastAPI app per level with one MCP server per table, one Google ADK A2A agent, a homepage and an AI Catalog (`/catalog.json`).
- DataManager: one table per query, enforced by `sqlglot` and a locked read-only DuckDB; snapshot paging; simulated per-source latency.
- A2A agent playbook (`demo-app/warehouse/playbook.md`): OKRs, rules, workflows, a live data dictionary, and the `semi_join_fetch` helper.
- `test-app/` harness: scripted ADK client agents for both paths, answer key built with DuckDB joins, scoring, reports, summary, statistics and charts.
- OpenTelemetry tracing end to end, with W3C `traceparent` + `baggage` propagation; server-side event counts.
- Results: sweep v2 (5 levels × 10 questions × 5 trials × 2 paths = 500 runs) and sweep v1 (1 trial, no playbook), with raw evidence in `docs/results/`.
- Docs: `docs/report.md`, `docs/blog.md` (draft), `docs/charts/`, `docs/questions.md`.
- CI: tests, data checksum check, and answer-key reproducibility check on every push and pull request.

### Fixed during development

- Unstable `LIMIT/OFFSET` paging without `ORDER BY`; affected v1 runs were discarded and re-run.
- JSON answer parsing for repeated or Python-style objects; affected attempts are re-scored from raw replies and listed in each batch report.
- Harness errors are truncated to a short, public-safe message.

### Known limitations

- Local runs only; Cloud Run deployment is a TODO (`deploy/TODO.md`).
- See `docs/report.md#limitations`.
