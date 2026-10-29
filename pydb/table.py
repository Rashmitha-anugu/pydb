import os
import struct
from .pager import Pager, PAGE_SIZE
from .btree import BTree, DuplicateKey
from .schema import Schema

# Page 0 header: magic | version | root page | num_rows | schema length | schema (JSON)
HEADER = struct.Struct("<4sHIIH")
MAGIC, VERSION = b"PYDB", 3


class Table:
    def __init__(self, pager: Pager, schema: Schema, tree: BTree, num_rows: int):
        self.pager = pager
        self.schema = schema
        self.tree = tree
        self.num_rows = num_rows

    @classmethod
    def create(cls, path: str, schema: Schema, cache_pages: int = 64) -> "Table":
        if os.path.exists(path) and os.path.getsize(path) > 0:
            raise FileExistsError(f"{path} already exists.")
        schema_bytes = schema.to_json()
        if HEADER.size + len(schema_bytes) > PAGE_SIZE:
            raise ValueError("Schema is too large to fit in the header page.")
        pager = Pager(path, cache_pages)
        pager.allocate_page()                                  # page 0 = header
        tree = BTree.create(pager, schema.row_size)            # page 1 = empty root leaf
        table = cls(pager, schema, tree, 0)
        table._write_header()
        return table

    @classmethod
    def open(cls, path: str, cache_pages: int = 64) -> "Table":
        pager = Pager(path, cache_pages)
        page = pager.get_page(0)
        magic, version, root, num_rows, schema_len = HEADER.unpack_from(page)
        if magic != MAGIC:
            raise ValueError("Not a pydb file.")
        if version != VERSION:
            raise ValueError(f"File is format v{version}, this code reads v{VERSION}. Delete it and recreate.")
        schema = Schema.from_json(bytes(page[HEADER.size:HEADER.size + schema_len]))
        return cls(pager, schema, BTree(pager, root, schema.row_size), num_rows)

    def _write_header(self):
        schema_bytes = self.schema.to_json()
        page = self.pager.get_page(0)
        HEADER.pack_into(page, 0, MAGIC, VERSION, self.tree.root, self.num_rows, len(schema_bytes))
        page[HEADER.size:HEADER.size + len(schema_bytes)] = schema_bytes
        self.pager.mark_dirty(0, page)

    def insert(self, values) -> None:
        row_bytes = self.schema.encode(values)          # validates types, NULLs, sizes
        self.tree.insert(values[0], row_bytes)          # first column is the key
        self.num_rows += 1
        self._write_header()

    def find(self, key: int):
        data = self.tree.find(key)
        return self.schema.decode(data) if data is not None else None

    def select_all(self):
        return (self.schema.decode(b) for b in self.tree.scan())

    def select_range(self, lo: int, hi: int):
        return (self.schema.decode(b) for b in self.tree.scan(lo, hi))

    def close(self):
        self.pager.close()
