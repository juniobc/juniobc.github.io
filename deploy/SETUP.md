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
