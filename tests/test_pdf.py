from io import BytesIO

from caplog.chain import Record
from caplog.pdfexport import write_pdf


def test_pdf_includes_accented_text():
    buf = BytesIO()
    record = Record(
        seq=1,
        kind="entry",
        created_at="2026-01-01T00:00:00Z",
        first_name="Ada",
        last_name="Lovelace",
        ip="192.0.2.1",
        body="café",
        hash="ab" * 32,
    )
    write_pdf(buf, "Watch Log", [record], record)
    data = buf.getvalue()
    assert data.startswith(b"%PDF")
    assert len(data) > 1000
