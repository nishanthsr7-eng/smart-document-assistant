import io

import anyio
import pytest
from pypdf import PdfWriter

from src.core.config import SETTINGS, replace
from src.core.errors import (
    DocumentTooLarge,
    DocumentTooManyPages,
    EmptyDocument,
    EncryptedDocument,
    ScannedDocument,
    UnsupportedFormat,
)
from src.ingestion import parsers, upload
from src.ingestion.parsers import parse_pdf, parse_txt, validate_upload
from src.ingestion.pipeline import doc_id_for, doc_id_for_stream


def _blank_pdf(pages: int = 2, encrypt: str | None = None) -> bytes:
    writer = PdfWriter()
    for _ in range(pages):
        writer.add_blank_page(width=612, height=792)
    if encrypt is not None:
        writer.encrypt(encrypt)
    buffer = io.BytesIO()
    writer.write(buffer)
    return buffer.getvalue()


def test_validate_rejects_unknown_extension():
    with pytest.raises(UnsupportedFormat):
        validate_upload("notes.md", b"hello")


def test_validate_rejects_zero_bytes():
    with pytest.raises(EmptyDocument):
        validate_upload("empty.txt", b"")


def test_validate_rejects_oversized():
    big = b"x" * (21 * 1024 * 1024)
    with pytest.raises(DocumentTooLarge):
        validate_upload("big.txt", big)


def test_validate_rejects_spoofed_pdf():
    with pytest.raises(UnsupportedFormat):
        validate_upload("fake.pdf", b"this is not a pdf")


def test_validate_accepts_pdf_and_txt():
    assert validate_upload("a.TXT", b"hi") == "txt"
    assert validate_upload("a.pdf", b"%PDF-1.7 ...") == "pdf"


def test_validate_rejects_pdf_bytes_spoofed_as_txt():
    with pytest.raises(UnsupportedFormat):
        validate_upload("fake.txt", b"%PDF-1.7 binary content here")


def test_validate_rejects_over_page_cap(monkeypatch):
    limited = replace(SETTINGS, ingestion=replace(SETTINGS.ingestion, max_pages=2))
    monkeypatch.setattr(parsers, "SETTINGS", limited)
    with pytest.raises(DocumentTooManyPages):
        parse_pdf(_blank_pdf(pages=3))


def test_parse_txt_detects_headings_and_lists():
    text = b"1 Introduction\n\nThis is a paragraph.\n\n- first item\n\n- second item"
    elements = parse_txt(text)
    kinds = [e.kind for e in elements]
    assert kinds[0] == "heading"
    assert "paragraph" in kinds
    assert kinds.count("list_item") == 2
    assert elements[1].section_path == ("1 Introduction",)


def test_parse_txt_cp1252_fallback():
    elements = parse_txt("Café policy details here.".encode("cp1252"))
    assert any("Café" in e.text for e in elements)


def test_parse_txt_empty_raises():
    with pytest.raises(EmptyDocument):
        parse_txt(b"   \n\n   ")


def test_parse_pdf_encrypted_raises():
    with pytest.raises(EncryptedDocument):
        parse_pdf(_blank_pdf(encrypt="secret"))


def test_parse_pdf_scanned_raises_when_ocr_disabled(monkeypatch):
    off = replace(SETTINGS, ingestion=replace(SETTINGS.ingestion, ocr_enabled=False))
    monkeypatch.setattr(parsers, "SETTINGS", off)
    with pytest.raises(ScannedDocument, match="disabled"):
        parse_pdf(_blank_pdf(pages=3))


def test_parse_pdf_scanned_raises_over_ocr_page_cap(monkeypatch):
    capped = replace(SETTINGS, ingestion=replace(SETTINGS.ingestion, ocr_max_pages=2))
    monkeypatch.setattr(parsers, "SETTINGS", capped)
    with pytest.raises(ScannedDocument, match="limited to 2 pages"):
        parse_pdf(_blank_pdf(pages=3))


def test_scanned_pdf_goes_through_ocr():
    """A rasterised page has no text layer at all, so every word here came out of OCR."""
    pdfium = pytest.importorskip("pypdfium2")
    if not (SETTINGS.paths.models / "RapidOcr").is_dir():
        pytest.skip("RapidOCR artifacts are not present in data/models")

    source = pdfium.PdfDocument(str(SETTINGS.paths.sample_docs / "leave_policy.pdf"))
    image = source[0].render(scale=2.0).to_pil().convert("RGB")
    source.close()
    buffer = io.BytesIO()
    image.save(buffer, format="PDF", resolution=144.0)
    scanned = buffer.getvalue()

    assert parsers._preflight_pdf(scanned)[1] is True
    elements = parse_pdf(scanned)
    text = " ".join(e.text for e in elements).lower()
    assert "leave" in text
    assert any(e.kind in ("paragraph", "list_item", "table") for e in elements)


# --- streaming upload receipt ---


async def _feed(data: bytes, step: int = 7):
    for start in range(0, len(data), step):
        yield data[start : start + step]


def test_receive_spools_body_and_reports_size():
    staged = anyio.run(upload.receive, "a.txt", _feed(b"hello world"))
    try:
        assert staged.extension == "txt"
        assert staged.size == 11
        assert staged.body.read() == b"hello world"
    finally:
        staged.close()


def test_receive_rejects_extension_before_reading_the_body():
    consumed = []

    async def watched():
        consumed.append(1)
        yield b"hello"

    with pytest.raises(UnsupportedFormat):
        anyio.run(upload.receive, "notes.md", watched())
    assert consumed == []


def test_receive_stops_reading_once_the_cap_is_passed():
    cap = SETTINGS.ingestion.max_upload_mb * 1024 * 1024
    step = 1024 * 1024
    served = 0

    async def endless():
        nonlocal served
        while True:
            served += step
            yield b"x" * step

    with pytest.raises(DocumentTooLarge):
        anyio.run(upload.receive, "big.txt", endless())
    # The point of streaming: the body stops being read one chunk past the cap rather than
    # being made resident in full first.
    assert served <= cap + step


def test_receive_rejects_spoofed_pdf_mid_stream():
    with pytest.raises(UnsupportedFormat):
        anyio.run(upload.receive, "fake.pdf", _feed(b"this is not a pdf"))


def test_receive_rejects_short_spoofed_pdf():
    with pytest.raises(UnsupportedFormat):
        anyio.run(upload.receive, "tiny.pdf", _feed(b"%PD"))


def test_receive_rejects_empty_body():
    with pytest.raises(EmptyDocument):
        anyio.run(upload.receive, "empty.txt", _feed(b""))


def test_streamed_doc_id_matches_the_resident_one():
    data = b"%PDF-1.7 " + b"payload" * 1000
    staged = anyio.run(upload.receive, "a.pdf", _feed(data, step=333))
    try:
        assert doc_id_for_stream("tenant-a", staged.body) == doc_id_for("tenant-a", data)
        # Rewound for the caller that stages it to the object store.
        assert staged.body.read() == data
    finally:
        staged.close()
