"""SQL shell:   python -m pydb my.db [cache_pages]

End each statement with ;  (a statement may span several lines).
Dot-commands (.help, .schema, ...) need no semicolon.
"""
import os
import re
import sys
from .sql_exec import Session, format_result
from .sql_parser import SQLError
from .schema import SchemaError
from .btree import DuplicateKey
from .table import TransactionError

HELP = """SQL (end each statement with ;):
  CREATE TABLE users (id INT PRIMARY KEY, name TEXT(32) NOT NULL, city TEXT(16), age INT, vip BOOL);
  INSERT INTO users VALUES (1, 'Ana', 'Austin', 31, TRUE), (2, 'Bob', NULL, 25, FALSE);
  INSERT INTO users (id, name) VALUES (3, 'Chikki');
  SELECT * FROM users WHERE age >= 25 AND city IS NOT NULL ORDER BY age DESC LIMIT 10;
  SELECT COUNT(*) FROM users WHERE id BETWEEN 1 AND 100;
  CREATE INDEX idx_city ON users (city);
  EXPLAIN SELECT * FROM users WHERE city = 'Austin';
  BEGIN;  ...  COMMIT;   (or ROLLBACK;)
Types: INT, FLOAT, BOOL, TEXT(n). The first column is the primary key (INT).
Columns are nullable unless NOT NULL.

Dot-commands:
  .schema  .indexes  .stats  .wal  .btree  .read <file.sql>  .help  .exit"""

ERRORS = (SQLError, SchemaError, DuplicateKey, TransactionError, FileExistsError)


def split_statements(buffer: str):
    """Return (complete statements, leftover text).
    Semicolons inside 'quotes' or after a -- comment don't end a statement."""
    stmts, start, in_quote, in_comment = [], 0, False, False
    for i, ch in enumerate(buffer):
        if in_comment:
            in_comment = ch != "\n"
        elif ch == "'":
            in_quote = not in_quote              # '' inside a string toggles twice: fine
        elif not in_quote and buffer.startswith("--", i):
            in_comment = True
        elif ch == ";" and not in_quote:
            stmts.append(buffer[start:i])
            start = i + 1
    rest = buffer[start:]
    if not strip_comments(rest):
        rest = ""                                  # only comments left over: nothing pending
    return [s.strip() for s in stmts if strip_comments(s)], rest


def strip_comments(text: str) -> str:
    return re.sub(r"--[^\n]*", "", text).strip()


def run_sql(session: Session, sql: str) -> str:
    try:
        return format_result(session.execute(sql))
    except ERRORS as e:
        return f"Error: {e}"


def meta(session: Session, line: str) -> str:
    cmd, _, arg = line.partition(" ")
    if cmd == ".help":
        return HELP
    if cmd == ".read":
        try:
            text = open(arg.strip(), encoding="utf-8").read()
        except OSError as e:
            return f"Error: {e}"
        stmts, rest = split_statements(text)
        out = [f"> {s}\n{run_sql(session, s)}" for s in stmts]
        if rest.strip():
            out.append(f"Error: last statement has no closing semicolon: {rest.strip()[:40]}...")
        return "\n".join(out)
    t = session.table
    if t is None:
        return "No table yet. Type .help for an example CREATE TABLE."
    if cmd == ".schema":
        cols = ", ".join(f"{c.name} {c.type.upper()}{f'({c.size})' if c.type == 'text' else ''}"
                         f"{' PRIMARY KEY' if i == 0 else ('' if c.nullable else ' NOT NULL')}"
                         for i, c in enumerate(t.schema.columns))
        return f"CREATE TABLE {t.name} ({cols});\n-- {t.schema.row_size} bytes per row, {t.tree.leaf_max} rows per page"
    if cmd == ".indexes":
        if not t.indexes:
            return "No secondary indexes. Create one with: CREATE INDEX ON <table> (<column>);"
        return "\n".join(f"{n}: {ix.depth()} levels, {ix.leaf_max} entries per leaf" for n, ix in t.indexes.items())
    if cmd == ".stats":
        p = t.pager
        return (f"rows={t.num_rows} pages={p.num_pages} tree_depth={t.tree.depth()}\n"
                f"cache: {len(p._cache)}/{p.capacity} pages, hit_rate={p.hit_rate():.1%} "
                f"evictions={p.evictions}\ndisk: reads={p.reads} writes={p.writes}")
    if cmd == ".wal":
        p = t.pager
        return (f"log: {p.wal.path}, {os.path.getsize(p.wal.path):,} bytes, {p.wal.frames} frames since checkpoint\n"
                f"commits={p.commits} checkpoints={p.checkpoints} recovered_on_open={p.recovered_transactions} "
                f"open_transaction={'yes' if t._in_txn() else 'no'}")
    if cmd == ".btree":
        return t.tree.print_tree(fmt=lambda k: int.from_bytes(k, "big"))
    return f"Unknown command {cmd}. Type .help"


def main():
    if len(sys.argv) not in (2, 3):
        print("Usage: python -m pydb <database-file> [cache_pages]")
        sys.exit(1)
    path = sys.argv[1]
    session = Session(path, int(sys.argv[2]) if len(sys.argv) == 3 else 64)
    if session.table:
        print(f"Opened {path}: table {session.table.name} ({session.table.num_rows} rows). Type .help")
        if session.table.pager.recovered_transactions:
            print(f"Crash recovery: replayed {session.table.pager.recovered_transactions} committed transaction(s).")
    else:
        print(f"New database {path}. Start with CREATE TABLE ... ; (type .help for an example)")

    buffer = ""
    try:
        while True:
            try:
                line = input("sql> " if not buffer.strip() else " ...> ")
            except EOFError:
                break
            if not buffer.strip() and line.strip().startswith("."):
                if line.strip() == ".exit":
                    break
                print(meta(session, line.strip()))
                continue
            buffer += line + "\n"
            stmts, buffer = split_statements(buffer)
            for s in stmts:
                print(run_sql(session, s))
    finally:
        if session.table is not None and session.table._in_txn():
            print("Open transaction rolled back (it was never committed).")
        session.close()


if __name__ == "__main__":
    main()
