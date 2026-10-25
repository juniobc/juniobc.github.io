from . import config

SEED = [
    ("presidente", "Helena", "Presidente", "Diretoria",
     "Você é a Presidente e REVISORA da produção. A cada turno leia a seção mais recente com read_product; se estiver fraca, peça reescrita com request_rewrite; se estiver boa, escreva a próxima seção do plano com write_section. Nada de planejar de novo nem de conversar sobre arquivos."),
    ("ceo", "Rafael", "CEO", "Diretoria",
     "Você é o CEO e executa a produção: a cada turno escreva COMPLETA a próxima seção do plano com write_section. Não peça nada a ninguém. Contrate só se faltar uma função, com motivo."),
]


def seed(conn):
    if conn.execute("SELECT COUNT(*) AS n FROM employees").fetchone()["n"]:
        return False
    for i, (slug, name, role, area, ins) in enumerate(SEED):
        conn.execute("INSERT INTO employees(slug,name,role,area,instructions,model,hire_reason) VALUES (%s,%s,%s,%s,%s,%s,%s)",
                     (slug, name, role, area, ins, config.DEFAULT_ROTATION[i % len(config.DEFAULT_ROTATION)], "Fundador"))
    return True


def refresh_instructions(conn):
    """As instruções dos fundadores vêm do código: atualiza se mudaram (o dono não as edita)."""
    for slug, _name, _role, _area, ins in SEED:
        conn.execute("UPDATE employees SET instructions=%s WHERE slug=%s AND instructions<>%s", (ins, slug, ins))
