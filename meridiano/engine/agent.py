import json
import logging
from . import config, llm, tools

log = logging.getLogger("meridiano")

RULES = """Você é um funcionário da Meridiano Digital, empresa de agentes de IA que começou com R$ 0 e quer ganhar dinheiro real de forma legal.
Regras inegociáveis:
- Você NÃO movimenta dinheiro e NÃO registra vendas. Receita só existe quando o dono registra um pagamento real. Nunca afirme que vendeu algo.
- Nada de spam nem mensagens em massa para estranhos (LGPD). Clientes vêm de conteúdo, busca e marketplaces.
- Você só fala com colegas internos, usando as tools. Seja objetivo e use as tools para agir.
- Contrate só quando houver uma função faltando, com motivo claro. O dono (Junio) decide contas, CPF e saques."""


def build_prompt(conn, me):
    tasks = conn.execute("SELECT id,title,details FROM tasks WHERE assignee_id=%s AND status='open' ORDER BY id LIMIT 5", (me["id"],)).fetchall()
    inbox = conn.execute("SELECT body FROM messages WHERE to_id=%s OR to_id IS NULL ORDER BY id DESC LIMIT 5", (me["id"],)).fetchall()
    team = conn.execute("SELECT slug,name,role FROM employees WHERE active ORDER BY id").fetchall()
    t = "\n".join(f"[{x['id']}] {x['title']}: {x['details']}" for x in tasks) or "(nenhuma tarefa aberta)"
    m = "\n".join("- " + x["body"] for x in inbox) or "(sem mensagens)"
    tm = "\n".join(f"{x['slug']}: {x['name']} - {x['role']}" for x in team)
    return (f"Seu slug: {me['slug']}. Seu nome: {me['name']}. Cargo: {me['role']} ({me['area']}).\n"
            f"Suas instruções: {me['instructions']}\n\nEquipe:\n{tm}\n\nSuas tarefas:\n{t}\n\nMensagens recentes:\n{m}\n\n"
            "Decida o próximo passo útil e execute com as tools. Se não há nada a fazer, crie tarefas para a equipe ou pesquise uma oportunidade.")


def run_turn(conn, me, chat=llm.chat_fallback):
    conn.execute("UPDATE employees SET status='working', activity=%s WHERE id=%s", ("Pensando no próximo passo", me["id"]))
    messages = [{"role": "system", "content": RULES}, {"role": "user", "content": build_prompt(conn, me)}]
    try:
        for _ in range(config.MAX_TOOL_ROUNDS):
            msg = chat(conn, me["id"], me["model"], messages, tools.TOOL_DEFS)
            calls = msg.get("tool_calls") or []
            messages.append({k: v for k, v in msg.items() if v is not None})
            if not calls:
                if msg.get("content"):
                    conn.execute("INSERT INTO messages(from_id,to_id,body) VALUES (%s,NULL,%s)", (me["id"], msg["content"][:1000]))
                break
            for c in calls:
                fn = c["function"]
                out = tools.run_tool(conn, me, fn["name"], tools.parse_args(fn.get("arguments")))
                log.info("%s %s -> %s", me["slug"], fn["name"], out[:120])
                conn.execute("UPDATE employees SET activity=%s WHERE id=%s", (f"Usando {fn['name']}", me["id"]))
                messages.append({"role": "tool", "tool_call_id": c["id"], "content": out[:3000]})
    except llm.EmptyResponse:
        raise
    finally:
        conn.execute("UPDATE employees SET status='idle', activity='' WHERE id=%s", (me["id"],))
