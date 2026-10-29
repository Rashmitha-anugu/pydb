"""Run a parsed SQL statement against a Table.

The interesting part is the QUERY PLANNER (plan_select): before reading any
data it looks at the WHERE clause and picks the cheapest way to find rows:

  1. id = 5                     -> PRIMARY KEY lookup     (one B+Tree search)
  2. id > 10 AND id <= 20       -> PRIMARY KEY RANGE      (search + walk the leaves)
  3. city = 'Austin' (indexed)  -> INDEX lookup           (secondary B+Tree, then main tree)
  4. anything else              -> FULL SCAN              (read every row)

Whatever access path is chosen, the WHERE clause is then checked against each
candidate row, so the plan only decides HOW rows are found, never WHICH rows
are returned. EXPLAIN SELECT ... shows the plan without running the query.
"""
import os
from dataclasses import dataclass, field
from .sql_parser import (parse, SQLError, CreateTable, CreateIndex, Insert, Select,
                         Compare, IsNull, And, Or, Not, Begin, Commit, Rollback)
from .schema import Schema, Column, SchemaError, FIXED, MAX_KEY
from .table import Table


@dataclass
class Result:
    message: str = ""
    columns: list = field(default_factory=list)
    rows: list = field(default_factory=list)


class Session:
    """One database file + the table inside it."""

    def __init__(self, path: str, cache_pages: int = 64):
        self.path = path
        self.cache_pages = cache_pages
        self.table = Table.open(path, cache_pages) if os.path.exists(path) and os.path.getsize(path) else None

    def execute(self, sql: str) -> Result:
        stmt = parse(sql)
        if isinstance(stmt, CreateTable):
            return self.create_table(stmt)
        if self.table is None:
            raise SQLError("This database has no table yet. Start with CREATE TABLE ...")
        if isinstance(stmt, (Begin, Commit, Rollback)):
            getattr(self.table, type(stmt).__name__.lower())()
            return Result({"Begin": "Transaction started.", "Commit": "Committed.",
                           "Rollback": "Rolled back."}[type(stmt).__name__])
        self.check_table(stmt.table)
        if isinstance(stmt, CreateIndex):
            n = self.table.create_index(stmt.column)
            return Result(f"Index on {stmt.column} created ({n} rows indexed).")
        if isinstance(stmt, Insert):
            return self.insert(stmt)
        return self.select(stmt)

    def check_table(self, name):
        if name.lower() != self.table.name.lower():
            raise SQLError(f"No table named {name!r}. This database has one table: {self.table.name}.")

    def close(self):
        if self.table:
            self.table.close()

    # ---------------------------------------------------------- CREATE TABLE
    def create_table(self, stmt: CreateTable) -> Result:
        if self.table is not None:
            raise SQLError(f"This database already has a table ({self.table.name}).")
        pks = [i for i, c in enumerate(stmt.columns) if c.primary_key]
        if pks and pks != [0]:
            raise SQLError("The PRIMARY KEY must be the first column (and there can only be one).")
        cols = []
        for i, c in enumerate(stmt.columns):
            size = c.size if c.type == "text" else FIXED[c.type].size
            nullable = not (c.not_null or i == 0)      # the key is never NULL
            cols.append(Column(c.name, c.type, size, nullable))
        schema = Schema(cols)                          # checks key type, row size, duplicates
        self.table = Table.create(self.path, schema, self.cache_pages, name=stmt.table)
        return Result(f"Table {stmt.table} created ({schema}). "
                      f"{schema.row_size} bytes per row, {self.table.tree.leaf_max} rows per page.")

    # ---------------------------------------------------------- INSERT
    def insert(self, stmt: Insert) -> Result:
        schema = self.table.schema
        names = [c.name for c in schema.columns]
        if stmt.columns is not None:
            for c in stmt.columns:
                schema.column(c)                                   # unknown column -> error
            if names[0] not in stmt.columns:
                raise SQLError(f"INSERT must include the primary key column ({names[0]}).")
        rows = []
        for values in stmt.rows:
            given = stmt.columns or names
            if len(values) != len(given):
                raise SQLError(f"{len(given)} columns but {len(values)} values in ({', '.join(map(repr, values))}).")
            row = dict(zip(given, values))
            rows.append(tuple(row.get(n) for n in names))          # missing columns -> NULL
        if self.table._in_txn():                                   # already inside BEGIN ... COMMIT
            for r in rows:
                self.table.insert(r)
        else:
            with self.table.transaction():                         # all rows, or none
                for r in rows:
                    self.table.insert(r)
        return Result(f"Inserted {len(rows)} row(s).")

    # ---------------------------------------------------------- SELECT
    def select(self, stmt: Select) -> Result:
        schema = self.table.schema
        names = [c.name for c in schema.columns]
        for col in referenced_columns(stmt):
            schema.column(col)                                     # unknown column -> error
        if stmt.where is not None:
            check_types(stmt.where, schema)
        plan = plan_select(stmt, self.table)
        if stmt.explain:
            return Result("\n".join(plan.describe()))

        rows = plan.fetch()
        if stmt.where is not None:
            rows = [r for r in rows if evaluate(stmt.where, dict(zip(names, r))) is True]
        if stmt.order_by:
            col, desc = stmt.order_by
            pos = names.index(col)
            if not (pos == 0 and not desc):                        # already sorted by primary key
                nulls = [r for r in rows if r[pos] is None]
                rest = sorted((r for r in rows if r[pos] is not None), key=lambda r: r[pos], reverse=desc)
                rows = nulls + rest if not desc else rest + nulls  # NULLs count as smallest
        if stmt.columns == ["COUNT(*)"]:
            return Result(columns=["COUNT(*)"], rows=[(len(rows),)])
        if stmt.limit is not None:
            rows = rows[:stmt.limit]
        if stmt.columns == ["*"]:
            return Result(columns=names, rows=rows)
        idx = [names.index(c) for c in stmt.columns]
        return Result(columns=stmt.columns, rows=[tuple(r[i] for i in idx) for r in rows])


# ================================================================ helpers
def referenced_columns(stmt: Select):
    cols = [c for c in stmt.columns if c not in ("*", "COUNT(*)")]
    if stmt.order_by:
        cols.append(stmt.order_by[0])

    def walk(e):
        if isinstance(e, (Compare, IsNull)):
            cols.append(e.column)
        elif isinstance(e, (And, Or)):
            walk(e.left); walk(e.right)
        elif isinstance(e, Not):
            walk(e.expr)
    walk(stmt.where)
    return cols


def check_types(e, schema):
    """Comparing an int column with 'text' is a mistake: say so instead of returning nothing."""
    if isinstance(e, Compare) and e.value is not None:
        col = schema.column(e.column)
        Schema.check_value(Column(col.name, col.type, 1 << 30, True), e.value)
    elif isinstance(e, (And, Or)):
        check_types(e.left, schema); check_types(e.right, schema)
    elif isinstance(e, Not):
        check_types(e.expr, schema)


OPS = {"=": lambda a, b: a == b, "!=": lambda a, b: a != b, "<": lambda a, b: a < b,
       "<=": lambda a, b: a <= b, ">": lambda a, b: a > b, ">=": lambda a, b: a >= b}


def evaluate(e, row: dict):
    """SQL three-valued logic: True, False, or None (= unknown, e.g. anything compared with NULL)."""
    if isinstance(e, Compare):
        a, b = row[e.column], e.value
        if a is None or b is None:
            return None
        return OPS[e.op](a, b)
    if isinstance(e, IsNull):
        return (row[e.column] is None) != e.negated
    if isinstance(e, Not):
        v = evaluate(e.expr, row)
        return None if v is None else not v
    l, r = evaluate(e.left, row), evaluate(e.right, row)
    if isinstance(e, And):
        if l is False or r is False:
            return False
        return None if l is None or r is None else True
    if l is True or r is True:                                 # Or
        return True
    return None if l is None or r is None else False


# ================================================================ planner
@dataclass
class Plan:
    table: Table
    kind: str                 # "pk", "pk_range", "index", "scan"
    detail: str = ""
    key: object = None
    lo: int = 0
    hi: int = MAX_KEY
    column: str = ""
    sort: str = ""
    limit: str = ""

    def fetch(self):
        t = self.table
        if self.kind == "pk":
            row = t.find(self.key)
            return [row] if row else []
        if self.kind == "pk_range":
            return t.select_range(self.lo, self.hi) if self.lo <= self.hi else []
        if self.kind == "index":
            return t.find_by(self.column, self.key)
        return t.select_all()

    def describe(self):
        name = self.table.name
        lines = {"pk": f"SEARCH {name} USING PRIMARY KEY ({self.detail})",
                 "pk_range": f"SEARCH {name} USING PRIMARY KEY RANGE ({self.detail})",
                 "index": f"SEARCH {name} USING INDEX ON {self.column} ({self.detail})",
                 "scan": f"SCAN {name} (reads every row: {self.table.num_rows})"}
        out = [lines[self.kind]]
        if self.sort:
            out.append(self.sort)
        if self.limit:
            out.append(self.limit)
        return out


def conjuncts(e):
    """Split  a AND b AND c  into [a, b, c]. Only these can narrow the search;
    anything under OR / NOT must be checked row by row."""
    if isinstance(e, And):
        return conjuncts(e.left) + conjuncts(e.right)
    return [e] if e is not None else []


def plan_select(stmt: Select, table: Table) -> Plan:
    pk = table.schema.columns[0].name
    conds = conjuncts(stmt.where)
    plan = None

    # 1. primary key equality
    for c in conds:
        if isinstance(c, Compare) and c.column == pk and c.op == "=" and isinstance(c.value, int):
            plan = Plan(table, "pk", f"{pk} = {c.value}", key=c.value)
            break

    # 2. primary key range: combine every bound on the key into [lo, hi]
    if plan is None:
        lo, hi, parts = 0, MAX_KEY, []
        for c in conds:
            if isinstance(c, Compare) and c.column == pk and c.op in ("<", "<=", ">", ">="):
                v = c.value                     # an int: check_types() already made sure
                if c.op == ">":
                    lo = max(lo, v + 1)
                elif c.op == ">=":
                    lo = max(lo, v)
                elif c.op == "<":
                    hi = min(hi, v - 1)
                else:
                    hi = min(hi, v)
                parts.append(f"{pk} {c.op} {v}")
        if parts:
            plan = Plan(table, "pk_range", " AND ".join(parts), lo=max(lo, 0), hi=min(hi, MAX_KEY))

    # 3. secondary index equality (or IS NULL)
    if plan is None:
        for c in conds:
            col = getattr(c, "column", None)
            if col in table.indexes:
                if isinstance(c, Compare) and c.op == "=" and c.value is not None:
                    plan = Plan(table, "index", f"{col} = {c.value!r}", column=col, key=c.value)
                    break
                if isinstance(c, IsNull) and not c.negated:
                    plan = Plan(table, "index", f"{col} IS NULL", column=col, key=None)
                    break

    # 4. nothing usable
    if plan is None:
        plan = Plan(table, "scan")

    if stmt.order_by and not (stmt.order_by[0] == pk and not stmt.order_by[1]):
        plan.sort = f"SORT rows by {stmt.order_by[0]} {'DESC' if stmt.order_by[1] else 'ASC'} (in memory)"
    if stmt.limit is not None:
        plan.limit = f"LIMIT {stmt.limit}"
    return plan


# ================================================================ output
def fmt_value(v):
    if v is None:
        return "NULL"
    if isinstance(v, bool):
        return "true" if v else "false"
    return str(v)


def format_result(res: Result) -> str:
    if not res.columns:
        return res.message
    cells = [[fmt_value(v) for v in r] for r in res.rows]
    widths = [max([len(h)] + [len(r[i]) for r in cells]) for i, h in enumerate(res.columns)]
    line = lambda vals: " | ".join(v.ljust(w) for v, w in zip(vals, widths)).rstrip()
    out = [line(res.columns), "-+-".join("-" * w for w in widths)]
    out += [line(r) for r in cells]
    out.append(f"({len(res.rows)} row{'s' if len(res.rows) != 1 else ''})")
    return "\n".join(out)
