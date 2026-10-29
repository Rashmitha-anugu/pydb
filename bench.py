"""How does cache size affect lookups?   python bench.py"""
import os, random, time
from pydb.table import Table
from pydb.schema import Schema

PATH, N_ROWS, N_LOOKUPS = "bench.db", 50_000, 20_000
SCHEMA = Schema.parse(["id:int", "username:text(32)", "email:text(255)"])

if os.path.exists(PATH):
    os.remove(PATH)
ids = random.Random(1).sample(range(10_000_000), N_ROWS)
t = Table.create(PATH, SCHEMA, cache_pages=100_000)
start = time.perf_counter()
for i in ids:
    t.insert((i, f"user{i}", f"u{i}@x.com"))
t.close()
print(f"Inserted {N_ROWS:,} rows in {time.perf_counter() - start:.1f}s "
      f"({os.path.getsize(PATH) // 4096:,} pages, {os.path.getsize(PATH) / 1e6:.1f} MB)\n")

lookups = random.Random(2).choices(ids, k=N_LOOKUPS)
print(f"{'cache pages':>12} {'cache MB':>9} {'hit rate':>9} {'disk reads':>11} {'time (s)':>9}")
for size in (8, 64, 512, 4096, 100_000):
    t = Table.open(PATH, size)
    start = time.perf_counter()
    for i in lookups:
        t.find(i)
    elapsed = time.perf_counter() - start
    p = t.pager
    print(f"{size:>12,} {size * 4096 / 1e6:>9.1f} {p.hit_rate():>9.1%} {p.reads:>11,} {elapsed:>9.2f}")
    t.close()
os.remove(PATH)
