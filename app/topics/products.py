from __future__ import annotations

from app.topics.locations import clean_phrase
from app.topics.temporal import normalized_terms, normalized_text
from app.year_utils import strip_year


GENERIC_PRODUCT_TERMS = {
    "dossie",
    "relatorio",
    "boletim",
    "anuario",
    "estudo",
    "publicacao",
    "pesquisa",
    "documento",
    "edicao",
    "serie",
    "instituto",
    "seguranca",
    "publica",
    "rio",
    "janeiro",
}


def product_anchor_from_name(product_name: str | None) -> str | None:
    name = clean_phrase(product_name)
    if not name:
        return None
    no_year = strip_year(name)
    return no_year or name


def anchored_product_variants(
    product_name: str,
    product_anchor: str | None,
) -> list[str]:
    variants = [product_name]
    if (
        product_anchor
        and normalized_text(product_anchor) != normalized_text(product_name)
    ):
        variants.append(product_anchor)
    return list(
        dict.fromkeys(
            clean_phrase(value)
            for value in variants
            if clean_phrase(value)
        )
    )


def subject_terms_from_product(product_name: str) -> list[str]:
    terms = sorted(normalized_terms(product_name))
    return [
        term
        for term in terms
        if term not in GENERIC_PRODUCT_TERMS
    ]
