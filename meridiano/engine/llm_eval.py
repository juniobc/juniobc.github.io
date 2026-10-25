"""Avaliação padrão de um perfil de LLM: 4 casos curtos medem o que a empresa realmente precisa (chamar tool, escrever 400 palavras
em português sem pedir arquivo, seguir formato, não devolver vazio). Guarda taxa de sucesso, latência e tokens por caso."""
import json
import re
import threading
import time

from . import config, db, llm

BACKGROUND = True      # testes desligam para rodar na hora
REQUIRED = ("chamar_tool", "resposta_nao_vazia")   # sem estes dois, nunca passa, mesmo com taxa alta

TOOL = [{"type": "function", "function": {"name": "registrar_tarefa", "description": "Registra uma tarefa para a equipe.",
                                          "parameters": {"type": "object", "properties": {"titulo": {"type": "string"}, "prioridade": {"type": "integer", "description": "1 (alta) a 3 (baixa)"}},
                                                         "required": ["titulo", "prioridade"]}}}]
PT_WORDS = {"de", "a", "o", "que", "e", "do", "da", "em", "um", "para", "com", "não", "uma", "os", "no", "se", "na", "por", "mais", "as", "dos", "como", "mas", "ao", "ele", "das", "à", "seu", "sua", "ou", "ser", "quando", "muito"}
ASK_FILE = re.compile(r"(envie|anexe|me\s+(mande|envie|passe)|forne[çc]a)[^.\n]{0,40}(arquivo|anexo)", re.I)


def _chk_tool(msg):
    calls = msg.get("tool_calls") or []
    for c in calls:
        fn = c.get("function") or {}
        if fn.get("name") != "registrar_tarefa":
            continue
        try:
            a = json.loads(fn.get("arguments") or "{}")
        except ValueError:
            return False, "argumentos não são JSON válido"
        if isinstance(a.get("titulo"), str) and a["titulo"].strip() and isinstance(a.get("prioridade"), int) and not isinstance(a["prioridade"], bool) and 1 <= a["prioridade"] <= 3:
            return True, "tool chamada com argumentos válidos"
        return False, "argumentos fora do esquema"
    return False, "não chamou a tool" + (" (respondeu em texto)" if msg.get("content") else "")


def _chk_400(msg):
    text = msg.get("content") or ""
    words = re.findall(r"\w+", text)
    if len(words) < 400:
        return False, f"só {len(words)} palavras (mínimo 400)"
    pt = sum(1 for w in words if w.lower() in PT_WORDS) / len(words)
    if pt < 0.12:
        return False, "texto não parece estar em português"
    if ASK_FILE.search(text):
        return False, "pediu arquivo/anexo em vez de escrever"
    return True, f"{len(words)} palavras em português"


def _chk_format(msg):
    text = (msg.get("content") or "").strip()
    try:
        d = json.loads(text)
    except ValueError:
        return False, "não respondeu SOMENTE com JSON" + (" (usou cerca de código)" if text.startswith("```") else "")
    if isinstance(d, dict) and set(d) == {"cidade", "pontos"} and isinstance(d["cidade"], str) and isinstance(d["pontos"], list) and len(d["pontos"]) == 3 and all(isinstance(x, str) and x.strip() for x in d["pontos"]):
        return True, "JSON exato"
    return False, "JSON fora do formato pedido"


def _chk_nonempty(msg):
    return (True, "respondeu") if (msg.get("content") or "").strip() else (False, "resposta vazia")


CASES = (
    ("chamar_tool", [{"role": "user", "content": "Use a ferramenta registrar_tarefa para registrar a tarefa 'Revisar o texto de vendas' com prioridade 2. Chame a ferramenta; não responda em texto."}], TOOL, _chk_tool),
    ("escrever_400_palavras", [{"role": "user", "content": "Escreva, em português do Brasil, um texto de no mínimo 450 palavras sobre como organizar o orçamento pessoal de um mês. "
                                                         "Escreva o texto completo AQUI na resposta. Não peça arquivo, anexo nem mais informações."}], None, _chk_400),
    ("seguir_formato", [{"role": "user", "content": 'Responda SOMENTE com um JSON válido, sem nenhum outro texto e sem cercas de código, exatamente neste formato: {"cidade": "<uma cidade brasileira>", "pontos": ["<ponto 1>", "<ponto 2>", "<ponto 3>"]}.'}], None, _chk_format),
    ("resposta_nao_vazia", [{"role": "user", "content": "Responda com uma única palavra: ok"}], None, _chk_nonempty),
)


def stale_cleanup(conn):
    conn.execute("UPDATE llm_evals SET status='erro', finished_at=now(), note='interrompida (sem resposta em 15 min)' WHERE status='rodando' AND started_at < now() - interval '15 minutes'")


def start(conn, profile, call=llm.call):
    """Cria a avaliação e a executa (em segundo plano, a menos que BACKGROUND seja falso). Devolve (eval_id, erro)."""
    stale_cleanup(conn)
    ok, why = llm.eval_ready(conn, profile)
    if not ok:
        return None, why
    if conn.execute("SELECT 1 FROM llm_evals WHERE profile_id=%s AND status='rodando'", (profile["id"],)).fetchone():
        return None, "este perfil já está em avaliação"
    if conn.execute("SELECT COUNT(*) AS n FROM llm_evals WHERE status='rodando'").fetchone()["n"] >= config.EVAL_MAX_RUNNING:
        return None, "há avaliações demais em andamento; aguarde"
    eid = conn.execute("INSERT INTO llm_evals(profile_id,total) VALUES (%s,%s) RETURNING id", (profile["id"], len(CASES))).fetchone()["id"]
    if BACKGROUND:
        threading.Thread(target=_background, args=(eid, profile["id"], call), daemon=True, name=f"avaliacao-{eid}").start()
    else:
        run(conn, profile, eid, call)
    return eid, None


def _background(eid, pid, call):
    c = db.connect()
    try:
        p = c.execute("SELECT * FROM llm_profiles WHERE id=%s", (pid,)).fetchone()
        run(c, p, eid, call)
    except Exception as e:
        c.execute("UPDATE llm_evals SET status='erro', finished_at=now(), note=%s WHERE id=%s", (f"erro inesperado: {type(e).__name__}", eid))
    finally:
        c.close()


def run(conn, profile, eid, call=llm.call):
    passed, lat, toks, cost = 0, [], 0, 0.0
    for name, messages, tools, check in CASES:
        t0 = time.time()
        pt = ct = 0
        try:
            msg = call(conn, None, profile, messages, tools)
            pt, ct = msg.get("_usage", (0, 0))
            ok, detail = check(msg)
        except llm.RateLimited:
            ok, detail = False, "provedor limitou o uso (429); tente de novo mais tarde"
        except llm.EmptyResponse as e:
            pt, ct = e.usage
            ok, detail = False, "resposta vazia (sem texto nem tool)"
        except Exception as e:
            ok, detail = False, ("erro do provedor: " + str(e))[:160]
        ms = int((time.time() - t0) * 1000)
        passed += 1 if ok else 0
        lat.append(ms)
        toks += pt + ct
        cost += llm.cost_usd(profile, pt, ct)
        conn.execute("INSERT INTO llm_eval_cases(eval_id,case_name,ok,latency_ms,prompt_tokens,completion_tokens,detail) VALUES (%s,%s,%s,%s,%s,%s,%s)", (eid, name, ok, ms, pt, ct, detail[:200]))
    conn.execute("UPDATE llm_evals SET status='concluida', finished_at=now(), passed=%s, total=%s, avg_latency_ms=%s, tokens=%s, cost_usd=%s WHERE id=%s",
                 (passed, len(CASES), int(sum(lat) / len(lat)) if lat else 0, toks, round(cost, 6), eid))


def latest(conn, profile_id):
    return conn.execute("SELECT * FROM llm_evals WHERE profile_id=%s ORDER BY id DESC LIMIT 1", (profile_id,)).fetchone()


def passes(conn, profile_id):
    """(ok, motivo): a avaliação concluída mais recente atinge o mínimo e acerta os casos obrigatórios?"""
    ev = conn.execute("SELECT * FROM llm_evals WHERE profile_id=%s AND status='concluida' ORDER BY id DESC LIMIT 1", (profile_id,)).fetchone()
    if not ev:
        return False, "avalie o modelo antes de ligar (nenhuma avaliação concluída)"
    rate = ev["passed"] / ev["total"] if ev["total"] else 0
    cases = {c["case_name"]: c["ok"] for c in conn.execute("SELECT case_name, ok FROM llm_eval_cases WHERE eval_id=%s", (ev["id"],)).fetchall()}
    miss = [n for n in REQUIRED if not cases.get(n)]
    if miss:
        return False, "não passou em casos obrigatórios: " + ", ".join(miss)
    if rate < config.EVAL_MIN_RATE:
        return False, f"taxa de sucesso {ev['passed']}/{ev['total']} abaixo do mínimo ({int(config.EVAL_MIN_RATE * 100)}%)"
    return True, ""


def table(conn):
    """Comparativo: última avaliação de cada perfil."""
    out = []
    for p in conn.execute("SELECT id,label,model,is_free,enabled,needs_eval FROM llm_profiles ORDER BY is_free DESC, id").fetchall():
        ev = latest(conn, p["id"])
        row = {"perfil_id": p["id"], "label": p["label"], "modelo": p["model"], "gratis": p["is_free"], "ligado": p["enabled"], "exige_avaliacao": p["needs_eval"], "avaliacao": None}
        if ev:
            cases = [{"caso": c["case_name"], "ok": c["ok"], "latencia_ms": c["latency_ms"], "tokens": c["prompt_tokens"] + c["completion_tokens"], "detalhe": c["detail"]}
                     for c in conn.execute("SELECT * FROM llm_eval_cases WHERE eval_id=%s ORDER BY id", (ev["id"],)).fetchall()]
            ok, why = passes(conn, p["id"]) if ev["status"] == "concluida" else (False, "avaliação em andamento" if ev["status"] == "rodando" else (ev["note"] or "avaliação falhou"))
            row["avaliacao"] = {"id": ev["id"], "status": ev["status"], "passou": ev["passed"], "total": ev["total"], "taxa": round(ev["passed"] / ev["total"], 2) if ev["total"] else 0,
                                "latencia_media_ms": ev["avg_latency_ms"], "tokens": ev["tokens"], "custo_usd": float(ev["cost_usd"]), "em": (ev["finished_at"] or ev["started_at"]).isoformat(),
                                "aprovado": ok, "motivo": why, "casos": cases}
        out.append(row)
    return out
