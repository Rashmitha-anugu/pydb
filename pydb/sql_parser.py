"""SQL text -> tokens -> a tree of Python objects (an AST).

Two stages, like every real database:

  1. TOKENIZE  "SELECT name FROM users WHERE age >= 30;"
               -> SELECT  name  FROM  users  WHERE  age  >=  30  ;
  2. PARSE     tokens -> Select(table='users', columns=['name'],
                                where=Compare('age', '>=', 30))

The parser is "recursive descent": one small method per grammar rule, each
calling the others. Supported:

  CREATE TABLE t (col TYPE [NOT NULL] [PRIMARY KEY], ...)
  CREATE INDEX [name] ON t (col)
  INSERT INTO t [(col, ...)] VALUES (v, ...), (v, ...)
  SELECT * | COUNT(*) | col, ... FROM t [WHERE cond] [ORDER BY col [ASC|DESC]] [LIMIT n]
  EXPLAIN SELECT ...
  BEGIN | COMMIT | ROLLBACK

  cond:  col = | != | <> | < | <= | > | >= value,  col IS [NOT] NULL,
         col BETWEEN a AND b,  AND,  OR,  NOT,  ( ... )
"""
import re
from dataclasses import dataclass, field


class SQLError(ValueError):
    pass


# ---------------------------------------------------------------- tokenizer
KEYWORDS = {
    "SELECT", "FROM", "WHERE", "AND", "OR", "NOT", "IS", "NULL", "TRUE", "FALSE",
    "INSERT", "INTO", "VALUES", "CREATE", "TABLE", "INDEX", "ON", "PRIMARY", "KEY",
    "ORDER", "BY", "ASC", "DESC", "LIMIT", "COUNT", "EXPLAIN", "BETWEEN",
    "BEGIN", "COMMIT", "ROLLBACK",
}

TOKEN_RE = re.compile(r"""
    (?:\s|--[^\n]*)*(?:                         # skip spaces and -- comments
      (?P<number>\d+\.\d*|\.\d+|\d+)          |
      (?P<string>'(?:[^']|'')*')               |
      (?P<word>[A-Za-z_][A-Za-z0-9_]*)         |
      (?P<op><=|>=|!=|<>|[=<>(),*;\-])
    )""", re.VERBOSE)


@dataclass
class Token:
    kind: str        # "number" | "string" | "word" | "keyword" | "op" | "end"
    value: object
    pos: int         # character position, for error messages


def tokenize(sql: str) -> list:
    tokens, pos = [], 0
    while pos < len(sql):
        if re.fullmatch(r"(?:\s|--[^\n]*)*", sql[pos:]):
            break                                  # only spaces / comments left
        m = TOKEN_RE.match(sql, pos)
        if not m or m.end() == pos:
            raise SQLError(f"Unexpected character {sql[pos:].strip()[0]!r} at position {pos}.")
        kind = m.lastgroup
        text = m.group(kind)
        start = m.start(kind)
        if kind == "number":
            tokens.append(Token("number", float(text) if "." in text else int(text), start))
        elif kind == "string":
            tokens.append(Token("string", text[1:-1].replace("''", "'"), start))
        elif kind == "word" and text.upper() in KEYWORDS:
            tokens.append(Token("keyword", text.upper(), start))
        elif kind == "word":
            tokens.append(Token("word", text, start))
        else:
            tokens.append(Token("op", text, start))
        pos = m.end()
    tokens.append(Token("end", None, len(sql)))
    return tokens


# ---------------------------------------------------------------- AST nodes
@dataclass
class ColumnDef:
    name: str
    type: str                 # int | float | bool | text
    size: int | None
    not_null: bool = False
    primary_key: bool = False


@dataclass
class CreateTable:
    table: str
    columns: list


@dataclass
class CreateIndex:
    table: str
    column: str


@dataclass
class Insert:
    table: str
    columns: list | None      # None = all columns, in order
    rows: list                # list of lists of literal values


@dataclass
class Compare:
    column: str
    op: str                   # = != < <= > >=
    value: object


@dataclass
class IsNull:
    column: str
    negated: bool = False     # IS NOT NULL


@dataclass
class And:
    left: object
    right: object


@dataclass
class Or:
    left: object
    right: object


@dataclass
class Not:
    expr: object


@dataclass
class Select:
    table: str
    columns: list             # ["*"], ["COUNT(*)"], or column names
    where: object = None
    order_by: tuple | None = None     # (column, descending)
    limit: int | None = None
    explain: bool = False


@dataclass
class Begin:
    pass


@dataclass
class Commit:
    pass


@dataclass
class Rollback:
    pass


TYPE_NAMES = {"INT": "int", "INTEGER": "int", "BIGINT": "int",
              "FLOAT": "float", "REAL": "float", "DOUBLE": "float",
              "BOOL": "bool", "BOOLEAN": "bool",
              "TEXT": "text", "VARCHAR": "text", "CHAR": "text"}
FLIP = {"<": ">", "<=": ">=", ">": "<", ">=": "<=", "=": "=", "!=": "!="}


# ---------------------------------------------------------------- parser
class Parser:
    def __init__(self, sql: str):
        self.tokens = tokenize(sql)
        self.i = 0

    # --- helpers ---
    @property
    def tok(self) -> Token:
        return self.tokens[self.i]

    def advance(self) -> Token:
        t = self.tokens[self.i]
        self.i += 1
        return t

    def at(self, kind, value=None) -> bool:
        return self.tok.kind == kind and (value is None or self.tok.value == value)

    def accept(self, kind, value=None):
        if self.at(kind, value):
            return self.advance()
        return None

    def found(self) -> str:
        return "the end of the statement" if self.tok.kind == "end" else repr(self.tok.value)

    def expect(self, kind, value=None) -> Token:
        if not self.at(kind, value):
            want = value if value is not None else {"word": "a name", "number": "a number",
                                                    "op": "a symbol", "string": "'text'"}[kind]
            raise SQLError(f"Expected {want} but found {self.found()} at position {self.tok.pos}.")
        return self.advance()

    def keyword(self, *words):
        for w in words:
            self.expect("keyword", w)

    def ident(self) -> str:
        return self.expect("word").value

    # --- entry point ---
    def parse(self):
        stmt = self.statement()
        self.accept("op", ";")
        if not self.at("end"):
            raise SQLError(f"Unexpected {self.found()} at position {self.tok.pos}.")
        return stmt

    def statement(self):
        if self.accept("keyword", "SELECT"):
            return self.select()
        if self.accept("keyword", "EXPLAIN"):
            self.keyword("SELECT")
            s = self.select()
            s.explain = True
            return s
        if self.accept("keyword", "INSERT"):
            return self.insert()
        if self.accept("keyword", "CREATE"):
            if self.accept("keyword", "TABLE"):
                return self.create_table()
            if self.accept("keyword", "INDEX"):
                return self.create_index()
            raise SQLError("Expected TABLE or INDEX after CREATE.")
        for kw, node in (("BEGIN", Begin), ("COMMIT", Commit), ("ROLLBACK", Rollback)):
            if self.accept("keyword", kw):
                return node()
        raise SQLError(f"Unknown statement starting with {self.tok.value!r}. "
                       f"Try SELECT, INSERT, CREATE, EXPLAIN, BEGIN, COMMIT or ROLLBACK.")

    # --- CREATE TABLE users (id INT PRIMARY KEY, name TEXT(32) NOT NULL, ...) ---
    def create_table(self):
        table = self.ident()
        self.expect("op", "(")
        cols = [self.column_def()]
        while self.accept("op", ","):
            cols.append(self.column_def())
        self.expect("op", ")")
        return CreateTable(table, cols)

    def column_def(self):
        name = self.ident()
        t = self.advance()
        tname = str(t.value).upper()
        if tname not in TYPE_NAMES:
            raise SQLError(f"Unknown type {t.value!r} for column {name}. Use INT, FLOAT, BOOL or TEXT(n).")
        ctype, size = TYPE_NAMES[tname], None
        if ctype == "text":
            if not self.accept("op", "("):
                raise SQLError(f"Column {name}: TEXT needs a size in bytes, e.g. TEXT(64).")
            size = self.expect("number").value
            self.expect("op", ")")
        col = ColumnDef(name, ctype, size)
        while True:
            if self.accept("keyword", "NOT"):
                self.keyword("NULL")
                col.not_null = True
            elif self.accept("keyword", "PRIMARY"):
                self.keyword("KEY")
                col.primary_key = True
            else:
                return col

    # --- CREATE INDEX [name] ON users (email) ---
    def create_index(self):
        if self.at("word"):
            self.advance()                          # index name: accepted, not needed
        self.keyword("ON")
        table = self.ident()
        self.expect("op", "(")
        column = self.ident()
        self.expect("op", ")")
        return CreateIndex(table, column)

    # --- INSERT INTO users [(id, name)] VALUES (1, 'ana'), (2, 'bob') ---
    def insert(self):
        self.keyword("INTO")
        table = self.ident()
        columns = None
        if self.accept("op", "("):
            columns = [self.ident()]
            while self.accept("op", ","):
                columns.append(self.ident())
            self.expect("op", ")")
        self.keyword("VALUES")
        rows = [self.value_list()]
        while self.accept("op", ","):
            rows.append(self.value_list())
        return Insert(table, columns, rows)

    def value_list(self):
        self.expect("op", "(")
        values = [self.literal()]
        while self.accept("op", ","):
            values.append(self.literal())
        self.expect("op", ")")
        return values

    def literal(self):
        if self.accept("op", "-"):
            n = self.expect("number").value
            return -n
        if self.at("number") or self.at("string"):
            return self.advance().value
        if self.accept("keyword", "NULL"):
            return None
        if self.accept("keyword", "TRUE"):
            return True
        if self.accept("keyword", "FALSE"):
            return False
        raise SQLError(f"Expected a value (number, 'text', TRUE, FALSE or NULL) "
                       f"but found {self.found()} at position {self.tok.pos}.")

    # --- SELECT ... FROM ... WHERE ... ORDER BY ... LIMIT ... ---
    def select(self):
        if self.accept("op", "*"):
            columns = ["*"]
        elif self.accept("keyword", "COUNT"):
            self.expect("op", "(")
            self.expect("op", "*")
            self.expect("op", ")")
            columns = ["COUNT(*)"]
        else:
            columns = [self.ident()]
            while self.accept("op", ","):
                columns.append(self.ident())
        self.keyword("FROM")
        node = Select(self.ident(), columns)
        if self.accept("keyword", "WHERE"):
            node.where = self.or_expr()
        if self.accept("keyword", "ORDER"):
            self.keyword("BY")
            col = self.ident()
            desc = bool(self.accept("keyword", "DESC"))
            if not desc:
                self.accept("keyword", "ASC")
            node.order_by = (col, desc)
        if self.accept("keyword", "LIMIT"):
            node.limit = self.expect("number").value
            if not isinstance(node.limit, int):
                raise SQLError("LIMIT needs a whole number.")
        return node

    # Operator precedence, lowest first:  OR  <  AND  <  NOT  <  comparison
    def or_expr(self):
        left = self.and_expr()
        while self.accept("keyword", "OR"):
            left = Or(left, self.and_expr())
        return left

    def and_expr(self):
        left = self.not_expr()
        while self.accept("keyword", "AND"):
            left = And(left, self.not_expr())
        return left

    def not_expr(self):
        if self.accept("keyword", "NOT"):
            return Not(self.not_expr())
        return self.comparison()

    def comparison(self):
        if self.accept("op", "("):
            e = self.or_expr()
            self.expect("op", ")")
            return e
        if self.at("word"):                                   # column  op  value
            col = self.advance().value
            if self.accept("keyword", "IS"):
                negated = bool(self.accept("keyword", "NOT"))
                self.keyword("NULL")
                return IsNull(col, negated)
            if self.accept("keyword", "BETWEEN"):
                lo = self.literal()
                self.keyword("AND")
                return And(Compare(col, ">=", lo), Compare(col, "<=", self.literal()))
            op = self.comparison_op()
            return Compare(col, op, self.literal())
        value = self.literal()                                # value  op  column  ->  flip it
        op = self.comparison_op()
        return Compare(self.ident(), FLIP[op], value)

    def comparison_op(self):
        t = self.expect("op")
        op = "!=" if t.value == "<>" else t.value
        if op not in FLIP:
            raise SQLError(f"Expected a comparison (=, !=, <, <=, >, >=) at position {t.pos}.")
        return op


def parse(sql: str):
    return Parser(sql).parse()
