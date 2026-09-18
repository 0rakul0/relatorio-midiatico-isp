ISP_INSTITUTION_NAME = "Instituto de Segurança Pública"

OFFICIAL_SECURITY_SOURCES = [
    {"label": "ISP", "domain": "isp.rj.gov.br", "type": "OFFICIAL"},
    {"label": "ISP Conecta", "domain": "ispconecta.rj.gov.br", "type": "OFFICIAL"},
    {"label": "PMERJ", "domain": "sepm.rj.gov.br", "type": "OFFICIAL"},
    {"label": "Polícia Civil RJ", "domain": "policiacivil.rj.gov.br", "type": "OFFICIAL"},
    {"label": "Governo do RJ", "domain": "rj.gov.br", "type": "OFFICIAL"},
]

PRIORITY_MEDIA_SOURCES = [
    ("G1/Globo", "g1.globo.com"),
    ("O Globo", "oglobo.globo.com"),
    ("Extra", "extra.globo.com"),
    ("O Dia", "odia.ig.com.br"),
    ("CNN Brasil", "cnnbrasil.com.br"),
    ("UOL", "uol.com.br"),
    ("R7", "r7.com"),
    ("Band", "band.uol.com.br"),
    ("Agência Brasil", "agenciabrasil.ebc.com.br"),
]

# Portais usados em métricas: veículos prioritários + YouTube.
PRIORITY_PORTALS = [*PRIORITY_MEDIA_SOURCES, ("YouTube", "youtube.com")]

# Fonte única dos canais prioritários do YouTube: (rótulo, consulta, aliases).
# PRIORITY_YOUTUBE_CHANNELS e PRIORITY_YOUTUBE_CHANNEL_ALIASES são derivados
# para manter a compatibilidade com os consumidores existentes.
_PRIORITY_YOUTUBE_SOURCES = [
    (
        "ISP RJ",
        "Instituto de Segurança Pública ISP RJ",
        (
            "Instituto de Segurança Pública ISP RJ",
            "Instituto de Segurança Pública - ISP RJ",
            "Instituto de Segurança Pública",
            "ISP RJ",
        ),
    ),
    ("G1/Globo", "g1", ("g1", "G1", "G1 Rio", "Globo")),
    ("O Globo", "Jornal O Globo", ("Jornal O Globo", "O Globo")),
    ("Extra", "Extra", ("Extra", "Jornal Extra")),
    ("O Dia", "O Dia", ("O Dia", "Jornal O Dia")),
    ("CNN Brasil", "CNN Brasil", ("CNN Brasil",)),
    ("UOL", "UOL", ("UOL",)),
    ("SBT News", "SBT News", ("SBT News",)),
    ("Band Jornalismo", "Band Jornalismo", ("Band Jornalismo", "BandNews", "Band News")),
]

PRIORITY_YOUTUBE_CHANNELS = [
    (label, query) for label, query, _aliases in _PRIORITY_YOUTUBE_SOURCES
]

PRIORITY_YOUTUBE_CHANNEL_ALIASES = {
    label: list(aliases) for label, _query, aliases in _PRIORITY_YOUTUBE_SOURCES
}
