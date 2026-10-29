"""A reader-writer lock.

  - Many threads may READ at the same time.
  - A WRITE gets the table to itself: it waits until current readers finish,
    and no new reader may start while it runs.
  - Writer-preferring: once a writer is waiting, new readers queue behind it,
    so a steady stream of readers can never starve writers.

Usage:
    with lock.read():   ...    # shared
    with lock.write():  ...    # exclusive
or, when the lock must be held across several calls (a transaction):
    lock.acquire_write()  ...  lock.release_write()
"""
import threading
from contextlib import contextmanager


class RWLock:
    def __init__(self):
        self._cond = threading.Condition()
        self._readers = 0            # threads currently reading
        self._writer = False         # is a thread currently writing?
        self._writers_waiting = 0

    def acquire_read(self):
        with self._cond:
            while self._writer or self._writers_waiting:
                self._cond.wait()
            self._readers += 1

    def release_read(self):
        with self._cond:
            self._readers -= 1
            if self._readers == 0:
                self._cond.notify_all()          # a waiting writer may go now

    def acquire_write(self):
        with self._cond:
            self._writers_waiting += 1
            while self._writer or self._readers:
                self._cond.wait()
            self._writers_waiting -= 1
            self._writer = True

    def release_write(self):
        with self._cond:
            self._writer = False
            self._cond.notify_all()              # wake readers and writers

    @contextmanager
    def read(self):
        self.acquire_read()
        try:
            yield
        finally:
            self.release_read()

    @contextmanager
    def write(self):
        self.acquire_write()
        try:
            yield
        finally:
            self.release_write()


class NoLock(RWLock):
    """Same interface, does nothing. Used to demonstrate what goes wrong without locking."""
    def acquire_read(self): pass
    def release_read(self): pass
    def acquire_write(self): pass
    def release_write(self): pass
