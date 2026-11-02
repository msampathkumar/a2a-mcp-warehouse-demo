# Cloud Run Infrastructure: 5 Isolated Data Warehouses & MCP Servers (`infra/`)

Deploys **5 MCP servers**, each connected to its own **isolated local DuckDB data warehouse** (`orders.duckdb`, `stores.duckdb`, `customers.duckdb`, `order_items.duckdb`, `products.duckdb`), along with a live telemetry homepage and structured Google Cloud Logging.

## Architecture

1. **5 Isolated Local Data Warehouses (`ISOLATED_WAREHOUSES=true`)**:
   - At startup, `DataManager` builds 5 separate read-only DuckDB database files in local storage—one per CSV table:
     - `orders.duckdb` → served only by `/mcp/orders/` (`describe_orders`, `query_orders`)
     - `stores.duckdb` → served only by `/mcp/stores/` (`describe_stores`, `query_stores`)
     - `customers.duckdb` → served only by `/mcp/customers/` (`describe_customers`, `query_customers`)
     - `order_items.duckdb` → served only by `/mcp/order_items/` (`describe_order_items`, `query_order_items`)
     - `products.duckdb` → served only by `/mcp/products/` (`describe_products`, `query_products`)
   - Each DuckDB connection is opened `read_only=True` with `enable_external_access=False` and `SET lock_configuration = true`. Even if SQL validation were bypassed, `orders.duckdb` physically does not contain the other 4 tables.

2. **Zero-Load, BigQuery-Backed Homepage Metrics (`/metrics.json`)**:
   - `Recorder` maintains O(1) in-memory atomic counters for:
     - **Total MCP Calls** (all-time)
     - **Calls in the Last 7 Days** (rolling 7-day UTC window)
     - **Calls in the Last 24 Hours** (rolling 24-hour window)
     - **Per-Warehouse Breakdown** (total calls, 7-day calls, `describe` vs `query` calls, rows returned, errors, average latency in ms)
   - **Durable across Cloud Run scale-to-zero (`BQ_TELEMETRY_TABLE=a2a-practical-demos.dw_telemetry.mcp_events`)**:
     - On container startup (cold start), `Recorder` executes a single BigQuery hydration query to restore all historical totals, 7-day daily series, 24-hour window timestamps, and per-warehouse statistics into memory.
     - Every new MCP `describe`/`query` call increments the in-memory counter immediately in O(1) and streams the event row asynchronously to BigQuery (`tabledata.insertAll`) via a background daemon thread.
     - Refreshing the homepage reads `/metrics.json` from memory in `< 1 ms` and **never queries DuckDB or BigQuery**.

3. **Structured Cloud Logging & Trace Correlation (`CLOUD_LOGGING=true`)**:
   - Every MCP `describe`/`query` event and every OpenTelemetry span is emitted to `stdout` as single-line structured JSON with:
     - `severity` (`INFO` / `WARNING`)
     - `logging.googleapis.com/trace` correlated with `projects/gemini-agent-practical-demos/traces/<trace_id>`
     - Full telemetry fields (`telemetry_type`, `source`, `op`, `sql`, `rows`, `ms`, `ok`, `error`, `run_id`, `q_id`).

4. **Custom Domain + Folder Path Ready**:
   - All homepage asset fetches (`catalog.json`, `tally.json`, `metrics.json`) use relative URLs so the app works seamlessly when mounted behind a reverse proxy or CDN under a dedicated folder path (e.g. `https://yourdomain.com/warehouses/`).
   - Set `PUBLIC_URL="https://yourdomain.com/warehouses"` when running `./infra/deploy.sh` so `/catalog.json` advertises the exact external MCP URLs.
