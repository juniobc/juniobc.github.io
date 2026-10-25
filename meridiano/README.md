# Meridiano Digital

Empresa de agentes de IA. Python (motor + API FastAPI), PostgreSQL, React (telas, já compiladas em `web/dist`).

## Regras que o código garante
- **Caixa começa em R$ 0,00** e só muda por `POST /api/ledger` com o token de admin (dono ou integração de pagamento). Agentes não têm nenhuma tool de dinheiro (há teste para isso).
- Só os 8 modelos grátis de `engine/config.py`; qualquer outro levanta erro antes da chamada.
- Limite diário de chamadas e tokens (`DAILY_CALL_LIMIT`, `DAILY_TOKEN_LIMIT`), registrado em `usage_log`.
- Sem tool de e-mail/WhatsApp/envio externo: nada de spam (LGPD).
- Segredos só em `~/meridiano/meridiano.env` (600).

## Instalar e atualizar
Veja `deploy/SETUP.md`. Deploy automático da branch `main` a cada minuto, com migrações (`sql/migrations`), supervisor e rollback. O app escuta em `127.0.0.1:8100`.

## Registrar uma venda real (só o Junio)
`curl -X POST .../api/ledger -H "X-Admin-Token: ..." -H 'Content-Type: application/json' -d '{"kind":"receita","amount_cents":4990,"description":"Venda Hotmart #123","external_ref":"HP123"}'`

## Pendências
- `stepfun/step-3.7-flash:free` (erro 400 "missing tags") está fora do rodízio até ser investigado.
- Webhook de pagamento (Hotmart/Kiwify) ainda não existe: depende de conta de vendedor do Junio.
- Testes: `TEST_DATABASE_URL=... pytest meridiano/tests` (ou sem root, sobe Postgres temporário).


## Fábrica de ferramentas (agentes escrevem código)
Os agentes **não têm acesso ao servidor**. Eles propõem ferramentas em Python puro com `propose_tool`; o código passa por:
1. validação por AST (imports permitidos: json, re, math, datetime, statistics, csv, textwrap, itertools, collections, decimal, hashlib, base64, unicodedata, string, html, urllib.parse; sem `_`/dunder, `open`, `eval`, `getattr`, `.format`, etc.);
2. os testes que o próprio agente enviou, rodando isolados;
3. revisão de outro chefe (Presidente ou CEO, nunca o autor) com `read_tool` + `approve_tool`.
Execução: subprocesso separado (`python -I`), ambiente vazio, diretório temporário, timeout de 10 s, limites de CPU, memória, arquivos e processos, saída de até 20 KB, builtins e imports restritos; sem rede quando `unshare -rn` funciona no servidor (`/api/engine` mostra o que valeu). Toda execução vai para `tool_runs`.

**Limites honestos:** sandbox em Python não é uma barreira absoluta. As camadas se somam, mas a rede só é realmente bloqueada se o `unshare` existir; o processo roda como o mesmo usuário do servidor. Por isso o desenho não dá rede, arquivos nem helpers aos agentes. Ferramentas que citam `http_get`/`read_file`/`write_file` (nível C) ficam **pendentes do dono** e esses helpers ainda não existem.
Ainda não feito: helpers de rede/arquivo com lista de domínios, pasta pública `/pub`, aba "Ferramentas" no painel e botão Aprovar/Recusar do dono.
