"""Estado real da equipe e as regras de contratação/revisão. Os números vêm do banco, não do agente."""
import re

from . import config

SUGGESTED = [  # (regex do cargo, nome sugerido, entrega mensurável típica)
    (r"produto", "Diretor/a de Produto", "uma seção completa revisada por dia e o plano dos próximos produtos"),
    (r"seo|redator", "Redator/a SEO", "2 artigos úteis por dia em /pub, com palavra-chave e sem promessas"),
    (r"atendimento|suporte", "Atendimento", "respostas a dúvidas de clientes em até 1 turno e um FAQ atualizado"),
    (r"oportunidade|analista", "Analista de Oportunidades", "3 ideias novas por dia pesquisadas, com público e esforço, e 1 teste decidido por dia"),
    (r"designer|página|pagina", "Designer de página", "texto e estrutura de páginas de venda testadas, com 1 melhoria por dia"),
    (r"financeiro|finan", "Financeiro", "relatório diário de caixa e custos reais, e o preço mínimo de cada produto"),
]


def status(conn):
    s = conn.execute("SELECT turn_count FROM engine_status WHERE id=1").fetchone()
    turns = s["turn_count"] if s else 0
    last = conn.execute("SELECT COALESCE(MAX(turn_no),0) AS n FROM team_reviews").fetchone()["n"]
    people = conn.execute("SELECT id,name,role,deliverable FROM employees WHERE active ORDER BY id").fetchall()
    open_tasks = conn.execute("SELECT COUNT(*) AS n FROM tasks WHERE status='open'").fetchone()["n"]
    n = len(people)
    return {"people": people, "headcount": n, "target": config.MIN_TEAM, "below_target": n < config.MIN_TEAM, "turns": turns,
            "last_review_turn": last, "review_due": turns - last >= config.TEAM_REVIEW_EVERY, "open_tasks": open_tasks,
            "tasks_per_person": round(open_tasks / n, 1) if n else 0, "overloaded": n > 0 and open_tasks / n > config.MAX_TASKS_PER_PERSON,
            "next_review_in": max(0, config.TEAM_REVIEW_EVERY - (turns - last))}


def missing_roles(people):
    """Funções sugeridas que ninguém da equipe cobre ainda."""
    cargos = " ".join(p["role"].lower() for p in people)
    return [(nome, entrega) for rx, nome, entrega in SUGGESTED if not re.search(rx, cargos)]


def delivered(conn, emp_id):
    """O que a pessoa realmente fez, dos registros: tarefas concluídas e atividades gravadas."""
    t = conn.execute("SELECT COUNT(*) AS n FROM tasks WHERE assignee_id=%s AND status='done'", (emp_id,)).fetchone()["n"]
    a = conn.execute("SELECT COUNT(*) AS n FROM activity WHERE employee_id=%s AND kind<>'parado'", (emp_id,)).fetchone()["n"]
    s = conn.execute("SELECT COUNT(*) AS n FROM product_sections WHERE written_by=%s", (emp_id,)).fetchone()["n"]
    return t, a, s
