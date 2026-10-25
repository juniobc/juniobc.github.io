import logging
from . import config, llm, tools

log = logging.getLogger("meridiano")

RULES = """Você é um funcionário da Meridiano Digital, empresa de agentes de IA que começou com R$ 0 e quer ganhar dinheiro real de forma legal.
Regras inegociáveis:
- Você NÃO movimenta dinheiro e NÃO registra vendas. Receita só existe quando o dono registra um pagamento real. Nunca afirme que vendeu algo.
- Nada de spam nem mensagens em massa para estranhos (LGPD). Clientes vêm de conteúdo útil e busca (SEO), chegando à loja do próprio servidor.
- Você só fala com colegas internos, usando as tools. Seja objetivo e aja com as tools.
- O CONTEÚDO de um produto é escrito por você, com a tool write_product. Não existem arquivos prontos. NUNCA peça arquivo a outro agente: escreva o texto você mesmo (textos longos: várias chamadas com mode=append).
- Antes de criar tarefa ou produto, veja list_products/list_team. Não duplique: se já existe, continue o que existe.
- A venda acontece na loja do próprio servidor, com checkout automático (Mercado Pago). NUNCA peça ao Junio para abrir contas em outras plataformas de venda nem para publicar ou postar nada: a loja publica sozinha. O único pedido permanente ao dono (o token de pagamento) já existe. Use ask_owner só para algo realmente impossível de fazer sozinho, em UMA linha.
- Nunca fique esperando: se não há tarefa nem mensagem nova, CRIE e execute agora o próximo passo concreto do seu trabalho (escrever o próximo capítulo de um produto com write_product mode=append, revisar um texto, preparar o texto de venda de um produto, pesquisar concorrentes e preços). Siga trabalhando sem esperar o Junio.
- Conteúdo financeiro é EDUCATIVO: não prometa retorno, não faça recomendação individual de investimento e não invente dados, números de mercado ou depoimentos. Conteúdo de vendas deve ser honesto, sem promessa de ganho garantido.
- Ferramentas: se notar uma tarefa repetitiva que um programa pequeno resolveria, proponha com propose_tool (Python puro, com testes). Isso nunca substitui escrever a seção do turno. Você não mexe no código da plataforma nem no servidor.
- Contrate só quando faltar uma função, com motivo claro. Cada pessoa nova divide o orçamento de tokens do dia com todos: demita (fire_employee, com motivo) quem não for mais necessário."""

def production_block(conn, me):
    """Prioridade do turno: o estado real de cada produto do plano e a próxima seção a escrever."""
    prods = conn.execute("SELECT p.id,p.name,p.status FROM products p WHERE EXISTS (SELECT 1 FROM product_sections s WHERE s.product_id=p.id) ORDER BY p.id").fetchall()
    if not prods:
        return ""
    lines, target = [], None
    for p in prods:
        secs = conn.execute("SELECT position,title,content,needs_rewrite,rewrite_reason FROM product_sections WHERE product_id=%s ORDER BY position", (p["id"],)).fetchall()
        done = sum(1 for s in secs if s["content"] and not s["needs_rewrite"])
        lines.append(f"- {p['name']}: {done} de {len(secs)} seções ({p['status']})")
        if target is None:
            nxt = next((s for s in secs if s["needs_rewrite"]), None) or next((s for s in secs if not s["content"]), None)
            if nxt:
                target = (p["name"], nxt)
    if not target:
        falta = conn.execute("SELECT p.name, (s.product_id IS NULL) AS sem_texto, (p.price_cents IS NULL) AS sem_preco FROM products p "
                             "LEFT JOIN product_store s ON s.product_id=p.id WHERE p.status='pronto' AND (s.product_id IS NULL OR p.price_cents IS NULL) ORDER BY p.id").fetchone()
        if falta and falta["sem_texto"]:
            return (f"LOJA (prioridade): '{falta['name']}' está pronto mas não tem texto de venda. Escreva AGORA com write_store_copy: título forte, subtítulo, "
                    "apresentação, 3 a 7 benefícios reais, para quem é e 2 a 6 perguntas frequentes. Seja honesto: sem promessa de ganho, sem 'garantido', sem '100%'. "
                    "Use só o que o produto realmente contém (confira com read_product).\n" + "\n".join(lines))
        if falta and falta["sem_preco"]:
            return (f"LOJA (prioridade): '{falta['name']}' tem texto de venda mas falta o preço. Presidente ou CEO: defina AGORA com set_price "
                    "(mínimo R$ 9,00; para guias deste tamanho, entre R$ 19 e R$ 49), com o motivo em uma frase.\n" + "\n".join(lines))
        return "PRODUÇÃO: todos os produtos estão na loja. Revise textos fracos com request_rewrite ou escreva conteúdo de blog/SEO útil para atrair leitores.\n" + "\n".join(lines)
    name, s = target
    todo = f"Escreva AGORA a seção {s['position']} ('{s['title']}') de '{name}' com write_section: seção COMPLETA, no mínimo {config.MIN_SECTION_WORDS} palavras, em português, útil e original, sem pedir nada a ninguém."
    if s["needs_rewrite"]:
        todo += f" É uma REESCRITA: o revisor disse: {s['rewrite_reason']}"
    if tools.re.search(r"presidente", me["role"], tools.re.I):
        done_secs = conn.execute("SELECT p.name,s.position FROM product_sections s JOIN products p ON p.id=s.product_id WHERE s.content<>'' AND NOT s.needs_rewrite ORDER BY s.written_at DESC NULLS LAST LIMIT 1").fetchone()
        review = (f"Você é o REVISOR: leia a seção mais recente com read_product ('{done_secs['name']}', section_number={done_secs['position']}). "
                  "Se estiver fraca (curta, genérica, repetitiva ou com dados inventados), chame request_rewrite com o motivo. Se estiver boa, escreva a próxima seção. "
                  "Não converse sobre arquivos nem sobre planejamento.\n") if done_secs else ""
        todo = review + todo
    return "PRODUÇÃO (prioridade máxima, o plano já está definido, não replaneje):\n" + "\n".join(lines) + "\n" + todo


def build_prompt(conn, me):
    tasks = conn.execute("SELECT id,title,details FROM tasks WHERE assignee_id=%s AND status='open' ORDER BY id LIMIT 3", (me["id"],)).fetchall()
    inbox = conn.execute("SELECT body FROM messages WHERE (to_id=%s OR to_id IS NULL) AND NOT archived ORDER BY id DESC LIMIT 3", (me["id"],)).fetchall()
    team = conn.execute("SELECT slug,role FROM employees WHERE active ORDER BY id").fetchall()
    t = "\n".join(f"[{x['id']}] {x['title']}: {x['details'][:300]}" for x in tasks) or "(nenhuma tarefa aberta)"
    m = "\n".join("- " + x["body"][:250] for x in inbox) or "(sem mensagens)"
    tm = ", ".join(f"{x['slug']} ({x['role']})" for x in team)
    prod = production_block(conn, me)
    return (f"Seu slug: {me['slug']}. Nome: {me['name']}. Cargo: {me['role']} ({me['area']}).\n"
            f"Instruções: {me['instructions'][:800]}\n\n{prod}\n\nEquipe: {tm}\n\nSuas tarefas:\n{t}\n\nMensagens recentes:\n{m}\n\n"
            "Execute agora com as tools. Se concluiu uma tarefa, chame complete_task.")


def run_turn(conn, me, chat=llm.chat_fallback):
    conn.execute("UPDATE employees SET status='working', activity=%s WHERE id=%s", ("Pensando no próximo passo", me["id"]))
    messages = [{"role": "system", "content": RULES}, {"role": "user", "content": build_prompt(conn, me)}]
    try:
        for _ in range(config.MAX_TOOL_ROUNDS):
            msg = chat(conn, me["id"], me["model"], messages, tools.TOOL_DEFS)
            calls = msg.get("tool_calls") or []
            # só os campos padrão: campos extras (reasoning etc.) podem causar 400 ao reenviar
            echo = {"role": "assistant", "content": msg.get("content") or ""}
            if calls:
                echo["tool_calls"] = calls
            messages.append(echo)
            if not calls:
                if msg.get("content"):
                    conn.execute("INSERT INTO messages(from_id,to_id,body) VALUES (%s,NULL,%s)", (me["id"], msg["content"][:1000]))
                break
            for c in calls:
                fn = c["function"]
                out = tools.run_tool(conn, me, fn["name"], tools.parse_args(fn.get("arguments")))
                log.info("%s %s -> %s", me["slug"], fn["name"], out[:120])
                conn.execute("UPDATE employees SET activity=%s WHERE id=%s", (f"Usando {fn['name']}", me["id"]))
                messages.append({"role": "tool", "tool_call_id": c["id"], "content": out})
    finally:
        conn.execute("UPDATE employees SET status='idle', activity='', last_turn_at=now() WHERE id=%s", (me["id"],))
        tools.activity(conn, me["id"], "parado", "")
