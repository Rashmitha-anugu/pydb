"""Pull the plug 30 times.   python crash_demo.py

A child process inserts rows as fast as it can and prints each id AFTER the insert
returns (i.e. once it is committed). We kill it at a random moment, reopen the
database, and check: every confirmed row is there, and the tree and the index are
not corrupted. Then we start writing again from where it left off.
"""
import os, random, subprocess, sys, time
from pydb.table import Table

PATH, KILLS = "crash_demo.db", 30
CHILD = r"""
import os, sys
from pydb.table import Table
from pydb.schema import Schema
path, start = sys.argv[1], int(sys.argv[2])
if os.path.exists(path) and os.path.getsize(path):
    t = Table.open(path, cache_pages=8)
else:
    t = Table.create(path, Schema.parse(["id:int", "name:text(16)", "city:text(16)?"]), cache_pages=8)
    t.create_index("city")
i = start
while True:
    t.insert((i, f"n{i}", "ABC"[i % 3]))
    print(i, flush=True)
    i += 1
"""

for f in (PATH, PATH + "-wal"):
    if os.path.exists(f):
        os.remove(f)

rnd = random.Random()
confirmed, next_id, replayed_total = set(), 0, 0
for k in range(1, KILLS + 1):
    proc = subprocess.Popen([sys.executable, "-c", CHILD, PATH, str(next_id)], stdout=subprocess.PIPE, text=True)
    time.sleep(rnd.uniform(0.2, 0.8))
    proc.kill()
    out, _ = proc.communicate()
    confirmed |= {int(x) for x in out.split()}
    wal_bytes = os.path.getsize(PATH + "-wal") if os.path.exists(PATH + "-wal") else 0

    t = Table.open(PATH)                                     # <- crash recovery runs here
    rows = t.select_all()
    ids = {r[0] for r in rows}
    ok = (len(rows) == t.num_rows
          and all(t.find(i) for i in ids)
          and sum(len(t.find_by("city", c)) for c in "ABC") == t.num_rows
          and confirmed <= ids)
    replayed_total += t.pager.recovered_transactions
    print(f"kill {k:>2}: {len(ids):>6} rows, {len(confirmed):>6} confirmed, "
          f"log had {wal_bytes / 1024:>6.0f} KB, replayed {t.pager.recovered_transactions:>4} txns  "
          f"{'OK' if ok else 'CORRUPT!'}")
    if not ok:
        sys.exit(1)
    next_id = max(ids) + 1 if ids else 0
    confirmed = ids
    t.close()

print(f"\n{KILLS} crashes, {replayed_total} transactions replayed from the log, 0 rows lost, 0 corruption.")
for f in (PATH, PATH + "-wal"):
    os.remove(f)
