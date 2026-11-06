"""Word export of a board draft: one heading per section, each paragraph followed by its sources table."""
from __future__ import annotations

import io

import docx

from dashboard import views

_HEADER = ["metric_id", "display", "source_file", "source_cell"]


def draft_to_docx(draft, p) -> bytes:
    doc = docx.Document()
    doc.add_paragraph("Board draft", style="Title")
    for section in draft.sections:
        doc.add_paragraph(views.section_name(section.company, p) or "Other", style="Heading 1")
        for para in section.paragraphs:
            doc.add_paragraph(para.rendered, style="Normal")
            table = doc.add_table(rows=1, cols=len(_HEADER))
            table.style = "Table Grid"
            for cell, title in zip(table.rows[0].cells, _HEADER):
                cell.text = title
            for row in views.sources_rows(para):
                cells = table.add_row().cells
                for cell, key in zip(cells, _HEADER):
                    cell.text = str(row[key])
    doc.add_paragraph("Open questions", style="Heading 1")
    for q in draft.questions:
        doc.add_paragraph(q.text, style="List Bullet")
    buf = io.BytesIO()
    doc.save(buf)
    return buf.getvalue()
