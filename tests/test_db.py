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
    for i in range(0, 1000, 2):
        t.insert(user(i))
    assert [r[0] for r in t.select_range(101, 111)] == [102, 104, 106, 108, 110]


# ---------- buffer pool still safe with the new row format ----------
@pytest.mark.parametrize("cache_pages", [1, 3])
def test_tiny_cache_never_loses_data(path, cache_pages):
    ids = random.Random(7).sample(range(1_000_000), 5_000)
    t = Table.create(path, USERS, cache_pages)
    for i in ids:
        t.insert(user(i))
    assert len(t.pager._cache) <= cache_pages
    t.close()
    t2 = Table.open(path, cache_pages)
    assert [r[0] for r in t2.select_all()] == sorted(ids)
    for i in ids[:300]:
        assert t2.find(i) == user(i)


def test_bigger_cache_means_higher_hit_rate(path):
    ids = random.Random(1).sample(range(1_000_000), 10_000)
    t = Table.create(path, USERS, 4096)
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


# ---------- REPL ----------
def test_repl_end_to_end(path):
    from pydb.__main__ import execute, meta
    state = {"path": path, "cache_pages": 64, "table": None}
    assert "No table yet" in execute(state, "insert 1 a")
    assert "Created table" in execute(state, "create id:int name:text(16) note:text(32)? vip:bool")
    assert execute(state, "insert 1 'Ana Silva' null true") == "Executed."
    assert "cannot be NULL" in execute(state, "insert 2 null hi false")
    assert "needs true/false" in execute(state, "insert 2 bo hi maybe")
    assert execute(state, "find 1") == "(1, 'Ana Silva', NULL, true)"
    assert "vip:bool" in meta(state, ".schema")
