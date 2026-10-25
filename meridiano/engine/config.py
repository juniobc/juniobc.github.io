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


DAILY_CALL_LIMIT = int(env("DAILY_CALL_LIMIT", "300"))
DAILY_TOKEN_LIMIT = int(env("DAILY_TOKEN_LIMIT", "400000"))
TICK_SECONDS = int(env("TICK_SECONDS", "150"))  # um funcionário a cada ~2,5 min
MAX_TOOL_ROUNDS = int(env("MAX_TOOL_ROUNDS", "5"))
MAX_EMPLOYEES = int(env("MAX_EMPLOYEES", "20"))
MAX_TOKENS = int(env("MAX_TOKENS", "3000"))
DAILY_FAIL_LIMIT = int(env("DAILY_FAIL_LIMIT", "60"))  # chamadas vazias/erro não gastam o limite útil, mas têm teto próprio
PRODUCTS_DIR = env("PRODUCTS_DIR", os.path.join(os.path.expanduser("~"), "meridiano_produtos"))
