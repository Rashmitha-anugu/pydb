"""Write-ahead log (WAL).

The rule: a change reaches the database file only AFTER it is safely in the log.

The log is a file next to the database (my.db-wal) holding records:

    page frame:     b"P" | page_num (4) | page bytes (4096)
    commit record:  b"C" | frame_count (4) | crc32 of those frames (4)

A transaction = some page frames followed by ONE commit record. On open, the
database replays every transaction whose commit record is present and whose
checksum matches. Anything after the last good commit record was a write that
was interrupted by a crash; it is ignored, so the database never sees half of it.
"""
import os
import struct
import zlib

FRAME = struct.Struct("<cI")          # type, page_num
COMMIT = struct.Struct("<cII")        # type, frame_count, crc32


class WAL:
    def __init__(self, path: str, page_size: int):
        self.path = path
        self.page_size = page_size
        self._file = open(path, "a+b")
        self.frames = 0                   # frames in the log since the last checkpoint
        self.crash_hook = None            # tests set this to simulate a crash at a given point

    def _hook(self, point):
        if self.crash_hook:
            self.crash_hook(point)

    def append_transaction(self, pages: dict) -> None:
        """Write all pages + a commit record, then fsync. When this returns, the
        transaction is DURABLE: it will survive a crash or power loss."""
        body = b"".join(FRAME.pack(b"P", n) + bytes(p) for n, p in sorted(pages.items()))
        self._file.seek(0, os.SEEK_END)
        self._file.write(body)
        self._hook("wal_frames_written")             # crash here: no commit record -> discarded
        self._file.write(COMMIT.pack(b"C", len(pages), zlib.crc32(body)))
        self._file.flush()
        os.fsync(self._file.fileno())
        self.frames += len(pages)
        self._hook("wal_committed")                  # crash here: replayed on next open

    def read_committed(self):
        """Yield {page_num: page_bytes} for each complete, checksum-valid transaction."""
        self._file.seek(0)
        data = self._file.read()
        pos, frame_size = 0, FRAME.size + self.page_size
        pending, start = {}, 0
        while pos < len(data):
            kind = data[pos:pos + 1]
            if kind == b"P" and pos + frame_size <= len(data):
                (_, page_num) = FRAME.unpack_from(data, pos)
                pending[page_num] = data[pos + FRAME.size:pos + frame_size]
                pos += frame_size
            elif kind == b"C" and pos + COMMIT.size <= len(data):
                _, count, crc = COMMIT.unpack_from(data, pos)
                if zlib.crc32(data[start:pos]) != crc or count != len(pending):
                    return                                # corrupt: stop here
                yield pending
                pos += COMMIT.size
                pending, start = {}, pos
            else:
                return                                    # torn record at the end: stop

    def truncate(self) -> None:
        self._file.seek(0)
        self._file.truncate()
        self._file.flush()
        os.fsync(self._file.fileno())
        self.frames = 0

    def close(self) -> None:
        self._file.close()
