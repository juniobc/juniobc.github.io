"""Sonda de isolamento. Roda DENTRO do mesmo envoltório (unshare/bwrap) que as ferramentas usam e relata o que enxerga."""
import json
import os
import socket
import sys


def can_read(path):
    try:
        with open(path, "rb") as f:
            f.read(1)
        return True
    except OSError:
        return False


def connects(host, port):
    s = socket.socket()
    s.settimeout(2)
    try:
        s.connect((host, port))
        return True
    except OSError:
        return False
    finally:
        s.close()


cfg = json.loads(sys.stdin.read())
pids = [d for d in os.listdir("/proc") if d.isdigit()] if os.path.isdir("/proc") else []
hidden = {}
for d in cfg["hide"]:
    try:
        hidden[d] = len(os.listdir(d))
    except OSError:
        hidden[d] = 0   # sumiu: ótimo
try:
    with open("sonda_ok.txt", "w") as f:
        f.write("x")
    wrote = True
except OSError:
    wrote = False
print(json.dumps({
    "net_conectou": [connects("127.0.0.1", 8100), connects("1.1.1.1", 443)],
    "canario_legivel": can_read(cfg["canary"]) if cfg.get("canary") else False,
    "env_file_legivel": can_read(cfg["env_file"]) if cfg.get("env_file") else False,
    "ocultos_nao_vazios": {d: n for d, n in hidden.items() if n},
    "pids_visiveis": len(pids),
    "environ_alheio_legivel": sum(1 for p in pids if int(p) != os.getpid() and can_read(f"/proc/{p}/environ")),
    "env_chaves": sorted(os.environ),
    "escreveu_tmp": wrote,
}))
