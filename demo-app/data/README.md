# Data

Five read-only tables. Synthetic, from jafgen (Apache-2.0). See `../../NOTICE`.

| Table | Rows | Added at level | Key columns |
| :--- | ---: | :---: | :--- |
| `orders` | 66,375 | 1 | `id`, `customer` → customers.id, `store_id` → stores.id, `ordered_at`, `subtotal`, `tax_paid`, `order_total` |
| `stores` | 6 | 2 | `id`, `name`, `opened_at`, `tax_rate` |
| `customers` | 2,583 | 3 | `id`, `name` |
| `order_items` | 99,753 | 4 | `id`, `order_id` → orders.id, `sku` → products.sku |
| `products` | 10 | 5 | `sku`, `name`, `type` (jaffle/beverage), `price` |

Money columns are integer **cents**. Orders span 2018-09-01 to 2021-08-30.

## Regenerate

jafgen has no seed, so a fresh run gives different rows. The committed CSVs are the reference.

```bash
uvx --python 3.12 jafgen@0.4.14 3
python demo-app/data/prepare.py ./jaffle-data demo-app/data
```

## SHA-256 of committed files

```text
efa0612fe82cd5b88803356fc99c8df7d7a5c120cc81010a519fc8d376ebe1fc  customers.csv
75b8495eb0de0c5cfbb89e48035e7872d841ed60c5c3759263dcd50984e91012  order_items.csv
d606df9c7fec492774b9053a18d17bb4fb1859b21b0cb8f6c222a2affa9ab791  orders.csv
6449dda1dddac23213506d757a79ac8c0680b4a38c37a7fc33bfb02d82392ee5  products.csv
6ae2f17ca59c8733b33f421be137ed16ca39278e7280855f3c2a253aa5b57506  stores.csv
```
