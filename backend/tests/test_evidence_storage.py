"""Private storage validates bytes and never overwrites finalized originals."""

import asyncio
import io
from collections.abc import AsyncIterator
from pathlib import Path
from uuid import uuid4

import pytest
from PIL import Image

from app.modules.progress.adapters.filesystem_storage import FilesystemEvidenceStorage

buffer = io.BytesIO()
Image.new("RGB", (1, 1), "white").save(buffer, format="PNG")
PNG = buffer.getvalue()


async def chunks(data: bytes) -> AsyncIterator[bytes]:
    for start in range(0, len(data), 17):
        yield data[start : start + 17]


def storage(root: Path) -> FilesystemEvidenceStorage:
    return FilesystemEvidenceStorage(root)


@pytest.mark.asyncio
async def test_private_stream_round_trip_checksum_and_immutable_replay(tmp_path: Path) -> None:
    store = storage(tmp_path)
    key = f"{uuid4()}/{uuid4()}/1"
    blob = await store.put(key, chunks(PNG), 20 * 1024 * 1024)
    assert blob.byte_length == len(PNG)
    assert blob.detected_mime == "image/png"
    assert len(blob.sha256) == 64
    assert b"".join([part async for part in store.open(key)]) == PNG
    assert await store.put(key, chunks(PNG), 20 * 1024 * 1024) == blob
    with pytest.raises(Exception, match="IDEMPOTENCY_KEY_REUSED"):
        await store.put(key, chunks(PNG + b"different"), 20 * 1024 * 1024)
    assert b"".join([part async for part in store.open(key)]) == PNG
    assert (await asyncio.to_thread(tmp_path.stat)).st_mode & 0o077 == 0


@pytest.mark.asyncio
async def test_oversize_and_spoofed_signature_leave_no_blob(tmp_path: Path) -> None:
    store = storage(tmp_path)
    key = f"{uuid4()}/{uuid4()}/1"
    with pytest.raises(Exception, match="EVIDENCE_TOO_LARGE"):
        await store.put(key, chunks(PNG), len(PNG) - 1)
    with pytest.raises(Exception, match="EVIDENCE_INVALID_FORMAT"):
        await store.put(key, chunks(b"%PDF-1.7\nnot a PDF"), 1024)
    with pytest.raises(Exception, match="EVIDENCE_INVALID_FORMAT"):
        await store.put(key, chunks(b"not an image"), 1024)
    assert not await asyncio.to_thread(lambda: list(tmp_path.rglob("1")))
    assert not await asyncio.to_thread(lambda: list(tmp_path.rglob("*.tmp")))


@pytest.mark.asyncio
async def test_path_traversal_and_symlink_are_rejected(tmp_path: Path) -> None:
    store = storage(tmp_path / "private")
    with pytest.raises(Exception, match="EVIDENCE_INVALID_KEY"):
        await store.put("../outside", chunks(PNG), 1024)
    (tmp_path / "private").mkdir()
    organization = uuid4()
    (tmp_path / "private" / str(organization)).symlink_to(tmp_path, target_is_directory=True)
    with pytest.raises(Exception, match="EVIDENCE_INVALID_KEY"):
        await store.put(f"{organization}/{uuid4()}/1", chunks(PNG), 1024)


@pytest.mark.asyncio
async def test_interrupted_stream_removes_temporary_bytes(tmp_path: Path) -> None:
    async def interrupted() -> AsyncIterator[bytes]:
        yield PNG[:20]
        raise RuntimeError("connection interrupted")

    store = storage(tmp_path)
    with pytest.raises(RuntimeError, match="connection interrupted"):
        await store.put(f"{uuid4()}/{uuid4()}/1", interrupted(), 1024)
    assert not await asyncio.to_thread(lambda: list(tmp_path.rglob("*.tmp")))


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "format_name, mime",
    [
        ("PDF", "application/pdf"),
        ("DOCX", "application/vnd.openxmlformats-officedocument.wordprocessingml.document"),
        ("JPEG", "image/jpeg"),
        ("PNG", "image/png"),
    ],
)
async def test_supported_original_formats(tmp_path: Path, format_name: str, mime: str) -> None:
    import zipfile

    from pypdf import PdfWriter

    buffer = io.BytesIO()
    if format_name == "PDF":
        writer = PdfWriter()
        writer.add_blank_page(width=100, height=100)
        writer.write(buffer)
    elif format_name == "DOCX":
        with zipfile.ZipFile(buffer, "w") as archive:
            archive.writestr(
                "[Content_Types].xml",
                '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
                '<Override PartName="/word/document.xml" '
                'ContentType="application/vnd.openxmlformats-officedocument.'
                'wordprocessingml.document.main+xml"/>'
                "</Types>",
            )
            archive.writestr(
                "word/document.xml",
                '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
                "<w:body><w:p><w:r><w:t>Evidence</w:t></w:r></w:p></w:body></w:document>",
            )
    else:
        Image.new("RGB", (2, 2), "white").save(buffer, format=format_name)
    store = storage(tmp_path)
    original = buffer.getvalue()
    blob = await store.put(f"{uuid4()}/{uuid4()}/1", chunks(original), 20 * 1024 * 1024)
    assert blob.detected_mime == mime
    assert b"".join([chunk async for chunk in store.open(blob.key)]) == original


@pytest.mark.asyncio
async def test_real_20_mib_boundary(tmp_path: Path) -> None:
    store = storage(tmp_path)
    limit = 20 * 1024 * 1024

    async def large(size: int) -> AsyncIterator[bytes]:
        yield PNG
        remaining = size - len(PNG)
        while remaining:
            length = min(remaining, 64 * 1024)
            yield b"\0" * length
            remaining -= length

    assert (await store.put(f"{uuid4()}/{uuid4()}/1", large(limit), limit)).byte_length == limit
    with pytest.raises(Exception, match="EVIDENCE_TOO_LARGE"):
        await store.put(f"{uuid4()}/{uuid4()}/1", large(limit + 1), limit)


@pytest.mark.asyncio
async def test_encrypted_pdf_and_macro_docx_are_rejected(tmp_path: Path) -> None:
    import zipfile

    from pypdf import PdfWriter

    store = storage(tmp_path)
    pdf = io.BytesIO()
    writer = PdfWriter()
    writer.add_blank_page(width=100, height=100)
    writer.encrypt("secret")
    writer.write(pdf)
    with pytest.raises(Exception, match="EVIDENCE_INVALID_FORMAT"):
        await store.put(f"{uuid4()}/{uuid4()}/1", chunks(pdf.getvalue()), 20 * 1024 * 1024)
    docx = io.BytesIO()
    with zipfile.ZipFile(docx, "w") as archive:
        archive.writestr("word/document.xml", "<document/>")
        archive.writestr("word/vbaProject.bin", "macro")
    with pytest.raises(Exception, match="EVIDENCE_INVALID_FORMAT"):
        await store.put(f"{uuid4()}/{uuid4()}/1", chunks(docx.getvalue()), 20 * 1024 * 1024)


@pytest.mark.asyncio
async def test_truncated_jpeg_is_rejected_before_publication(tmp_path: Path) -> None:
    buffer = io.BytesIO()
    Image.new("RGB", (10, 10), "white").save(buffer, format="JPEG")
    truncated = buffer.getvalue()[:-2]
    with pytest.raises(Exception, match="EVIDENCE_INVALID_FORMAT"):
        await storage(tmp_path).put(f"{uuid4()}/{uuid4()}/1", chunks(truncated), 1024)
