# Cloud Run deployment — TODO

Local first. Nothing here is implemented yet.

## Plan

- One image, `LEVEL` env var, five URLs (one per level).
- Verify first: do min-instances apply to tagged revisions with 0% traffic? If not, deploy five services from the same image (`dw-level-1` … `dw-level-5`).
- `--min-instances=1 --max-instances=1` per level: in-memory stats and the A2A cache stay consistent; no cold starts in timings. Scale to zero after sweeps.

## Security and cost (review notes)

- [ ] Private by default: `--no-allow-unauthenticated`; invoker role only for the owner and the harness service account.
- [ ] Harness mints fresh ID tokens per call (they expire after ~1 h). Manual tools use `gcloud run services proxy` on localhost — no tokens in tool configs.
- [ ] Hard spend cap: Vertex quota override (requests/day) on the model. A budget alert alone only emails.
- [ ] `ADMIN_TOKEN` from Secret Manager for `/_stats` and `/_reset` (already compared in constant time when set).
- [ ] `PUBLIC_DEMO=true` (talks only): unauthenticated, A2A rate-limited per IP + global daily cap, `/_reset` disabled.
- [ ] Dedicated least-privilege service account: `roles/aiplatform.user` + access to the one secret.
- [ ] Project ID, `*.run.app` URLs, service-account emails only from env vars. Scrub `Authorization` headers from logs.
- [ ] `PUBLIC_URL` → pre-built AgentCard with the public URL (see `TODO(deploy)` in `demo-app/warehouse/app.py`).
- [ ] `OTEL_EXPORTER=gcp` → Cloud Trace exporter (see `TODO(deploy)` in `demo-app/warehouse/telemetry.py`).
