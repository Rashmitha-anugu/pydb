import sys
from .table import Table
from .row import Row
from .btree import DuplicateKey


def execute(table: Table, line: str) -> str:
    parts = line.split()
    if not parts:
        return ""
    cmd = parts[0].lower()

    if cmd == "insert":
        if len(parts) != 4:
            return "Syntax error. Usage: insert <id> <username> <email>"
        try:
            table.insert(Row(int(parts[1]), parts[2], parts[3]))
            return "Executed."
        except (ValueError, DuplicateKey) as e:
            return f"Error: {e}"

    if cmd == "find":
        if len(parts) != 2 or not parts[1].isdigit():
            return "Syntax error. Usage: find <id>"
        row = table.find(int(parts[1]))
        return str(row) if row else "Not found."

    if cmd == "select":
        if len(parts) == 1:
            rows = table.select_all()
        elif len(parts) == 2 and parts[1].isdigit():
            rows = table.select_range(int(parts[1]), int(parts[1]))
        elif len(parts) == 3 and parts[1].isdigit() and parts[2].isdigit():
            rows = table.select_range(int(parts[1]), int(parts[2]))
        else:
            return "Syntax error. Usage: select  |  select <id>  |  select <from_id> <to_id>"
        return "\n".join([str(r) for r in rows] + ["Executed."])

    return f"Unrecognized command '{parts[0]}'."


def main():
    if len(sys.argv) != 2:
        print("Usage: python -m pydb <database-file>")
        sys.exit(1)

    table = Table(sys.argv[1])
    try:
        while True:
            try:
                line = input("db > ").strip()
            except EOFError:
                break
            if line == ".exit":
                break
            if line == ".stats":
                p = table.pager
                print(f"rows={table.num_rows} pages={p.num_pages} tree_depth={table.tree.depth()} "
                      f"disk_reads={p.reads} disk_writes={p.writes}")
                continue
            if line == ".btree":
                print(table.tree.print_tree())
                continue
            out = execute(table, line)
            if out:
                print(out)
    finally:
        table.close()


if __name__ == "__main__":
    main()