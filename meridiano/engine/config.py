"""Configuração lida só do ambiente. Nenhum segredo vive no repositório."""
import os

# Lista fixa: nunca chamar modelo fora dela (evita modelo pago).
ALLOWED_MODELS = (
    "poolside/laguna-s-2.1:free",
    "poolside/laguna-xs-2.1:free",
    "upstage/solar-mini4:free",
    "meituan/longcat-2.0:free",
    "meituan/longcat-2.5-preview:free",
    "stepfun/step-3.7-flash:free",
    "reflection/beam-1.0-501b-a23b:free",
    "ornith/ornith-1.5-122b:free",
)
# step-3.7-flash devolveu 400 "missing tags" num teste simples: fica fora do
# rodízio padrão até alguém confirmar que funciona (ver README).
# laguna-xs gasta o max_tokens só "pensando" e devolve content nulo: também fora do rodízio.
AVOID = ("stepfun/step-3.7-flash:free", "poolside/laguna-xs-2.1:free")
DEFAULT_ROTATION = tuple(m for m in ALLOWED_MODELS if m not in AVOID)

API_BASE = "https://inference-api.nousresearch.com/v1"


def env(name, default=None):
    return os.environ.get(name, default)


def database_url():
    url = env("DATABASE_URL")
    if not url:
        raise RuntimeError("DATABASE_URL não definida (veja /home/empresa/.pg_env)")
    return url


def _limit(name, default, old_deploy_default):
    """Os arquivos de ambiente antigos trazem valores de versões anteriores (300 / 400000, depois 600 / 1500000).
    Esses valores (e menores) são tratados como "valor antigo" e substituídos pelo padrão novo; só um valor maior é respeitado."""
    v = int(env(name, str(default)))
    return default if v <= old_deploy_default else v


# modelos são :free; o limite duro só deixa o ritmo lento (SLOW_TICK_SECONDS), nunca para o motor
DAILY_CALL_LIMIT = _limit("DAILY_CALL_LIMIT", 8000, 2000)          # valores antigos (300, 600, 2000) viram 8000
DAILY_TOKEN_LIMIT = _limit("DAILY_TOKEN_LIMIT", 8000000, 1500000)  # valores antigos (400000, 1500000) viram 8000000
SLOW_TICK_SECONDS = int(env("SLOW_TICK_SECONDS", "600"))
TICK_SECONDS = int(env("TICK_SECONDS", "60"))  # um funcionário por minuto, em rodízio
MAX_TOOL_ROUNDS = int(env("MAX_TOOL_ROUNDS", "5"))
MAX_EMPLOYEES = int(env("MAX_EMPLOYEES", "20"))
MAX_TOKENS = int(env("MAX_TOKENS", "3000"))
DAILY_FAIL_LIMIT = int(env("DAILY_FAIL_LIMIT", "60"))  # chamadas vazias/erro não gastam o limite útil, mas têm teto próprio
PRODUCTS_DIR = env("PRODUCTS_DIR", os.path.join(os.path.expanduser("~"), "meridiano_produtos"))
MIN_SECTION_WORDS = int(env("MIN_SECTION_WORDS", "400"))
TOOL_TIMEOUT_SECONDS = int(env("TOOL_TIMEOUT_SECONDS", "10"))
WORKSPACE_DIR = env("WORKSPACE_DIR", os.path.join(os.path.expanduser("~"), "meridiano", "workspace"))
