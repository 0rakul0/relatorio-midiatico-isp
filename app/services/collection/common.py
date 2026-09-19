from __future__ import annotations

import re
from datetime import date, datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

from app.models import MediaItem, Project, SearchQuery
from app.services.collection.youtube_helpers import is_youtube_host
from app.year_utils import find_year


# Parâmetros removidos na canonicalização por serem apenas rastreamento.
# Parâmetros semanticamente relevantes (paginadores, ids, filtros) são
# preservados para não fundir URLs diferentes do mesmo site.
_TRACKING_PARAMS = {
    "utm_source",
    "utm_medium",
    "utm_campaign",
    "utm_term",
    "utm_content",
    "utm_id",
    "utm_name",
    "fbclid",
    "gclid",
    "gbraid",
    "wbraid",
    "msclkid",
    "mc_cid",
    "mc_eid",
    "igshid",
    "yclid",
    "dclid",
    "_openstat",
}


def canonicalize(url: str) -> str:
    parsed = urlparse(url)
    host = parsed.netloc.lower().split(":")[0]
    if host == "youtu.be":
        video_id = parsed.path.strip("/").split("/")[0]
        if video_id:
            return urlunparse(("https", "www.youtube.com", "/watch", "", urlencode({"v": video_id}), ""))
    query = ""
    if is_youtube_host(host) and parsed.path.rstrip("/") == "/watch":
        video_id = next((value for key, value in parse_qsl(parsed.query) if key == "v"), "")
        query = urlencode({"v": video_id}) if video_id else ""
    else:
        pairs = [
            (key, value)
            for key, value in parse_qsl(parsed.query, keep_blank_values=True)
            if key.lower() not in _TRACKING_PARAMS
        ]
        pairs.sort()
        query = urlencode(pairs)
    return urlunparse((parsed.scheme.lower(), host, parsed.path.rstrip("/"), "", query, ""))


def parse_provider_date(value: object) -> date | None:
    """Interpreta datas heterogêneas de provedores (DDG).

    Aceita ``date``/``datetime`` nativos, ISO 8601 (com ou sem ``Z``), datas
    simples ``YYYY-MM-DD``, o formato RFC 2822 usado por feeds de notícia e
    formatos comuns ``DD/MM/YYYY`` e ``YYYY/MM/DD``. Retorna ``None`` quando não
    há como interpretar de forma confiável.
    """
    if not value:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value).strip()
    if not text:
        return None

    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).date()
    except ValueError:
        pass

    try:
        return parsedate_to_datetime(text).date()
    except (TypeError, ValueError, IndexError, OverflowError):
        pass

    isolated = re.search(r"(?<!\d)(\d{4})[/-](\d{1,2})[/-](\d{1,2})(?!\d)", text)
    if isolated:
        year, month, day = (int(part) for part in isolated.groups())
        try:
            return date(year, month, day)
        except ValueError:
            pass

    brazilian = re.search(r"(?<!\d)(\d{1,2})/(\d{1,2})/(\d{4})(?!\d)", text)
    if brazilian:
        day, month, year = (int(part) for part in brazilian.groups())
        try:
            return date(year, month, day)
        except ValueError:
            pass

    return None


def result_publication_date(value: object) -> date | None:
    """Compatibilidade: usa o parser central de datas de provedor."""
    return parse_provider_date(value)


def inferred_publication_date(item: MediaItem) -> date | None:
    if item.published_at:
        return item.published_at
    for text_value in (item.url, item.title):
        for match in re.finditer(r"(?<!\d)(20\d{2})[/-](\d{1,2})[/-](\d{1,2})(?!\d)", text_value or ""):
            try:
                return date(int(match.group(1)), int(match.group(2)), int(match.group(3)))
            except ValueError:
                continue
    return None


def publication_year(item: MediaItem) -> str:
    inferred = inferred_publication_date(item)
    if inferred:
        return str(inferred.year)
    for text_value in (item.url, item.title):
        year = find_year(text_value)
        if year:
            return year
    return "N/D"


def source_label(item: MediaItem) -> str:
    host = (item.domain or "").lower()
    if "youtube.com" in host:
        return f"YouTube - {item.source_name}" if item.source_name else "YouTube"
    return item.source_name or item.domain or "Fonte aberta"


def media_window(project: Project) -> tuple[date | None, date | None]:
    """Retorna a janela midiática somente quando ela foi realmente definida.

    Quando o usuário não informou datas e o tema também não permitiu inferir
    uma janela temporal, a coleta deve ser temática. Nesse caso devolvemos
    ``(None, None)`` para que os coletores não recebam um filtro artificial de
    um único dia.
    """
    if not project.has_custom_date_window:
        return None, None
    return project.collection_start, project.collection_end


def query_window(project: Project, query: SearchQuery) -> tuple[date | None, date | None]:
    """Resolve a janela de busca sem inventar datas.

    - OFFICIAL_FACT: sempre temática/documental, sem filtro temporal obrigatório.
    - Projetos sem janela explícita/inferida: busca temática, sem start/end.
    - FACT_DISCOVERY/NOMINAL_FOLLOWUP com janela: usa a janela factual + grace days.
    - MEDIA_REPERCUSSION com janela: usa a janela de repercussão.
    """
    if query.purpose == "OFFICIAL_FACT":
        return None, None

    if not project.has_custom_date_window:
        return None, None

    if query.purpose in {"FACT_DISCOVERY", "NOMINAL_FOLLOWUP"}:
        start = project.event_start or project.collection_start
        end = project.event_end or project.collection_end
        return start, end + timedelta(days=project.fact_grace_days or 0)

    return project.collection_start, project.collection_end


def _valid_search_window(start: date | None, end: date | None) -> bool:
    """Só consideramos uma janela apta a ser enviada ao provedor quando existem
    duas datas e o início é estritamente anterior ao fim.
    """
    return bool(start and end and start < end)


def _append_purpose(item: MediaItem, purpose: str) -> None:
    purposes = list(item.discovery_purposes or [])
    if purpose not in purposes:
        purposes.append(purpose)
        item.discovery_purposes = purposes


def record_source_provenance(
    item: MediaItem,
    *,
    source: str,
    title: str | None,
    url: str,
    published_at: object = None,
    snippet: str | None = None,
    channel: str | None = None,
    view_count: int | None = None,
    query: str | None = None,
    target: str | None = None,
) -> None:
    """Acrescenta evidência por coleta, sem apagar o histórico auditável."""
    snapshots = list(item.source_provenance or [])
    snapshot = {
        "source": source,
        "title": title,
        "url": url,
        "published_at": str(published_at) if published_at else None,
        "snippet": snippet,
        "channel": channel,
        "view_count": view_count,
        "query": query,
        "target": target,
        "retrieved_at": datetime.now(timezone.utc).isoformat(),
    }
    item.source_provenance = [*snapshots, snapshot]


def _site_domain_from_query(query_text: str) -> str | None:
    """Extrai ``site:dominio`` para reaproveitar a intenção no Web Search."""
    match = re.search(r"(?:^|\s)site:([^\s]+)", query_text or "", flags=re.IGNORECASE)
    if not match:
        return None
    domain = match.group(1).strip().strip('"\'()[]{}').lower()
    domain = domain.split("/")[0].split(":")[0]
    return domain or None


