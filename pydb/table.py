import json
import os
import struct
from .pager import Pager, PAGE_SIZE
from .btree import BTree, DuplicateKey
from .schema import Schema, SchemaError
from .keys import PK_SIZE, pk_key, pk_from_key, index_key, index_key_size, index_prefix
from .rwlock import RWLock, NoLock

# Page 0 header: magic | version | main-tree root | num_rows | meta length | meta (JSON)
# meta = {"columns": [...], "indexes": {"email": <root page>, ...}}
HEADER = struct.Struct("<4sHIIH")
MAGIC, VERSION = b"PYDB", 4


class Table:
    """Thread-safe: reads (find, find_by, select) share the table; writes (insert,
    create_index, close) get it exclusively. Public methods take the lock; the
    _unlocked helpers do the work, so a locked method never re-takes the lock."""

    def __init__(self, pager, schema, tree, num_rows, indexes, locking=True):
        self.lock = RWLock() if locking else NoLock()
        self.pager = pager
        self.schema = schema
        self.tree = tree                 # main tree: primary key -> row
        self.num_rows = num_rows
        self.indexes = indexes           # column name -> BTree of index keys
        self.last_lookup = ""            # how the last find_by() was answered

    # ---------- open / create ----------
    @classmethod
    def create(cls, path: str, schema: Schema, cache_pages: int = 64, locking: bool = True) -> "Table":
        if os.path.exists(path) and os.path.getsize(path) > 0:
            raise FileExistsError(f"{path} already exists.")
        pager = Pager(path, cache_pages)
        pager.allocate_page()                                       # page 0 = header
        tree = BTree.create(pager, PK_SIZE, schema.row_size)        # page 1 = root leaf
        table = cls(pager, schema, tree, 0, {}, locking)
        table._write_header()
        return table

    @classmethod
    def open(cls, path: str, cache_pages: int = 64, locking: bool = True) -> "Table":
        pager = Pager(path, cache_pages)
        page = pager.get_page(0)
        magic, version, root, num_rows, meta_len = HEADER.unpack_from(page)
        if magic != MAGIC:
            raise ValueError("Not a pydb file.")
        if version != VERSION:
            raise ValueError(f"File is format v{version}, this code reads v{VERSION}. Delete it and recreate.")
        meta = json.loads(bytes(page[HEADER.size:HEADER.size + meta_len]))
        schema = Schema.from_json(json.dumps(meta["columns"]).encode())
        tree = BTree(pager, root, PK_SIZE, schema.row_size)
        indexes = {name: BTree(pager, iroot, index_key_size(schema.column(name)), 0)
                   for name, iroot in meta["indexes"].items()}
        return cls(pager, schema, tree, num_rows, indexes, locking)

    def _write_header(self):
        meta = json.dumps({"columns": json.loads(self.schema.to_json()),
                           "indexes": {n: t.root for n, t in self.indexes.items()}}).encode()
        if HEADER.size + len(meta) > PAGE_SIZE:
            raise ValueError("Table metadata no longer fits in the header page.")
        page = self.pager.get_page(0)
        HEADER.pack_into(page, 0, MAGIC, VERSION, self.tree.root, self.num_rows, len(meta))
        page[HEADER.size:HEADER.size + len(meta)] = meta
        self.pager.mark_dirty(0, page)

    # ---------- writes ----------
    def insert(self, values) -> None:
        with self.lock.write():
            self._insert_unlocked(values)

    def _insert_unlocked(self, values) -> None:
        row_bytes = self.schema.encode(values)                 # validate before touching disk
        pk = values[0]
        self.tree.insert(pk_key(pk), row_bytes)                # DuplicateKey stops here
        for name, idx in self.indexes.items():                 # keep every index in sync
            col = self.schema.column(name)
            idx.insert(index_key(col, values[self.schema.position(name)], pk), b"")
        self.num_rows += 1
        self._write_header()

    def create_index(self, name: str) -> int:
        with self.lock.write():
            return self._create_index_unlocked(name)

    def _create_index_unlocked(self, name: str) -> int:
        col = self.schema.column(name)                         # SchemaError if unknown
        if self.schema.position(name) == 0:
            raise SchemaError(f"'{name}' is the primary key; it is already indexed.")
        if name in self.indexes:
            raise SchemaError(f"Index on '{name}' already exists.")
        idx = BTree.create(self.pager, index_key_size(col), 0)
        pos = self.schema.position(name)
        count = 0
        for row in self._scan():                               # back-fill existing rows
            idx.insert(index_key(col, row[pos], row[0]), b"")
            count += 1
        self.indexes[name] = idx
        self._write_header()
        return count

    # ---------- reads ----------
    def find(self, pk: int):
        with self.lock.read():
            return self._find_unlocked(pk)

    def _find_unlocked(self, pk: int):
        data = self.tree.find(pk_key(pk))
        return self.schema.decode(data) if data is not None else None

    def find_by(self, name: str, value):
        """All rows where column == value (value None means IS NULL)."""
        with self.lock.read():
            return self._find_by_unlocked(name, value)

    def _find_by_unlocked(self, name: str, value):
        col = self.schema.column(name)
        pos = self.schema.position(name)
        if pos == 0:
            self.last_lookup = "primary key"
            row = self._find_unlocked(value) if value is not None else None
            return [row] if row else []
        if value is not None:
            self.schema.check_value(col, value)
        if name in self.indexes:
            prefix = index_prefix(col, value)
            lo, hi = prefix + bytes(PK_SIZE), prefix + b"\xff" * PK_SIZE
            pks = [pk_from_key(k) for k, _ in self.indexes[name].scan(lo, hi)]
            self.last_lookup = f"index on {name}"
            return [self._find_unlocked(pk) for pk in pks]
        self.last_lookup = "full scan"
        return [r for r in self._scan() if r[pos] == value]

    # Selects return LISTS, not generators: a generator would hold the read lock
    # for as long as the caller kept it alive, blocking every writer.
    def select_all(self):
        with self.lock.read():
            return list(self._scan())

    def select_range(self, lo: int, hi: int):
        with self.lock.read():
            return list(self._scan(pk_key(lo), pk_key(hi)))

    def _scan(self, lo=None, hi=None):
        return (self.schema.decode(v) for _, v in self.tree.scan(lo, hi))

    def close(self):
        with self.lock.write():
            self.pager.close()
