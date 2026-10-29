import os
import shlex
import sys
from .table import Table
from .schema import Schema, SchemaError
from .btree import DuplicateKey

HELP = """Commands:
  create <col:type> ...      define the table (new database only). Types: int bool float text(N)
                             add ? for nullable. First column = primary key (int).
                             e.g.  create id:int name:text(32) email:text(255)? age:int? active:bool
  insert <v1> <v2> ...       values in column order; use null for NULL, quotes for spaces: 'Ana Silva'
  find <id>                  one row by primary key
  select                     all rows, sorted by key
  select <id>                one row
  select <from> <to>         rows with key between from and to
  .schema  .stats  .btree  .help  .exit"""


def execute(state: dict, line: str) -> str:
    try:
        parts = shlex.split(line)
    except ValueError as e:                     # e.g. an unclosed quote
        return f"Syntax error: {e}"
    if not parts:
        return ""
    cmd = parts[0].lower()
    table = state["table"]

    if cmd == "create":
        if table is not None:
            return "Error: this database already has a table."
        try:
            schema = Schema.parse(parts[1:])
        except SchemaError as e:
            return f"Error: {e}"
        state["table"] = Table.create(state["path"], schema, state["cache_pages"])
        return f"Created table ({schema}). Row size: {schema.row_size} bytes, " \
               f"{state['table'].tree.leaf_max} rows per leaf page."

    if table is None:
        return "No table yet. Define one first, e.g.:  create id:int name:text(32) email:text(255)?"

    if cmd == "insert":
        try:
            table.insert(table.schema.parse_values(parts[1:]))
            return "Executed."
        except (SchemaError, DuplicateKey) as e:
            return f"Error: {e}"

    if cmd == "find":
        if len(parts) != 2 or not parts[1].isdigit():
            return "Syntax error. Usage: find <id>"
        row = table.find(int(parts[1]))
        return table.schema.format_row(row) if row else "Not found."

    if cmd == "select":
        if len(parts) == 1:
            rows = table.select_all()
        elif len(parts) == 2 and parts[1].isdigit():
            rows = table.select_range(int(parts[1]), int(parts[1]))
        elif len(parts) == 3 and parts[1].isdigit() and parts[2].isdigit():
            rows = table.select_range(int(parts[1]), int(parts[2]))
        else:
            return "Syntax error. Usage: select  |  select <id>  |  select <from> <to>"
        return "\n".join([table.schema.format_row(r) for r in rows] + ["Executed."])

    return f"Unrecognized command '{parts[0]}'. Type .help"


def meta(state: dict, line: str) -> str:
    table = state["table"]
    if line == ".help":
        return HELP
    if table is None:
        return "No table yet. Type .help"
    if line == ".schema":
        return f"{table.schema}\nrow size: {table.schema.row_size} bytes, {table.tree.leaf_max} rows per leaf page"
    if line == ".stats":
        p = table.pager
        return (f"rows={table.num_rows} pages={p.num_pages} tree_depth={table.tree.depth()}\n"
                f"cache: {len(p._cache)}/{p.capacity} pages, hit_rate={p.hit_rate():.1%} "
                f"(hits={p.hits} misses={p.misses}) evictions={p.evictions}\n"
                f"disk: reads={p.reads} writes={p.writes}")
    if line == ".btree":
        return table.tree.print_tree()
    return f"Unrecognized command '{line}'. Type .help"


def main():
    if len(sys.argv) not in (2, 3):
        print("Usage: python -m pydb <database-file> [cache_pages]")
        sys.exit(1)
    path = sys.argv[1]
    cache_pages = int(sys.argv[2]) if len(sys.argv) == 3 else 64
    state = {"path": path, "cache_pages": cache_pages, "table": None}

    if os.path.exists(path) and os.path.getsize(path) > 0:
        state["table"] = Table.open(path, cache_pages)
        print(f"Opened {path}: {state['table'].schema}")
    else:
        print(f"New database {path}. Define a table first (type .help for an example).")

    try:
        while True:
            try:
                line = input("db > ").strip()
            except EOFError:
                break
            if line == ".exit":
                break
            out = meta(state, line) if line.startswith(".") else execute(state, line)
            if out:
                print(out)
    finally:
        if state["table"] is not None:
            state["table"].close()


if __name__ == "__main__":
    main()
