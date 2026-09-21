"""Nuvem de palavras do corpus jornalistico validado.

A nuvem usa somente itens VALID classificados como PORTAL_NOTICIAS. Artigos
academicos, redes sociais e YouTube ficam fora por desenho metodologico.
"""

from __future__ import annotations

from collections import Counter, defaultdict
import re
import unicodedata

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import MediaItem
from app.services.collection.media_origin import PORTAL_NOTICIAS, classify_media_origin


# Stopwords portuguesas + termos jornalisticos muito genericos que tendem a
# dominar a visualizacao sem acrescentar conteudo tematico.
_STOPWORDS = {
    "a", "ao", "aos", "aquela", "aquelas", "aquele", "aqueles", "aquilo", "as",
    "ate", "com", "como", "da", "das", "de", "dela", "delas", "dele", "deles",
    "depois", "do", "dos", "e", "ela", "elas", "ele", "eles", "em", "entre",
    "era", "eram", "essa", "essas", "esse", "esses", "esta", "estao", "estar",
    "estas", "este", "estes", "eu", "foi", "foram", "ha", "isso", "isto", "ja",
    "mais", "mas", "me", "mesmo", "meu", "minha", "muito", "na", "nas", "nao",
    "nem", "no", "nos", "nossa", "nosso", "num", "numa", "o", "os", "ou", "para",
    "pela", "pelas", "pelo", "pelos", "por", "porque", "quando", "que", "quem",
    "se", "sem", "ser", "seu", "sua", "tambem", "tem", "ter", "teve", "um", "uma",
    "umas", "uns", "vai", "voce",
    # ruido jornalistico recorrente
    "afirma", "afirmou", "ainda", "ano", "anos", "apos", "caso", "cerca", "dia",
    "dias", "disse", "durante", "feira", "foto", "hoje", "informou", "nesta",
    "neste", "nova", "novo", "outro", "outros", "segundo", "sobre", "texto",
    "vez", "via",
    # componentes de dominio/plataforma
    "www", "http", "https", "com", "br", "org", "net", "news", "noticias",
}

_TOKEN_RE = re.compile(r"[A-Za-zÀ-ÖØ-öø-ÿ]{3,}", flags=re.UNICODE)


def _key(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", value.casefold())
    return "".join(char for char in normalized if not unicodedata.combining(char))


def _site_tokens(item: MediaItem) -> set[str]:
    excluded: set[str] = set()
    for value in (item.domain, item.source_name):
        if not value:
            continue
        for token in _TOKEN_RE.findall(str(value).replace(".", " ").replace("-", " ")):
            key = _key(token)
            if len(key) >= 3:
                excluded.add(key)
    return excluded


def _document_text(item: MediaItem) -> str:
    """Evita contar title/snippet/content tres vezes quando o corpo existe."""
    body = item.content or item.snippet or ""
    return f"{item.title or ''}\n{body}".strip()


def _variant_candidates(value: str) -> list[str]:
    """Variantes morfologicas conservadoras para reduzir repeticao visual.

    Nao e stemming geral: so gera transformacoes frequentes e reversiveis do
    portugues. A fusao so acontece quando a forma candidata existe de fato no
    corpus, evitando aproximar palavras apenas por semelhanca semantica.
    """
    candidates: list[str] = []
    length = len(value)

    if length >= 6:
        # operacoes -> operacao; intervencoes -> intervencao
        if value.endswith("oes"):
            candidates.append(value[:-3] + "ao")
        # policiais/sociais -> policial/social
        if value.endswith("ais"):
            candidates.append(value[:-3] + "al")
        # homens -> homem
        if value.endswith("ens"):
            candidates.append(value[:-3] + "em")
        # autores/trabalhadores -> autor/trabalhador (quando a forma existe)
        if value.endswith("res"):
            candidates.append(value[:-2])
        # plural regular: mulheres -> mulher, crimes -> crime etc.
        if value.endswith("es"):
            candidates.append(value[:-2])
        if value.endswith("s") and not value.endswith(("ss", "us")):
            candidates.append(value[:-1])

    # Genero gramatical: criminoso/criminos a, armado/armada etc.
    # So funde se a outra forma estiver presente no corpus.
    if length >= 6 and value[-1:] in {"a", "o"}:
        candidates.append(value[:-1] + ("o" if value.endswith("a") else "a"))

    return list(dict.fromkeys(candidate for candidate in candidates if len(candidate) >= 3))


def _merge_term_variants(
    counts: Counter[str],
    surfaces: dict[str, Counter[str]],
) -> tuple[Counter[str], dict[str, Counter[str]], int]:
    """Agrupa variantes morfologicas sem alterar termos semanticamente distintos."""
    merged_counts: Counter[str] = Counter(counts)
    merged_surfaces: dict[str, Counter[str]] = {
        key: Counter(value) for key, value in surfaces.items()
    }
    merged_variants = 0

    # Formas mais frequentes viram representantes naturais. Em empate, a forma
    # mais curta tende a ser o singular/base e recebe prioridade.
    representatives = sorted(
        merged_counts,
        key=lambda key: (-merged_counts[key], len(key), key),
    )
    available = set(merged_counts)

    for value in sorted(list(available), key=lambda key: (len(key), key), reverse=True):
        if value not in available:
            continue

        candidates = [
            candidate for candidate in _variant_candidates(value)
            if candidate in available and candidate != value
        ]
        if not candidates:
            continue

        target = min(
            candidates,
            key=lambda candidate: (
                -merged_counts[candidate],
                len(candidate),
                representatives.index(candidate) if candidate in representatives else 10_000,
            ),
        )

        merged_counts[target] += merged_counts.pop(value)
        merged_surfaces[target].update(merged_surfaces.pop(value, Counter()))
        available.remove(value)
        merged_variants += 1

    return merged_counts, merged_surfaces, merged_variants


def word_cloud_for_project(
    db: Session,
    project_id: int,
    *,
    max_words: int = 45,
) -> dict:
    """Gera frequencias para a nuvem usando apenas noticias validadas."""

    candidates = db.scalars(
        select(MediaItem)
        .where(MediaItem.project_id == project_id, MediaItem.status == "VALID")
        .order_by(MediaItem.id.asc())
    ).all()

    items = [
        item
        for item in candidates
        if (item.media_origin or classify_media_origin(item.url, item.domain)) == PORTAL_NOTICIAS
    ]

    # Remove nomes dos veiculos presentes no proprio corpus, inclusive quando
    # aparecem no corpo de outras materias.
    excluded_sites: set[str] = set()
    for item in items:
        excluded_sites.update(_site_tokens(item))

    counts: Counter[str] = Counter()
    surfaces: dict[str, Counter[str]] = defaultdict(Counter)
    total_tokens = 0

    for item in items:
        for token in _TOKEN_RE.findall(_document_text(item)):
            normalized = _key(token)
            if (
                len(normalized) < 3
                or normalized in _STOPWORDS
                or normalized in excluded_sites
                or normalized.isdigit()
            ):
                continue
            display = token.casefold()
            counts[normalized] += 1
            surfaces[normalized][display] += 1
            total_tokens += 1

    normalized_counts, normalized_surfaces, merged_variants = _merge_term_variants(
        counts,
        surfaces,
    )

    top = normalized_counts.most_common(max(1, min(int(max_words or 45), 80)))
    max_count = top[0][1] if top else 0
    min_count = top[-1][1] if top else 0
    spread = max(1, max_count - min_count)

    words = []
    for normalized, count in top:
        display = normalized_surfaces[normalized].most_common(1)[0][0]
        weight = 1.0 if max_count == min_count else (count - min_count) / spread
        words.append(
            {
                "word": display,
                "count": int(count),
                "weight": round(float(weight), 4),
            }
        )

    return {
        "source": "validated_news_corpus",
        "media_origin": PORTAL_NOTICIAS,
        "documents": len(items),
        "total_tokens": total_tokens,
        "unique_tokens": len(normalized_counts),
        "raw_unique_tokens": len(counts),
        "merged_variants": merged_variants,
        "excluded_site_tokens": len(excluded_sites),
        "words": words,
    }
