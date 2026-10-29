"""Secondary index vs full scan.   python bench_index.py"""
import os, random, time
from pydb.table import Table
from pydb.schema import Schema

PATH, N_ROWS, N_LOOKUPS = "bench_index.db", 50_000, 200
SCHEMA = Schema.parse(["id:int", "email:text(64)", "age:int", "city:text(16)"])
CITIES = ["Austin", "Boston", "Chicago", "Denver", "Seattle"]

if os.path.exists(PATH):
    os.remove(PATH)
rnd = random.Random(1)
ids = rnd.sample(range(10_000_000), N_ROWS)
t = Table.create(PATH, SCHEMA, cache_pages=4096)
with t.transaction():                      # one commit for the whole load
    for i in ids:
        t.insert((i, f"user{i}@example.com", rnd.randint(18, 80), rnd.choice(CITIES)))
emails = [f"user{i}@example.com" for i in random.Random(2).choices(ids, k=N_LOOKUPS)]


def run(label):
    p = t.pager
    before = p.hits + p.misses
    start = time.perf_counter()
    for e in emails:
        assert len(t.find_by("email", e)) == 1
    ms = (time.perf_counter() - start) / N_LOOKUPS * 1000
    pages = (p.hits + p.misses - before) / N_LOOKUPS
    print(f"{label:<22} {ms:>9.3f} ms/lookup {pages:>10,.0f} pages/lookup   ({t.last_lookup})")
    return ms


print(f"{N_ROWS:,} rows, {N_LOOKUPS} lookups by email\n")
slow = run("without index")
start = time.perf_counter()
t.create_index("email")
print(f"\n(building the index took {time.perf_counter() - start:.1f}s)\n")
fast = run("with index")
print(f"\nSpeedup: {slow / fast:,.0f}x")
t.close()
os.remove(PATH)
