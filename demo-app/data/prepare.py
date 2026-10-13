"""Build the demo's 5 CSV tables from raw jafgen output.

jafgen has no seed, so we generate once and commit the result. This script is
the deterministic part: same jafgen input -> same CSVs.

Usage:
    uvx --python 3.12 jafgen@0.4.14 3          # writes ./jaffle-data/raw_*.csv
    python demo-app/data/prepare.py ./jaffle-data demo-app/data

Modifications to jafgen output (see NOTICE):
  * keep 10% of orders, chosen by md5(order id) so the sample is stable;
  * keep only order items belonging to kept orders;
  * rename raw_items -> order_items and drop the raw_ prefix;
  * drop raw_supplies and raw_tweets (not used by the demo).
"""

import sys
from pathlib import Path

import duckdb

SAMPLE_MODULO = 10  # keep 1 in 10 orders


def main(src: Path, dst: Path) -> None:
    con = duckdb.connect()
    con.execute(
        f"CREATE TABLE raw_orders AS SELECT * FROM read_csv_auto('{src}/raw_orders.csv')"
    )
    con.execute(
        'CREATE TABLE orders AS SELECT * EXCLUDE (h) FROM ('
        "  SELECT *, (('0x' || substr(md5(id), 1, 8))::BIGINT % "
        f'{SAMPLE_MODULO}) AS h FROM raw_orders) WHERE h = 0 ORDER BY ordered_at, id'
    )
    con.execute(
        f"CREATE TABLE order_items AS SELECT i.* FROM read_csv_auto('{src}/raw_items.csv') i "
        'JOIN orders o ON i.order_id = o.id ORDER BY i.order_id, i.id'
    )
    dst.mkdir(parents=True, exist_ok=True)
    outputs = {
        'orders': 'SELECT * FROM orders',
        'order_items': 'SELECT * FROM order_items',
        'customers': f"SELECT * FROM read_csv_auto('{src}/raw_customers.csv') ORDER BY id",
        'stores': f"SELECT * FROM read_csv_auto('{src}/raw_stores.csv') ORDER BY opened_at",
        'products': f"SELECT * FROM read_csv_auto('{src}/raw_products.csv') ORDER BY sku",
    }
    for name, sql in outputs.items():
        out = dst / f'{name}.csv'
        con.execute(f"COPY ({sql}) TO '{out}' (HEADER, DELIMITER ',')")
        n = con.execute(f'SELECT count(*) FROM ({sql})').fetchone()[0]
        print(f'{name:12s} {n:>8,d} rows -> {out}')


if __name__ == '__main__':
    if len(sys.argv) != 3:
        sys.exit(__doc__)
    main(Path(sys.argv[1]), Path(sys.argv[2]))
