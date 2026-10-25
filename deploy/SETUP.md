# Configuração única (Spark, como usuário `empresa`)

Depois disto a empresa se atualiza sozinha: **o Junio mescla um PR na `main` e em até 1 minuto está no ar**, com rollback automático se der erro. O Spark não é mais necessário no dia a dia.

## 1. Desligar o deploy antigo (uma vez)
O deploy antigo seguia a branch `claude/play-game-czq8sk` e a API antiga ocupa a porta 8100.
- Pare e desabilite o serviço da API antiga e o cron/timer de `git fetch` que o Spark criou.
- Confirme que a porta 8100 está livre: `ss -ltn | grep 8100` (não deve listar nada).

## 2. Instalar (uma vez, só depois que o PR #1 for mesclado na `main`)
```
mkdir -p ~/meridiano
git clone -b main https://github.com/juniobc/juniobc.github.io.git ~/meridiano/repo
chmod +x ~/meridiano/repo/deploy/*.sh
(crontab -l 2>/dev/null; cat <<'CRON'
@reboot   ~/meridiano/repo/deploy/deploy.sh
* * * * * ~/meridiano/repo/deploy/deploy.sh
CRON
) | crontab -
~/meridiano/repo/deploy/deploy.sh
```
O `meridiano.env` e o `venv` que já existem em `~/meridiano` são reaproveitados. Se não existirem, o `deploy.sh` cria (token de leitura e de admin sorteados).

## 3. O que o `deploy.sh` faz sozinho
- Só olha a branch `main`. Nenhuma outra branch vira deploy.
- Se há commit novo: `git checkout`, `pip install -r requirements.txt` no venv, migrações SQL (`schema_migrations`), reinicia API e motor.
- Se `/api/health` não responder em 30 s, volta ao commit anterior e marca o ruim em `run/bad_commit` para não tentar de novo até a `main` mudar.
- Trava com `flock`, log em `~/meridiano/deploy.log` (o Claude lê pela rota `/api/deploy-log`).
- O `supervisor.sh` mantém API (127.0.0.1:8100) e motor vivos e os religa se caírem. O motor só inicia quando `NOUS_API_KEY` está preenchida no `meridiano.env`.

## 4. Para ligar o motor
O Junio entrega a chave nova da Nous; ela vai só em `~/meridiano/meridiano.env` (`NOUS_API_KEY=...`, permissão 600). O supervisor religa o motor em até 1 minuto.

## O que ainda depende de root (Spark)
Só o que está fora do usuário `empresa`: Caddy/HTTPS (`meridiano.163-176-9-70.sslip.io` → `127.0.0.1:8100`), firewall da Oracle e portas. Já está feito.


## Pagamentos e loja: dois caminhos para entregar o token (um só basta)
O Junio precisa entregar **uma vez** o Access Token do Mercado Pago (Credenciais de produção) e a identificação do vendedor (nome e e-mail de contato, exigência legal).

**(a) Painel.** O cartão "Pedidos para você" tem os campos. Exige o `MERIDIANO_ADMIN_TOKEN` (está no `meridiano.env`).

**(b) Arquivos no servidor**, como o usuário `empresa`, sem reiniciar nada:
```
mkdir -p ~/meridiano/secrets && chmod 700 ~/meridiano/secrets
printf '%s\n' 'APP_USR-...o-token...' > ~/meridiano/secrets/mp-token && chmod 600 ~/meridiano/secrets/mp-token
printf '%s\n' '{"seller_name": "Nome Completo", "contact_email": "contato@exemplo.com.br"}' > ~/meridiano/secrets/store.json && chmod 600 ~/meridiano/secrets/store.json
```
- O arquivo `mp-token` aceita o token puro ou `MP_ACCESS_TOKEN=...`, com ou sem aspas. Permissões frouxas são corrigidas sozinhas (pasta 700, arquivos 600).
- O motor lê os arquivos e reverifica o token em `api.mercadopago.com/users/me` **a cada 60 s**; a API também reverifica ao consultar o status. O pedido ao dono fecha sozinho quando o token e a identificação existem.
- `GET /api/secrets/mp-token` (token de leitura) mostra `configurado`, `modo`, `verificado`, `verificado_em`, `detalhe` e `problema`. **Nunca** mostra o token.
- Nenhum agente nem ferramenta lê essa pasta: ela fica sob a home, que o isolamento das ferramentas esconde.
