import io

import docx
import pymupdf
import pytest

from app.extract import ExtractionError, extract_text
from app.tools import CalculatorError, find_arithmetic, run_python, safe_eval


def test_txt_docx_pdf_extraction():
    assert "hello" in extract_text("a.txt", b"hello")
    d = docx.Document()
    d.add_paragraph("1. What is 2+2?")
    t = d.add_table(rows=1, cols=2)
    t.rows[0].cells[0].text, t.rows[0].cells[1].text = "a", "b"
    buf = io.BytesIO()
    d.save(buf)
    out = extract_text("a.docx", buf.getvalue())
    assert "What is 2+2" in out and "a | b" in out
    pdf = pymupdf.open()
    pdf.new_page().insert_text((72, 72), "1. What is 3+3?")
    assert "What is 3+3" in extract_text("a.pdf", pdf.tobytes())


@pytest.mark.parametrize("name,data", [("a.exe", b"x"), ("a.txt", b""), ("a.pdf", b"notpdf"),
                                       ("a.docx", b"notzip"), ("a.txt", b"x" * 50)])
def test_bad_files_rejected(name, data):
    with pytest.raises(ExtractionError):
        extract_text(name, data, max_bytes=40)


def test_calculator_safe():
    assert safe_eval("12 * (3 + 4)") == 84 and safe_eval("2^3") == 8 and safe_eval("7/2") == 3.5
    for evil in ("__import__('os')", "open('x')", "2**9999", "1/0", "a+1", "(1).real"):
        with pytest.raises(CalculatorError):
            safe_eval(evil)


def test_find_arithmetic_ignores_dates_and_numbering():
    assert find_arithmetic("1. Compute 12 * (3 + 4).") == "12 * (3 + 4)"
    assert find_arithmetic("Due 2024-05-12. Explain.") is None


def test_code_runner():
    ok = run_python("print(1+1)")
    assert ok.ran and ok.exit_code == 0 and ok.stdout.strip() == "2"
    assert run_python("import sys; sys.exit(3)").exit_code == 3
    assert run_python("while True: pass", timeout_s=1).timed_out
    assert not run_python("import subprocess").ran
