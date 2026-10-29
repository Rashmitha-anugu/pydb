"""What goes wrong WITHOUT locks?   python race_demo.py

8 threads each insert 400 rows (plus some reads) into the same table, once with
locking switched off and once with it on, then we check what actually survived.
"""
import os, random, sys, threading, time
from pydb.table import Table
from pydb.schema import Schema

SCHEMA = Schema.parse(["id:int", "name:text(16)", "city:text(16)?"])
THREADS, PER_THREAD = 8, 400


def run(locking: bool):
    path = f"race_{'on' if locking else 'off'}.db"
    if os.path.exists(path):
        os.remove(path)
    t = Table.create(path, SCHEMA, cache_pages=16, locking=locking)
    t.create_index("city")

    def worker(w):
        rnd = random.Random(w)
        for k in range(PER_THREAD):
            i = w * 100_000 + k
            t.insert((i, f"n{i}", rnd.choice(["A", "B", "C"])))
            if k % 10 == 0:
                t.find(i)
                t.find_by("city", "A")

    threads = [threading.Thread(target=worker, args=(w,)) for w in range(THREADS)]
    start = time.perf_counter()
    for th in threads:
        th.start()
    for th in threads:
        th.join()
    elapsed = time.perf_counter() - start

    expected = THREADS * PER_THREAD
    findable = sum(1 for w in range(THREADS) for k in range(PER_THREAD) if t.find(w * 100_000 + k))
    scanned = len(t.select_all())
    in_index = sum(len(t.find_by("city", c)) for c in "ABC")
    print(f"locking {'ON ' if locking else 'OFF'}  ({elapsed:.1f}s)")
    print(f"  row counter says     {t.num_rows:>5} / {expected}")
    print(f"  findable by id       {findable:>5} / {expected}")
    print(f"  seen by select       {scanned:>5} / {expected}")
    print(f"  entries in index     {in_index:>5} / {expected}\n")
    t.close()
    os.remove(path)


sys.setswitchinterval(1e-5)     # make threads switch very often, so races show up quickly
run(locking=False)
run(locking=True)
