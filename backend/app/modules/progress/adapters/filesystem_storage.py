"""Bounded private filesystem originals with atomic, immutable publication."""

import asyncio
import hashlib
import os
import re
import sys
import tempfile
import warnings
import zipfile
from collections.abc import AsyncIterator
from pathlib import Path
from xml.etree import ElementTree

from PIL import Image
from pypdf import PdfReader

from app.modules.progress.domain.evidence import EvidenceError, StoredBlob

_KEY = re.compile(r"^[0-9a-f-]{36}/[0-9a-f-]{36}/[1-9][0-9]*$")
_DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


def detect_original_format(path: Path) -> str:
    """Validate supported containers without executing or extracting their contents."""
    try:
        with path.open("rb") as stream:
            signature = stream.read(8)
        if signature.startswith(b"%PDF-"):
            reader = PdfReader(path, strict=True)
            if reader.is_encrypted or not reader.pages:
                raise ValueError("unsupported PDF")
            return "application/pdf"
        if signature.startswith(b"PK\x03\x04"):
            with zipfile.ZipFile(path) as archive:
                entries = archive.infolist()
                if len(entries) > 2000 or sum(e.file_size for e in entries) > 100 * 1024 * 1024:
                    raise ValueError("archive limit")
                names = [e.filename for e in entries]
                if len(set(names)) != len(names) or "word/document.xml" not in names:
                    raise ValueError("not a DOCX")
                if any(e.flag_bits & 1 for e in entries):
                    raise ValueError("encrypted archive")
                for name in ("[Content_Types].xml", "word/document.xml"):
                    data = archive.read(name)
                    if b"<!DOCTYPE" in data.upper() or b"<!ENTITY" in data.upper():
                        raise ValueError("unsafe XML")
                    root = ElementTree.fromstring(data)
                    if name == "[Content_Types].xml" and not any(
                        item.attrib.get("ContentType")
                        == (
                            "application/vnd.openxmlformats-officedocument."
                            "wordprocessingml.document.main+xml"
                        )
                        for item in root
                    ):
                        raise ValueError("unsupported document")
                if any("vbaproject" in n.lower() for n in names):
                    raise ValueError("macros")
                if archive.testzip() is not None:
                    raise ValueError("corrupt archive")
            return _DOCX_MIME
        if signature.startswith(b"\x89PNG\r\n\x1a\n") or signature.startswith(b"\xff\xd8\xff"):
            with warnings.catch_warnings():
                warnings.simplefilter("error", Image.DecompressionBombWarning)
                with Image.open(path) as image:
                    if image.width * image.height > 25_000_000:
                        raise ValueError("pixel limit")
                    image_format = image.format
                    image.verify()
                with Image.open(path) as image:
                    image.load()
                return {"PNG": "image/png", "JPEG": "image/jpeg"}[str(image_format)]
    except Exception as exc:
        raise EvidenceError("EVIDENCE_INVALID_FORMAT") from exc
    raise EvidenceError("EVIDENCE_INVALID_FORMAT")


class FilesystemEvidenceStorage:
    def __init__(self, root: Path) -> None:
        self.root = root.absolute()
        self.backend_root = Path(__file__).resolve().parents[4]

    def _path(self, key: str) -> Path:
        if not _KEY.fullmatch(key):
            raise EvidenceError("EVIDENCE_INVALID_KEY")
        path = self.root / key
        if any(p.is_symlink() for p in (self.root, *path.parents, path)):
            raise EvidenceError("EVIDENCE_INVALID_KEY")
        return path

    async def put(self, key: str, source: AsyncIterator[bytes], max_bytes: int) -> StoredBlob:
        path = self._path(key)
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        for directory in (self.root, path.parent.parent, path.parent):
            directory.chmod(0o700)
        descriptor, temporary = tempfile.mkstemp(suffix=".tmp", dir=path.parent)
        temp = Path(temporary)
        checksum = hashlib.sha256()
        length = 0
        try:
            with os.fdopen(descriptor, "wb") as target:
                async for chunk in source:
                    length += len(chunk)
                    if length > max_bytes:
                        raise EvidenceError("EVIDENCE_TOO_LARGE", 413)
                    checksum.update(chunk)
                    await asyncio.to_thread(target.write, chunk)
                await asyncio.to_thread(target.flush)
                await asyncio.to_thread(os.fsync, target.fileno())
            process = await asyncio.create_subprocess_exec(
                sys.executable,
                "-m",
                "app.modules.progress.adapters.evidence_validator",
                str(temp),
                cwd=str(self.backend_root),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
            )
            try:
                async with asyncio.timeout(60):
                    output, _ = await process.communicate()
                if process.returncode != 0:
                    raise EvidenceError("EVIDENCE_INVALID_FORMAT")
                mime = output.decode("ascii").strip()
            finally:
                if process.returncode is None:
                    process.kill()
                    await process.wait()
            blob = StoredBlob(key, checksum.hexdigest(), length, mime)
            try:
                # Hard-link publication is atomic and refuses replacement.
                os.link(temp, path)
            except FileExistsError:
                existing = await asyncio.to_thread(path.read_bytes)
                if hashlib.sha256(existing).hexdigest() != blob.sha256:
                    raise EvidenceError("IDEMPOTENCY_KEY_REUSED", 409) from None
            directory_fd = os.open(path.parent, os.O_DIRECTORY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
            return blob
        finally:
            await asyncio.to_thread(temp.unlink, missing_ok=True)

    async def open(self, key: str) -> AsyncIterator[bytes]:
        path = self._path(key)
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(descriptor, "rb") as source:
            while chunk := await asyncio.to_thread(source.read, 64 * 1024):
                yield chunk

    async def delete(self, key: str) -> None:
        path = self._path(key)
        # A lease must be expired and the metadata row locked by the caller.
        await asyncio.to_thread(self._delete, path)

    def _delete(self, path: Path) -> None:
        path.unlink(missing_ok=True)
        if path.parent.exists():
            for temporary in path.parent.glob("*.tmp"):
                temporary.unlink(missing_ok=True)
