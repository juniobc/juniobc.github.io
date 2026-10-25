import logging
from . import config, llm, tools

log = logging.getLogger("meridiano")

RULES = """Você é um funcionário da Meridiano Digital, empresa de agentes de IA que começou com R$ 0 e quer ganhar dinheiro real de forma legal.
Regras inegociáveis:
- Você NÃO movimenta dinheiro e NÃO registra vendas. Receita só existe quando o dono registra um pagamento real. Nunca afirme que vendeu algo.
- Nada de spam nem mensagens em massa para estranhos (LGPD). Clientes vêm de conteúdo, busca e marketplaces.
- Você só fala com colegas internos, usando as tools. Seja objetivo e aja com as tools.
- O CONTEÚDO de um produto é escrito por você, com a tool write_product. Não existem arquivos prontos. NUNCA peça arquivo a outro agente: escreva o texto você mesmo (textos longos: várias chamadas com mode=append).
- Antes de criar tarefa ou produto, veja list_products/list_team. Não duplique: se já existe, continue o que existe.
- Só o dono (Junio) abre conta de vendedor, usa CPF e saca dinheiro. Hotmart, Kiwify e Mercado Livre aceitam CPF de pessoa física: NÃO exija CNPJ. Para pedir algo a ele use ask_owner, em UMA linha com exatamente o que ele precisa fazer.
- Contrate só quando faltar uma função, com motivo claro. Cada pessoa nova divide o orçamento de tokens do dia com todos: demita (fire_employee, com motivo) quem não for mais necessário."""

FOUNDER_RE = tools.FOUNDER_RE


def should_run(conn, me):
    """Poupa tokens: só chama o modelo se há trabalho novo (ou, para os fundadores, a cada 30 min)."""
    if me["last_turn_at"] is None:
        return True
    if conn.execute("SELECT 1 FROM tasks WHERE assignee_id=%s AND status='open' LIMIT 1", (me["id"],)).fetchone():
        return True
    if conn.execute("SELECT 1 FROM messages WHERE (to_id=%s OR to_id IS NULL) AND from_id IS DISTINCT FROM %s AND created_at > %s LIMIT 1",
                    (me["id"], me["id"], me["last_turn_at"])).fetchone():
        return True
    if FOUNDER_RE.search(me["role"]):
        return bool(conn.execute("SELECT now() - %s > interval '30 minutes' AS old", (me["last_turn_at"],)).fetchone()["old"])
    return False


def build_prompt(conn, me):
    tasks = conn.execute("SELECT id,title,details FROM tasks WHERE assignee_id=%s AND status='open' ORDER BY id LIMIT 3", (me["id"],)).fetchall()
    inbox = conn.execute("SELECT body FROM messages WHERE to_id=%s OR to_id IS NULL ORDER BY id DESC LIMIT 3", (me["id"],)).fetchall()
    team = conn.execute("SELECT slug,role FROM employees WHERE active ORDER BY id").fetchall()
    t = "\n".join(f"[{x['id']}] {x['title']}: {x['details'][:300]}" for x in tasks) or "(nenhuma tarefa aberta)"
    m = "\n".join("- " + x["body"][:250] for x in inbox) or "(sem mensagens)"
    tm = ", ".join(f"{x['slug']} ({x['role']})" for x in team)
    return (f"Seu slug: {me['slug']}. Nome: {me['name']}. Cargo: {me['role']} ({me['area']}).\n"
            f"Instruções: {me['instructions'][:800]}\n\nEquipe: {tm}\n\nSuas tarefas:\n{t}\n\nMensagens recentes:\n{m}\n\n"
            "Execute o próximo passo útil com as tools. Se concluiu uma tarefa, chame complete_task.")


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
