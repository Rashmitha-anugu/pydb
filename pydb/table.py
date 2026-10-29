import json
import os
import struct
import threading
from contextlib import contextmanager, nullcontext
from .pager import Pager, PAGE_SIZE
from .btree import BTree, DuplicateKey
from .schema import Schema, SchemaError
from .keys import PK_SIZE, pk_key, pk_from_key, index_key, index_key_size, index_prefix
from .rwlock import RWLock, NoLock

# Page 0 header: magic | version | main-tree root | num_rows | meta length | meta (JSON)
# meta = {"name": "users", "columns": [...], "indexes": {"email": <root page>, ...}}
HEADER = struct.Struct("<4sHIIH")
MAGIC, VERSION = b"PYDB", 4


class TransactionError(Exception):
    pass


class Table:
    """Thread-safe and crash-safe.

    Locking: reads (find, find_by, select) share the table; writes (insert,
    create_index) get it exclusively.

    Transactions: every insert is atomic on its own. To group several changes so
    they ALL happen or NONE do, use

        with table.transaction():
            table.insert(...)
            table.insert(...)

    (or begin() / commit() / rollback()). An open transaction holds the write
    lock, so other threads wait until it finishes.
    """

    def __init__(self, pager, locking=True):
        self.lock = RWLock() if locking else NoLock()
        self.pager = pager
        self._txn_owner = None                 # thread id that has an open transaction
        self.last_lookup = ""
        self._load_meta()

    # ---------- open / create ----------
    @classmethod
    def create(cls, path: str, schema: Schema, cache_pages: int = 64, locking: bool = True,
               name: str = "t") -> "Table":
        if os.path.exists(path) and os.path.getsize(path) > 0:
            raise FileExistsError(f"{path} already exists.")
        pager = Pager(path, cache_pages)
        pager.allocate_page()                                       # page 0 = header
        tree = BTree.create(pager, PK_SIZE, schema.row_size)        # page 1 = root leaf
        cls._write_meta(pager, schema, tree.root, 0, {}, name)
        pager.commit()
        return cls(pager, locking)

    @classmethod
    def open(cls, path: str, cache_pages: int = 64, locking: bool = True) -> "Table":
        return cls(Pager(path, cache_pages), locking)

    # ---------- header page ----------
    def _load_meta(self):
        """(Re)build in-memory state from page 0. Also used after a rollback."""
        page = self.pager.get_page(0)
        magic, version, root, num_rows, meta_len = HEADER.unpack_from(page)
        if magic != MAGIC:
            raise ValueError("Not a pydb file.")
        if version != VERSION:
            raise ValueError(f"File is format v{version}, this code reads v{VERSION}. Delete it and recreate.")
        meta = json.loads(bytes(page[HEADER.size:HEADER.size + meta_len]))
        self.schema = Schema.from_json(json.dumps(meta["columns"]).encode())
        self.name = meta.get("name", "t")
        self.tree = BTree(self.pager, root, PK_SIZE, self.schema.row_size)
        self.num_rows = num_rows
        self.indexes = {name: BTree(self.pager, iroot, index_key_size(self.schema.column(name)), 0)
                        for name, iroot in meta["indexes"].items()}

    @staticmethod
    def _write_meta(pager, schema, root, num_rows, indexes, name):
        meta = json.dumps({"name": name, "columns": json.loads(schema.to_json()),
                           "indexes": indexes}).encode()
        if HEADER.size + len(meta) > PAGE_SIZE:
            raise ValueError("Table metadata no longer fits in the header page.")
        page = pager.get_page(0)
        HEADER.pack_into(page, 0, MAGIC, VERSION, root, num_rows, len(meta))
        page[HEADER.size:HEADER.size + len(meta)] = meta
        pager.mark_dirty(0, page)

    def _save_meta(self):
        self._write_meta(self.pager, self.schema, self.tree.root, self.num_rows,
                         {n: t.root for n, t in self.indexes.items()}, self.name)

    # ---------- locking + transactions ----------
    def _in_txn(self) -> bool:
        return self._txn_owner == threading.get_ident()

    def _reading(self):
        return nullcontext() if self._in_txn() else self.lock.read()

    @contextmanager
    def _writing(self):
        """Inside a transaction: just do it. Otherwise: lock, do it, commit (or roll back)."""
        if self._in_txn():
            yield
            return
        with self.lock.write():
            try:
                yield
                self.pager.commit()
            except BaseException:
                self._rollback_unlocked()
                raise

    def begin(self) -> None:
        if self._in_txn():
            raise TransactionError("A transaction is already open.")
        self.lock.acquire_write()
        self._txn_owner = threading.get_ident()

    def commit(self) -> None:
        if not self._in_txn():
            raise TransactionError("No open transaction.")
        try:
            self.pager.commit()
        except BaseException:
            self._rollback_unlocked()
            raise
        finally:
            self._end_txn()

    def rollback(self) -> None:
        if not self._in_txn():
            raise TransactionError("No open transaction.")
        try:
            self._rollback_unlocked()
        finally:
            self._end_txn()

    def _end_txn(self):
        self._txn_owner = None
        self.lock.release_write()

    def _rollback_unlocked(self):
        self.pager.rollback()
        self._load_meta()            # roots / counts go back to the committed version

    @contextmanager
    def transaction(self):
        self.begin()
        try:
            yield self
        except BaseException:
            self.rollback()
            raise
        self.commit()

    # ---------- writes ----------
    def insert(self, values) -> None:
        with self._writing():
            row_bytes = self.schema.encode(values)             # validate before touching pages
            pk = values[0]
            try:
                self.tree.insert(pk_key(pk), row_bytes)        # DuplicateKey stops here
            except DuplicateKey:
                raise DuplicateKey(f"Duplicate primary key {pk}.") from None
            for name, idx in self.indexes.items():             # keep every index in sync
                col = self.schema.column(name)
                idx.insert(index_key(col, values[self.schema.position(name)], pk), b"")
            self.num_rows += 1
            self._save_meta()

    def create_index(self, name: str) -> int:
        with self._writing():
            col = self.schema.column(name)
            if self.schema.position(name) == 0:
                raise SchemaError(f"'{name}' is the primary key; it is already indexed.")
            if name in self.indexes:
                raise SchemaError(f"Index on '{name}' already exists.")
            idx = BTree.create(self.pager, index_key_size(col), 0)
            pos, count = self.schema.position(name), 0
            for row in self._scan():                           # back-fill existing rows
                idx.insert(index_key(col, row[pos], row[0]), b"")
                count += 1
            self.indexes[name] = idx
            self._save_meta()
            return count

    # ---------- reads ----------
    def find(self, pk: int):
        with self._reading():
            return self._find(pk)

    def _find(self, pk: int):
        data = self.tree.find(pk_key(pk))
        return self.schema.decode(data) if data is not None else None

    def find_by(self, name: str, value):
        """All rows where column == value (value None means IS NULL)."""
        with self._reading():
            col = self.schema.column(name)
            pos = self.schema.position(name)
            if pos == 0:
                self.last_lookup = "primary key"
                row = self._find(value) if value is not None else None
                return [row] if row else []
            if value is not None:
                self.schema.check_value(col, value)
            if name in self.indexes:
                prefix = index_prefix(col, value)
                lo, hi = prefix + bytes(PK_SIZE), prefix + b"\xff" * PK_SIZE
                pks = [pk_from_key(k) for k, _ in self.indexes[name].scan(lo, hi)]
                self.last_lookup = f"index on {name}"
                return [self._find(pk) for pk in pks]
            self.last_lookup = "full scan"
            return [r for r in self._scan() if r[pos] == value]

    # Selects return LISTS, not generators: a generator would hold the read lock
    # for as long as the caller kept it alive, blocking every writer.
    def select_all(self):
        with self._reading():
            return list(self._scan())

    def select_range(self, lo: int, hi: int):
        with self._reading():
            return list(self._scan(pk_key(lo), pk_key(hi)))

    def _scan(self, lo=None, hi=None):
        return (self.schema.decode(v) for _, v in self.tree.scan(lo, hi))

    def close(self):
        if self._in_txn():
            self.rollback()            # an unfinished transaction is NOT saved
        with self.lock.write():
            self.pager.close()
