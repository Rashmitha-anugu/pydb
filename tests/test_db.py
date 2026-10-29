import random
import pytest
from pydb.table import Table
from pydb.schema import Schema, SchemaError, MAX_ROW_SIZE
from pydb.btree import DuplicateKey

USERS = Schema.parse(["id:int", "name:text(32)", "email:text(255)?", "age:int?", "score:float", "active:bool"])


@pytest.fixture
def path(tmp_path):
    return str(tmp_path / "test.db")


def user(i):
    return (i, f"user{i}", f"u{i}@x.com", 20 + i % 50, i * 1.5, i % 2 == 0)


# ---------- schema: types, NULLs, validation ----------
def test_encode_decode_roundtrip():
    values = (7, "alice", "a@x.com", 30, 9.5, True)
    data = USERS.encode(values)
    assert len(data) == USERS.row_size
    assert USERS.decode(data) == values


def test_nulls_roundtrip():
    values = (8, "bob", None, None, 0.0, False)
    assert USERS.decode(USERS.encode(values)) == values


def test_null_in_non_nullable_column_rejected():
    with pytest.raises(SchemaError, match="cannot be NULL"):
        USERS.encode((1, None, None, None, 0.0, True))


@pytest.mark.parametrize("bad", [
    (1, "a", None, "thirty", 0.0, True),    # text in int column
    (1, "a", None, 3, 0.0, "yes"),          # text in bool column
    (1, "a", None, True, 0.0, True),        # bool is not an int
    (1, "x" * 33, None, 3, 0.0, True),      # text too long
    (1, "a", None, 3, 0.0),                 # too few values
    (-1, "a", None, 3, 0.0, True),          # negative primary key
])
def test_bad_values_rejected(bad):
    with pytest.raises(SchemaError):
        USERS.encode(bad)


def test_unicode_text_measured_in_bytes():
    s = Schema.parse(["id:int", "name:text(4)"])
    s.encode((1, "éé"))                      # 2 chars, 4 bytes: fits
    with pytest.raises(SchemaError):
        s.encode((1, "ééé"))                 # 3 chars, 6 bytes: too long


@pytest.mark.parametrize("specs", [
    ["name:text(10)"],                       # key must be int
    ["id:int?"],                             # key must not be nullable
    ["id:int", "id:bool"],                   # duplicate names
    ["id:int", "x:varchar"],                 # unknown type
    ["id:int", f"big:text({MAX_ROW_SIZE})"], # row too large
])
def test_bad_schemas_rejected(specs):
    with pytest.raises(SchemaError):
        Schema.parse(specs)


def test_parse_values_from_repl_strings():
    assert USERS.parse_values(["3", "Ana Silva", "null", "41", "2.5", "TRUE"]) == \
        (3, "Ana Silva", None, 41, 2.5, True)


# ---------- table on top of the B+Tree ----------
def test_schema_survives_reopen(path):
    t = Table.create(path, USERS)
    t.insert(user(1))
    t.close()
    t2 = Table.open(path)
    assert str(t2.schema) == str(USERS)
    assert t2.find(1) == user(1)


def test_row_size_sets_leaf_capacity(path, tmp_path):
    small = Table.create(path, Schema.parse(["id:int", "flag:bool"]))
    big = Table.create(str(tmp_path / "big.db"), USERS)
    assert small.tree.leaf_max > big.tree.leaf_max     # small rows: many more per page


def test_select_sorted_and_duplicates_rejected(path):
    t = Table.create(path, USERS)
    for i in [5, 3, 9, 1, 7]:
        t.insert(user(i))
    assert [r[0] for r in t.select_all()] == [1, 3, 5, 7, 9]
    with pytest.raises(DuplicateKey):
        t.insert(user(3))


def test_many_random_inserts_survive_reopen(path):
    ids = random.Random(42).sample(range(1_000_000), 20_000)
    t = Table.create(path, USERS)
    with t.transaction():
        for i in ids:
            t.insert(user(i))
    t.close()
    t2 = Table.open(path)
    assert t2.num_rows == 20_000
    assert t2.tree.depth() >= 3
    assert [r[0] for r in t2.select_all()] == sorted(ids)
    for i in ids[:500]:
        assert t2.find(i) == user(i)


def test_range_scan(path):
    t = Table.create(path, USERS)
    with t.transaction():
        for i in range(0, 1000, 2):
            t.insert(user(i))
    assert [r[0] for r in t.select_range(101, 111)] == [102, 104, 106, 108, 110]


# ---------- buffer pool still safe with the new row format ----------
@pytest.mark.parametrize("cache_pages", [1, 3])
def test_tiny_cache_never_loses_data(path, cache_pages):
    ids = random.Random(7).sample(range(1_000_000), 5_000)
    t = Table.create(path, USERS, cache_pages)
    for start in range(0, len(ids), 250):            # commit every 250 rows
        with t.transaction():
            for i in ids[start:start + 250]:
                t.insert(user(i))
    assert len(t.pager._cache) <= cache_pages        # dirty pages are evicted only after commit
    t.close()
    t2 = Table.open(path, cache_pages)
    assert [r[0] for r in t2.select_all()] == sorted(ids)
    for i in ids[:300]:
        assert t2.find(i) == user(i)


def test_bigger_cache_means_higher_hit_rate(path):
    ids = random.Random(1).sample(range(1_000_000), 10_000)
    t = Table.create(path, USERS, 4096)
    with t.transaction():
        for i in ids:
            t.insert(user(i))
    t.close()
    lookups = random.Random(2).choices(ids, k=3_000)
    rates = {}
    for size in (4, 64, 1024):
        t = Table.open(path, size)
        for i in lookups:
            t.find(i)
        rates[size] = t.pager.hit_rate()
        t.close()
    assert rates[4] < rates[64] < rates[1024]


# ---------- Milestone 6: secondary indexes ----------
from pydb.schema import Column
from pydb.keys import encode_value


def test_key_encoding_preserves_order():
    rnd = random.Random(3)
    ints = [rnd.randint(-2**63, 2**63 - 1) for _ in range(500)] + [-1, 0, 1]
    floats = [rnd.uniform(-1e9, 1e9) for _ in range(500)] + [-0.5, 0.0, 0.5, -1e-300, 1e-300]
    texts = ["", "a", "ab", "abc", "b", "Z", "zz", "é"] + [f"user{i}" for i in range(50)]
    for col, vals in [(Column("x", "int", 8), ints),
                      (Column("x", "float", 8), floats),
                      (Column("x", "text", 8), texts),
                      (Column("x", "bool", 1), [True, False])]:
        by_bytes = sorted(vals, key=lambda v: encode_value(col, v))
        assert by_bytes == sorted(vals, key=lambda v: v.encode() if isinstance(v, str) else v)


PEOPLE = Schema.parse(["id:int", "name:text(16)", "city:text(16)?", "age:int?", "score:float", "vip:bool"])
CITIES = ["Austin", "Boston", "Chicago", "Denver", None]


def person(i, rnd):
    return (i, f"p{i}", rnd.choice(CITIES), rnd.choice([None, *range(18, 30)]),
            round(rnd.uniform(-100, 100), 1), rnd.random() < 0.3)


def test_index_matches_full_scan(path):
    rnd = random.Random(11)
    t = Table.create(path, PEOPLE)
    ids = rnd.sample(range(100_000), 3_000)
    with t.transaction():
        for i in ids[:1_500]:
            t.insert(person(i, rnd))
    for col in ("city", "age", "score", "vip"):
        t.create_index(col)                        # back-fills the first 1,500 rows
    with t.transaction():
        for i in ids[1_500:]:
            t.insert(person(i, rnd))               # later rows go through insert()

    rows = list(t.select_all())
    for col in ("city", "age", "vip"):
        pos = PEOPLE.position(col)
        for value in {r[pos] for r in rows}:
            expected = sorted(r[0] for r in rows if r[pos] == value)
            got = t.find_by(col, value)
            assert t.last_lookup == f"index on {col}"
            assert sorted(r[0] for r in got) == expected


def test_index_survives_reopen(path):
    t = Table.create(path, PEOPLE)
    rnd = random.Random(5)
    with t.transaction():
        for i in range(500):
            t.insert(person(i, rnd))
    t.create_index("city")
    t.close()
    t2 = Table.open(path)
    assert "city" in t2.indexes
    boston = t2.find_by("city", "Boston")
    assert t2.last_lookup == "index on city"
    assert boston and all(r[2] == "Boston" for r in boston)
    t2.insert((10_000, "new", "Boston", 20, 1.0, False))          # index kept in sync after reopen
    assert 10_000 in [r[0] for r in t2.find_by("city", "Boston")]


def test_unindexed_column_uses_full_scan(path):
    t = Table.create(path, PEOPLE)
    t.insert((1, "a", "Austin", 20, 1.0, True))
    assert t.find_by("name", "a")[0][0] == 1
    assert t.last_lookup == "full scan"


def test_index_errors(path):
    t = Table.create(path, PEOPLE)
    with pytest.raises(SchemaError):
        t.create_index("id")                       # primary key
    with pytest.raises(SchemaError):
        t.create_index("nope")                     # unknown column
    t.create_index("city")
    with pytest.raises(SchemaError):
        t.create_index("city")                     # already exists


def test_duplicate_primary_key_does_not_touch_indexes(path):
    t = Table.create(path, PEOPLE)
    t.create_index("city")
    t.insert((1, "a", "Austin", 20, 1.0, True))
    with pytest.raises(DuplicateKey):
        t.insert((1, "b", "Boston", 21, 2.0, False))
    assert t.find_by("city", "Boston") == []


@pytest.mark.parametrize("cache_pages", [1, 3])
def test_indexes_with_tiny_cache(path, cache_pages):
    rnd = random.Random(9)
    t = Table.create(path, PEOPLE, cache_pages)
    t.create_index("city")
    t.create_index("age")
    rows = [person(i, rnd) for i in rnd.sample(range(100_000), 2_000)]
    for start in range(0, len(rows), 200):
        with t.transaction():
            for r in rows[start:start + 200]:
                t.insert(r)
    t.close()
    t2 = Table.open(path, cache_pages)
    expected = sorted(r[0] for r in rows if r[3] == 21)
    assert sorted(r[0] for r in t2.find_by("age", 21)) == expected


# ---------- Milestone 7: concurrency ----------
import sys
import threading
import time
from pydb.rwlock import RWLock


def test_rwlock_allows_concurrent_readers():
    lock = RWLock()
    barrier = threading.Barrier(4, timeout=5)      # only passes if all 4 are inside at once
    def reader():
        with lock.read():
            barrier.wait()
    ts = [threading.Thread(target=reader) for _ in range(4)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert not barrier.broken


def test_rwlock_writer_is_exclusive():
    lock, inside, problems = RWLock(), [0], []
    def reader():
        for _ in range(200):
            with lock.read():
                if inside[0] < 0:
                    problems.append("read during write")
    def writer():
        for _ in range(200):
            with lock.write():
                inside[0] -= 1                     # -1 = a writer is inside
                if inside[0] != -1:
                    problems.append("two writers")
                inside[0] += 1
    ts = [threading.Thread(target=f) for f in [reader, reader, writer, writer]]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert problems == []


def test_rwlock_waiting_writer_blocks_new_readers():
    lock, order = RWLock(), []
    first_reader_in, writer_waiting = threading.Event(), threading.Event()
    def first_reader():
        with lock.read():
            first_reader_in.set()
            writer_waiting.wait()
            time.sleep(0.1)                         # writer is queued during this
            order.append("reader1 done")
    def writer():
        first_reader_in.wait()
        writer_waiting.set()
        with lock.write():
            order.append("writer")
    def late_reader():
        writer_waiting.wait()
        time.sleep(0.05)                            # arrives while the writer is queued
        with lock.read():
            order.append("reader2")
    ts = [threading.Thread(target=f) for f in [first_reader, writer, late_reader]]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert order == ["reader1 done", "writer", "reader2"]   # writer was not starved


def test_concurrent_inserts_and_reads_lose_nothing(path):
    old = sys.getswitchinterval()
    sys.setswitchinterval(1e-5)                     # switch threads very often to provoke races
    try:
        t = Table.create(path, PEOPLE, cache_pages=16)
        t.create_index("city")
        errors = []
        def worker(w):
            rnd = random.Random(w)
            try:
                for k in range(150):
                    i = w * 10_000 + k
                    t.insert(person(i, rnd))
                    if k % 5 == 0:
                        assert t.find(i)[0] == i
                        t.find_by("city", "Boston")
            except Exception as e:
                errors.append(e)
        ts = [threading.Thread(target=worker, args=(w,)) for w in range(6)]
        [x.start() for x in ts]
        [x.join() for x in ts]
    finally:
        sys.setswitchinterval(old)
    assert errors == []
    expected = 6 * 150
    assert t.num_rows == expected
    assert len(t.select_all()) == expected
    assert sum(len(t.find_by("city", c)) for c in CITIES) == expected
    t.close()
    t2 = Table.open(path)
    assert len(t2.select_all()) == expected


# ---------- Milestone 8: write-ahead log, transactions, crash recovery ----------
import os
import subprocess
from pydb.table import TransactionError

SIMPLE = Schema.parse(["id:int", "name:text(16)", "city:text(16)?"])


def check_consistent(t):
    """Every structure agrees: counter, leaf chain, point lookups, index."""
    rows = t.select_all()
    assert len(rows) == t.num_rows
    assert [r[0] for r in rows] == sorted(r[0] for r in rows)
    for r in rows:
        assert t.find(r[0]) == r
    if "city" in t.indexes:
        assert sum(len(t.find_by("city", c)) for c in ("A", "B", "C", None)) == t.num_rows
    return {r[0] for r in rows}


def test_transaction_commit_and_rollback(path):
    t = Table.create(path, SIMPLE)
    t.create_index("city")
    with t.transaction():
        t.insert((1, "a", "A"))
        t.insert((2, "b", "B"))
    t.begin()
    t.insert((3, "c", "C"))
    t.insert((4, "d", "A"))
    t.rollback()                                       # 3 and 4 never happened
    assert check_consistent(t) == {1, 2}
    t.close()
    assert check_consistent(Table.open(path)) == {1, 2}


def test_failed_statement_inside_transaction_rolls_back_everything(path):
    t = Table.create(path, SIMPLE)
    t.insert((1, "a", "A"))
    with pytest.raises(DuplicateKey):
        with t.transaction():
            for i in range(2, 200):                    # enough rows to cause splits
                t.insert((i, f"n{i}", "B"))
            t.insert((1, "dup", "C"))                  # fails -> whole transaction undone
    assert check_consistent(t) == {1}
    assert t.tree.depth() == 1                         # the splits were undone too


def test_unclosed_transaction_is_discarded_on_close(path):
    t = Table.create(path, SIMPLE)
    t.insert((1, "a", "A"))
    t.begin()
    t.insert((2, "b", "B"))
    t.close()
    assert check_consistent(Table.open(path)) == {1}


def test_transaction_misuse(path):
    t = Table.create(path, SIMPLE)
    with pytest.raises(TransactionError):
        t.commit()
    t.begin()
    with pytest.raises(TransactionError):
        t.begin()
    t.rollback()


CRASH_CHILD = r"""
import os, sys
sys.path.insert(0, {root!r})
from pydb.table import Table
from pydb.schema import Schema
path, point, n = sys.argv[1], sys.argv[2], int(sys.argv[3])
t = Table.create(path, Schema.parse(["id:int", "name:text(16)", "city:text(16)?"]), cache_pages=8)
t.create_index("city")
for i in range(n):                                  # n committed rows (enough for splits)
    t.insert((i, f"n{{i}}", "ABC"[i % 3]))
def crash(p):
    if p == point:
        os._exit(1)                                 # the process dies instantly: no cleanup at all
t.pager.wal.crash_hook = crash
t.insert((n, "last", "A"))                          # dies part-way through this insert
"""


@pytest.mark.parametrize("point, last_row_survives", [
    ("wal_frames_written", False),   # log has the pages but no commit record -> discarded
    ("wal_committed", True),         # committed in the log, database file untouched -> replayed
    ("data_page_written", True),     # database file half-updated -> log replays the rest
])
def test_crash_at_each_point_recovers(path, point, last_row_survives):
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    n = 300
    proc = subprocess.run([sys.executable, "-c", CRASH_CHILD.format(root=root), path, point, str(n)])
    assert proc.returncode == 1                        # it really crashed
    t = Table.open(path)
    expected = set(range(n)) | ({n} if last_row_survives else set())
    assert check_consistent(t) == expected
    assert os.path.getsize(path + "-wal") == 0         # recovery folded the log into the file


KILL_CHILD = r"""
import sys
sys.path.insert(0, {root!r})
from pydb.table import Table
from pydb.schema import Schema
path, start = sys.argv[1], int(sys.argv[2])
schema = Schema.parse(["id:int", "name:text(16)", "city:text(16)?"])
import os
if os.path.exists(path) and os.path.getsize(path):
    t = Table.open(path, cache_pages=8)
else:
    t = Table.create(path, schema, cache_pages=8)
    t.create_index("city")
i = start
while True:
    t.insert((i, f"n{{i}}", "ABC"[i % 3]))
    print(i, flush=True)                            # printed => committed
    i += 1
"""


def test_random_kills_never_lose_committed_rows(path):
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    rnd = random.Random(4)
    confirmed, next_id = set(), 0
    for round_ in range(8):
        proc = subprocess.Popen([sys.executable, "-c", KILL_CHILD.format(root=root), path, str(next_id)],
                                stdout=subprocess.PIPE, text=True)
        time.sleep(rnd.uniform(0.3, 1.0))
        proc.kill()                                    # like pulling the plug on the process
        out, _ = proc.communicate()
        confirmed |= {int(x) for x in out.split()}
        t = Table.open(path)
        survived = check_consistent(t)                 # never corrupted
        assert confirmed <= survived                   # nothing committed is ever lost
        assert len(survived - confirmed) <= 1          # at most the row in flight at the kill
        next_id = max(survived) + 1 if survived else 0
        confirmed = survived
        t.close()
    assert len(confirmed) > 0


# ---------- Milestone 9: SQL ----------
from pydb.sql_parser import parse, SQLError, Compare, And, Or, Not, IsNull
from pydb.sql_exec import Session, evaluate
from pydb.__main__ import split_statements


def test_parser_precedence_and_flipping():
    w = parse("SELECT * FROM t WHERE a = 1 OR b = 2 AND NOT c > 3").where
    assert isinstance(w, Or) and isinstance(w.right, And) and isinstance(w.right.right, Not)
    assert parse("SELECT * FROM t WHERE 10 < id").where == Compare("id", ">", 10)
    assert parse("SELECT * FROM t WHERE x <> -2.5").where == Compare("x", "!=", -2.5)
    assert parse("INSERT INTO t VALUES (1, 'O''Brien', NULL, TRUE)").rows == [[1, "O'Brien", None, True]]


@pytest.mark.parametrize("bad", [
    "SELEC * FROM t", "SELECT * FROM", "SELECT * FROM t WHERE", "SELECT * FROM t WHERE a >",
    "CREATE TABLE t (a TEXT)", "CREATE TABLE t (a WEIRD)", "SELECT * FROM t LIMIT 1.5",
    "SELECT * FROM t WHERE a = @", "INSERT INTO t VALUES (1", "SELECT * FROM t extra",
])
def test_parser_errors(bad):
    with pytest.raises(SQLError):
        parse(bad)


def test_three_valued_logic():
    row = {"a": None, "b": 1}
    assert evaluate(Compare("a", "=", 1), row) is None              # NULL = 1  -> unknown
    assert evaluate(Not(Compare("a", "=", 1)), row) is None         # NOT unknown -> unknown
    assert evaluate(Or(Compare("a", "=", 1), Compare("b", "=", 1)), row) is True
    assert evaluate(And(Compare("a", "=", 1), Compare("b", "=", 2)), row) is False
    assert evaluate(IsNull("a"), row) is True


def test_split_statements_ignores_semicolons_in_quotes():
    stmts, rest = split_statements("INSERT INTO t VALUES (1, 'a;b'); SELECT * FROM t; SELECT")
    assert stmts == ["INSERT INTO t VALUES (1, 'a;b')", "SELECT * FROM t"]
    assert rest.strip() == "SELECT"


@pytest.fixture
def db(path):
    s = Session(path)
    s.execute("CREATE TABLE users (id INT PRIMARY KEY, name TEXT(16) NOT NULL, "
              "city TEXT(16), age INT, score FLOAT, vip BOOL)")
    s.execute("INSERT INTO users VALUES (3, 'Ana', 'Austin', 31, 9.5, TRUE), "
              "(1, 'Bob', NULL, 31, 7, FALSE), (2, 'Chi', 'Boston', 25, -8.25, TRUE)")
    s.execute("INSERT INTO users (id, name) VALUES (4, 'Dan')")
    return s


def ids(result):
    return [r[0] for r in result.rows]


def test_select_basics(db):
    assert ids(db.execute("SELECT * FROM users")) == [1, 2, 3, 4]
    assert db.execute("SELECT name, age FROM users WHERE id = 3").rows == [("Ana", 31)]
    assert ids(db.execute("SELECT * FROM users WHERE city IS NULL")) == [1, 4]
    assert ids(db.execute("SELECT * FROM users WHERE city = NULL")) == []          # never true in SQL
    assert ids(db.execute("SELECT * FROM users WHERE age > 30 AND vip = TRUE")) == [3]
    assert ids(db.execute("SELECT * FROM users WHERE id BETWEEN 2 AND 3")) == [2, 3]
    assert ids(db.execute("SELECT * FROM users ORDER BY age DESC LIMIT 2")) == [1, 3]
    assert ids(db.execute("SELECT * FROM users ORDER BY age")) == [4, 2, 1, 3]     # NULL sorts first
    assert db.execute("SELECT COUNT(*) FROM users WHERE score < 8").rows == [(2,)]
    assert db.execute("SELECT * FROM users WHERE id = 4").rows == [(4, "Dan", None, None, None, None)]


def test_explain_picks_the_right_plan(db):
    explain = lambda q: db.execute("EXPLAIN " + q).message
    assert "USING PRIMARY KEY (id = 3)" in explain("SELECT * FROM users WHERE id = 3")
    assert "PRIMARY KEY RANGE" in explain("SELECT * FROM users WHERE id > 1 AND id < 4")
    assert "SCAN" in explain("SELECT * FROM users WHERE city = 'Austin'")
    db.execute("CREATE INDEX ON users (city)")
    assert "USING INDEX ON city" in explain("SELECT * FROM users WHERE city = 'Austin' AND age > 1")
    assert "USING INDEX ON city" in explain("SELECT * FROM users WHERE city IS NULL")
    assert "SCAN" in explain("SELECT * FROM users WHERE city = 'Austin' OR age > 1")   # OR can't use it
    assert "SORT" in explain("SELECT * FROM users ORDER BY age")
    assert "SORT" not in explain("SELECT * FROM users ORDER BY id")                   # already in id order


def test_every_plan_returns_the_same_rows_as_brute_force(path):
    rnd = random.Random(21)
    s = Session(path)
    s.execute("CREATE TABLE p (id INT PRIMARY KEY, city TEXT(8), age INT)")
    data = {i: (i, rnd.choice(["A", "B", "C", None]), rnd.choice([None, *range(20, 30)]))
            for i in rnd.sample(range(1, 2000), 400)}
    s.execute("INSERT INTO p VALUES " + ", ".join(
        f"({i}, {'NULL' if c is None else repr(c)}, {'NULL' if a is None else a})" for i, c, a in data.values()))
    queries = []
    for _ in range(150):
        lo, hi = sorted(rnd.sample(range(0, 2100), 2))
        c, a = rnd.choice(["A", "B", "C"]), rnd.randint(18, 32)
        queries += [(f"id = {lo}", lambda r, lo=lo: r[0] == lo),
                    (f"id >= {lo} AND id < {hi}", lambda r, lo=lo, hi=hi: lo <= r[0] < hi),
                    (f"city = '{c}'", lambda r, c=c: r[1] == c),
                    (f"city = '{c}' AND age >= {a}", lambda r, c=c, a=a: r[1] == c and r[2] is not None and r[2] >= a),
                    (f"city IS NULL OR age < {a}", lambda r, a=a: r[1] is None or (r[2] is not None and r[2] < a))]
    for indexed in (False, True):
        if indexed:
            s.execute("CREATE INDEX ON p (city)")
        for where, check in queries:
            got = ids(s.execute(f"SELECT * FROM p WHERE {where}"))
            assert got == sorted(i for i, r in data.items() if check(r)), where


def test_multi_row_insert_is_atomic(db):
    with pytest.raises(DuplicateKey):
        db.execute("INSERT INTO users VALUES (10, 'x', NULL, 1, 1, TRUE), (3, 'dup', NULL, 1, 1, TRUE)")
    assert db.execute("SELECT COUNT(*) FROM users").rows == [(4,)]                  # row 10 rolled back too


def test_sql_transactions(db):
    db.execute("BEGIN")
    db.execute("INSERT INTO users (id, name) VALUES (20, 'tmp')")
    assert ids(db.execute("SELECT * FROM users WHERE id = 20")) == [20]            # visible inside
    db.execute("ROLLBACK")
    assert ids(db.execute("SELECT * FROM users WHERE id = 20")) == []


@pytest.mark.parametrize("bad, error", [
    ("SELECT * FROM orders", SQLError),
    ("SELECT nope FROM users", SchemaError),
    ("SELECT * FROM users WHERE age = 'x'", SchemaError),
    ("INSERT INTO users (name) VALUES ('x')", SQLError),
    ("INSERT INTO users VALUES (9, NULL, NULL, 1, 1, TRUE)", SchemaError),
    ("INSERT INTO users VALUES (9, 'x')", SQLError),
    ("CREATE TABLE again (id INT)", SQLError),
])
def test_sql_errors(db, bad, error):
    with pytest.raises(error):
        db.execute(bad)


def test_table_name_and_data_survive_reopen(db, path):
    db.close()
    s = Session(path)
    assert s.table.name == "users"
    assert ids(s.execute("SELECT * FROM users WHERE age = 31")) == [1, 3]


def test_create_table_rules(path, tmp_path):
    with pytest.raises(SQLError):
        Session(path).execute("CREATE TABLE t (a TEXT(8), id INT PRIMARY KEY)")      # key not first
    with pytest.raises(SchemaError):
        Session(str(tmp_path / "x.db")).execute("CREATE TABLE t (name TEXT(8))")    # key must be INT
