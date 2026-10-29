import struct
from .pager import Pager
from .row import Row
from .btree import BTree, DuplicateKey

# Page 0 header: magic | version | root page | num_rows
HEADER = struct.Struct("<4sHII")
MAGIC, VERSION = b"PYDB", 2


class Table:
    def __init__(self, path: str):
        self.pager = Pager(path)
        if self.pager.num_pages == 0:                      # brand-new file
            self.pager.allocate_page()                     # page 0 = header
            self.tree = BTree.create(self.pager)           # page 1 = empty root leaf
            self.num_rows = 0
            self._write_header()
        else:
            magic, version, root, self.num_rows = HEADER.unpack_from(self.pager.get_page(0))
            if magic != MAGIC:
                raise ValueError("Not a pydb file.")
            if version != VERSION:
                raise ValueError(f"File is format v{version}, this code reads v{VERSION}. Delete it and recreate.")
            self.tree = BTree(self.pager, root)

    def _write_header(self):
        HEADER.pack_into(self.pager.get_page(0), 0, MAGIC, VERSION, self.tree.root, self.num_rows)
        self.pager.mark_dirty(0)

    def insert(self, row: Row) -> None:
        self.tree.insert(row.id, row.serialize())   # raises DuplicateKey on repeats
        self.num_rows += 1
        self._write_header()                        # root may have changed after a split

    def find(self, row_id: int):
        return self.tree.find(row_id)

    def select_all(self):
        return self.tree.scan()

    def select_range(self, lo: int, hi: int):
        return self.tree.scan(lo, hi)

    def close(self):
        self.pager.close()