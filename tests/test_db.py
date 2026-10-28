import pytest
from pydb.table import Table, MAX_ROWS, ROWS_PER_PAGE, TableFull
from pydb.row import Row, RowError, USERNAME_SIZE, EMAIL_SIZE


@pytest.fixture
def path(tmp_path):
    # a fresh, temporary database file for every test
    return str(tmp_path / "test.db")


def test_insert_and_select(path):
    t = Table(path)
    t.insert(Row(1, "alice", "alice@example.com"))
    assert list(t.select_all()) == [Row(1, "alice", "alice@example.com")]


def test_data_survives_reopen(path):
    t = Table(path)
    for i in range(ROWS_PER_PAGE + 5):          # 19 rows: crosses a page boundary
        t.insert(Row(i, f"user{i}", f"u{i}@x.com"))
    t.close()

    t2 = Table(path)                            # reopen from disk
    rows = list(t2.select_all())
    assert len(rows) == ROWS_PER_PAGE + 5
    assert rows[-1] == Row(18, "user18", "u18@x.com")


def test_max_length_strings_ok(path):
    t = Table(path)
    t.insert(Row(1, "a" * USERNAME_SIZE, "b" * EMAIL_SIZE))
    assert list(t.select_all())[0].email == "b" * EMAIL_SIZE


def test_too_long_rejected(path):
    with pytest.raises(RowError):
        Table(path).insert(Row(1, "a" * (USERNAME_SIZE + 1), "x"))


def test_negative_id_rejected(path):
    with pytest.raises(RowError):
        Table(path).insert(Row(-1, "a", "b"))


def test_table_full(path):
    t = Table(path)
    for i in range(MAX_ROWS):
        t.insert(Row(i, "u", "e"))
    with pytest.raises(TableFull):
        t.insert(Row(MAX_ROWS, "u", "e"))