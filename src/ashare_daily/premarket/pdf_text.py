"""Bounded local PDF extraction in a killable worker; no OCR or external model."""
import json
from pathlib import Path
import subprocess
import sys


def extract_pdf(path, *, max_seconds=10):
    path = Path(path).resolve()
    if not 0 < max_seconds <= 30 or path.stat().st_size > 5_000_000:
        raise ValueError("pdf_bounds_invalid")
    with path.open("rb") as handle:
        if handle.read(5) != b"%PDF-":
            raise ValueError("pdf_signature_invalid")
    # The worker writes only a bounded JSON result. subprocess.run kills and
    # waits on timeout; no orphan parse continues after the collection deadline.
    try:
        result = subprocess.run([sys.executable, str(Path(__file__).resolve()), str(path)],
            capture_output=True, timeout=max_seconds, check=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except subprocess.TimeoutExpired as exc:
        raise TimeoutError("pdf_parse_deadline") from exc
    if result.returncode or len(result.stdout) > 1_000_000:
        raise ValueError("pdf_parse_failed")
    parsed = json.loads(result.stdout)
    if parsed.get("error"):
        raise ValueError(parsed["error"])
    return parsed


def _extract(path):
    from pypdf import PdfReader
    # Linux production has an additional address-space ceiling. On Windows,
    # pypdf's decompression bounds and the worker deadline remain applicable.
    if sys.platform != "win32":
        import resource
        resource.setrlimit(resource.RLIMIT_AS, (512 * 1024**2, 512 * 1024**2))
    from pypdf import overwrite_configuration
    overwrite_configuration(maximum_declared_stream_length=5_000_000,
        array_based_stream_maximum_output_length=2_000_000, zlib_maximum_output_length=2_000_000,
        lzw_maximum_output_length=2_000_000, run_length_maximum_output_length=2_000_000,
        brotli_maximum_output_length=2_000_000, jbig2_maximum_output_length=2_000_000,
        stream_decoding_work_maximum_length=8_000_000, page_tree_maximum_entries=200,
        xform_maximum_invocations_per_extraction=100, jbig2dec_binary=None)
    reader = PdfReader(path, strict=True)
    if reader.is_encrypted or not 1 <= len(reader.pages) <= 60:
        raise ValueError("pdf_encrypted_or_page_limit")
    parts, count = [], 0
    for page in reader.pages:
        stream = page.get_contents()
        if stream is not None and len(stream.get_data()) > 2_000_000:
            raise ValueError("pdf_stream_limit")
        text = page.extract_text() or ""
        # A blank/image page can hide important content; never call partial text
        # a completed body review, even when the other pages are extractable.
        if len(text.strip()) < 10:
            raise ValueError("pdf_text_unavailable")
        parts.append(text)
        count += len(text)
        if count > 200_000:
            raise ValueError("pdf_text_limit")
    text = "\n".join(parts)
    if len(text.strip()) < 50:
        raise ValueError("pdf_text_unavailable")
    return {"text": text, "pages": len(parts), "extractor": "pypdf-6.20.1", "ocr": False}


if __name__ == "__main__":
    try:
        output = _extract(sys.argv[1])
    except Exception as exc:
        # Do not echo untrusted PDF internals to operator logs.
        output = {"error": str(exc) if isinstance(exc, ValueError) and str(exc).startswith("pdf_") else "pdf_parse_failed"}
    sys.stdout.buffer.write(json.dumps(output, ensure_ascii=False).encode("utf-8"))
