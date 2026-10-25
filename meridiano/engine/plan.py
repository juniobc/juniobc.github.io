"""Plano inicial de produção: lista fixa de seções por produto. Garante produtos e seções no banco (idempotente)."""
from . import tools

PLAN = [
    ("Toolkit do Vendedor E-commerce", "Guia prático para quem quer vender online no Brasil, do nicho ao pós-venda.", [
        "Introdução e como usar este toolkit", "Escolhendo nicho e produto", "Precificação e margem de lucro",
        "Fornecedores e logística", "Página de produto que vende", "Marketing de entrada: SEO e conteúdo",
        "Anúncios pagos para iniciantes", "Atendimento e pós-venda", "Indicadores e rotina semanal", "Checklists e modelos prontos"]),
    ("Plano Mestre Financeiro Pessoal", "Material educativo de organização financeira pessoal. Não é recomendação individual de investimento.", [
        "Diagnóstico da sua situação financeira", "Orçamento 50/30/20 adaptável", "Reserva de emergência",
        "Saindo das dívidas", "Cartão de crédito e crédito consciente", "Investimentos para iniciantes no Brasil",
        "Metas e planejamento anual", "Seguros e proteção", "Renda extra e aumento de receita", "Rotina mensal e planilhas modelo"]),
    ("Copy de Vendas", "Como escrever textos de venda honestos que convertem.", [
        "Princípios de copy que converte", "Conhecendo o cliente: pesquisa de público", "Headlines e ganchos",
        "Estrutura de página de vendas", "Ofertas, preço e garantia", "Prova social e objeções",
        "Copy para anúncios e e-mails (somente com consentimento)", "Testes, métricas e checklist final"]),
]


def ensure(conn):
    """Cria (ou reaproveita, por nome parecido) os produtos do plano e suas seções."""
    for name, desc, titles in PLAN:
        prod = next((p for p in conn.execute("SELECT id,name FROM products").fetchall() if tools.similar(p["name"], name)), None)
        if prod:
            pid = prod["id"]
            if prod["name"] != name:   # produto antigo com outro nome (ex.: com ano): renomeia em vez de duplicar
                conn.execute("UPDATE products SET name=%s, description=%s WHERE id=%s", (name, desc, pid))
        else:
            pid = conn.execute("INSERT INTO products(name,description,file_path) VALUES (%s,%s,%s) RETURNING id",
                               (name, desc, tools.slugify(name) + ".md")).fetchone()["id"]
        for i, t in enumerate(titles, 1):
            conn.execute("INSERT INTO product_sections(product_id,position,title) VALUES (%s,%s,%s) ON CONFLICT (product_id, position) DO UPDATE SET title=EXCLUDED.title", (pid, i, t))
