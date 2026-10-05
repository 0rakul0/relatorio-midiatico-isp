from __future__ import annotations

from app.utils.rendering import text


def window_label(start: object, end: object, *, empty: str = "Não delimitado") -> str:
    start_text = text(start).strip()
    end_text = text(end).strip()
    if start_text and end_text:
        return f"{start_text} a {end_text}"
    if start_text:
        return start_text
    if end_text:
        return end_text
    return empty


def scope_label(value: object) -> str:
    if value is True:
        return "núcleo principal"
    if value is False:
        return "caso relacionado"
    return "escopo pendente"


def status_label(value: str | None) -> str:
    labels = {
        "CONFIRMED": "Confirmado",
        "PARTIALLY_CONFIRMED": "Confirmação parcial",
        "SOURCE_CONFLICT": "Conflito entre fontes",
        "NOT_FOUND_IN_SAMPLE": "Não localizado na amostra",
    }
    return labels.get(value or "", value or "N/D")


def view_count_label(value: object) -> str:
    if isinstance(value, int) and not isinstance(value, bool):
        return f"{value:,}".replace(",", ".")
    return "N/D"


def pdf_link(url: object, label: str = "Abrir") -> dict[str, str]:
    href = text(url).strip()
    return {"_pdf_link": href, "label": label if href else "N/D"}
