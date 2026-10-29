import struct
from bisect import bisect_left
from .pager import Pager, PAGE_SIZE
from .row import Row, ROW_SIZE

# ---------- node layout (every node is exactly one page) ----------
# Common header (8 bytes): node_type (1) | unused (1) | num_keys (2) | link (4)
#   leaf:     link = page number of the next leaf (0 = none)
#   internal: link = right_child (the child for keys > the last key)
NODE_HEADER = struct.Struct("<BBHI")
LEAF, INTERNAL = 1, 2
HEADER_SIZE = NODE_HEADER.size                        # 8

# Leaf cell: key (4) + row (291) = 295 bytes
LEAF_CELL_SIZE = 4 + ROW_SIZE
LEAF_MAX = (PAGE_SIZE - HEADER_SIZE) // LEAF_CELL_SIZE   # 13

# Internal cell: child page (4) + key (4) = 8 bytes
INTERNAL_MAX = (PAGE_SIZE - HEADER_SIZE) // 8            # 511

U32 = struct.Struct("<I")
INTERNAL_CELL = struct.Struct("<II")


class DuplicateKey(Exception):
    pass


def read_header(page):
    node_type, _, num_keys, link = NODE_HEADER.unpack_from(page, 0)
    return node_type, num_keys, link


def write_header(page, node_type, num_keys, link):
    NODE_HEADER.pack_into(page, 0, node_type, 0, num_keys, link)


# ---- leaf helpers: a leaf is read into / written from a list of (key, row_bytes) ----
def read_leaf(page):
    _, n, next_leaf = read_header(page)
    cells = []
    for i in range(n):
        off = HEADER_SIZE + i * LEAF_CELL_SIZE
        key = U32.unpack_from(page, off)[0]
        cells.append((key, bytes(page[off + 4: off + LEAF_CELL_SIZE])))
    return cells, next_leaf


def write_leaf(page, cells, next_leaf):
    write_header(page, LEAF, len(cells), next_leaf)
    for i, (key, row_bytes) in enumerate(cells):
        off = HEADER_SIZE + i * LEAF_CELL_SIZE
        U32.pack_into(page, off, key)
        page[off + 4: off + LEAF_CELL_SIZE] = row_bytes


# ---- internal helpers: children has one more entry than keys ----
# keys[i] = largest key in the subtree of children[i]; children[-1] is the right child
def read_internal(page):
    _, n, right_child = read_header(page)
    keys, children = [], []
    for i in range(n):
        child, key = INTERNAL_CELL.unpack_from(page, HEADER_SIZE + i * 8)
        children.append(child)
        keys.append(key)
    children.append(right_child)
    return keys, children


def write_internal(page, keys, children):
    write_header(page, INTERNAL, len(keys), children[-1])
    for i, key in enumerate(keys):
        INTERNAL_CELL.pack_into(page, HEADER_SIZE + i * 8, children[i], key)


class BTree:
    """B+Tree keyed by uint32 id. Rows live only in leaves; leaves are linked
    left-to-right so a scan never has to go back up the tree."""

    def __init__(self, pager: Pager, root: int):
        self.pager = pager
        self.root = root

    @classmethod
    def create(cls, pager: Pager) -> "BTree":
        root = pager.allocate_page()
        page = pager.get_page(root)
        write_leaf(page, [], 0)
        pager.mark_dirty(root, page)
        return cls(pager, root)

    def _node_type(self, page_num):
        return read_header(self.pager.get_page(page_num))[0]

    # ---------- search ----------
    def _find_leaf(self, key):
        page_num = self.root
        while self._node_type(page_num) == INTERNAL:
            keys, children = read_internal(self.pager.get_page(page_num))
            page_num = children[bisect_left(keys, key)]
        return page_num

    def find(self, key):
        cells, _ = read_leaf(self.pager.get_page(self._find_leaf(key)))
        i = bisect_left([k for k, _ in cells], key)
        if i < len(cells) and cells[i][0] == key:
            return Row.deserialize(cells[i][1])
        return None

    def scan(self, lo=0, hi=2**32 - 1):
        """Yield rows with lo <= id <= hi, in key order, by walking linked leaves."""
        page_num = self._find_leaf(lo)
        while page_num:
            cells, next_leaf = read_leaf(self.pager.get_page(page_num))
            for key, row_bytes in cells:
                if key > hi:
                    return
                if key >= lo:
                    yield Row.deserialize(row_bytes)
            page_num = next_leaf

    # ---------- insert ----------
    def insert(self, key, row_bytes):
        split = self._insert(self.root, key, row_bytes)
        if split:  # the root itself split: grow the tree by one level
            sep_key, new_page = split
            new_root = self.pager.allocate_page()
            root_page = self.pager.get_page(new_root)
            write_internal(root_page, [sep_key], [self.root, new_page])
            self.pager.mark_dirty(new_root, root_page)
            self.root = new_root

    def _insert(self, page_num, key, row_bytes):
        """Insert into the subtree at page_num. Returns None, or (sep_key, new_page)
        if this node split: page_num keeps keys <= sep_key, new_page gets the rest."""
        page = self.pager.get_page(page_num)
        if read_header(page)[0] == LEAF:
            cells, next_leaf = read_leaf(page)
            keys = [k for k, _ in cells]
            i = bisect_left(keys, key)
            if i < len(keys) and keys[i] == key:
                raise DuplicateKey(f"Duplicate key {key}.")
            cells.insert(i, (key, row_bytes))
            if len(cells) <= LEAF_MAX:
                write_leaf(page, cells, next_leaf)
                self.pager.mark_dirty(page_num, page)
                return None
            # leaf split: left half stays, right half moves to a new page
            mid = len(cells) // 2
            new_page = self.pager.allocate_page()
            right = self.pager.get_page(new_page)
            write_leaf(right, cells[mid:], next_leaf)
            self.pager.mark_dirty(new_page, right)
            write_leaf(page, cells[:mid], new_page)          # relink: left -> new -> old next
            self.pager.mark_dirty(page_num, page)
            return cells[mid - 1][0], new_page

        # internal node: recurse into the right child, then absorb its split
        keys, children = read_internal(page)
        i = bisect_left(keys, key)
        split = self._insert(children[i], key, row_bytes)
        if not split:
            return None
        sep_key, new_child = split
        keys.insert(i, sep_key)
        children.insert(i + 1, new_child)
        if len(keys) <= INTERNAL_MAX:
            write_internal(page, keys, children)
            self.pager.mark_dirty(page_num, page)
            return None
        # internal split: the middle key moves UP, it is not kept in either half
        mid = len(keys) // 2
        new_page = self.pager.allocate_page()
        right = self.pager.get_page(new_page)
        write_internal(right, keys[mid + 1:], children[mid + 1:])
        self.pager.mark_dirty(new_page, right)
        write_internal(page, keys[:mid], children[:mid + 1])
        self.pager.mark_dirty(page_num, page)
        return keys[mid], new_page

    # ---------- debugging ----------
    def depth(self):
        d, page_num = 1, self.root
        while self._node_type(page_num) == INTERNAL:
            page_num = read_internal(self.pager.get_page(page_num))[1][0]
            d += 1
        return d

    def print_tree(self, page_num=None, indent=0, max_lines=60):
        lines = []
        self._print(self.root if page_num is None else page_num, indent, lines)
        if len(lines) > max_lines:
            lines = lines[:max_lines] + [f"... ({len(lines) - max_lines} more lines)"]
        return "\n".join(lines)

    def _print(self, page_num, indent, lines):
        page = self.pager.get_page(page_num)
        pad = "  " * indent
        if read_header(page)[0] == LEAF:
            cells, nxt = read_leaf(page)
            keys = [k for k, _ in cells]
            lines.append(f"{pad}- leaf p{page_num} ({len(keys)} keys): {keys} -> p{nxt}")
        else:
            keys, children = read_internal(page)
            lines.append(f"{pad}- internal p{page_num} ({len(keys)} keys): {keys}")
            for c in children:
                self._print(c, indent + 1, lines)
