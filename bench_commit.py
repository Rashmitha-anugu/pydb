"""What does durability cost?   python bench_commit.py

Every commit waits for fsync: the operating system must confirm the log is
physically on disk. Grouping inserts into one transaction pays that cost once.
"""
import os, time
from pydb.table import Table
from pydb.schema import Schema

SCHEMA = Schema.parse(["id:int", "name:text(32)"])
N = 2_000


def fresh(path):
    for f in (path, path + "-wal"):
        if os.path.exists(f):
            os.remove(f)
    return Table.create(path, SCHEMA)


t = fresh("bench_commit.db")
start = time.perf_counter()
for i in range(N):
    t.insert((i, f"user{i}"))                       # one commit (and one fsync) each
one_each = time.perf_counter() - start
t.close()

t = fresh("bench_commit.db")
start = time.perf_counter()
with t.transaction():
    for i in range(N):
        t.insert((i, f"user{i}"))                   # one commit for all of them
batched = time.perf_counter() - start
t.close()
for f in ("bench_commit.db", "bench_commit.db-wal"):
    os.remove(f)

print(f"{N:,} inserts, one commit each : {one_each:6.2f}s  ({N / one_each:>8,.0f} rows/s)")
print(f"{N:,} inserts, one transaction : {batched:6.2f}s  ({N / batched:>8,.0f} rows/s)")
print(f"Batching is {one_each / batched:.0f}x faster.")
