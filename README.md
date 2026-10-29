# pydb: a relational database built from scratch in Python

![tests](https://github.com/<your-username>/pydb/actions/workflows/tests.yml/badge.svg)

pydb is a small embedded database written from first principles, in about 1,850 lines of plain Python with no dependencies. It stores data in 4 KB pages, indexes it with B+Trees, runs SQL through its own parser and query planner, supports transactions, and survives crashes using a write-ahead log.

It started as a Python take on [testudo](https://github.com/sepgh/testudo), a Java embedded database. pydb goes further in two areas testudo leaves unfinished: transactions and querying non-indexed fields.

```
$ python -m pydb shop.db
sql> CREATE TABLE customers (id INT PRIMARY KEY, name TEXT(32) NOT NULL, city TEXT(16), spend FLOAT);
sql> INSERT INTO customers VALUES (1, 'Ana', 'Austin', 1250.5), (2, 'Bob', NULL, 310), (3, 'Eve', 'Austin', 2210);
sql> SELECT name, spend FROM customers WHERE city = 'Austin' ORDER BY spend DESC;
name | spend
-----+-------
Eve  | 2210.0
Ana  | 1250.5
(2 rows)
sql> EXPLAIN SELECT * FROM customers WHERE city = 'Austin';
SCAN customers (reads every row: 3)
sql> CREATE INDEX idx_city ON customers (city);
sql> EXPLAIN SELECT * FROM customers WHERE city = 'Austin';
SEARCH customers USING INDEX ON city (city = 'Austin')
```

## Features

| | |
|---|---|
| **SQL** | `CREATE TABLE`, `CREATE INDEX`, `INSERT` (multi-row), `SELECT` with `WHERE` / `ORDER BY` / `LIMIT` / `COUNT(*)`, `BETWEEN`, `IS [NOT] NULL`, `AND` / `OR` / `NOT`, `EXPLAIN` |
| **Query planner** | Chooses a primary-key lookup, a primary-key range scan, a secondary-index lookup, or a full scan |
| **Transactions** | `BEGIN` / `COMMIT` / `ROLLBACK`; every statement is atomic; a failed multi-row insert is fully undone |
| **Crash safety** | Write-ahead log with CRC32-checked commit records, replayed on open |
| **Concurrency** | Writer-preferring reader-writer lock, so many readers can run at once and writers get exclusive access |
| **Storage** | B+Tree over 4 KB pages; rows sorted by primary key; leaves linked for range scans |
| **Secondary indexes** | On any column, non-unique and NULL-aware; kept in sync on every insert |
| **Types** | `INT`, `FLOAT`, `BOOL`, `TEXT(n)`, with NULL support via a per-row null bitmap |
| **Buffer pool** | LRU page cache with a configurable size, plus hit-rate statistics |

## Architecture

Each layer only talks to the one below it.

```
 ┌─────────────────────────────────────────────────────────────┐
 │  SQL shell            __main__.py                           │  statements, .read, .schema, .wal ...
 ├─────────────────────────────────────────────────────────────┤
 │  Parser               sql_parser.py                         │  text → tokens → AST (recursive descent)
 │  Planner + executor   sql_exec.py                           │  pick access path, evaluate WHERE (3-valued logic)
 ├─────────────────────────────────────────────────────────────┤
 │  Table                table.py                              │  rows, indexes, transactions, locking
 │  Schema               schema.py                             │  typed columns, null bitmap, validation
 ├─────────────────────────────────────────────────────────────┤
 │  B+Tree               btree.py                              │  fixed-size byte keys → byte values
 │  Key encoding         keys.py                               │  values → bytes that sort correctly
 ├─────────────────────────────────────────────────────────────┤
 │  Pager                pager.py                              │  4 KB pages, LRU cache, commit / rollback
 │  Write-ahead log      wal.py                                │  durable commits, crash recovery
 │  Reader-writer lock   rwlock.py                             │  thread safety
 └─────────────────────────────────────────────────────────────┘
          my.db  (page 0 = header + schema, then tree pages)      my.db-wal  (log)
```

## Design decisions

**Fixed-width rows.** Every column has a fixed byte width (for example, `TEXT(32)` always takes 32 bytes), so every row in a table is the same size. This keeps page arithmetic simple and lets the B+Tree calculate cell positions directly. The trade-off is wasted space for short strings.

**Order-preserving keys.** The B+Tree compares keys as raw bytes, so every value is encoded so that byte order matches value order. Integers are written big-endian with the sign bit flipped (so negative numbers sort first), floats use the IEEE-754 bit trick, and text is padded with zero bytes. A secondary-index key is `null-flag | value | primary key`. Appending the primary key makes duplicate values unique, and putting the null flag first groups NULLs together.

**No-steal buffer pool.** A page changed by an uncommitted transaction is never evicted, so half-finished changes can't reach the database file. I found and fixed a related bug during development. If the B+Tree held a page that the cache evicted in the middle of an insert, the parent's new separator key was silently lost: in testing, `find()` failed for 2,982 of 3,000 rows while a plain scan still looked correct. The fix is for `mark_dirty()` to take the page buffer itself, so an evicted page gets put back into the cache instead of being dropped.

**Write-ahead log.** A commit writes full images of every changed page to `my.db-wal`, followed by a CRC32-checked commit record, then calls `fsync`. Only after that are the pages copied into `my.db`. On open, every committed transaction in the log is replayed, and anything after the last valid commit record is discarded. A checkpoint (`fsync` the database file, then empty the log) runs every 1,000 page frames.

**Writer-preferring lock.** Once a writer is waiting, new readers queue behind it, so a steady stream of reads can't starve writes indefinitely. Table methods take the lock once and call `_unlocked` helpers internally, which avoids self-deadlock.

**Planner versus filter.** The planner only decides *how* candidate rows are found. The full `WHERE` clause is always evaluated afterwards, so choosing a different plan can change the speed of a query but never its result. The test suite checks this against brute force on 750 random queries.

## Results

Measured on a Windows laptop.

**Crash safety:** `crash_demo.py` kills the writer process at random moments 30 times.
```
30 crashes, 3962 transactions replayed from the log, 0 rows lost, 0 corruption.
```

**Secondary index vs. full scan:** `bench_index.py`, 50,000 rows, lookup by email.
```
without index    93.361 ms/lookup    1,803 pages/lookup   (full scan)
with index        0.157 ms/lookup       12 pages/lookup   (index on email)
Speedup: 595x
```

**Buffer pool size:** `bench.py`, 20,000 random lookups on 50,000 rows (5,453 pages).
```
 cache pages   hit rate   disk reads   time
           8      69.8%       36,236   4.11 s
          64      82.7%       20,755   3.27 s
       4,096      94.1%        7,082   2.66 s
     100,000      95.6%        5,285   2.33 s
```

**Cost of durability:** `bench_commit.py`.
```
2,000 inserts, one commit each :   1.96s  (   1,021 rows/s)
2,000 inserts, one transaction :   0.27s  (   7,359 rows/s)
Batching is 7x faster.
```

**Concurrency:** `race_demo.py`, 8 threads each inserting 400 rows into one table.
```
locking OFF:  row counter 3200/3200, findable 3117, seen by select 852, in index 2215   ← silent corruption
locking ON :  row counter 3200/3200, findable 3200, seen by select 3200, in index 3200
```

## Quick start

Requires Python 3.10 or later. The database itself has no dependencies; pytest is used only for the tests.

```bash
git clone https://github.com/<your-username>/pydb.git
cd pydb
python -m pydb shop.db          # then type:  .read demo.sql
```

```bash
pip install -r requirements.txt
python -m pytest -q             # 71 tests
python crash_demo.py            # kill the database 30 times and verify nothing is lost
python bench_index.py           # index vs. full scan
```

Using it from Python:

```python
from pydb.sql_exec import Session, format_result

db = Session("app.db")
db.execute("CREATE TABLE users (id INT PRIMARY KEY, name TEXT(32) NOT NULL, age INT)")
db.execute("INSERT INTO users VALUES (1, 'Ana', 31), (2, 'Bob', 25)")
print(format_result(db.execute("SELECT * FROM users WHERE age > 30")))
db.close()
```

## Tests

71 tests cover:
- **Storage:** row and page encoding, persistence across reopen, B+Tree splits at every level (stress-tested with node sizes forced down to 2 keys and trees up to 10 levels deep).
- **Buffer pool:** correctness with a 1-page cache.
- **Keys:** order-preserving encoding for ints, floats, and text.
- **Indexes:** results always match a full scan.
- **Concurrency:** the reader-writer lock's guarantees, and threads inserting and reading together with nothing lost.
- **Crash recovery:** a real process is killed at each point in the commit protocol, and at random moments.
- **SQL:** parser errors, three-valued NULL logic, plan selection, and the planner-vs-brute-force check.

CI runs the full suite on Linux and Windows.

## Limitations and next steps

- **No `UPDATE` or `DELETE` yet.** These need B+Tree deletion with node merging or rebalancing.
- **One table per database file; no `JOIN`.**
- **Fixed-width text,** with no overflow pages for long values.
- **The primary key must be an unsigned 32-bit `INT`.**
- **Large transactions are limited by memory,** because under the no-steal policy every changed page stays in memory until commit.
- **Not built for speed.** It's pure Python, and the goal is clarity rather than throughput.

## Project layout

```
pydb/
  __main__.py     SQL shell
  sql_parser.py   tokenizer + recursive-descent parser
  sql_exec.py     query planner, executor, result formatting
  table.py        table, indexes, transactions
  schema.py       column types, row encoding, null bitmap
  btree.py        B+Tree
  keys.py         order-preserving key encoding
  pager.py        page cache + commit / rollback / recovery
  wal.py          write-ahead log
  rwlock.py       reader-writer lock
tests/test_db.py  test suite
demo.sql          example session
bench*.py, crash_demo.py, race_demo.py   benchmarks and demonstrations
```
