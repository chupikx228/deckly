from typing import Literal

from deckly.domain.exceptions import UnsupportedDocumentError

type ByteOrder = Literal["big", "little"]

BIG_ENDIAN: ByteOrder = "big"
LITTLE_ENDIAN: ByteOrder = "little"


def unsigned(content: bytes, offset: int, size: int, byteorder: ByteOrder) -> int:
    end = offset + size
    if offset < 0 or end > len(content):
        message = "the recording ends inside one of its headers"
        raise UnsupportedDocumentError(message)
    return int.from_bytes(content[offset:end], byteorder)


def big_endian(content: bytes, offset: int, size: int) -> int:
    return unsigned(content, offset, size, BIG_ENDIAN)


def little_endian(content: bytes, offset: int, size: int) -> int:
    return unsigned(content, offset, size, LITTLE_ENDIAN)
