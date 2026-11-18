"""The two OCR models, called exactly as ocr_text.py and ocr_markdown.py call them.

Accuracy rule: whatever reaches the model here must be what the original
scripts send, and whatever comes back is stored untouched. If you change how a
page is loaded or how a model is set up, re-run tests/accuracy_test.py.
"""
from __future__ import annotations

import sys
import threading
from pathlib import Path

from PIL import Image

ROOT = Path(__file__).resolve().parents[2]          # ~/thai-ocr
TEXT_DIR = ROOT / "OpenThai2.0-OCR-Text"
MARKDOWN_DIR = ROOT / "OpenThai2.0-OCR-Markdown"
PDF_SCALE = 200 / 72                                # both scripts draw PDF pages at 200 dpi


class Cancelled(Exception):
    """The user stopped the job while a page was being read; nothing is saved for that page."""


def page_count(path: Path) -> int:
    if path.suffix.lower() == ".pdf":
        import pypdfium2 as pdfium
        pdf = pdfium.PdfDocument(str(path))
        try:
            return len(pdf)
        finally:
            pdf.close()
    return 1


def render_pdf_page(path: Path, number: int, scale: float = PDF_SCALE) -> Image.Image:
    """Page `number` (1-based) of a PDF, drawn as the scripts draw it."""
    import pypdfium2 as pdfium
    pdf = pdfium.PdfDocument(str(path))
    try:
        return pdf[number - 1].render(scale=scale).to_pil()
    finally:
        pdf.close()


class TextEngine:
    """OpenThai2.0-OCR-Text. Same constructor arguments as ocr_text.py."""

    def __init__(self):
        self._ocr = None
        self._lock = threading.Lock()

    def _get(self):
        with self._lock:
            if self._ocr is None:
                if str(TEXT_DIR) not in sys.path:
                    sys.path.insert(0, str(TEXT_DIR))
                from runtime.line_engine import LineOCR
                self._ocr = LineOCR(str(TEXT_DIR), threads=2)
            return self._ocr

    def read(self, path: Path, number: int, should_stop=None) -> dict:
        # a page takes under a second, so a cancel is checked between pages only
        # ocr_text.py: a PDF page as the PIL picture pdfium draws; a picture file by its path
        page = render_pdf_page(path, number) if path.suffix.lower() == ".pdf" else str(path)
        result = self._get().read(page)
        return {
            "content": result["text"],
            "lines": [{"text": l["text"], "confidence": l["confidence"], "row": l["row"]} for l in result["lines"]],
            "dropped": [{"text": d["text"], "confidence": d["confidence"], "reason": d["reason"]}
                        for d in result.get("dropped", [])],
        }


class MarkdownEngine:
    """OpenThai2.0-OCR-Markdown, loaded once on first use (it is 2.5 GB)."""

    def __init__(self):
        self._parser = None
        self._lock = threading.Lock()
        self.device = None
        self._should_stop = None   # set for the page being read; checked after every generated token

    @property
    def loaded(self) -> bool:
        return self._parser is not None

    def _get(self):
        with self._lock:
            if self._parser is None:
                self._parser = self._load()
            return self._parser

    def _load(self):
        # Copied from ocr_markdown.py local_parser(); keep the two in step.
        import torch
        from transformers import AutoModel, AutoProcessor
        from nemotron_parse.inference import DocumentParser, GenerationSettings, TransformersBackend

        if torch.cuda.is_available():
            device, dtype = "cuda", torch.bfloat16
        elif torch.backends.mps.is_available():
            device, dtype = "mps", torch.float32
        else:
            device, dtype = "cpu", torch.float32
        model = AutoModel.from_pretrained(MARKDOWN_DIR, trust_remote_code=True, dtype=dtype).to(device).eval()
        self._add_stop_check(model)
        processor = AutoProcessor.from_pretrained(MARKDOWN_DIR, trust_remote_code=True)

        class Backend(TransformersBackend):
            # a vLLM server reads the prompt after a new line; send the same tokens here
            def __call__(self, image, *, prompt, **kwargs):
                return super().__call__(image, prompt="\n" + prompt, **kwargs)

        self.device = device
        # greedy (temperature 0): with sampling, a Mac's GPU garbles pages
        return DocumentParser(backend=Backend(model, processor), fullpage=GenerationSettings(temperature=0))

    def _add_stop_check(self, model):
        """Lets a cancel end generation at the next token. Until it fires it answers "keep going"
        for every token, so finished pages are exactly what the script produces."""
        import torch
        from transformers import StoppingCriteria, StoppingCriteriaList

        engine = self

        class StopWhenCancelled(StoppingCriteria):
            def __call__(self, input_ids, scores, **kwargs):
                if engine._should_stop and engine._should_stop():
                    # leave the whole page at once; the parser would otherwise start its next pass
                    raise Cancelled()
                return torch.zeros((input_ids.shape[0],), dtype=torch.bool, device=input_ids.device)

        generate = model.generate

        def generate_with_stop(*args, **kwargs):
            criteria = StoppingCriteriaList(kwargs.pop("stopping_criteria", None) or [])
            criteria.append(StopWhenCancelled())
            return generate(*args, stopping_criteria=criteria, **kwargs)

        model.generate = generate_with_stop

    def read(self, path: Path, number: int, should_stop=None) -> dict:
        from nemotron_parse.inference import GenerationError

        parser = self._get()
        self._should_stop = should_stop
        # ocr_markdown.py: a PDF page as the PIL picture pdfium draws; a picture file as Image.open(path)
        page = render_pdf_page(path, number) if path.suffix.lower() == ".pdf" else Image.open(path)
        try:
            if should_stop and should_stop():
                raise Cancelled()
            content = parser.parse(page, mode="single")["markdown"]
            incomplete = False
        except GenerationError:
            if should_stop and should_stop():
                raise Cancelled()
            # the exact placeholder ocr_markdown.py writes, so downloads match the script
            content = f"<!-- page {number} could not be read completely -->"
            incomplete = True
        finally:
            self._should_stop = None
            self._free_gpu_memory()
        if should_stop and should_stop():
            raise Cancelled()   # generation was cut short: the output is partial
        return {"content": content, "incomplete": incomplete}

    def _free_gpu_memory(self):
        # Hands cached GPU blocks back to macOS between pages; it does not touch the model or results.
        try:
            import torch
            if self.device == "mps":
                torch.mps.empty_cache()
            elif self.device == "cuda":
                torch.cuda.empty_cache()
        except Exception:
            pass

    def unload(self) -> bool:
        with self._lock:
            if self._parser is None:
                return False
            try:
                self._parser.close()
            except Exception:
                pass
            self._parser = None
            import gc
            gc.collect()
            self._free_gpu_memory()
            return True
