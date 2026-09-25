"""Conversa auditavel sobre o corpus coletado.

A camada sempre faz retrieval local primeiro. O ReportAgent pode decidir chamar
ferramentas externas (web, videos ou arXiv) quando o corpus nao bastar ou quando
o usuario pedir pesquisa/atualizacao. Resultados externos nunca sao persistidos
como corpus validado do relatorio.
"""

from __future__ import annotations

import math
import re
import unicodedata
from datetime import date
from urllib.parse import urlparse

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.agent import get_report_agent
from app.config import get_settings
from app.cost_tracker import cost_context
from app.llm import llm_is_configured
from app.models import GeneratedReport, MediaItem, Project
from app.schemas import ChatResponse
from app.tools.registry import build_agent_tools


_STOPWORDS = {
    "a", "as", "o", "os", "de", "da", "das", "do", "dos", "e", "em",
    "no", "na", "nos", "nas", "para", "por", "com", "um", "uma", "ao",
    "aos", "que", "sobre", "qual", "quais", "como", "foi", "foram", "ser",
    "tem", "teve", "mais", "menos", "entre", "me", "diga", "mostre",
}

_CASUAL_GREETINGS = {
    "oi", "ola", "opa", "e ai", "bom dia", "boa tarde", "boa noite",
    "oi tudo bem", "ola tudo bem", "e ai tudo bem", "tudo bem",
}
_CASUAL_THANKS = {
    "obrigado", "obrigada", "muito obrigado", "muito obrigada", "valeu",
    "agradecido", "agradecida",
}
_CASUAL_FAREWELLS = {
    "tchau", "ate mais", "ate logo", "falou",
}


def _topic_key(value: str | None) -> str:
    text = unicodedata.normalize("NFKD", value or "")
    text = "".join(char for char in text if not unicodedata.combining(char))
    text = text.casefold()
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return " ".join(text.split())


def _tokens(value: str | None) -> set[str]:
    return {
        token
        for token in re.findall(r"[a-z0-9]+", _topic_key(value))
        if len(token) >= 3 and token not in _STOPWORDS
    }


def _visible_projects(db: Session, user) -> list[Project]:
    query = select(Project)
    if not user.is_admin:
        query = query.where(Project.owner_id == user.id)
    return list(db.scalars(query.order_by(Project.created_at.desc())).all())


def _group_project_ids(db: Session, project: Project) -> list[int]:
    query = select(Project.id, Project.topic).where(Project.owner_id == project.owner_id)
    rows = db.execute(query).all()
    key = _topic_key(project.topic)
    return [project_id for project_id, topic in rows if _topic_key(topic) == key]


def _dedupe_items(items: list[MediaItem]) -> list[MediaItem]:
    unique: dict[str, MediaItem] = {}
    for item in items:
        key = (item.canonical_url or item.url or f"id:{item.id}").strip()
        current = unique.get(key)
        if current is None:
            unique[key] = item
            continue
        current_text = (current.content or current.snippet or "").strip()
        candidate_text = (item.content or item.snippet or "").strip()
        if len(candidate_text) > len(current_text):
            unique[key] = item
    return list(unique.values())


def _recent_key(item: MediaItem) -> tuple[date, int]:
    return item.published_at or date.min, item.id or 0


def _latest_user_question(messages: list[dict]) -> str:
    for message in reversed(messages or []):
        if str(message.get("role") or "").lower() == "user":
            return str(message.get("content") or "").strip()
    return ""


def _casual_kind(question: str) -> str | None:
    normalized = _topic_key(question)
    if normalized in _CASUAL_GREETINGS:
        return "GREETING"
    if normalized in _CASUAL_THANKS:
        return "THANKS"
    if normalized in _CASUAL_FAREWELLS:
        return "FAREWELL"
    return None


def _retrieval_question(messages: list[dict]) -> str:
    """Usa contexto conversacional só quando a pergunta atual é curta demais."""
    user_messages = [
        str(message.get("content") or "").strip()
        for message in (messages or [])
        if str(message.get("role") or "").lower() == "user"
        and str(message.get("content") or "").strip()
    ]
    if not user_messages:
        return ""

    latest = user_messages[-1]
    if len(_tokens(latest)) >= 2:
        return latest

    for previous in reversed(user_messages[:-1]):
        if _tokens(previous):
            return f"{previous}\n{latest}"
    return latest


def _casual_answer(kind: str, project_payload: dict, corpus_size: int) -> str:
    topic = str(project_payload.get("topic") or "acervo").strip()
    scope = str(project_payload.get("scope") or "TOPIC").upper()
    base_label = "todo o acervo" if scope == "ALL" else f'a base “{topic}”'

    if kind == "THANKS":
        return "Por nada! Se quiser, posso continuar consultando o acervo ou aprofundar alguma informação."
    if kind == "FAREWELL":
        return "Até mais! Quando quiser retomar, a base continuará disponível para consulta."

    return (
        f"Oi! Estou conectado a {base_label}, com {corpus_size} notícias validadas. "
        "Você pode me perguntar sobre temas, períodos, operações, pessoas, veículos, "
        "dados encontrados ou comparar informações do acervo."
    )


def _normalize_member_references(answer: str, by_index: dict[int, dict]) -> str:
    """Converte referências técnicas como '(Index 0)' em referências públicas [F1]."""
    text = str(answer or "")

    def replace(match: re.Match) -> str:
        index = int(match.group(1))
        if index not in by_index:
            return match.group(0)
        return f"[F{index + 1}]"

    return re.sub(r"[\(\[]?\s*Index\s+(\d+)\s*[\)\]]?", replace, text, flags=re.IGNORECASE)


def _rank_items(items: list[MediaItem], question: str, limit: int) -> list[MediaItem]:
    """BM25-like leve, sem custo externo, para reduzir contexto enviado a LLM."""
    if not items:
        return []
    query_tokens = _tokens(question)
    if not query_tokens:
        # Pergunta sem termos recuperáveis não deve puxar notícias arbitrárias.
        return []

    normalized_question = _topic_key(question)
    document_tokens: dict[int, set[str]] = {}
    document_frequency = {token: 0 for token in query_tokens}

    for item in items:
        combined = " ".join(filter(None, [item.title, item.snippet, (item.content or "")[:6000]]))
        tokens = _tokens(combined)
        document_tokens[item.id] = tokens
        for token in query_tokens:
            if token in tokens:
                document_frequency[token] += 1

    total = max(1, len(items))
    ranked: list[tuple[float, date, int, MediaItem]] = []
    for item in items:
        title_tokens = _tokens(item.title)
        snippet_tokens = _tokens(item.snippet)
        body_tokens = document_tokens.get(item.id, set())
        matched = query_tokens & body_tokens
        score = 0.0
        for token in matched:
            idf = math.log((total + 1) / (document_frequency[token] + 1)) + 1.0
            weight = 1.0
            if token in snippet_tokens:
                weight += 0.8
            if token in title_tokens:
                weight += 2.2
            score += idf * weight

        if query_tokens:
            score += 4.0 * (len(matched) / len(query_tokens))

        haystack = _topic_key(" ".join(filter(None, [item.title, item.snippet, (item.content or "")[:6000]])))
        if len(normalized_question) >= 12 and normalized_question in haystack:
            score += 8.0

        published, item_id = _recent_key(item)
        ranked.append((score, published, item_id, item))

    ranked.sort(key=lambda row: (row[0], row[1], row[2]), reverse=True)
    positive = [row[3] for row in ranked if row[0] > 0]
    selected = positive[:limit]

    # Se a pergunta tiver poucos matches lexicais, completa com documentos
    # recentes. Isso evita contexto vazio para perguntas resumidoras.
    if len(selected) < min(6, limit):
        seen = {item.id for item in selected}
        for item in sorted(items, key=_recent_key, reverse=True):
            if item.id in seen:
                continue
            selected.append(item)
            seen.add(item.id)
            if len(selected) >= limit:
                break
    return selected[:limit]


def _serialize_members(items: list[MediaItem]) -> list[dict]:
    max_chars = get_settings().chat_item_max_chars
    members: list[dict] = []
    for index, item in enumerate(items):
        text = (item.content or "").strip() or (item.snippet or "").strip()
        members.append(
            {
                "index": index,
                "reference": f"F{index + 1}",
                "id": item.id,
                "title": item.title,
                "domain": item.domain,
                "url": item.url,
                "published_at": item.published_at.isoformat() if item.published_at else None,
                "source_name": item.source_name,
                "media_origin": item.media_origin,
                "search_source": item.search_source,
                "content": text[:max_chars],
            }
        )
    return members


def _project_items(db: Session, project: Project) -> tuple[list[MediaItem], list[int]]:
    project_ids = _group_project_ids(db, project) or [project.id]
    items = list(db.scalars(
        select(MediaItem).where(
            MediaItem.project_id.in_(project_ids),
            MediaItem.status == "VALID",
        )
    ).all())
    return _dedupe_items(items), project_ids


def _all_visible_items(db: Session, user) -> tuple[list[MediaItem], list[int]]:
    projects = _visible_projects(db, user)
    project_ids = [project.id for project in projects]
    if not project_ids:
        return [], []
    items = list(db.scalars(
        select(MediaItem).where(
            MediaItem.project_id.in_(project_ids),
            MediaItem.status == "VALID",
        )
    ).all())
    valid_project_ids = sorted({item.project_id for item in items})
    return _dedupe_items(items), valid_project_ids


def list_chat_projects(db: Session, user) -> dict:
    """Uma entrada por tema e um resumo separado para o acervo completo."""
    projects = _visible_projects(db, user)
    if not projects:
        return {"projects": [], "all_corpus": None, "last_active_project_id": None}

    project_ids = [project.id for project in projects]
    generated_at = {
        project_id: generated
        for project_id, generated in db.execute(
            select(GeneratedReport.project_id, GeneratedReport.generated_at)
            .where(GeneratedReport.project_id.in_(project_ids))
        ).all()
    }

    media_rows = db.execute(
        select(MediaItem.id, MediaItem.project_id, MediaItem.canonical_url, MediaItem.url)
        .where(MediaItem.status == "VALID", MediaItem.project_id.in_(project_ids))
    ).all()

    media_by_project: dict[int, set[str]] = {}
    all_urls: set[str] = set()
    valid_project_ids: set[int] = set()
    for item_id, project_id, canonical_url, url in media_rows:
        key = (canonical_url or url or f"id:{item_id}").strip()
        media_by_project.setdefault(project_id, set()).add(key)
        all_urls.add(key)
        valid_project_ids.add(project_id)

    groups: dict[str, list[Project]] = {}
    for project in projects:
        groups.setdefault(_topic_key(project.topic), []).append(project)

    rows: list[dict] = []
    for group_projects in groups.values():
        representative = max(
            group_projects,
            key=lambda item: (
                generated_at.get(item.id) is not None,
                generated_at.get(item.id) or item.created_at,
            ),
        )
        urls: set[str] = set()
        for item in group_projects:
            urls.update(media_by_project.get(item.id, set()))

        report_dates = [generated_at[item.id] for item in group_projects if generated_at.get(item.id)]
        latest_generated = max(report_dates) if report_dates else None
        created_dates = [item.created_at for item in group_projects if item.created_at]
        latest_created = max(created_dates) if created_dates else None
        collection_starts = [item.collection_start for item in group_projects if item.collection_start]
        collection_ends = [item.collection_end for item in group_projects if item.collection_end]

        rows.append(
            {
                "id": representative.id,
                "scope": "TOPIC",
                "topic": " ".join((representative.topic or "").split()),
                "project_type": representative.project_type,
                "collection_start": min(collection_starts).isoformat() if collection_starts else None,
                "collection_end": max(collection_ends).isoformat() if collection_ends else None,
                "valid_items": len(urls),
                "project_count": len(group_projects),
                "project_ids": [item.id for item in group_projects],
                "generated_at": latest_generated.isoformat() if latest_generated else None,
                "created_at": latest_created.isoformat() if latest_created else None,
            }
        )

    rows.sort(
        key=lambda row: (
            bool(row["generated_at"]),
            row["generated_at"] or row["created_at"] or "",
            row["valid_items"],
        ),
        reverse=True,
    )
    available = [row for row in rows if row["valid_items"] > 0]
    last_active = available[0] if available else (rows[0] if rows else None)

    all_corpus = None
    if all_urls:
        all_corpus = {
            "id": "all",
            "scope": "ALL",
            "topic": "Todo o acervo",
            "valid_items": len(all_urls),
            "project_count": len(valid_project_ids),
            "generated_at": None,
            "created_at": None,
        }

    return {
        "projects": rows,
        "all_corpus": all_corpus,
        "last_active_project_id": last_active["id"] if last_active else None,
    }


def _chat_corpus_members(
    db: Session,
    project: Project,
    limit: int | None = None,
    question: str = "",
) -> list[dict]:
    items, _project_ids = _project_items(db, project)
    resolved_limit = limit or get_settings().chat_context_items
    selected = _rank_items(items, question, resolved_limit)
    return _serialize_members(selected)


def _answer_from_items(
    *,
    items: list[MediaItem],
    messages: list[dict],
    project_payload: dict,
    project_id: int | None,
) -> dict:
    question = _latest_user_question(messages)
    casual_kind = _casual_kind(question)
    if casual_kind:
        return {
            "answer": _casual_answer(casual_kind, project_payload, len(items)),
            "sources": [],
            "corpus_size": len(items),
            "context_size": 0,
            "project_count": project_payload.get("consolidated_runs") or project_payload.get("project_count") or 0,
            "retrieval_strategy": "casual_no_retrieval",
            "external_research_used": False,
            "external_source_mode": None,
            "tools_used": [],
        }

    if not llm_is_configured():
        raise RuntimeError(
            "Nenhuma LLM configurada. Defina OPENAI_API_KEY ou configure o fallback local do Ollama."
        )

    retrieval_question = _retrieval_question(messages)
    context_limit = get_settings().chat_context_items
    selected = _rank_items(items, retrieval_question, context_limit)
    members = _serialize_members(selected)

    external_rows: list[dict] = []

    def capture_search(tool_name: str):
        def sink(rows: list[dict], provider: str, query: str) -> list[dict]:
            for row in rows:
                external_rows.append(
                    {
                        **dict(row),
                        "_tool": tool_name,
                        "_provider": provider,
                        "_query": query,
                    }
                )
            # Chat nunca grava esses resultados no corpus: apenas devolve as
            # linhas intactas para a propria tool mostrar ao agente.
            return rows
        return sink

    def capture_academic(rows: list[dict], provider: str) -> None:
        for row in rows:
            external_rows.append(
                {
                    **dict(row),
                    "_tool": "pesquisar_artigos_arxiv",
                    "_provider": provider,
                    "_query": None,
                }
            )

    tools = build_agent_tools(
        enable_web=True,
        enable_video=True,
        enable_academic=True,
        web_sink=capture_search("pesquisar_internet"),
        video_sink=capture_search("pesquisar_videos"),
        academic_sink=capture_academic,
    )

    payload = {
        "project": project_payload,
        "retrieval": {
            "strategy": "lexical_ranked_with_conversation_then_agent_tools",
            "question": question,
            "retrieval_query": retrieval_question,
            "corpus_size": len(items),
            "context_size": len(members),
        },
        "corpus": members,
        "conversation": messages,
    }

    with cost_context(project_id=project_id, operation="corpus_chat", schema_name="chat"):
        result = get_report_agent().run(
            task="chat",
            payload=payload,
            response_model=ChatResponse,
            tools=tools,
            max_output_tokens=4000,
            max_tool_rounds=2,
        )

    used = [int(value) for value in (result.get("used_member_indices") or []) if value is not None]
    by_index = {member["index"]: member for member in members}
    answer = _normalize_member_references(result.get("answer") or "", by_index)
    sources: list[dict] = []
    for index in dict.fromkeys(used):
        member = by_index.get(index)
        if member is None:
            continue
        sources.append(
            {
                "reference": member.get("reference") or f"F{index + 1}",
                "title": member.get("title") or member.get("source_name") or member.get("domain") or member.get("url") or "Fonte do acervo",
                "url": member.get("url"),
                "domain": member.get("domain"),
                "published_at": member.get("published_at"),
                "source_name": member.get("source_name"),
                "media_origin": member.get("media_origin"),
                "search_source": member.get("search_source"),
                "source_scope": "CORPUS",
                "tool": None,
                "query": None,
            }
        )

    captured_by_url: dict[str, dict] = {}
    for row in external_rows:
        url = str(row.get("url") or "").strip()
        if not url:
            continue
        tool_name = str(row.get("_tool") or "pesquisar_internet")
        if url in captured_by_url:
            continue
        parsed = urlparse(url)
        if tool_name == "pesquisar_videos":
            media_origin = "YOUTUBE"
        elif tool_name == "pesquisar_artigos_arxiv":
            media_origin = "ACADEMIC"
        else:
            media_origin = "WEB"

        authors = list(row.get("authors") or [])
        source_name = (
            row.get("source_name")
            or ("arXiv" if tool_name == "pesquisar_artigos_arxiv" else None)
            or row.get("provider")
            or row.get("_provider")
        )
        captured_by_url[url] = {
            "reference": None,
            "title": row.get("title") or row.get("source_name") or parsed.netloc or url or "Fonte externa",
            "url": url,
            "domain": parsed.netloc,
            "published_at": row.get("published_at"),
            "source_name": source_name,
            "media_origin": media_origin,
            "search_source": row.get("_provider") or row.get("provider"),
            "source_scope": "EXTERNAL",
            "tool": tool_name,
            "query": row.get("_query"),
            "authors": authors,
        }

    requested_external_urls = {
        str(value).strip()
        for value in (result.get("used_external_urls") or [])
        if str(value).strip()
    }
    external_source_mode = "used"
    if requested_external_urls:
        external_sources = [
            captured_by_url[url]
            for url in requested_external_urls
            if url in captured_by_url
        ]
    else:
        # Se a tool foi chamada mas o modelo nao marcou URLs na saida final,
        # preservamos uma lista curta como "consultada" para auditoria.
        external_source_mode = "consulted"
        external_sources = list(captured_by_url.values())[:12]

    sources.extend(external_sources)
    tools_used = list(
        dict.fromkeys(
            source.get("tool")
            for source in external_sources
            if source.get("tool")
        )
    )

    return {
        "answer": answer,
        "sources": sources,
        "corpus_size": len(items),
        "context_size": len(members),
        "project_count": project_payload.get("consolidated_runs") or project_payload.get("project_count") or 0,
        "retrieval_strategy": "lexical_ranked_with_conversation_then_agent_tools",
        "external_research_used": bool(external_sources),
        "external_source_mode": external_source_mode if external_sources else None,
        "tools_used": tools_used,
    }


def chat_with_corpus(db: Session, project: Project, messages: list[dict]) -> dict:
    items, project_ids = _project_items(db, project)
    return _answer_from_items(
        items=items,
        messages=messages,
        project_id=project.id,
        project_payload={
            "id": project.id,
            "project_ids": project_ids,
            "scope": "TOPIC",
            "topic": project.topic,
            "project_type": project.project_type,
            "collection_start": _iso(project.collection_start),
            "collection_end": _iso(project.collection_end),
            "consolidated_runs": len(project_ids),
            "unique_valid_items": len(items),
        },
    )


def chat_with_all_corpus(db: Session, user, messages: list[dict]) -> dict:
    items, project_ids = _all_visible_items(db, user)
    return _answer_from_items(
        items=items,
        messages=messages,
        project_id=None,
        project_payload={
            "id": None,
            "project_ids": project_ids,
            "scope": "ALL",
            "topic": "Todo o acervo",
            "project_type": "CORPUS_GLOBAL",
            "project_count": len(project_ids),
            "unique_valid_items": len(items),
        },
    )


def _iso(value: date | None) -> str | None:
    return value.isoformat() if value else None
