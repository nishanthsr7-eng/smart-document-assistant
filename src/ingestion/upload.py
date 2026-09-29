import hashlib
from dataclasses import dataclass
from tempfile import SpooledTemporaryFile
from typing import IO, AsyncIterator

from src.core.config import SETTINGS
from src.core.errors import DocumentTooLarge, EmptyDocument
from src.ingestion.parsers import MAGIC_BYTES, allowed_extension, check_magic


@dataclass
class StagedUpload:
    extension: str
    size: int
    body: IO[bytes]

    def close(self) -> None:
        self.body.close()


async def receive(filename: str, chunks: AsyncIterator[bytes]) -> StagedUpload:
    """Spool an upload to disk, enforcing the size cap as the bytes arrive.

    Reading the whole body first and checking the cap afterwards kills the process on a large
    POST: 2 GB is resident before validation gets a chance to reject it.
    """
    extension = allowed_extension(filename)
    max_bytes = SETTINGS.ingestion.max_upload_mb * 1024 * 1024
    # Outlives this call: the caller stages it to the object store, then closes it.
    body: IO[bytes] = SpooledTemporaryFile(  # noqa: SIM115
        max_size=SETTINGS.ingestion.upload_spool_mb * 1024 * 1024
    )
    size = 0
    head = b""
    checked = False
    try:
        async for chunk in chunks:
            size += len(chunk)
            if size > max_bytes:
                raise DocumentTooLarge(SETTINGS.ingestion.max_upload_mb)
            if not checked:
                head = (head + chunk)[:MAGIC_BYTES]
                if len(head) == MAGIC_BYTES:
                    check_magic(extension, head)
                    checked = True
            body.write(chunk)
        if size == 0:
            raise EmptyDocument()
        if not checked:
            check_magic(extension, head)
    except BaseException:
        body.close()
        raise
    body.seek(0)
    return StagedUpload(extension=extension, size=size, body=body)


def digest(salt: bytes, body: IO[bytes]) -> str:
    """sha256 of salt || body, leaving the stream rewound for the caller."""
    sha = hashlib.sha256(salt)
    for chunk in iter(lambda: body.read(SETTINGS.ingestion.upload_chunk_bytes), b""):
        sha.update(chunk)
    body.seek(0)
    return sha.hexdigest()
