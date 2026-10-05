from __future__ import annotations

import re
import unicodedata
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Classification, MediaItem, Project
from app.services.collection.common import (
    inferred_publication_date,
    publication_year,
    source_label,
)
from app.services.collection.media_origin import (
    PORTAL_NOTICIAS,
    REDE_SOCIAL,
    YOUTUBE,
    classify_media_origin,
)


TRACKING_QUERY_KEYS = {
    "fbclid", "gclid", "dclid", "msclkid", "mc_cid", "mc_eid",
    "igshid", "mkt_tok", "vero_id",
}
TITLE_STOPWORDS = {
    "a", "as", "com", "da", "das", "de", "do", "dos", "e", "em",
    "na", "nas", "no", "nos", "o", "os", "para", "por", "um", "uma",
}
TITLE_TOKEN_RE = re.compile(r"[a-z0-9]+", flags=re.IGNORECASE)


def report_url_key(value: str | None) -> str:
    raw = (value or "").strip()
    if not raw:
        return ""
    try:
        parsed = urlsplit(raw)
    except ValueError:
        return raw.casefold()

    host = (parsed.hostname or "").casefold()
    if host.startswith("www."):
        host = host[4:]
    port = parsed.port
    netloc = host
    if port and not (
        (parsed.scheme.casefold() == "http" and port == 80)
        or (parsed.scheme.casefold() == "https" and port == 443)
    ):
        netloc = f"{host}:{port}"

    path = re.sub(r"/{2,}", "/", parsed.path or "/")
    if path != "/":
        path = path.rstrip("/")

    kept_query = []
    for key, value_part in parse_qsl(parsed.query, keep_blank_values=True):
        lowered = key.casefold()
        if lowered.startswith("utm_") or lowered in TRACKING_QUERY_KEYS:
            continue
        kept_query.append((key, value_part))
    kept_query.sort(key=lambda pair: (pair[0].casefold(), pair[1]))

    return urlunsplit(
        (
            parsed.scheme.casefold() or "https",
            netloc,
            path,
            urlencode(kept_query, doseq=True),
            "",
        )
    )


def normalized_report_title(value: str | None) -> str:
    normalized = unicodedata.normalize("NFKD", (value or "").casefold())
    normalized = "".join(
        char for char in normalized if not unicodedata.combining(char)
    )
    return " ".join(TITLE_TOKEN_RE.findall(normalized))


def report_title_tokens(value: str | None) -> set[str]:
    return {
        token
        for token in normalized_report_title(value).split()
        if token not in TITLE_STOPWORDS
    }


def corpus_domain(item: dict) -> str:
    domain = str(item.get("domain") or "").casefold().strip()
    if not domain and item.get("url"):
        try:
            domain = (urlsplit(str(item["url"])).hostname or "").casefold()
        except ValueError:
            domain = ""
    return domain[4:] if domain.startswith("www.") else domain


def corpus_year(item: dict) -> str:
    published_at = str(item.get("published_at") or "")
    if len(published_at) >= 4 and published_at[:4].isdigit():
        return published_at[:4]
    year = str(item.get("published_year") or "")
    return year if len(year) == 4 and year.isdigit() else ""


def same_report_item(left: dict, right: dict) -> bool:
    left_url = report_url_key(
        str(left.get("canonical_url") or left.get("url") or "")
    )
    right_url = report_url_key(
        str(right.get("canonical_url") or right.get("url") or "")
    )
    if left_url and right_url and left_url == right_url:
        return True

    if corpus_domain(left) != corpus_domain(right):
        return False

    left_origin = left.get("media_origin")
    right_origin = right.get("media_origin")
    if left_origin and right_origin and left_origin != right_origin:
        return False

    left_year = corpus_year(left)
    right_year = corpus_year(right)
    if left_year and right_year and left_year != right_year:
        return False

    left_title = normalized_report_title(left.get("title"))
    right_title = normalized_report_title(right.get("title"))
    if not left_title or not right_title:
        return False
    if left_title == right_title:
        return True

    left_tokens = report_title_tokens(left.get("title"))
    right_tokens = report_title_tokens(right.get("title"))
    if min(len(left_tokens), len(right_tokens)) < 3:
        return False

    intersection = len(left_tokens & right_tokens)
    containment = intersection / min(len(left_tokens), len(right_tokens))
    union = len(left_tokens | right_tokens)
    jaccard = intersection / union if union else 0.0
    shorter = min(left_title, right_title, key=len)
    title_containment = (
        len(shorter) >= 12
        and (left_title in right_title or right_title in left_title)
    )
    return containment >= 0.90 and (jaccard >= 0.72 or title_containment)


def corpus_item_quality(item: dict) -> tuple[int, int, int, int]:
    return (
        1 if item.get("published_at") else 0,
        1 if item.get("evidence") or item.get("relation_evidence") else 0,
        len(str(item.get("title") or "")),
        1 if item.get("url") else 0,
    )


def deduplicate_corpus(corpus: list[dict]) -> list[dict]:
    representatives: list[dict] = []

    for source_item in corpus or []:
        candidate = dict(source_item)
        candidate["duplicate_count"] = int(candidate.get("duplicate_count") or 0)
        candidate["duplicate_urls"] = list(candidate.get("duplicate_urls") or [])

        duplicate_index = next(
            (
                index
                for index, existing in enumerate(representatives)
                if same_report_item(existing, candidate)
            ),
            None,
        )
        if duplicate_index is None:
            representatives.append(candidate)
            continue

        existing = representatives[duplicate_index]
        duplicate_count = (
            int(existing.get("duplicate_count") or 0)
            + int(candidate.get("duplicate_count") or 0)
            + 1
        )

        all_urls: list[str] = []
        for value in [
            existing.get("url"),
            *(existing.get("duplicate_urls") or []),
            candidate.get("url"),
            *(candidate.get("duplicate_urls") or []),
        ]:
            url = str(value or "").strip()
            if url and url not in all_urls:
                all_urls.append(url)

        representative = (
            candidate
            if corpus_item_quality(candidate) > corpus_item_quality(existing)
            else existing
        )
        representative = dict(representative)
        representative["duplicate_count"] = duplicate_count
        representative["duplicate_urls"] = [
            url for url in all_urls if url != representative.get("url")
        ]
        representatives[duplicate_index] = representative

    return representatives


def corpus_for_project(db: Session, project_id: int) -> list[dict]:
    project = db.get(Project, project_id)
    rows = db.execute(
        select(MediaItem, Classification)
        .outerjoin(Classification, Classification.media_item_id == MediaItem.id)
        .where(MediaItem.project_id == project_id, MediaItem.status == "VALID")
        .order_by(MediaItem.published_at.desc().nullslast(), MediaItem.id.desc())
    ).all()
    if project and project.has_custom_date_window:
        rows = [
            (item, classification)
            for item, classification in rows
            if (
                (publication_date := inferred_publication_date(item)) is not None
                and project.collection_start
                <= publication_date
                <= project.collection_end
            )
        ]

    raw_corpus = [
        {
            "title": item.title,
            "url": item.url,
            "canonical_url": item.canonical_url,
            "domain": item.domain,
            "media_origin": item.media_origin
            or classify_media_origin(item.url, item.domain),
            "theme": classification.theme if classification else None,
            "framing": classification.framing if classification else None,
            "evidence": classification.evidence
            if classification
            else item.relevance_evidence,
            "relation_type": item.relation_type,
            "relation_evidence": item.relevance_evidence,
            "corpus_origin": item.corpus_origin or "SEARCH",
            "search_source": item.search_source,
            "isp_mentioned": classification.isp_mentioned
            if classification
            else False,
            "published_at": item.published_at.isoformat()
            if item.published_at
            else None,
            "published_year": publication_year(item),
            "source": source_label(item),
            "view_count": item.view_count,
        }
        for item, classification in rows
    ]
    return deduplicate_corpus(raw_corpus)


def split_corpus(corpus: list[dict]) -> tuple[list[dict], list[dict]]:
    traditional: list[dict] = []
    social: list[dict] = []
    for item in corpus:
        origin = item.get("media_origin")
        if not origin:
            domain = (item.get("domain") or "").lower()
            origin = classify_media_origin(None, domain)
        target = traditional if origin == PORTAL_NOTICIAS else social
        target.append(item)
    return traditional, social


def split_corpus_by_origin(corpus: list[dict]) -> dict[str, list[dict]]:
    buckets: dict[str, list[dict]] = {
        PORTAL_NOTICIAS: [],
        REDE_SOCIAL: [],
        YOUTUBE: [],
    }
    for item in corpus:
        origin = item.get("media_origin")
        if origin not in buckets:
            domain = (item.get("domain") or "").lower()
            origin = classify_media_origin(item.get("url"), domain)
        buckets[origin].append(item)
    return {
        "portal_noticias": buckets[PORTAL_NOTICIAS],
        "redes_sociais": buckets[REDE_SOCIAL],
        "youtube": buckets[YOUTUBE],
    }
