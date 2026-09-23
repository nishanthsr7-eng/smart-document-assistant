import re
from dataclasses import dataclass

from src.core.config import SETTINGS
from src.core.tokens import count_tokens as _count_tokens
from src.ingestion.parsers import Element

_SENTENCE_RE = re.compile(r"(?<=[.!?:])\s+(?=[A-Z0-9(\[])")


@dataclass
class ParentChunk:
    parent_id: str
    doc_id: str
    filename: str
    text: str
    page_start: int
    page_end: int
    section_path: tuple[str, ...]
    kind: str


@dataclass
class ChildChunk:
    chunk_id: str
    parent_id: str
    doc_id: str
    filename: str
    text: str
    embed_text: str
    page_start: int
    page_end: int
    section_path: tuple[str, ...]
    kind: str
    char_span_in_parent: tuple[int, int]


def build_header(filename: str, section_path: tuple[str, ...]) -> str:
    title = filename.rsplit(".", 1)[0]
    return f"{title} > {' > '.join(section_path)}" if section_path else title


def chunk_document(
    elements: list[Element], doc_id: str, filename: str
) -> tuple[list[ParentChunk], list[ChildChunk]]:
    parents: list[ParentChunk] = []
    children: list[ChildChunk] = []

    for group in _group_into_parents(elements):
        parent = _build_parent(group, doc_id, filename, len(parents))
        parents.append(parent)
        children.extend(_build_children(parent, len(children)))

    return parents, children


def _group_into_parents(elements: list[Element]) -> list[list[Element]]:
    groups: list[list[Element]] = []
    current: list[Element] = []
    current_tokens = 0

    def flush() -> None:
        nonlocal current, current_tokens
        if current:
            groups.append(current)
            current = []
            current_tokens = 0

    for element in elements:
        if element.kind in ("table", "figure"):
            flush()
            groups.append([element])
            continue

        section = current[0].section_path if current else None
        element_tokens = _count_tokens(element.text)
        if current and (
            element.section_path != section
            or current_tokens + element_tokens > SETTINGS.ingestion.parent_tokens
        ):
            flush()
        current.append(element)
        current_tokens += element_tokens

    flush()
    return groups


def _build_parent(group: list[Element], doc_id: str, filename: str, index: int) -> ParentChunk:
    kind = group[0].kind if group[0].kind in ("table", "figure") else "prose"
    text = group[0].text if kind in ("table", "figure") else "\n".join(e.text for e in group)
    return ParentChunk(
        parent_id=f"{doc_id}:p{index}",
        doc_id=doc_id,
        filename=filename,
        text=text,
        page_start=min(e.page for e in group),
        page_end=max(e.page for e in group),
        section_path=group[0].section_path,
        kind=kind,
    )


def _build_children(parent: ParentChunk, start_index: int) -> list[ChildChunk]:
    if parent.kind in ("table", "figure"):
        windows = _atomic_windows(parent.text)
    else:
        windows = _sentence_windows(parent.text)

    header = build_header(parent.filename, parent.section_path)
    children: list[ChildChunk] = []
    for offset, (span_text, span) in enumerate(windows):
        children.append(
            ChildChunk(
                chunk_id=f"{parent.doc_id}:c{start_index + offset}",
                parent_id=parent.parent_id,
                doc_id=parent.doc_id,
                filename=parent.filename,
                text=span_text,
                embed_text=f"{header}\n{span_text}",
                page_start=parent.page_start,
                page_end=parent.page_end,
                section_path=parent.section_path,
                kind=parent.kind,
                char_span_in_parent=span,
            )
        )
    return children


def _atomic_windows(text: str) -> list[tuple[str, tuple[int, int]]]:
    if _count_tokens(text) <= SETTINGS.ingestion.child_tokens:
        return [(text, (0, len(text)))]

    windows: list[tuple[str, tuple[int, int]]] = []
    start = 0
    pos = 0
    tokens = 0
    for line in text.splitlines(keepends=True):
        line_tokens = _count_tokens(line)
        if pos > start and tokens + line_tokens > SETTINGS.ingestion.child_tokens:
            windows.append((text[start:pos], (start, pos)))
            start = pos
            tokens = 0
        tokens += line_tokens
        pos += len(line)
    windows.append((text[start:pos], (start, pos)))
    return windows


def _sentence_windows(text: str) -> list[tuple[str, tuple[int, int]]]:
    sentences = _split_sentences(text)
    if not sentences:
        return [(text, (0, len(text)))]

    windows: list[tuple[str, tuple[int, int]]] = []
    i = 0
    while i < len(sentences):
        j = i
        tokens = 0
        while j < len(sentences):
            tokens += _count_tokens(sentences[j][0])
            if j > i and tokens > SETTINGS.ingestion.child_tokens:
                break
            j += 1
        start = sentences[i][1]
        end = sentences[j - 1][2]
        windows.append((text[start:end], (start, end)))
        if j >= len(sentences):
            break
        i = max(i + 1, j - SETTINGS.ingestion.child_overlap_sentences)
    return windows


def _split_sentences(text: str) -> list[tuple[str, int, int]]:
    spans: list[tuple[str, int, int]] = []
    pos = 0
    for piece in _SENTENCE_RE.split(text):
        piece = piece.strip()
        if not piece:
            continue
        start = text.find(piece, pos)
        if start == -1:
            start = pos
        end = start + len(piece)
        spans.append((piece, start, end))
        pos = end
    return spans
