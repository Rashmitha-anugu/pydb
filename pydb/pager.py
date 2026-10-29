import os
from collections import OrderedDict

PAGE_SIZE = 4096
DEFAULT_CACHE_PAGES = 64


class PagerError(Exception):
    pass


class Pager:
    """Keeps at most `cache_pages` pages in memory. When full, the least-recently-used
    page is evicted (written to disk first if it has unsaved changes)."""

    def __init__(self, path: str, cache_pages: int = DEFAULT_CACHE_PAGES):
        if cache_pages < 1:
            raise PagerError("cache_pages must be at least 1.")
        self.path = path
        self.capacity = cache_pages
        exists = os.path.exists(path)
        self._file = open(path, "r+b" if exists else "w+b")
        size = os.fstat(self._file.fileno()).st_size
        if size % PAGE_SIZE != 0:
            raise PagerError(f"Corrupt file: size {size} is not a multiple of {PAGE_SIZE}.")
        self.num_pages = size // PAGE_SIZE
        self._cache: OrderedDict[int, bytearray] = OrderedDict()   # oldest first
        self._dirty: set[int] = set()
        self.hits = 0        # page was already in memory
        self.misses = 0      # page had to be loaded
        self.evictions = 0
        self.reads = 0       # disk page reads
        self.writes = 0      # disk page writes

    def get_page(self, page_num: int) -> bytearray:
        if page_num < 0:
            raise PagerError(f"Invalid page number {page_num}.")
        page = self._cache.get(page_num)
        if page is not None:
            self.hits += 1
            self._cache.move_to_end(page_num)          # now the most recently used
            return page
        self.misses += 1
        if page_num < self.num_pages:
            self._file.seek(page_num * PAGE_SIZE)
            data = self._file.read(PAGE_SIZE)
            page = bytearray(data.ljust(PAGE_SIZE, b"\0"))   # page may not be on disk yet
            self.reads += 1
        else:
            page = bytearray(PAGE_SIZE)
            self.num_pages = page_num + 1
        self._cache[page_num] = page
        self._evict_if_full()
        return page

    def allocate_page(self) -> int:
        page_num = self.num_pages
        self.get_page(page_num)
        return page_num

    def mark_dirty(self, page_num: int, page: bytearray) -> None:
        """Call AFTER changing a page. Passing the page itself means a change is never
        lost, even if the page was evicted while the caller was still working on it."""
        if self._cache.get(page_num) is not page:
            self._cache[page_num] = page                # put it back in the cache
        self._cache.move_to_end(page_num)
        self._dirty.add(page_num)
        self._evict_if_full()

    def _evict_if_full(self) -> None:
        while len(self._cache) > self.capacity:
            page_num, page = self._cache.popitem(last=False)   # least recently used
            if page_num in self._dirty:
                self._write(page_num, page)
                self._dirty.discard(page_num)
            self.evictions += 1

    def _write(self, page_num: int, page: bytearray) -> None:
        self._file.seek(page_num * PAGE_SIZE)
        self._file.write(page)
        self.writes += 1

    def hit_rate(self) -> float:
        total = self.hits + self.misses
        return self.hits / total if total else 0.0

    def flush(self) -> None:
        for page_num in sorted(self._dirty):
            self._write(page_num, self._cache[page_num])
        self._dirty.clear()
        self._file.flush()
        os.fsync(self._file.fileno())

    def close(self) -> None:
        if self._file.closed:
            return
        self.flush()
        self._file.close()