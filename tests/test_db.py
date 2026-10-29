import random
import pytest
from pydb.table import Table
from pydb.row import Row, RowError, USERNAME_SIZE, EMAIL_SIZE
from pydb.btree import DuplicateKey, LEAF_MAX


@pytest.fixture
def path(tmp_path):
    return str(tmp_path / "test.db")


def row(i):
    return Row(i, f"user{i}", f"u{i}@x.com")


def test_insert_and_find(path):
    t = Table(path)
    t.insert(row(1))
    assert t.find(1) == row(1)
    assert t.find(2) is None


def test_select_returns_sorted_order(path):
    t = Table(path)
    for i in [5, 3, 9, 1, 7]:
        t.insert(row(i))
    assert [r.id for r in t.select_all()] == [1, 3, 5, 7, 9]


def test_duplicate_key_rejected(path):
    t = Table(path)
    t.insert(row(1))
    with pytest.raises(DuplicateKey):
        t.insert(row(1))
    assert t.num_rows == 1


def test_first_leaf_split(path):
    t = Table(path)
    for i in range(LEAF_MAX + 1):          # 14 rows: one more than a leaf holds
        t.insert(row(i))
    assert t.tree.depth() == 2
    assert [r.id for r in t.select_all()] == list(range(LEAF_MAX + 1))


def test_many_random_inserts_survive_reopen(path):
    ids = random.Random(42).sample(range(1_000_000), 20_000)   # forces internal-node splits
    t = Table(path)
    for i in ids:
        t.insert(row(i))
    depth = t.tree.depth()
    t.close()

    t2 = Table(path)
    assert t2.num_rows == 20_000
    assert t2.tree.depth() == depth >= 3
    assert [r.id for r in t2.select_all()] == sorted(ids)
    for i in ids[:500]:
        assert t2.find(i) == row(i)


def test_range_scan(path):
    t = Table(path)
    for i in range(0, 1000, 2):            # even ids only
        t.insert(row(i))
    assert [r.id for r in t.select_range(101, 111)] == [102, 104, 106, 108, 110]
    assert list(t.select_range(5000, 6000)) == []


def test_max_length_strings_ok(path):
    t = Table(path)
    t.insert(Row(1, "a" * USERNAME_SIZE, "b" * EMAIL_SIZE))
    assert t.find(1).email == "b" * EMAIL_SIZE


def test_too_long_rejected(path):
    with pytest.raises(RowError):
        Table(path).insert(Row(1, "a" * (USERNAME_SIZE + 1), "x"))


def test_negative_id_rejected(path):
    with pytest.raises(RowError):
        Table(path).insert(Row(-1, "a", "b"))