---
id: amwd-4a0q
status: closed
deps: []
links: []
created: 2026-10-26T20:49:25Z
type: feature
priority: 1
assignee: Sampath Kumar
---
# Add Cloud Run deployment, single-warehouse isolation, and BigQuery-backed telemetry


## Notes

**2026-10-26T20:49:39Z**

Added Dockerfile, .gcloudignore, infra/deploy.sh, infra/README.md, single-warehouse isolation in data_manager.py/a2a_agent.py, live dashboard in index.html/app.py, and BigQuery-backed telemetry with cold-start hydration in telemetry.py; pytest demo-app/tests/: 36 passed; live Cloud Run revision dw-mcp-00004-xhf verified.
