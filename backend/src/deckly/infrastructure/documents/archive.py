import io
import zipfile
from typing import Self

from deckly.application.exceptions import UnreadableDocumentError

MAX_ARCHIVE_ENTRIES = 10_000
MAX_PART_BYTES = 8 * 1024 * 1024
MAX_TOTAL_PART_BYTES = 64 * 1024 * 1024
MAX_COMPRESSION_RATIO = 100
RATIO_GRACE_BYTES = 100 * 1024
CENTRAL_DIRECTORY_SIGNATURE = b"PK\x01\x02"


def too_many_entries(count: int) -> UnreadableDocumentError:
    return UnreadableDocumentError(f"the archive has {count} entries, over {MAX_ARCHIVE_ENTRIES}")


def check_entry(entry: zipfile.ZipInfo) -> None:
    if entry.file_size > MAX_PART_BYTES:
        message = f"archive part {entry.filename!r} declares {entry.file_size} bytes, over {MAX_PART_BYTES}"
        raise UnreadableDocumentError(message)
    if entry.file_size > RATIO_GRACE_BYTES and entry.file_size > entry.compress_size * MAX_COMPRESSION_RATIO:
        message = (
            f"archive part {entry.filename!r} inflates {entry.file_size} bytes from {entry.compress_size}, "
            f"over the {MAX_COMPRESSION_RATIO}:1 ratio limit"
        )
        raise UnreadableDocumentError(message)


class BoundedArchive:
    def __init__(self, archive: zipfile.ZipFile) -> None:
        entries = archive.infolist()
        if len(entries) > MAX_ARCHIVE_ENTRIES:
            raise too_many_entries(len(entries))
        self._archive = archive
        self._entries = {entry.filename: entry for entry in entries}
        self._read_bytes = 0

    @classmethod
    def of(cls, content: bytes) -> Self:
        directory_records = content.count(CENTRAL_DIRECTORY_SIGNATURE)
        if directory_records > MAX_ARCHIVE_ENTRIES:
            raise too_many_entries(directory_records)
        return cls(zipfile.ZipFile(io.BytesIO(content)))

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_: object) -> None:
        self._archive.close()

    def has(self, name: str) -> bool:
        return name in self._entries

    def read(self, name: str) -> bytes:
        entry = self._entries.get(name)
        if entry is None:
            message = f"the archive has no part {name!r}"
            raise UnreadableDocumentError(message)
        check_entry(entry)
        if self._read_bytes + entry.file_size > MAX_TOTAL_PART_BYTES:
            message = f"the parts read from the archive would exceed {MAX_TOTAL_PART_BYTES} bytes"
            raise UnreadableDocumentError(message)
        with self._archive.open(entry) as stream:
            data = stream.read(MAX_PART_BYTES + 1)
        if len(data) > MAX_PART_BYTES:
            message = f"archive part {name!r} inflates past {MAX_PART_BYTES} bytes"
            raise UnreadableDocumentError(message)
        self._read_bytes += len(data)
        return data
