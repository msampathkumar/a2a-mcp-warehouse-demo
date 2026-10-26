"""Build the A2A agent's playbook: playbook.md + a live data dictionary.

The data dictionary is read once at startup, straight from DuckDB (not via
DataManager calls), for the tables of the current level: columns, row counts,
date range, and the full content of the small tables (stores, products).
"""

from __future__ import annotations

from pathlib import Path

SMALL_TABLE_ROWS = 20
JOIN_KEYS = {
    "orders": "id (order) · customer -> customers.id · store_id -> stores.id",
    "stores": "id -> orders.store_id",
    "customers": "id -> orders.customer",
    "order_items": "order_id -> orders.id · sku -> products.sku",
    "products": "sku -> order_items.sku",
}


def build_playbook(settings, dm) -> str:
    text = (Path(__file__).parent / "playbook.md").read_text()
    lines = ["", "## Data dictionary (this level)", ""]
    for t in dm.tables:
        con = dm._cons[t].cursor()
        cols = ", ".join(f"{c} {ty}" for c, ty in dm._schemas[t])
        lines.append(f"### {t} — {dm._row_counts[t]:,} rows")
        lines.append(f"- Columns: {cols}")
        lines.append(f"- Join keys: {JOIN_KEYS[t]}")
        if dm._row_counts[t] <= SMALL_TABLE_ROWS:
            rows = con.execute(f'SELECT * FROM "{t}"').fetchall()
            names = [c for c, _ in dm._schemas[t]]
            lines.append("- Full content (preloaded):")
            lines.append("")
            lines.append("| " + " | ".join(names) + " |")
            lines.append("|" + "---|" * len(names))
            for r in rows:
                lines.append("| " + " | ".join(str(v) for v in r) + " |")
        lines.append("")
    if "orders" in dm.tables:
        lo, hi = dm._cons["orders"].cursor().execute("SELECT min(ordered_at), max(ordered_at) FROM orders").fetchone()
        lines.append(f"orders.ordered_at range: {lo} to {hi}.")
    return text + "\n".join(lines)
