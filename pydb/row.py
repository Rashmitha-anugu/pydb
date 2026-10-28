import struct
from dataclasses import dataclass

USERNAME_SIZE = 32
EMAIL_SIZE = 255
_ROW = struct.Struct(f"<I{USERNAME_SIZE}s{EMAIL_SIZE}s")  # 4 + 32 + 255 bytes
ROW_SIZE = _ROW.size  # 291
MAX_ID = 2**32 - 1


class RowError(ValueError):
    pass


@dataclass(frozen=True)
class Row:
    id: int
    username: str
    email: str

    def validate(self):
        if not 0 <= self.id <= MAX_ID:
            raise RowError(f"ID must be between 0 and {MAX_ID}.")
        if len(self.username.encode()) > USERNAME_SIZE:
            raise RowError(f"Username is longer than {USERNAME_SIZE} bytes.")
        if len(self.email.encode()) > EMAIL_SIZE:
            raise RowError(f"Email is longer than {EMAIL_SIZE} bytes.")

    def serialize(self) -> bytes:
        self.validate()
        return _ROW.pack(self.id, self.username.encode(), self.email.encode())

    @classmethod
    def deserialize(cls, data):
        rid, user, email = _ROW.unpack(bytes(data[:ROW_SIZE]))
        return cls(rid, user.rstrip(b"\0").decode(), email.rstrip(b"\0").decode())

    def __str__(self):
        return f"({self.id}, {self.username}, {self.email})"