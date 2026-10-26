from __future__ import annotations

import hashlib
from typing import Any

from markdown_it import MarkdownIt


def chunk_document(
    content: str, *, article_id: str, version: int, max_chars: int = 1800
) -> list[dict[str, Any]]:
    """Chunk normalized Markdown on parser block boundaries, retaining exact source offsets."""
    if max_chars <= 0:
        raise ValueError("max_chars must be positive")
    if not content:
        return []

    lines = content.splitlines(keepends=True)
    offsets = [0]
    for line in lines:
        offsets.append(offsets[-1] + len(line))
    if offsets[-1] < len(content):
        offsets.append(len(content))

    tokens = MarkdownIt("commonmark", {"html": True}).enable("table").parse(content)
    spans: list[tuple[int, int, str | None]] = []
    headings: dict[int, tuple[int, str]] = {}
    for index, token in enumerate(tokens):
        if token.type == "heading_open" and token.map:
            inline = next(
                (candidate for candidate in tokens[index + 1 :] if candidate.type == "inline"),
                None,
            )
            if inline is not None:
                headings[token.map[0]] = (int(token.tag[1:]), inline.content.strip())
        if token.level != 0 or not token.block or not token.map:
            continue
        start_line, end_line = token.map
        code = token.type in {"fence", "code_block"}
        spans.append((start_line, end_line, "code" if code else None))

    # Parser emits paired container/open/close maps. Merge overlapping ranges into AST blocks.
    merged: list[tuple[int, int, str | None]] = []
    for start_line, end_line, kind in sorted(spans):
        if merged and start_line < merged[-1][1]:
            old_start, old_end, old_kind = merged[-1]
            merged[-1] = (old_start, max(old_end, end_line), "code" if kind == "code" or old_kind == "code" else None)
        elif start_line < end_line:
            merged.append((start_line, end_line, kind))

    if not merged:
        merged = [(0, len(lines), None)]

    # Flush before headings and at size boundaries. Assign blank lines at each boundary
    # to the preceding group so adjacent ranges cover every original character exactly.
    groups: list[tuple[int, int]] = []
    group_start = 0
    group_end = 0
    has_block = False
    for start_line, end_line, kind in merged:
        start_line = max(start_line, group_end)
        end_line = max(end_line, start_line)
        is_heading = start_line in headings
        code_size = offsets[min(end_line, len(offsets) - 1)] - offsets[start_line]
        if has_block and is_heading:
            groups.append((group_start, start_line))
            group_start = start_line
            has_block = False
        if kind == "code" and code_size > max_chars:
            if has_block:
                groups.append((group_start, start_line))
            groups.append((start_line, end_line))
            group_start = end_line
            group_end = end_line
            has_block = False
            continue
        candidate_size = offsets[min(end_line, len(offsets) - 1)] - offsets[group_start]
        if has_block and candidate_size > max_chars:
            groups.append((group_start, start_line))
            group_start = start_line
            has_block = False
        group_end = max(group_end, end_line)
        has_block = True
    if has_block:
        groups.append((group_start, len(lines)))
    elif groups and groups[-1][1] < len(lines):
        start, _end = groups[-1]
        groups[-1] = (start, len(lines))
    if not groups:
        groups = [(0, len(lines))]
    boundaries = sorted({max(0, group[0]) for group in groups} | {0})
    groups = [(start, boundaries[index + 1] if index + 1 < len(boundaries) else len(lines)) for index, start in enumerate(boundaries)]

    result: list[dict[str, Any]] = []
    for ordinal, (start_line, end_line) in enumerate(groups):
        start = offsets[start_line]
        end = offsets[min(end_line, len(offsets) - 1)]
        if ordinal == 0:
            start = 0
        if ordinal == len(groups) - 1:
            end = len(content)
        text = content[start:end]
        if not text:
            continue
        heading_stack: list[tuple[int, str]] = []
        for line_no, (level, title) in sorted(headings.items()):
            if line_no > start_line:
                break
            heading_stack = [entry for entry in heading_stack if entry[0] < level]
            heading_stack.append((level, title))
        heading_path = [title for _level, title in heading_stack]
        chunk_id = hashlib.sha256(f"{article_id}:{version}:{start}:{end}".encode()).hexdigest()
        result.append(
            {
                "id": chunk_id,
                "article_id": article_id,
                "version": version,
                "text": text,
                "text_hash": hashlib.sha256(text.encode("utf-8")).hexdigest(),
                "heading_path": heading_path,
                "start": start,
                "end": end,
                "ordinal": len(result),
                "previous_id": None,
                "next_id": None,
            }
        )
    for index, chunk in enumerate(result):
        if index:
            chunk["previous_id"] = result[index - 1]["id"]
        if index + 1 < len(result):
            chunk["next_id"] = result[index + 1]["id"]
    return result
