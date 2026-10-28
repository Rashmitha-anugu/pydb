import os

PAGE_SIZE = 4096
MAX_PAGES = 100  # temporary cap; the B+Tree milestone removes it


class PagerError(Exception):
    pass


class Pager:
    def __init__(self, path: str):
        self.path = path
        exists = os.path.exists(path)
        self._file = open(path, "r+b" if exists else "w+b")
        size = os.fstat(self._file.fileno()).st_size
        if size % PAGE_SIZE != 0:
            raise PagerError(f"Corrupt file: size {size} is not a multiple of {PAGE_SIZE}.")
        self.num_pages = size // PAGE_SIZE
        self._cache: dict[int, bytearray] = {}
        self._dirty: set[int] = set()
        self.reads = 0   # disk page reads
        self.writes = 0  # disk page writes

    def get_page(self, page_num: int) -> bytearray:
        if not 0 <= page_num < MAX_PAGES:
            raise PagerError(f"Page {page_num} out of bounds (max {MAX_PAGES}).")
        page = self._cache.get(page_num)
        if page is None:
            if page_num < self.num_pages:
                self._file.seek(page_num * PAGE_SIZE)
                page = bytearray(self._file.read(PAGE_SIZE))
                self.reads += 1
            else:
                page = bytearray(PAGE_SIZE)
                self.num_pages = page_num + 1
            self._cache[page_num] = page
        return page

    def mark_dirty(self, page_num: int) -> None:
        self._dirty.add(page_num)

    def flush(self) -> None:
        for page_num in sorted(self._dirty):
            self._file.seek(page_num * PAGE_SIZE)
            self._file.write(self._cache[page_num])
            self.writes += 1
        self._dirty.clear()
        self._file.flush()
        os.fsync(self._file.fileno())

    def close(self) -> None:
        if self._file.closed:
            return
        self.flush()
        self._file.close()