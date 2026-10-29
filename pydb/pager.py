import os
import threading
from collections import OrderedDict
from .wal import WAL

PAGE_SIZE = 4096
DEFAULT_CACHE_PAGES = 64
CHECKPOINT_FRAMES = 1000      # fold the log into the database file after this many frames


class PagerError(Exception):
    pass


class Pager:
    """Pages in memory (LRU cache) + crash-safe writes through a write-ahead log.

    Changes are grouped into transactions:
      - mark_dirty() records a changed page. Dirty pages are NEVER evicted, so a
        half-finished change can't leak into the database file ("no-steal").
      - commit() writes every dirty page to the log and fsyncs it (now durable),
        then copies them into the database file.
      - rollback() throws dirty pages away; the next read gets the committed version.
      - checkpoint() fsyncs the database file and empties the log.

    Thread-safe: every public method runs under one mutex.
    """

    def __init__(self, path: str, cache_pages: int = DEFAULT_CACHE_PAGES):
        if cache_pages < 1:
            raise PagerError("cache_pages must be at least 1.")
        self.path = path
        self.capacity = cache_pages
        wal_path = path + "-wal"
        exists = os.path.exists(path)
        if not exists and os.path.exists(wal_path):
            os.remove(wal_path)                     # log left over from a deleted database
        self._file = open(path, "r+b" if exists else "w+b")
        self.wal = WAL(wal_path, PAGE_SIZE)
        self._mutex = threading.RLock()
        self._cache: OrderedDict[int, bytearray] = OrderedDict()
        self._dirty: set[int] = set()
        self.hits = self.misses = self.evictions = 0
        self.reads = self.writes = 0
        self.commits = self.checkpoints = 0
        self.recovered_transactions = self._recover()
        size = os.fstat(self._file.fileno()).st_size
        if size % PAGE_SIZE != 0:
            raise PagerError(f"Corrupt file: size {size} is not a multiple of {PAGE_SIZE}.")
        self.num_pages = size // PAGE_SIZE
        self._committed_num_pages = self.num_pages

    # ---------- crash recovery ----------
    def _recover(self) -> int:
        """Replay every committed transaction in the log into the database file."""
        count = 0
        for pages in self.wal.read_committed():
            for page_num, data in pages.items():
                self._file.seek(page_num * PAGE_SIZE)
                self._file.write(data)
            count += 1
        if count or os.path.getsize(self.wal.path):
            self._file.flush()
            os.fsync(self._file.fileno())
            self.wal.truncate()          # everything useful is now in the database file
        return count

    # ---------- page access ----------
    def get_page(self, page_num: int) -> bytearray:
        with self._mutex:
            if page_num < 0:
                raise PagerError(f"Invalid page number {page_num}.")
            page = self._cache.get(page_num)
            if page is not None:
                self.hits += 1
                self._cache.move_to_end(page_num)
                return page
            self.misses += 1
            if page_num < self.num_pages:
                self._file.seek(page_num * PAGE_SIZE)
                data = self._file.read(PAGE_SIZE)
                page = bytearray(data.ljust(PAGE_SIZE, b"\0"))
                self.reads += 1
            else:
                page = bytearray(PAGE_SIZE)
                self.num_pages = page_num + 1
            self._cache[page_num] = page
            self._evict_if_full()
            return page

    def allocate_page(self) -> int:
        with self._mutex:
            page_num = self.num_pages
            page = self.get_page(page_num)
            self.mark_dirty(page_num, page)       # a new page must be written by the commit
            return page_num

    def mark_dirty(self, page_num: int, page: bytearray) -> None:
        """Call AFTER changing a page. Passing the page itself means a change is never
        lost, even if the page was evicted while the caller was still working on it."""
        with self._mutex:
            if self._cache.get(page_num) is not page:
                self._cache[page_num] = page
            self._cache.move_to_end(page_num)
            self._dirty.add(page_num)

    def _evict_if_full(self) -> None:
        """Evict least-recently-used CLEAN pages. Dirty pages stay until commit."""
        if len(self._cache) <= self.capacity:
            return
        for page_num in list(self._cache):
            if len(self._cache) <= self.capacity:
                break
            if page_num not in self._dirty:
                del self._cache[page_num]
                self.evictions += 1

    def hit_rate(self) -> float:
        total = self.hits + self.misses
        return self.hits / total if total else 0.0

    # ---------- transactions ----------
    def commit(self) -> None:
        with self._mutex:
            if not self._dirty:
                return
            pages = {n: self._cache[n] for n in self._dirty}
            self.wal.append_transaction(pages)          # 1. durable in the log
            for n, page in sorted(pages.items()):       # 2. copy into the database file
                self._file.seek(n * PAGE_SIZE)
                self._file.write(page)
                self.writes += 1
                self.wal._hook("data_page_written")     # crash here: log replays the rest
            self._file.flush()
            self._dirty.clear()
            self._committed_num_pages = self.num_pages
            self.commits += 1
            if self.wal.frames >= CHECKPOINT_FRAMES:
                self.checkpoint()
            self._evict_if_full()

    def rollback(self) -> None:
        with self._mutex:
            for n in self._dirty:
                self._cache.pop(n, None)                 # next read comes from disk
            self._dirty.clear()
            self.num_pages = self._committed_num_pages

    def checkpoint(self) -> None:
        with self._mutex:
            self._file.flush()
            os.fsync(self._file.fileno())               # database file now has everything
            self.wal.truncate()
            self.checkpoints += 1

    def close(self) -> None:
        with self._mutex:
            if self._file.closed:
                return
            self.commit()
            self.checkpoint()
            self._file.close()
            self.wal.close()
