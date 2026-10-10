from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
from typing import Callable

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.cost_tracker import cost_context
from app.models import Project, SocialAnalysis, SocialComment, SocialPost
from app.schemas import SocialCommentBatchResponse, SocialDiscourseAnalysisResponse
from app.social.methodology import METHODOLOGY_NOTE
from app.social.sampling import balanced_sample


def human_summary(
    analyzed: int,
    sentiment: Counter,
    emotion: Counter,
    position: Counter,
    themes: Counter,
) -> str:
    if analyzed <= 0:
        return "Comentarios coletados, mas sem classificacao semantica disponivel."

    def top(counter: Counter, fallback: str) -> str:
        return str(counter.most_common(1)[0][0]) if counter else fallback

    summary = (
        f"Na amostra de {analyzed} comentario(s) classificado(s), "
        f"o sentimento mais frequente foi {top(sentiment, 'nao identificado').lower()}, "
        f"a emocao mais frequente foi {top(emotion, 'nao identificada').lower()} "
        f"e a posicao mais frequente foi {top(position, 'nao identificada').lower()}."
    )
    top_themes = ", ".join(label for label, _count in themes.most_common(5))
    if top_themes:
        summary += f" Temas recorrentes: {top_themes}."
    return summary


def analyze_social_comments(
    db: Session,
    project: Project,
    *,
    settings,
    llm_configured: bool,
    agent_factory: Callable,
    report_builder: Callable[[Session, int], dict],
) -> dict:
    comments = list(
        db.scalars(
            select(SocialComment)
            .where(SocialComment.project_id == project.id)
            .order_by(SocialComment.like_count.desc(), SocialComment.id.asc())
        ).all()
    )
    posts = list(
        db.scalars(
            select(SocialPost).where(SocialPost.project_id == project.id)
        ).all()
    )
    # Somente comentarios associados a posts ligados ao tema entram na analise.
    from app.social.discovery import social_topic_relevance
    from app.models import MediaItem
    media_ids = [post.media_item_id for post in posts if post.media_item_id]
    media = {item.id: item for item in db.scalars(
        select(MediaItem).where(MediaItem.id.in_(media_ids))
    ).all()} if media_ids else {}
    posts = [
        post for post in posts
        if social_topic_relevance(
            project,
            media[post.media_item_id].title if post.media_item_id in media else post.post_text,
            media[post.media_item_id].snippet if post.media_item_id in media else None,
        )[0]
    ]
    allowed_posts = {post.id for post in posts}
    comments = [comment for comment in comments if comment.social_post_id in allowed_posts]
    platform_counts = dict(Counter(comment.platform for comment in comments))

    analysis = db.scalar(
        select(SocialAnalysis).where(SocialAnalysis.project_id == project.id)
    )
    if analysis is None:
        analysis = SocialAnalysis(
            project_id=project.id,
            methodology_note=METHODOLOGY_NOTE,
        )
        db.add(analysis)

    analysis.total_posts = len(posts)
    analysis.total_comments = len(comments)
    analysis.platform_counts = platform_counts
    analysis.methodology_note = METHODOLOGY_NOTE
    analysis.generated_at = datetime.now(timezone.utc).replace(tzinfo=None)

    if not comments:
        analysis.status = "NO_COMMENTS"
        analysis.analyzed_comments = 0
        analysis.sentiment_counts = {}
        analysis.emotion_counts = {}
        analysis.position_counts = {}
        analysis.themes = []
        analysis.discourse_analysis = {}
        analysis.summary = "Nenhum comentario publico foi recuperado na amostra."
        db.commit()
        return report_builder(db, project.id)

    if not llm_configured:
        analysis.status = "COLLECTED_ONLY"
        analysis.analyzed_comments = 0
        analysis.sentiment_counts = {}
        analysis.emotion_counts = {}
        analysis.position_counts = {}
        analysis.themes = []
        analysis.discourse_analysis = {}
        analysis.summary = (
            "Comentarios coletados; classificacao semantica indisponivel sem LLM."
        )
        db.commit()
        return report_builder(db, project.id)

    sample = balanced_sample(
        comments,
        max(1, int(settings.social_analysis_max_comments)),
    )
    batch_size = max(5, int(settings.social_analysis_batch_size))
    sentiment: Counter = Counter()
    emotion: Counter = Counter()
    position: Counter = Counter()
    themes: Counter = Counter()
    analyzed_indices: set[int] = set()

    indexed = list(enumerate(sample))
    with cost_context(
        project_id=project.id,
        operation="social_comment_analysis",
        schema_name="social_comment_analysis_v1",
    ):
        for offset in range(0, len(indexed), batch_size):
            batch = indexed[offset : offset + batch_size]
            payload = {
                "project": {
                    "topic": project.topic,
                    "collection_start": project.collection_start.isoformat(),
                    "collection_end": project.collection_end.isoformat(),
                },
                "methodology": METHODOLOGY_NOTE,
                "comments": [
                    {
                        "index": index,
                        "platform": comment.platform,
                        "post_id": comment.social_post_id,
                        "text": comment.text[:700],
                        "like_count": comment.like_count,
                        "reply_count": comment.reply_count,
                    }
                    for index, comment in batch
                ],
            }
            result = agent_factory().run(
                task="social_comment_analysis",
                payload=payload,
                response_model=SocialCommentBatchResponse,
                schema_name="social_comment_analysis_v1",
                max_output_tokens=3500,
            )
            valid_indices = {index for index, _comment in batch}
            for item in result.get("assessments") or []:
                index = int(item.get("index", -1))
                if index not in valid_indices or index in analyzed_indices:
                    continue
                analyzed_indices.add(index)
                sentiment[str(item.get("sentiment") or "NEUTRO")] += 1
                emotion[str(item.get("emotion") or "NAO_IDENTIFICAVEL")] += 1
                position[str(item.get("position") or "NAO_IDENTIFICAVEL")] += 1
                for theme in item.get("themes") or []:
                    cleaned = " ".join(str(theme).split()).strip().lower()
                    if 2 <= len(cleaned) <= 80:
                        themes[cleaned] += 1

    discourse_analysis: dict = {}
    if analyzed_indices:
        discourse_comments = [
            {
                "platform": comment.platform,
                "post_id": comment.social_post_id,
                "text": comment.text[:700],
                "like_count": comment.like_count,
                "reply_count": comment.reply_count,
            }
            for index, comment in indexed
            if index in analyzed_indices
        ]
        discourse_payload = {
            "project": {
                "topic": project.topic,
                "collection_start": project.collection_start.isoformat(),
                "collection_end": project.collection_end.isoformat(),
            },
            "sample": {
                "comments_collected": len(comments),
                "comments_classified": len(analyzed_indices),
                "comments_in_discourse_analysis": len(discourse_comments),
                "posts_represented": len(
                    {comment["post_id"] for comment in discourse_comments}
                ),
                "platform_counts": platform_counts,
                "sentiment_counts": dict(sentiment),
                "emotion_counts": dict(emotion),
                "position_counts": dict(position),
                "themes": [
                    {"theme": theme, "count": count}
                    for theme, count in themes.most_common(12)
                ],
            },
            "comments": discourse_comments,
            "methodology": METHODOLOGY_NOTE,
        }
        with cost_context(
            project_id=project.id,
            operation="social_discourse_analysis",
            schema_name="social_discourse_analysis_v1",
        ):
            discourse_analysis = agent_factory().run(
                task="social_discourse_analysis",
                payload=discourse_payload,
                extra_instructions=(
                    "Faça uma análise qualitativa e discursiva dos comentários públicos da amostra. "
                    "Considere que a amostra foi balanceada hierarquicamente por plataforma e por post, "
                    "misturando comentários mais curtidos, mais respondidos, recentes e de baixo engajamento. "
                    "Não se limite a repetir percentuais de positivo/negativo. Explique narrativas, "
                    "argumentos, conflitos, formas de interação, rejeição, apoio, fadiga, confiança, "
                    "desconfiança, ironia, personalismo e sinais de polarização quando sustentados pelos comentários. "
                    "Nunca escreva 'a população pensa', 'os brasileiros são' ou equivalentes. Use formulações como "
                    "'entre os comentários analisados', 'uma parcela da amostra manifesta' e 'o debate observado sugere'. "
                    "Não invente grupos, intenções ou causas que não estejam sustentados pela amostra. "
                    "Não reproduza nomes de usuários nem dados pessoais. Aponte contradições e limitações da amostra."
                ),
                response_model=SocialDiscourseAnalysisResponse,
                schema_name="social_discourse_analysis_v1",
                max_output_tokens=5000,
            )

    analysis.status = "COMPLETED" if analyzed_indices else "COLLECTED_ONLY"
    analysis.analyzed_comments = len(analyzed_indices)
    analysis.sentiment_counts = dict(sentiment)
    analysis.emotion_counts = dict(emotion)
    analysis.position_counts = dict(position)
    analysis.themes = [
        {"theme": theme, "count": count}
        for theme, count in themes.most_common(12)
    ]
    analysis.discourse_analysis = discourse_analysis
    analysis.summary = (
        discourse_analysis.get("overall_reading")
        if discourse_analysis
        else human_summary(
            len(analyzed_indices),
            sentiment,
            emotion,
            position,
            themes,
        )
    )
    db.commit()
    return report_builder(db, project.id)
