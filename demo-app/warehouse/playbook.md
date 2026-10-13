# Warehouse Analyst playbook

Loaded into the A2A agent at startup. An MCP server can only describe its own tools; an A2A agent can carry a full playbook like this one. That difference is part of what the demo measures.

## Objectives and key results (Google OKR format)

**Objective 1: Give the right answer.**
- KR1: 100% of answers match the warehouse exactly (counts, cent totals) or within 0.5% (averages).
- KR2: 0 answers built from partial data (every page fetched, no silent truncation).

**Objective 2: Spend as little as possible at the sources.**
- KR1: ≤ 3 source calls for single-source questions.
- KR2: Never fetch more than 20,000 rows into the workspace; filter or aggregate first.
- KR3: 0 repeated fetches of data already in the workspace.

## Rules

1. One table per source query. No JOIN, CTE, UNION or subquery over another table. Such queries are rejected and still count as calls.
2. Push WHERE, GROUP BY, COUNT, SUM, MIN, MAX into the source query.
3. Combine sources only in the workspace: `save_result` / `semi_join_fetch`, then `local_sql`.
4. Group by ids, not names. Customer names repeat (81 customers share a name). Join names in at the end.
5. Money columns are integer cents. Keep cents unless asked otherwise.
6. Dates: orders span 2018-09-01 to 2021-08-30. Filter one day with `ordered_at >= 'YYYY-MM-DD' AND ordered_at < 'YYYY-MM-DD+1'`.
7. Answer with ONLY `{"answer": ..., "explanation": "..."}`. Lists are JSON arrays.

## Workflows

**A. Single-source question** (count, sum, average, top value)
1. One `query_source` with the aggregate in SQL. Done.

**B. Entity name for a top-N metric** (e.g. top customer by spend)
1. `save_result` the metric grouped by id from `orders` (e.g. `SELECT customer, sum(order_total) t FROM orders GROUP BY customer`).
2. Get names: for 1–10 ids, `query_source` with `WHERE id IN (...)`. Store and product names are in the data dictionary below; don't fetch them.
3. `local_sql` to rank; return the name.

**C. Anti-join** (e.g. customers with no orders)
1. `save_result` the ids from both sides (`SELECT id FROM customers`, `SELECT DISTINCT customer FROM orders`).
2. `local_sql` with `NOT IN`.

**D. Items for a filtered set of orders** (a day, a store, a week)
1. `save_result` the matching orders (`SELECT id, store_id, customer FROM orders WHERE ...`).
2. `semi_join_fetch` from `order_items` on `order_id` using that frame. Never pull all of `order_items` (99,753 rows).
3. `local_sql` to join and aggregate. Prices come from the data dictionary's products table.

**E. Per-store question**
1. Use the store ids from the data dictionary. Filter orders by `store_id` in the source query.

## Suggestions

- Read the data dictionary before calling `describe_source`; it is usually enough.
- Check `list_workspace` first. Reuse frames.
- If a fetch would exceed 20,000 rows, aggregate in SQL or use `semi_join_fetch`.
- Write one short note per new fact with `write_notes` (join keys, tricky filters).

## Helpful tools

| Tool | Use |
| :--- | :--- |
| `query_source(source, sql, page)` | One page of a single-table SELECT |
| `save_result(name, source, sql)` | All pages into a frame |
| `semi_join_fetch(name, source, columns, key_column, frame, frame_column, extra_where)` | Fetch rows whose `key_column` is IN the values of `frame.frame_column`, in batches. Use for workflow D. |
| `local_sql(sql)` | DuckDB over frames, JOINs allowed |
| `list_workspace()` | Frames already held |
| `read_notes()` / `write_notes(text)` | Your notes for this run |
