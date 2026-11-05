"""Export formats: Markdown, plain text, DOCX."""

from __future__ import annotations

import io
import re
from pathlib import Path

from .models import SessionState

SUPPORTED_FORMATS = {
    "md": "text/markdown",
    "txt": "text/plain",
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
}


class ExportError(Exception):
    pass


def report_markdown(state: SessionState) -> str:
    if not state.validation:
        return ""
    labels = {r.id: r.text.splitlines()[0][:80] for r in state.requirements}
    lines = ["## Validation report", "", state.validation.summary, ""]
    lines += [f"- **{c.requirement_id.upper()}** ({c.status}): {labels.get(c.requirement_id, '')} — {c.detail}"
              for c in state.validation.results]
    if state.limitations:
        lines += ["", "### Limitations", *[f"- {item}" for item in state.limitations]]
    return "\n".join(lines)


def build_markdown(state: SessionState, include_report: bool) -> str:
    text = state.deliverable
    if include_report and state.validation:
        text += "\n\n---\n\n" + report_markdown(state)
    return text


def _strip_md(text: str) -> str:
    text = re.sub(r"^#{1,6}\s*", "", text, flags=re.M)
    return re.sub(r"\*\*(.+?)\*\*|__(.+?)__", lambda m: m.group(1) or m.group(2), text)


def _add_runs(paragraph, text: str) -> None:
    for part in re.split(r"(\*\*.+?\*\*)", text):
        if part.startswith("**") and part.endswith("**") and len(part) > 4:
            paragraph.add_run(part[2:-2]).bold = True
        elif part:
            paragraph.add_run(part)


def _to_docx(markdown: str) -> bytes:
    import docx

    document = docx.Document()
    in_code = False
    for line in markdown.splitlines():
        if line.strip().startswith("```"):
            in_code = not in_code
            continue
        if in_code:
            run = document.add_paragraph().add_run(line)
            run.font.name = "Courier New"
        elif m := re.match(r"^(#{1,4})\s+(.*)", line):
            document.add_heading(m.group(2), level=len(m.group(1)))
        elif re.match(r"^\s*[-*]\s+", line):
            _add_runs(document.add_paragraph(style="List Bullet"), re.sub(r"^\s*[-*]\s+", "", line))
        elif re.match(r"^\s*\d+[.)]\s+", line):
            _add_runs(document.add_paragraph(style="List Number"), re.sub(r"^\s*\d+[.)]\s+", "", line))
        elif line.strip() == "---":
            continue
        elif line.strip():
            _add_runs(document.add_paragraph(), line)
    buffer = io.BytesIO()
    document.save(buffer)
    return buffer.getvalue()


def export(state: SessionState, fmt: str, include_report: bool = True) -> tuple[bytes, str, str]:
    """Return (data, media_type, filename). Raises ExportError if nothing can be exported."""
    if fmt not in SUPPORTED_FORMATS:
        raise ExportError(f"Unsupported format '{fmt}'. Use one of: {', '.join(SUPPORTED_FORMATS)}.")
    if not state.deliverable.strip():
        raise ExportError("There is no deliverable to export yet.")
    markdown = build_markdown(state, include_report)
    stem = re.sub(r"[^\w.-]+", "_", Path(state.filename).stem) or "deliverable"
    if fmt == "md":
        data = markdown.encode("utf-8")
    elif fmt == "txt":
        data = _strip_md(markdown).encode("utf-8")
    else:
        data = _to_docx(markdown)
    return data, SUPPORTED_FORMATS[fmt], f"{stem}_deliverable.{fmt}"
