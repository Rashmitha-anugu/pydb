"""Typed columns and the byte layout of a row.

Row layout:  | null bitmap | column 1 | column 2 | ... |
  - null bitmap: 1 bit per column (1 = NULL), rounded up to whole bytes
  - every column has a FIXED width, so every row is still the same size

Types:  int   -> 8 bytes, signed          bool -> 1 byte
        float -> 8 bytes (double)         text(n) -> n bytes, UTF-8, NUL-padded

The first column is the primary key: it must be `int`, NOT NULL, and 0..4294967295.
"""
import json
import re
import struct
from dataclasses import dataclass

FIXED = {"int": struct.Struct("<q"), "bool": struct.Struct("<?"), "float": struct.Struct("<d")}
MAX_KEY = 2**32 - 1
MAX_ROW_SIZE = 2040       # a leaf page must hold at least 2 rows, or it could never split


class SchemaError(ValueError):
    pass


@dataclass(frozen=True)
class Column:
    name: str
    type: str            # "int" | "bool" | "float" | "text"
    size: int            # bytes on disk
    nullable: bool = False

    def __str__(self):
        t = f"text({self.size})" if self.type == "text" else self.type
        return f"{self.name}:{t}{'?' if self.nullable else ''}"


def parse_column(spec: str) -> Column:
    """'email:text(255)?'  ->  Column('email', 'text', 255, nullable=True)"""
    m = re.fullmatch(r"([A-Za-z_]\w*):(int|bool|float|text\((\d+)\))(\?)?", spec)
    if not m:
        raise SchemaError(f"Bad column '{spec}'. Use name:type, e.g. age:int  name:text(32)  email:text(255)?")
    name, type_, text_size, nullable = m.groups()
    if type_.startswith("text"):
        size = int(text_size)
        if size < 1:
            raise SchemaError("text size must be at least 1.")
        return Column(name, "text", size, bool(nullable))
    return Column(name, type_, FIXED[type_].size, bool(nullable))


class Schema:
    def __init__(self, columns):
        self.columns = list(columns)
        if not self.columns:
            raise SchemaError("A table needs at least one column.")
        names = [c.name for c in self.columns]
        if len(set(names)) != len(names):
            raise SchemaError("Column names must be unique.")
        key = self.columns[0]
        if key.type != "int" or key.nullable:
            raise SchemaError("The first column is the primary key: it must be int and NOT nullable.")
        self.bitmap_size = (len(self.columns) + 7) // 8
        self.row_size = self.bitmap_size + sum(c.size for c in self.columns)
        if self.row_size > MAX_ROW_SIZE:
            raise SchemaError(f"Row is {self.row_size} bytes; the maximum is {MAX_ROW_SIZE}.")

    @classmethod
    def parse(cls, specs):
        return cls(parse_column(s) for s in specs)

    # ---------- persistence (stored in the file header) ----------
    def to_json(self) -> bytes:
        return json.dumps([[c.name, c.type, c.size, c.nullable] for c in self.columns]).encode()

    @classmethod
    def from_json(cls, data: bytes) -> "Schema":
        return cls(Column(*c) for c in json.loads(data))

    # ---------- validation ----------
    def validate(self, values) -> None:
        if len(values) != len(self.columns):
            raise SchemaError(f"Expected {len(self.columns)} values, got {len(values)}.")
        for col, v in zip(self.columns, values):
            if v is None:
                if not col.nullable:
                    raise SchemaError(f"Column '{col.name}' cannot be NULL.")
                continue
            if col.type == "int":
                if not isinstance(v, int) or isinstance(v, bool):
                    raise SchemaError(f"Column '{col.name}' needs an int.")
                if not -2**63 <= v < 2**63:
                    raise SchemaError(f"Column '{col.name}' is out of range for int.")
            elif col.type == "bool" and not isinstance(v, bool):
                raise SchemaError(f"Column '{col.name}' needs true/false.")
            elif col.type == "float" and (not isinstance(v, (int, float)) or isinstance(v, bool)):
                raise SchemaError(f"Column '{col.name}' needs a number.")
            elif col.type == "text":
                if not isinstance(v, str):
                    raise SchemaError(f"Column '{col.name}' needs text.")
                if len(v.encode()) > col.size:
                    raise SchemaError(f"Column '{col.name}' is longer than {col.size} bytes.")
        if not 0 <= values[0] <= MAX_KEY:
            raise SchemaError(f"Primary key must be between 0 and {MAX_KEY}.")

    # ---------- bytes <-> values ----------
    def encode(self, values) -> bytes:
        self.validate(values)
        bitmap = 0
        parts = []
        for i, (col, v) in enumerate(zip(self.columns, values)):
            if v is None:
                bitmap |= 1 << i
                parts.append(bytes(col.size))                  # placeholder zeros
            elif col.type == "text":
                parts.append(v.encode().ljust(col.size, b"\0"))
            else:
                parts.append(FIXED[col.type].pack(float(v) if col.type == "float" else v))
        return bitmap.to_bytes(self.bitmap_size, "little") + b"".join(parts)

    def decode(self, data) -> tuple:
        data = bytes(data[:self.row_size])
        bitmap = int.from_bytes(data[:self.bitmap_size], "little")
        values, off = [], self.bitmap_size
        for i, col in enumerate(self.columns):
            raw = data[off:off + col.size]
            off += col.size
            if bitmap & (1 << i):
                values.append(None)
            elif col.type == "text":
                values.append(raw.rstrip(b"\0").decode())
            else:
                values.append(FIXED[col.type].unpack(raw)[0])
        return tuple(values)

    # ---------- text input from the REPL ----------
    def parse_values(self, tokens) -> tuple:
        """Turn REPL strings into typed values: 'null', 'true', '42', '3.5', 'alice'."""
        if len(tokens) != len(self.columns):
            raise SchemaError(f"Expected {len(self.columns)} values ({self}), got {len(tokens)}.")
        out = []
        for col, tok in zip(self.columns, tokens):
            if tok.lower() == "null":
                out.append(None)
            elif col.type == "int":
                try:
                    out.append(int(tok))
                except ValueError:
                    raise SchemaError(f"Column '{col.name}' needs an int, got '{tok}'.")
            elif col.type == "float":
                try:
                    out.append(float(tok))
                except ValueError:
                    raise SchemaError(f"Column '{col.name}' needs a number, got '{tok}'.")
            elif col.type == "bool":
                if tok.lower() not in ("true", "false"):
                    raise SchemaError(f"Column '{col.name}' needs true/false, got '{tok}'.")
                out.append(tok.lower() == "true")
            else:
                out.append(tok)
        return tuple(out)

    def format_row(self, values) -> str:
        def fmt(v):
            if v is None:
                return "NULL"
            if isinstance(v, bool):
                return "true" if v else "false"
            if isinstance(v, str):
                return repr(v)
            return str(v)
        return "(" + ", ".join(fmt(v) for v in values) + ")"

    def __str__(self):
        return " ".join(str(c) for c in self.columns)
