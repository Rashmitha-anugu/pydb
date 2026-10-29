import struct
from bisect import bisect_left
from .pager import Pager, PAGE_SIZE

# A B+Tree mapping fixed-size BYTE keys -> fixed-size byte values.
# Keys are compared as raw bytes (b"abc" < b"abd"), so callers encode their keys
# so that byte order == the order they want (see keys.py).
#
# Node header (8 bytes): node_type (1) | unused (1) | num_keys (2) | link (4)
#   leaf:     link = next leaf page (0 = none)      cells: key | value
#   internal: link = right child                    cells: child page (4) | key
NODE_HEADER = struct.Struct("<BBHI")
LEAF, INTERNAL = 1, 2
HEADER_SIZE = NODE_HEADER.size
U32 = struct.Struct("<I")
MAX_KEY_SIZE = 1000          # keeps at least 3 keys per internal node


class DuplicateKey(Exception):
    pass


def read_header(page):
    node_type, _, num_keys, link = NODE_HEADER.unpack_from(page, 0)
    return node_type, num_keys, link


def write_header(page, node_type, num_keys, link):
    NODE_HEADER.pack_into(page, 0, node_type, 0, num_keys, link)


class BTree:
    def __init__(self, pager: Pager, root: int, key_size: int, value_size: int):
        if key_size > MAX_KEY_SIZE:
            raise ValueError(f"Keys of {key_size} bytes are too large (max {MAX_KEY_SIZE}).")
        self.pager = pager
        self.root = root
        self.key_size = key_size
        self.value_size = value_size
        self.leaf_cell_size = key_size + value_size
        self.internal_cell_size = 4 + key_size
        self.leaf_max = (PAGE_SIZE - HEADER_SIZE) // self.leaf_cell_size
        self.internal_max = (PAGE_SIZE - HEADER_SIZE) // self.internal_cell_size
        if self.leaf_max < 2:
            raise ValueError("Entries are too large to fit 2 per page.")

    @classmethod
    def create(cls, pager: Pager, key_size: int, value_size: int) -> "BTree":
        root = pager.allocate_page()
        tree = cls(pager, root, key_size, value_size)
        page = pager.get_page(root)
        tree.write_leaf(page, [], 0)
        pager.mark_dirty(root, page)
        return tree

    # ---------- node <-> python lists ----------
    def read_leaf(self, page):
        _, n, next_leaf = read_header(page)
        cells, ks = [], self.key_size
        for i in range(n):
            off = HEADER_SIZE + i * self.leaf_cell_size
            cells.append((bytes(page[off:off + ks]), bytes(page[off + ks:off + self.leaf_cell_size])))
        return cells, next_leaf

    def write_leaf(self, page, cells, next_leaf):
        write_header(page, LEAF, len(cells), next_leaf)
        for i, (key, value) in enumerate(cells):
            off = HEADER_SIZE + i * self.leaf_cell_size
            page[off:off + self.leaf_cell_size] = key + value

    def read_internal(self, page):
        _, n, right_child = read_header(page)
        keys, children = [], []
        for i in range(n):
            off = HEADER_SIZE + i * self.internal_cell_size
            children.append(U32.unpack_from(page, off)[0])
            keys.append(bytes(page[off + 4:off + self.internal_cell_size]))
        children.append(right_child)
        return keys, children

    def write_internal(self, page, keys, children):
        write_header(page, INTERNAL, len(keys), children[-1])
        for i, key in enumerate(keys):
            off = HEADER_SIZE + i * self.internal_cell_size
            U32.pack_into(page, off, children[i])
            page[off + 4:off + self.internal_cell_size] = key

    def _node_type(self, page_num):
        return read_header(self.pager.get_page(page_num))[0]

    # ---------- search ----------
    def _find_leaf(self, key):
        page_num = self.root
        while self._node_type(page_num) == INTERNAL:
            keys, children = self.read_internal(self.pager.get_page(page_num))
            page_num = children[bisect_left(keys, key)]
        return page_num

    def find(self, key: bytes):
        cells, _ = self.read_leaf(self.pager.get_page(self._find_leaf(key)))
        i = bisect_left([k for k, _ in cells], key)
        if i < len(cells) and cells[i][0] == key:
            return cells[i][1]
        return None

    def scan(self, lo: bytes = None, hi: bytes = None):
        """Yield (key, value) for lo <= key <= hi in order (None = unbounded)."""
        lo = lo if lo is not None else bytes(self.key_size)
        page_num = self._find_leaf(lo)
        while page_num:
            cells, next_leaf = self.read_leaf(self.pager.get_page(page_num))
            for key, value in cells:
                if hi is not None and key > hi:
                    return
                if key >= lo:
                    yield key, value
            page_num = next_leaf

    # ---------- insert ----------
    def insert(self, key: bytes, value: bytes):
        assert len(key) == self.key_size and len(value) == self.value_size
        split = self._insert(self.root, key, value)
        if split:
            sep_key, new_page = split
            new_root = self.pager.allocate_page()
            root_page = self.pager.get_page(new_root)
            self.write_internal(root_page, [sep_key], [self.root, new_page])
            self.pager.mark_dirty(new_root, root_page)
            self.root = new_root

    def _insert(self, page_num, key, value):
        page = self.pager.get_page(page_num)

        if read_header(page)[0] == LEAF:
            cells, next_leaf = self.read_leaf(page)
            keys = [k for k, _ in cells]
            i = bisect_left(keys, key)
            if i < len(keys) and keys[i] == key:
                raise DuplicateKey("Duplicate key.")
            cells.insert(i, (key, value))
            if len(cells) <= self.leaf_max:
                self.write_leaf(page, cells, next_leaf)
                self.pager.mark_dirty(page_num, page)
                return None
            mid = len(cells) // 2
            new_page = self.pager.allocate_page()
            right = self.pager.get_page(new_page)
            self.write_leaf(right, cells[mid:], next_leaf)
            self.pager.mark_dirty(new_page, right)
            self.write_leaf(page, cells[:mid], new_page)
            self.pager.mark_dirty(page_num, page)
            return cells[mid - 1][0], new_page

        keys, children = self.read_internal(page)
        i = bisect_left(keys, key)
        split = self._insert(children[i], key, value)
        if not split:
            return None
        sep_key, new_child = split
        keys.insert(i, sep_key)
        children.insert(i + 1, new_child)
        if len(keys) <= self.internal_max:
            self.write_internal(page, keys, children)
            self.pager.mark_dirty(page_num, page)
            return None
        mid = len(keys) // 2
        new_page = self.pager.allocate_page()
        right = self.pager.get_page(new_page)
        self.write_internal(right, keys[mid + 1:], children[mid + 1:])
        self.pager.mark_dirty(new_page, right)
        self.write_internal(page, keys[:mid], children[:mid + 1])
        self.pager.mark_dirty(page_num, page)
        return keys[mid], new_page

    # ---------- debugging ----------
    def depth(self):
        d, page_num = 1, self.root
        while self._node_type(page_num) == INTERNAL:
            page_num = self.read_internal(self.pager.get_page(page_num))[1][0]
            d += 1
        return d

    def print_tree(self, fmt=lambda k: k.hex(), max_lines=60):
        lines = []
        self._print(self.root, 0, lines, fmt)
        if len(lines) > max_lines:
            lines = lines[:max_lines] + [f"... ({len(lines) - max_lines} more lines)"]
        return "\n".join(lines)

    def _print(self, page_num, indent, lines, fmt):
        page = self.pager.get_page(page_num)
        pad = "  " * indent
        if read_header(page)[0] == LEAF:
            cells, nxt = self.read_leaf(page)
            lines.append(f"{pad}- leaf p{page_num} ({len(cells)} keys): {[fmt(k) for k, _ in cells]} -> p{nxt}")
        else:
            keys, children = self.read_internal(page)
            lines.append(f"{pad}- internal p{page_num} ({len(keys)} keys): {[fmt(k) for k in keys]}")
            for c in children:
                self._print(c, indent + 1, lines, fmt)
