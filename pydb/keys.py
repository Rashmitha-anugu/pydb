"""Order-preserving key encoding.

The B+Tree compares keys as raw bytes. For an index to work, the BYTE order of
an encoded value must match its NATURAL order:  -5 < 3,  1.5 < 2.0,  "ana" < "bob".
Little-endian ints break this, so keys use these tricks instead:

  primary key  uint32 -> 4 bytes BIG-endian (most significant byte first)
  int          flip the sign bit, then big-endian   (so negatives sort first)
  float        positive: flip sign bit; negative: flip ALL bits; then big-endian
  bool         1 byte (0 / 1)
  text(n)      UTF-8, padded with NUL bytes to n  ("ab" < "abc" because NUL is smallest)

Secondary-index key = null_flag (1) | encoded value | primary key (4)
  - null_flag 0 = NULL, 1 = has value  ->  NULLs sort first
  - appending the primary key makes every entry unique, so a non-unique column
    (many people aged 30) still works: (30, id 4), (30, id 9), (30, id 12) ...
"""
import struct

PK_SIZE = 4
_U64 = struct.Struct(">Q")
_F64 = struct.Struct(">d")


def pk_key(pk: int) -> bytes:
    return pk.to_bytes(PK_SIZE, "big")


def pk_from_key(key: bytes) -> int:
    return int.from_bytes(key[-PK_SIZE:], "big")


def encode_value(col, v) -> bytes:
    if col.type == "int":
        return _U64.pack((v + 2**63) & (2**64 - 1))      # same as flipping the sign bit
    if col.type == "float":
        v = float(v) + 0.0                                  # turns -0.0 into 0.0
        bits = _U64.unpack(_F64.pack(float(v)))[0]
        bits = bits ^ (2**64 - 1) if bits >> 63 else bits | (1 << 63)
        return _U64.pack(bits)
    if col.type == "bool":
        return b"\x01" if v else b"\x00"
    return v.encode().ljust(col.size, b"\0")              # text


def index_key_size(col) -> int:
    return 1 + col.size + PK_SIZE


def index_prefix(col, v) -> bytes:
    """Everything except the primary key: identifies one VALUE."""
    if v is None:
        return b"\x00" + bytes(col.size)
    return b"\x01" + encode_value(col, v)


def index_key(col, v, pk: int) -> bytes:
    return index_prefix(col, v) + pk_key(pk)
