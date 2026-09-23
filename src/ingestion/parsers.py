import io
import re
import unicodedata
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Literal, Optional

from src.core.config import SETTINGS
from src.core.errors import (
    DocumentTooLarge,
    DocumentTooManyPages,
    EmptyDocument,
    EncryptedDocument,
    ScannedDocument,
    UnsupportedFormat,
)

ElementKind = Literal["heading", "paragraph", "list_item", "table", "figure"]

_PDF_MAGIC = b"%PDF-"
_ALLOWED_EXTENSIONS = ("pdf", "txt")


@dataclass
class Element:
    kind: ElementKind
    text: str
    page: int
    section_path: tuple[str, ...] = field(default_factory=tuple)


def validate_upload(filename: str, data: bytes) -> str:
    extension = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    if extension not in _ALLOWED_EXTENSIONS:
        raise UnsupportedFormat(extension or filename)
    if not data:
        raise EmptyDocument()
    max_bytes = SETTINGS.ingestion.max_upload_mb * 1024 * 1024
    if len(data) > max_bytes:
        raise DocumentTooLarge(SETTINGS.ingestion.max_upload_mb)
    if extension == "pdf" and not data.startswith(_PDF_MAGIC):
        raise UnsupportedFormat("pdf")
    if extension == "txt" and data.startswith(_PDF_MAGIC):
        raise UnsupportedFormat("pdf")
    return extension


class FigureCaptioner:
    def __init__(self) -> None:
        from transformers import BlipForConditionalGeneration, BlipProcessor

        name = SETTINGS.models.blip_name
        self._processor = BlipProcessor.from_pretrained(name)
        self._model = BlipForConditionalGeneration.from_pretrained(name)
        self._model.eval()

    def caption(self, image) -> str:
        import torch

        inputs = self._processor(image.convert("RGB"), return_tensors="pt")
        with torch.no_grad():
            out = self._model.generate(**inputs, max_new_tokens=40)
        return self._processor.decode(out[0], skip_special_tokens=True).strip()


def parse(extension: str, data: bytes, captioner: Optional[FigureCaptioner] = None) -> list[Element]:
    if extension == "pdf":
        return parse_pdf(data, captioner)
    return parse_txt(data)


def parse_txt(data: bytes) -> list[Element]:
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        try:
            text = data.decode("cp1252")
        except UnicodeDecodeError as exc:
            raise EmptyDocument() from exc

    text = _normalise(text)
    elements: list[Element] = []
    section_stack: dict[int, str] = {}

    for block in re.split(r"\n\s*\n", text):
        block = block.strip()
        if not block:
            continue
        depth = _txt_heading_depth(block)
        if depth is not None:
            _push_heading(section_stack, depth, block)
            elements.append(Element("heading", block, 1, _section_path(section_stack)))
            continue
        kind: ElementKind = "list_item" if re.match(r"^\s*[-*•]\s+", block) else "paragraph"
        elements.append(Element(kind, block, 1, _section_path(section_stack)))

    if not any(e.kind in ("paragraph", "list_item") for e in elements):
        raise EmptyDocument()
    return elements


def parse_pdf(data: bytes, captioner: Optional[FigureCaptioner] = None) -> list[Element]:
    pages_text = _preflight_pdf(data)
    fast = _try_fast_pdf(pages_text)
    if fast:
        return fast

    from docling_core.types.doc.document import (
        ListItem,
        PictureItem,
        SectionHeaderItem,
        TableItem,
        TextItem,
    )

    result = _converter().convert(_docling_source(data))
    doc = result.document

    elements: list[Element] = []
    section_stack: dict[int, str] = {}

    for item, level in doc.iterate_items():
        prov = getattr(item, "prov", None)
        page = prov[0].page_no if prov else 1

        if isinstance(item, SectionHeaderItem):
            heading = _clean(item.text)
            if not heading:
                continue
            depth = _pdf_heading_depth(heading, level)
            _push_heading(section_stack, depth, heading)
            elements.append(Element("heading", heading, page, _section_path(section_stack)))
            continue

        path = _section_path(section_stack)

        if isinstance(item, TableItem):
            markdown = item.export_to_markdown(doc).strip()
            linearised = _linearise_table(item, doc)
            text = f"{markdown}\n\n{linearised}".strip() if linearised else markdown
            if re.search(r"[A-Za-z0-9]", text):
                elements.append(Element("table", text, page, path))
            continue

        if isinstance(item, PictureItem):
            caption = _caption_figure(item, doc, page, captioner)
            if caption:
                elements.append(Element("figure", caption, page, path))
            continue

        if isinstance(item, TextItem):
            text = _clean(item.text)
            if not text:
                continue
            kind: ElementKind = "list_item" if isinstance(item, ListItem) else "paragraph"
            elements.append(Element(kind, text, page, path))

    elements = _strip_running_lines(elements)

    if not any(e.kind in ("paragraph", "list_item", "table") for e in elements):
        raise EmptyDocument()
    return elements


def _preflight_pdf(data: bytes) -> list[tuple[int, str]]:
    from pypdf import PdfReader
    from pypdf.errors import PdfReadError

    try:
        reader = PdfReader(io.BytesIO(data))
    except PdfReadError as exc:
        raise EmptyDocument() from exc

    if reader.is_encrypted and reader.decrypt("") == 0:
        raise EncryptedDocument()

    page_count = len(reader.pages)
    if page_count > SETTINGS.ingestion.max_pages:
        raise DocumentTooManyPages(SETTINGS.ingestion.max_pages)

    pages_text: list[tuple[int, str]] = []
    total_chars = 0
    for i, page in enumerate(reader.pages, 1):
        text = (page.extract_text() or "").strip()
        total_chars += len(text)
        pages_text.append((i, text))
    if page_count and total_chars / page_count < SETTINGS.ingestion.min_chars_per_page:
        raise ScannedDocument()
    return pages_text


_FAST_PDF_MIN_CHARS_PER_PAGE = 200


def _try_fast_pdf(pages_text: list[tuple[int, str]]) -> Optional[list[Element]]:
    if not pages_text:
        return None
    total_chars = sum(len(t) for _, t in pages_text)
    avg = total_chars / len(pages_text) if pages_text else 0
    if avg < _FAST_PDF_MIN_CHARS_PER_PAGE:
        return None

    elements: list[Element] = []
    section_stack: dict[int, str] = {}
    for page_num, text in pages_text:
        text = _normalise(text)
        for block in re.split(r"\n\s*\n", text):
            block = block.strip()
            if not block:
                continue
            depth = _txt_heading_depth(block)
            if depth is not None:
                _push_heading(section_stack, depth, block)
                elements.append(Element("heading", block, page_num, _section_path(section_stack)))
                continue
            kind: ElementKind = "list_item" if re.match(r"^\s*[-*]\s+", block) else "paragraph"
            elements.append(Element(kind, block, page_num, _section_path(section_stack)))

    elements = _strip_running_lines(elements)
    if not any(e.kind in ("paragraph", "list_item") for e in elements):
        return None
    return elements


@lru_cache(maxsize=1)
def _converter():
    from docling.datamodel.base_models import InputFormat
    from docling.datamodel.pipeline_options import PdfPipelineOptions
    from docling.document_converter import DocumentConverter, PdfFormatOption

    options = PdfPipelineOptions()
    options.artifacts_path = str(SETTINGS.paths.models)
    options.do_ocr = False
    options.do_table_structure = True
    options.generate_picture_images = True
    options.do_picture_classification = True
    return DocumentConverter(
        format_options={InputFormat.PDF: PdfFormatOption(pipeline_options=options)}
    )


def _docling_source(data: bytes):
    from docling.datamodel.base_models import DocumentStream

    return DocumentStream(name="upload.pdf", stream=io.BytesIO(data))


def _caption_figure(item, doc, page: int, captioner: Optional[FigureCaptioner]) -> str:
    if captioner is None:
        return f"[Figure, p.{page}]"
    try:
        image = item.get_image(doc)
    except Exception:
        image = None
    if image is None:
        return f"[Figure, p.{page}]"
    caption = captioner.caption(image)
    return f"[Figure, p.{page}: {caption}]" if caption else f"[Figure, p.{page}]"


def _linearise_table(item, doc) -> str:
    try:
        frame = item.export_to_dataframe(doc)
    except Exception:
        return ""
    if frame.empty:
        return ""
    columns = [str(c) for c in frame.columns]
    rows = []
    for _, row in frame.iterrows():
        cells = [f"{col}: {row[orig]}" for col, orig in zip(columns, frame.columns) if str(row[orig]).strip()]
        if cells:
            rows.append("; ".join(cells))
    return "\n".join(rows)


def _pdf_heading_depth(text: str, docling_level: int) -> int:
    stripped = text.strip().lstrip("*-").strip()
    if re.match(r"^\d+(\.\d+)*\s", stripped):
        return 1
    if re.match(r"^[A-Z]\s", stripped):
        return 2
    return max(1, docling_level)


def _txt_heading_depth(block: str) -> Optional[int]:
    if "\n" in block:
        return None
    line = block.strip()
    if len(line) > 120:
        return None
    if re.match(r"^\d+(\.\d+)*\s", line):
        return 1
    if line.isupper() and len(line.split()) <= 12:
        return 1
    if line.endswith(":") and len(line.split()) <= 12:
        return 2
    return None


def _push_heading(stack: dict[int, str], depth: int, text: str) -> None:
    for deeper in [d for d in stack if d >= depth]:
        del stack[deeper]
    stack[depth] = text


def _section_path(stack: dict[int, str]) -> tuple[str, ...]:
    return tuple(stack[d] for d in sorted(stack))


def _clean(text: Optional[str]) -> str:
    return _normalise(text) if text else ""


def _normalise(text: str) -> str:
    text = unicodedata.normalize("NFKC", text)
    text = re.sub(r"(\w)-\n(\w)", r"\1\2", text)
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _strip_running_lines(elements: list[Element]) -> list[Element]:
    pages = {e.page for e in elements}
    if len(pages) < 3:
        return elements
    counts: dict[str, set[int]] = {}
    for element in elements:
        if element.kind in ("heading", "paragraph"):
            counts.setdefault(element.text, set()).add(element.page)
    threshold = len(pages) * 0.5
    recurring = {text for text, seen in counts.items() if len(seen) > threshold}
    return [e for e in elements if e.text not in recurring]
