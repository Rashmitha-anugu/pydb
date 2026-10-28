import struct
from .pager import Pager, PAGE_SIZE, MAX_PAGES
from .row import Row, ROW_SIZE

HEADER = struct.Struct("<4sHI")             # magic, version, num_rows
MAGIC, VERSION = b"PYDB", 1
ROWS_PER_PAGE = PAGE_SIZE // ROW_SIZE        # 14
MAX_ROWS = ROWS_PER_PAGE * (MAX_PAGES - 1)   # page 0 is the header


class TableFull(Exception):
    pass


class Table:
    def __init__(self, path: str):
        self.pager = Pager(path)
        header = self.pager.get_page(0)
        if self.pager.num_pages == 1 and header[:4] == b"\0\0\0\0":   # brand-new file
            self.num_rows = 0
            self._write_header()
        else:
            magic, version, self.num_rows = HEADER.unpack_from(header)
            if magic != MAGIC:
                raise ValueError("Not a pydb file.")

    def _write_header(self):
        HEADER.pack_into(self.pager.get_page(0), 0, MAGIC, VERSION, self.num_rows)
        self.pager.mark_dirty(0)

    def _slot(self, row_num: int):
        page_num = 1 + row_num // ROWS_PER_PAGE
        offset = (row_num % ROWS_PER_PAGE) * ROW_SIZE
        return page_num, offset

    def insert(self, row: Row) -> None:
        if self.num_rows >= MAX_ROWS:
            raise TableFull("Table full.")
        data = row.serialize()
        page_num, offset = self._slot(self.num_rows)
        page = self.pager.get_page(page_num)
        page[offset:offset + ROW_SIZE] = data
        self.pager.mark_dirty(page_num)
        self.num_rows += 1
        self._write_header()

    def select_all(self):
        for i in range(self.num_rows):
            page_num, offset = self._slot(i)
            yield Row.deserialize(memoryview(self.pager.get_page(page_num))[offset:])

    def close(self):
        self.pager.close()