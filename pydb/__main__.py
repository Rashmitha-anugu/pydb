import sys
from .table import Table, TableFull
from .row import Row


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
        except (ValueError, TableFull) as e:
            return f"Error: {e}"

    if cmd == "select":
        rows = [str(r) for r in table.select_all()]
        return "\n".join(rows + ["Executed."])

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
                print(f"rows={table.num_rows} pages={p.num_pages} "
                      f"disk_reads={p.reads} disk_writes={p.writes}")
                continue
            out = execute(table, line)
            if out:
                print(out)
    finally:
        table.close()   # always flush to disk, even on Ctrl+C


if __name__ == "__main__":
    main()