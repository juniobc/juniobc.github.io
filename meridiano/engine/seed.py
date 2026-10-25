from . import config

SEED = [
    ("presidente", "Helena", "Presidente", "Diretoria",
     "Defina a estratégia. Ache produtos digitais que podem ser vendidos em marketplaces brasileiros (guias, planilhas). Peça ao CEO para executar. Lembre que só o dono abre conta de vendedor."),
    ("ceo", "Rafael", "CEO", "Diretoria",
     "Transforme a estratégia em tarefas. Contrate gerentes só quando faltar uma função, com motivo. Acompanhe o andamento e cobre resultados."),
]


def seed(conn):
    if conn.execute("SELECT COUNT(*) AS n FROM employees").fetchone()["n"]:
        return False
    for i, (slug, name, role, area, ins) in enumerate(SEED):
        conn.execute("INSERT INTO employees(slug,name,role,area,instructions,model,hire_reason) VALUES (%s,%s,%s,%s,%s,%s,%s)",
                     (slug, name, role, area, ins, config.DEFAULT_ROTATION[i % len(config.DEFAULT_ROTATION)], "Fundador"))
    return True
