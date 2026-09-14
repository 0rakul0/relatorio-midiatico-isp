"""Agente de monitoramento por veículo e plataforma.

O agente não redige nem altera a análise: ele decide quais consultas auditáveis
devem ser feitas em cada plataforma e entrega os candidatos à coleta. Dessa
forma, novos conectores sociais podem entrar sem alterar a metodologia geral.
"""

from dataclasses import dataclass
import re


PRIORITY_YOUTUBE_CHANNELS = [
    ("ISP RJ", "Instituto de Segurança Pública - ISP RJ"),
    ("G1/Globo", "g1"),
    ("O Globo", "Jornal O Globo"),
    ("Extra", "Extra"),
    ("CNN Brasil", "CNN Brasil"),
    ("UOL", "UOL"),
    ("Agência Brasil", "Agência Brasil"),
    ("SBT News", "SBT News"),
    ("SBT Jornalismo", "SBT Jornalismo"),
]

PRIORITY_WEB_PORTALS = [
    ("G1/Globo", "g1.globo.com"),
    ("O Globo", "oglobo.globo.com"),
    ("Extra", "extra.globo.com"),
    ("CNN Brasil", "cnnbrasil.com.br"),
    ("UOL", "uol.com.br"),
    ("Agência Brasil", "agenciabrasil.ebc.com.br"),
]


@dataclass(frozen=True)
class ScoutTask:
    platform: str
    query: str
    target: str
    rationale: str


class MediaScout:
    """Planejador dedicado para cobertura em veículos de mídia e redes sociais."""

    def __init__(self, topic: str):
        self.topic = topic.strip()
        self.topic_without_year = re.sub(r"\b20\d{2}\b", "", self.topic).strip(" -–—")

    def youtube_tasks(self) -> list[ScoutTask]:
        """Retorna busca ampla e verificações nominais dos canais priorizados."""
        base_topic = self.topic_without_year or self.topic
        tasks = [
            ScoutTask("youtube", self.topic, "Busca temática", "Localizar vídeos que citam o tema informado."),
        ]
        if base_topic.casefold() != self.topic.casefold():
            tasks.append(ScoutTask("youtube", base_topic, "Busca temática", "Capturar títulos que omitem apenas o ano."))
        if "mulher" in self.topic.casefold():
            tasks.append(ScoutTask("youtube", "violência contra mulheres", "Busca temática", "Capturar cobertura jornalística da pauta associada."))
        tasks.extend(
            ScoutTask("youtube", f"{base_topic} {channel_query}", label, f"Verificar cobertura no canal {label}.")
            for label, channel_query in PRIORITY_YOUTUBE_CHANNELS
        )
        # A mesma consulta pode surgir quando o tema não contém ano.
        unique: dict[tuple[str, str], ScoutTask] = {}
        for task in tasks:
            unique.setdefault((task.platform, task.query.casefold()), task)
        return list(unique.values())

    def web_tasks(self) -> list[ScoutTask]:
        """Planeja a cobertura em sites jornalísticos, separada das redes sociais."""
        tasks = [ScoutTask("web", f'"{self.topic}"', "Busca temática", "Localizar matérias e páginas sobre o tema.")]
        tasks.extend(
            ScoutTask("web", f'site:{domain} "{self.topic}"', label, f"Verificar cobertura no portal {label}.")
            for label, domain in PRIORITY_WEB_PORTALS
        )
        return tasks

    @staticmethod
    def platform_status(youtube_enabled: bool) -> list[dict[str, str]]:
        """Expõe plataformas atuais e próximas integrações sem simular cobertura."""
        return [
            {"platform": "Sites jornalísticos", "status": "ativo"},
            {"platform": "YouTube", "status": "ativo" if youtube_enabled else "aguarda YOUTUBE_API_KEY"},
            {"platform": "Instagram", "status": "conector planejado"},
            {"platform": "X", "status": "conector planejado"},
        ]
