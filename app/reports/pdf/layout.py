from __future__ import annotations

from reportlab.lib import colors
from reportlab.lib.enums import TA_JUSTIFY, TA_LEFT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import cm
from reportlab.platypus import Flowable, Paragraph, SimpleDocTemplate


class LeftAccentParagraph(Flowable):
    def __init__(
        self,
        text: str,
        paragraph_style,
        *,
        bar_color: str = "#0879bd",
        bar_width: float = 2.2,
        gap: float = 9.0,
    ):
        super().__init__()
        self.text = text
        self.paragraph_style = paragraph_style
        self.bar_color = bar_color
        self.bar_width = bar_width
        self.gap = gap
        self.paragraph = Paragraph(text, paragraph_style)
        self._content_width = 0.0

    def wrap(self, avail_width, avail_height):
        self._content_width = max(
            1.0,
            avail_width - self.bar_width - self.gap,
        )
        _, height = self.paragraph.wrap(
            self._content_width,
            avail_height,
        )
        self.width = avail_width
        self.height = height
        return avail_width, height

    def split(self, avail_width, avail_height):
        content_width = max(
            1.0,
            avail_width - self.bar_width - self.gap,
        )
        parts = self.paragraph.split(content_width, avail_height)
        if len(parts) <= 1:
            return []

        flowables = []
        for part in parts:
            block = LeftAccentParagraph(
                "",
                self.paragraph_style,
                bar_color=self.bar_color,
                bar_width=self.bar_width,
                gap=self.gap,
            )
            block.paragraph = part
            flowables.append(block)
        return flowables

    def draw(self):
        canvas = self.canv
        canvas.saveState()
        canvas.setStrokeColor(colors.HexColor(self.bar_color))
        canvas.setLineWidth(self.bar_width)
        x = self.bar_width / 2.0
        canvas.line(x, 0, x, self.height)
        canvas.restoreState()
        self.paragraph.drawOn(
            canvas,
            self.bar_width + self.gap,
            0,
        )


def pdf_styles() -> dict:
    styles = getSampleStyleSheet()
    title = ParagraphStyle(
        "ReportTitle",
        parent=styles["Title"],
        fontName="Helvetica-Bold",
        fontSize=19,
        leading=23,
        textColor=colors.HexColor("#102b46"),
        spaceAfter=8,
    )
    subtitle = ParagraphStyle(
        "Subtitle",
        parent=styles["Normal"],
        fontSize=10,
        leading=14,
        textColor=colors.HexColor("#516579"),
        spaceAfter=14,
    )
    heading = ParagraphStyle(
        "Section",
        parent=styles["Heading2"],
        fontName="Helvetica-Bold",
        fontSize=13,
        leading=16,
        textColor=colors.HexColor("#102b46"),
        spaceBefore=15,
        spaceAfter=7,
        keepWithNext=True,
    )
    body = ParagraphStyle(
        "Body",
        parent=styles["BodyText"],
        fontSize=9.2,
        leading=13,
        spaceAfter=8,
        alignment=TA_JUSTIFY,
    )
    small = ParagraphStyle(
        "Small",
        parent=body,
        fontSize=7.2,
        leading=9,
        alignment=TA_LEFT,
    )
    cloud_note = ParagraphStyle(
        "WordCloudNote",
        parent=small,
        alignment=1,
        textColor=colors.HexColor("#66788a"),
        spaceBefore=2,
    )
    return {
        "title": title,
        "subtitle": subtitle,
        "heading": heading,
        "body": body,
        "small": small,
        "cloud_note": cloud_note,
    }


def create_document(buffer):
    return SimpleDocTemplate(
        buffer,
        pagesize=A4,
        rightMargin=1.7 * cm,
        leftMargin=1.7 * cm,
        topMargin=1.6 * cm,
        bottomMargin=1.8 * cm,
    )


def page_number(canvas, doc):
    canvas.saveState()
    canvas.setFont("Helvetica", 7)
    canvas.setFillColor(colors.HexColor("#66788a"))
    canvas.drawString(
        2 * cm,
        1.15 * cm,
        "Relatório de Repercussão Midiática - corpus auditável",
    )
    canvas.drawRightString(
        A4[0] - 2 * cm,
        1.15 * cm,
        f"Página {doc.page}",
    )
    canvas.restoreState()
