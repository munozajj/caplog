"""PDF export.

The file lists every record that was included in the export, then the
fingerprint of the last one. Text is drawn with the bundled Inter font so
letters outside the old PDF encoding (accents, for example) are kept.
"""

from datetime import datetime
from pathlib import Path

from fpdf import FPDF

from caplog.chain import KIND_ENTRY, KIND_GENESIS, KIND_IMPORTED, Record

FONT = Path(__file__).resolve().parent.parent / "static" / "fonts" / "Inter-Regular.ttf"


def _when(value: str) -> str:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return value
    return parsed.strftime("%Y-%m-%d %H:%M:%S UTC")



def _who(record: Record) -> str:
    name = f"{record.first_name} {record.last_name}".strip()
    if record.kind == KIND_GENESIS:
        return "Logbook opened"
    if record.kind == KIND_ENTRY:
        return name
    if record.kind == "system":
        return name or "System"
    if record.kind == KIND_IMPORTED:
        return (name + " (imported)").strip()
    return record.kind


def _line(pdf: FPDF, size: int, text: str) -> None:
    pdf.set_font("Inter", size=size)
    # new_x stays at the left margin. The default (right edge) leaves no room
    # for the next line. CHAR wrapping keeps a long fingerprint on the page.
    pdf.multi_cell(0, size * 0.5, text, align="L", new_x="LMARGIN", new_y="NEXT", wrapmode="CHAR")


def write_pdf(handle, title: str, records: list[Record], final: Record, fqdn: str = "") -> None:
    pdf = FPDF(format="A4")
    pdf.set_auto_page_break(auto=True, margin=18)
    pdf.set_margins(18, 18, 18)
    pdf.add_font("Inter", "", str(FONT))
    pdf.add_page()
    _line(pdf, 18, title)
    if fqdn:
        _line(pdf, 10, fqdn)
    _line(pdf, 10, f"Records #0-#{final.seq}")
    _line(pdf, 10, "Exported " + _when(final.created_at))
    pdf.ln(4)
    for record in records:
        head = f"#{record.seq:04d}  {_when(record.created_at)}  {_who(record)}"
        if record.ip:
            head += f"  {record.ip}"
        _line(pdf, 11, head)
        _line(pdf, 11, record.body or "")
        pdf.ln(2)
    pdf.ln(2)
    _line(pdf, 10, "Last record fingerprint (SHA-256):")
    _line(pdf, 10, final.hash)
    handle.write(bytes(pdf.output()))
