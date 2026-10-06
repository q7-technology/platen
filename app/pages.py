"""Page templates: packing slips, invoices, pick lists. Paper, not label stock.

A page template is a header, a body and a footer, each a list of blocks, laid
out top to bottom on A4 or Letter. A table block grows with the data: three
lines one day, thirty the next, carrying onto the next page with its headings
repeated. The header and footer are drawn on every page and can say which
page it is: ``{{ page }}`` and ``{{ pages }}``.

One query, and one document per group. A packing slip query returns a row per
order line with the order's own columns alongside; ``group_by: order_no`` makes
one slip per order, its lines in the table and its first row behind every
other binding. With no group_by, every row lands in one document.

ReportLab draws the PDF. The preview is a picture of that same PDF, so what
the screen shows is what the printer is sent. Bindings are the same dotted
lookups and named filters a label uses, and every value is escaped before it
reaches ReportLab, whose paragraphs would otherwise read markup out of the
data, ``<img src>`` included.
"""

from __future__ import annotations

import io
from collections.abc import Mapping
from typing import Annotated, Any, Literal
from xml.sax.saxutils import escape

from pydantic import BaseModel, Field
from reportlab.graphics.barcode import createBarcodeDrawing
from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_LEFT, TA_RIGHT
from reportlab.lib.pagesizes import A4, LETTER, landscape
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.lib.utils import ImageReader
from reportlab.platypus import (
    BaseDocTemplate,
    Flowable,
    Frame,
    HRFlowable,
    Image,
    Paragraph,
    Spacer,
    Table,
    TableStyle,
)
from reportlab.platypus import (
    PageTemplate as RlPageTemplate,
)

from . import binding, images

MAX_DOCUMENTS = 2000          # one run; a bigger one is two runs


class RenderError(Exception):
    """Names the block and what went wrong, the way a label's errors do."""


# ------------------------------------------------------------------ the schema

Align = Literal["left", "centre", "right"]


class TextBlock(BaseModel):
    kind: Literal["text"] = "text"
    value: str                       # literal text, bindings, or both
    size_pt: float = 10.0
    bold: bool = False
    align: Align = "left"


class ImageBlock(BaseModel):
    """An image from the data (a base64 or bytea column), like a label's."""

    kind: Literal["image"] = "image"
    value: str
    width_mm: float = 40.0
    align: Align = "left"
    on_missing: Literal["blank", "fail"] = "blank"


class BarcodeBlock(BaseModel):
    kind: Literal["barcode"] = "barcode"
    value: str
    symbology: Literal["code128", "qr"] = "code128"
    height_mm: float = 12.0
    align: Align = "left"


class Column(BaseModel):
    title: str
    value: str                       # usually one binding: "{{ sku }}"
    width_mm: float | None = None    # None shares what is left
    align: Align = "left"


class TableBlock(BaseModel):
    """One line per row of the document's group. Splits across pages."""

    kind: Literal["table"] = "table"
    columns: list[Column] = Field(default_factory=list)
    size_pt: float = 9.0


class SpaceBlock(BaseModel):
    kind: Literal["space"] = "space"
    height_mm: float = 5.0


class RuleBlock(BaseModel):
    kind: Literal["rule"] = "rule"


Block = Annotated[
    TextBlock | ImageBlock | BarcodeBlock | TableBlock | SpaceBlock | RuleBlock,
    Field(discriminator="kind"),
]


class PageTemplate(BaseModel):
    id: str
    name: str
    kind: Literal["page"] = "page"
    version: int = 1
    folder: str = ""
    datasource: str | None = None
    query: str | None = None
    group_by: str | None = None      # a column; one document per value
    paper: Literal["A4", "Letter"] = "A4"
    landscape: bool = False
    margin_mm: float = 15.0
    header: list[Block] = Field(default_factory=list)
    body: list[Block] = Field(default_factory=list)
    footer: list[Block] = Field(default_factory=list)

    @property
    def size_mm(self) -> tuple[float, float]:
        w, h = (A4 if self.paper == "A4" else LETTER)
        if self.landscape:
            w, h = h, w
        return round(w / mm, 1), round(h / mm, 1)


# ------------------------------------------------------------------ grouping

def group(t: PageTemplate, rows: list[Mapping[str, Any]]) -> list[list[Mapping[str, Any]]]:
    """Rows into documents, in the order each group first appears."""
    if not rows:
        return [[{}]] if not t.query else []
    if not t.group_by:
        return [list(rows)]
    if t.group_by not in rows[0]:
        raise RenderError(f"group by: the query returned no column {t.group_by!r}")
    groups: dict[Any, list[Mapping[str, Any]]] = {}
    for row in rows:
        groups.setdefault(row.get(t.group_by), []).append(row)
    return list(groups.values())


# ------------------------------------------------------------------ drawing

_ALIGN = {"left": TA_LEFT, "centre": TA_CENTER, "right": TA_RIGHT}
_HALIGN = {"left": "LEFT", "centre": "CENTER", "right": "RIGHT"}
INK = colors.HexColor("#111111")
FAINT = colors.HexColor("#c8ccd4")


def _text(expr: str, row: Mapping[str, Any], where: str) -> str:
    try:
        return binding.render_value(expr, row)
    except binding.MissingField as exc:
        raise RenderError(f"{where}: the query returned no column {exc.args[0]!r}") from None
    except binding.BindingError as exc:
        raise RenderError(f"{where}: {exc}") from None


def _para(text: str, size: float, bold: bool, align: Align) -> Paragraph:
    style = ParagraphStyle(
        "p", fontName="Helvetica-Bold" if bold else "Helvetica", fontSize=size,
        leading=size * 1.25, alignment=_ALIGN[align], textColor=INK)
    # escaped, so data can never become markup; line breaks survive
    return Paragraph(escape(text).replace("\n", "<br/>"), style)


class _Aligned(Flowable):
    """Places a fixed-size flowable left, centre or right in the frame."""

    def __init__(self, inner: Flowable, align: Align) -> None:
        super().__init__()
        self.inner, self.align = inner, align

    def wrap(self, aw: float, ah: float) -> tuple[float, float]:
        self.aw = aw
        self.iw, self.ih = self.inner.wrap(aw, ah)
        return aw, self.ih

    def draw(self) -> None:
        x = {"left": 0, "centre": (self.aw - self.iw) / 2, "right": self.aw - self.iw}[self.align]
        self.inner.drawOn(self.canv, x, 0)


class _Drawing(Flowable):
    def __init__(self, drawing: Any) -> None:
        super().__init__()
        self.drawing = drawing

    def wrap(self, aw: float, ah: float) -> tuple[float, float]:
        return self.drawing.width, self.drawing.height

    def draw(self) -> None:
        from reportlab.graphics import renderPDF

        renderPDF.draw(self.drawing, self.canv, 0, 0)


def _block(b: Any, rows: list[Mapping[str, Any]], width: float, where: str,
           warnings: list[str]) -> list[Flowable]:
    first = rows[0] if rows else {}
    if b.kind == "text":
        return [_para(_text(b.value, first, where), b.size_pt, b.bold, b.align)]
    if b.kind == "space":
        return [Spacer(1, b.height_mm * mm)]
    if b.kind == "rule":
        return [HRFlowable(width="100%", thickness=0.6, color=INK, spaceBefore=2, spaceAfter=2)]
    if b.kind == "image":
        try:
            raw = binding.raw_value(b.value, first)
        except binding.MissingField as exc:
            raise RenderError(f"{where}: the query returned no column {exc.args[0]!r}") from None
        if raw in (None, "", b""):
            if b.on_missing == "fail":
                raise RenderError(f"{where}: the image is empty and this block is set to fail")
            warnings.append(f"{where}: the image was empty, so it was left out")
            return []
        try:
            data = images.decode(raw)
            reader = ImageReader(io.BytesIO(data))
        except Exception as exc:
            if b.on_missing == "fail":
                raise RenderError(f"{where}: that isn't an image Platen can read ({exc})") from None
            warnings.append(f"{where}: that isn't an image Platen can read, so it was left out")
            return []
        iw, ih = reader.getSize()
        w = min(b.width_mm * mm, width)
        return [_Aligned(Image(io.BytesIO(data), width=w, height=w * ih / iw), b.align)]
    if b.kind == "barcode":
        value = _text(b.value, first, where).strip()
        if not value:
            warnings.append(f"{where}: the barcode was blank, so it was left out")
            return []
        try:
            if b.symbology == "qr":
                side = b.height_mm * mm
                d = createBarcodeDrawing("QR", value=value, width=side, height=side, barBorder=0)
            else:
                d = createBarcodeDrawing("Code128", value=value, barHeight=b.height_mm * mm,
                                         humanReadable=True, quiet=False)
        except Exception as exc:
            raise RenderError(
                f"{where}: {value!r} can't be drawn as {b.symbology}: {exc}") from None
        return [_Aligned(_Drawing(d), b.align)]
    if b.kind == "table":
        return [_table(b, rows, width, where)]
    raise RenderError(f"{where}: unknown block {b.kind!r}")


def _table(b: TableBlock, rows: list[Mapping[str, Any]], width: float, where: str) -> Table:
    if not b.columns:
        raise RenderError(f"{where}: a table needs at least one column")
    head = [_para(c.title, b.size_pt, True, c.align) for c in b.columns]
    body = [[_para(_text(c.value, r, f"{where}, column {c.title!r}"), b.size_pt, False, c.align)
             for c in b.columns] for r in rows]
    fixed = sum(c.width_mm * mm for c in b.columns if c.width_mm)
    loose = [c for c in b.columns if not c.width_mm]
    share = max(width - fixed, 0) / len(loose) if loose else 0
    widths = [c.width_mm * mm if c.width_mm else share for c in b.columns]
    t = Table([head, *body], colWidths=widths, repeatRows=1)
    t.setStyle(TableStyle([
        ("LINEBELOW", (0, 0), (-1, 0), 0.8, INK),
        ("LINEBELOW", (0, 1), (-1, -1), 0.3, FAINT),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("TOPPADDING", (0, 0), (-1, -1), 3),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
        ("LEFTPADDING", (0, 0), (-1, -1), 4),
        ("RIGHTPADDING", (0, 0), (-1, -1), 4),
    ]))
    return t


def _blocks(blocks: list[Any], rows: list[Mapping[str, Any]], width: float, part: str,
            warnings: list[str]) -> list[Flowable]:
    out: list[Flowable] = []
    for i, b in enumerate(blocks, start=1):
        out += _block(b, rows, width, f"{part} block {i} ({b.kind})", warnings)
    return out


def _height(flowables: list[Flowable], width: float) -> float:
    return sum(f.wrap(width, 10_000)[1] + f.getSpaceBefore() + f.getSpaceAfter()
               for f in flowables)


def _build(t: PageTemplate, rows: list[Mapping[str, Any]], pages: int | None,
           warnings: list[str]) -> tuple[bytes, int]:
    size = (A4 if t.paper == "A4" else LETTER)
    if t.landscape:
        size = landscape(size)
    pw, ph = size
    m = t.margin_mm * mm
    width = pw - 2 * m
    first = dict(rows[0]) if rows else {}
    gap = 4 * mm

    def furniture(part: str, page: int) -> list[Flowable]:
        # header and footer see the page they are on; the body never needs to
        row = {**first, "page": page, "pages": pages or "?"}
        blocks = t.header if part == "header" else t.footer
        return _blocks(blocks, [row], width, part, [])

    # measured once, with a stand-in page number, to size the body frame
    head_h = _height(furniture("header", 1), width) if t.header else 0
    foot_h = _height(furniture("footer", 1), width) if t.footer else 0
    body_y = m + (foot_h + gap if foot_h else 0)
    body_h = ph - 2 * m - (head_h + gap if head_h else 0) - (foot_h + gap if foot_h else 0)
    if body_h < 40 * mm:
        raise RenderError("the header and footer leave less than 40 mm for the body; "
                          "make them shorter or the margins smaller")

    def on_page(canvas: Any, doc: Any) -> None:
        if t.header:
            Frame(m, ph - m - head_h, width, head_h, 0, 0, 0, 0, showBoundary=0) \
                .addFromList(furniture("header", doc.page), canvas)
        if t.footer:
            Frame(m, m, width, foot_h, 0, 0, 0, 0, showBoundary=0) \
                .addFromList(furniture("footer", doc.page), canvas)

    out = io.BytesIO()
    doc = BaseDocTemplate(out, pagesize=size, leftMargin=m, rightMargin=m, topMargin=m,
                          bottomMargin=m, title=t.name, author="Platen")
    doc.addPageTemplates([RlPageTemplate(
        id="page", frames=[Frame(m, body_y, width, body_h, 0, 0, 0, 0, id="body")],
        onPage=on_page)])
    story = _blocks(t.body, rows, width, "body", warnings)
    try:
        doc.build(story or [Spacer(1, 1)])
    except RenderError:
        raise
    except Exception as exc:
        # ReportLab's own complaint is usually a block too tall for one page
        raise RenderError(f"the page could not be laid out: {exc}") from None
    return out.getvalue(), doc.page


def render_document(t: PageTemplate, rows: list[Mapping[str, Any]]) -> tuple[bytes, list[str]]:
    """One document as PDF bytes. Built twice when the furniture says "of N":
    the first pass is how N is known."""
    warnings: list[str] = []
    pdf, pages = _build(t, rows, None, warnings)
    if _counts_pages(t):
        warnings.clear()
        pdf, _ = _build(t, rows, pages, warnings)
    return pdf, warnings


def _counts_pages(t: PageTemplate) -> bool:
    exprs = [b.value for b in (*t.header, *t.footer) if b.kind == "text"]
    return any("pages" in binding.fields_used(e) for e in exprs)


def render_run(t: PageTemplate, rows: list[Mapping[str, Any]],
               copies: int = 1) -> tuple[list[bytes], list[str]]:
    """Every document in a run, rendered before any of it is printed."""
    docs = group(t, rows)
    if len(docs) > MAX_DOCUMENTS:
        raise RenderError(f"that is {len(docs)} documents; one run takes at most "
                          f"{MAX_DOCUMENTS}. Narrow the query and print it in two")
    out, warnings = [], []
    for i, rows_for_doc in enumerate(docs, start=1):
        try:
            pdf, said = render_document(t, rows_for_doc)
        except RenderError as exc:
            raise RenderError(f"document {i}: {exc}") from None
        warnings += [f"document {i}: {w}" for w in said]
        out.extend([pdf] * copies)
    return out, warnings


# ------------------------------------------------------------------ pictures

def merge(pdfs: list[bytes]) -> bytes:
    """Many documents as one file, to look at before printing."""
    import pypdfium2 as pdfium

    whole = pdfium.PdfDocument.new()
    for data in pdfs:
        part = pdfium.PdfDocument(data)
        whole.import_pages(part)
    out = io.BytesIO()
    whole.save(out)
    return out.getvalue()


def page_count(pdf: bytes) -> int:
    import pypdfium2 as pdfium

    return len(pdfium.PdfDocument(pdf))


def to_png(pdf: bytes, page: int = 1, dpi: int = 110) -> bytes:
    """A picture of one page of the very PDF that would be printed."""
    import pypdfium2 as pdfium

    doc = pdfium.PdfDocument(pdf)
    n = len(doc)
    if not 1 <= page <= n:
        raise RenderError(f"there is no page {page}; this document has {n}")
    image = doc[page - 1].render(scale=dpi / 72).to_pil()
    out = io.BytesIO()
    image.save(out, "PNG")
    return out.getvalue()


TEST_PAGE = PageTemplate(
    id="platen-test", name="Platen test page",
    body=[TextBlock(value="Platen test page", size_pt=22, bold=True),
          SpaceBlock(height_mm=4),
          TextBlock(value="If you can read this, the path from Platen to this printer works."),
          SpaceBlock(height_mm=6),
          BarcodeBlock(value="PLATEN-TEST")])
