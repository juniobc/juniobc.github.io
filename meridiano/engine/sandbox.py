"""Lado do pai: valida e executa código de agente em subprocesso isolado (ver sandbox_runner.py)."""
import json
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
import time
from . import config
from .sandbox_runner import validate  # noqa: F401  (reexportado)

RUNNER = str(pathlib.Path(__file__).with_name("sandbox_runner.py"))
PROBE = str(pathlib.Path(__file__).with_name("sandbox_probe.py"))
MAX_OUT = 20000
OFF_MSG = "ferramentas desligadas: isolamento indisponível"
HARMLESS_ENV = {"PWD", "OLDPWD", "SHLVL", "_", "LC_CTYPE"}   # o que o /bin/sh acrescenta sozinho
REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]

# Estado do portão. Começa DESLIGADO: só liga depois de refresh_gate() com todos os testes passando.
_GATE = {"enabled": False, "reason": "isolamento ainda não verificado", "backend": "", "help": "", "detail": "", "checked_at": None}


def gate():
    return dict(_GATE)


def enabled():
    return bool(_GATE["enabled"])


def _tmp_base():
    return "/tmp" if os.path.isdir("/tmp") else tempfile.gettempdir()


def _python():
    return config.TOOL_PYTHON or os.path.realpath(sys.executable)


def hide_dirs():
    """Diretórios que o subprocesso não pode ver: home do usuário, pasta da empresa e o repositório."""
    cands = {os.path.expanduser("~"), str(REPO_ROOT)}
    if config.env("MERIDIANO_HOME"):
        cands.add(config.env("MERIDIANO_HOME"))
    cands = {os.path.realpath(c) for c in cands if c and os.path.isdir(c) and os.path.realpath(c) not in ("/", "/usr", "/tmp")}
    return sorted(c for c in cands if not any(o != c and c.startswith(o.rstrip("/") + "/") for o in cands))


def _backends():
    out = []
    if shutil.which("bwrap"):
        out.append("bwrap")
    if shutil.which("unshare") and shutil.which("mount") and shutil.which("sh"):
        out.append("unshare")
    return out


def build_cmd(backend, tmp, script, hide):
    """Linha de comando que executa tmp/script isolado. O interpretador é o do sistema (o do venv fica escondido)."""
    py = _python()
    if backend == "bwrap":
        cmd = [shutil.which("bwrap"), "--unshare-user", "--unshare-net", "--unshare-pid", "--unshare-ipc", "--unshare-uts",
               "--die-with-parent", "--new-session", "--ro-bind", "/usr", "/usr"]
        for p in ("/lib", "/lib64", "/bin", "/sbin"):
            cmd += ["--ro-bind-try", p, p]
        return cmd + ["--proc", "/proc", "--dev", "/dev", "--bind", tmp, "/work", "--chdir", "/work", py, "-I", "/work/" + script]
    sh_script = ('py="$1"; mt="$2"; tmp="$3"; script="$4"; shift 4; '
                 'for d in "$@"; do "$mt" -t tmpfs tmpfs "$d" || exit 97; done; '
                 'cd "$tmp" && exec "$py" -I "$tmp/$script"')
    return [shutil.which("unshare"), "-r", "-m", "-n", "-p", "-f", "--mount-proc", shutil.which("sh"), "-c", sh_script,
            "sh", py, shutil.which("mount"), tmp, script] + list(hide)


def _spawn(backend, script_src, script_name, stdin, timeout):
    """Copia o script para um tmp novo e roda no envoltório. Devolve (CompletedProcess|None, motivo_de_erro)."""
    tmp = tempfile.mkdtemp(prefix="meridiano_tool_", dir=_tmp_base())
    try:
        shutil.copy(script_src, os.path.join(tmp, script_name))
        cmd = build_cmd(backend, tmp, script_name, hide_dirs())
        try:
            return subprocess.run(cmd, input=stdin.encode(), capture_output=True, timeout=timeout, cwd=tmp, env={}), None
        except subprocess.TimeoutExpired:
            return None, f"tempo esgotado ({timeout}s)"
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _help_for(stderr, code):
    low = stderr.lower()
    if code == 97:
        return ("mount negado dentro do namespace", "o administrador precisa liberar user namespaces com mount (instalar bubblewrap, "
                "ou kernel.apparmor_restrict_unprivileged_userns=0, ou criar um usuário Linux separado, sem login, para as ferramentas)")
    if "operation not permitted" in low or "permission denied" in low or "unshare failed" in low or "cannot" in low or "setgroups" in low or "uid_map" in low:
        return ("unshare/namespace negado: " + stderr.strip()[:160], "o administrador precisa liberar user namespaces para o usuário (kernel.unprivileged_userns_clone=1 e "
                "kernel.apparmor_restrict_unprivileged_userns=0), ou instalar bubblewrap, ou criar um usuário Linux separado, sem login, para as ferramentas")
    return (f"o envoltório falhou (código {code}): {stderr.strip()[:160]}", "verificar o interpretador do sistema (python3 fora de /home) e util-linux ou bubblewrap")


def _run_probe(backend, canary, env_file, timeout=20):
    p, err = _spawn(backend, PROBE, "sonda.py", json.dumps({"hide": hide_dirs(), "canary": canary, "env_file": env_file}), timeout)
    if err:
        return None, (err, "o autoteste demorou demais")
    if p.returncode != 0:
        return None, _help_for(p.stderr.decode("utf-8", "replace"), p.returncode)
    try:
        return json.loads(p.stdout.decode("utf-8", "replace")), None
    except ValueError:
        return None, ("a sonda não respondeu", "verificar o interpretador do sistema")


def _judge(r):
    """Lista de falhas a partir do relato da sonda. Lista vazia = isolamento aprovado."""
    fails = []
    if any(r["net_conectou"]):
        fails.append("rede NÃO bloqueada (conectou a 127.0.0.1:8100 ou 1.1.1.1:443)")
    if r["canario_legivel"] or r["env_file_legivel"]:
        fails.append("arquivos do usuário (meridiano.env/repositório) legíveis de dentro do isolamento")
    if r["ocultos_nao_vazios"]:
        fails.append("diretórios que deveriam estar escondidos têm conteúdo: " + ", ".join(sorted(r["ocultos_nao_vazios"])))
    if r["environ_alheio_legivel"]:
        fails.append("o ambiente de outros processos é legível (/proc)")
    extra = [k for k in r["env_chaves"] if k not in HARMLESS_ENV]
    if extra:
        fails.append("variáveis de ambiente presentes: " + ", ".join(extra))
    if not r["escreveu_tmp"]:
        fails.append("não consegue escrever no diretório temporário")
    return fails


def refresh_gate():
    """Roda o autoteste de isolamento e liga ou desliga as ferramentas. Qualquer dúvida desliga."""
    import secrets
    backends = _backends()
    reasons, helps = [], []
    canary = os.path.join(os.path.expanduser("~"), ".meridiano_isolation_canary")
    try:
        with open(canary, "w") as f:
            f.write(secrets.token_hex(8))
        os.chmod(canary, 0o600)
    except OSError:
        canary = None
    env_file = os.path.join(config.env("MERIDIANO_HOME") or os.path.join(os.path.expanduser("~"), "meridiano"), "meridiano.env")
    chosen = None
    if not backends:
        reasons.append("nem unshare nem bubblewrap encontrados")
        helps.append("instalar util-linux (unshare) ou bubblewrap")
    for b in backends:
        res, err = _run_probe(b, canary, env_file)
        if err:
            reasons.append(f"{b}: {err[0]}")
            helps.append(err[1])
            continue
        fails = _judge(res)
        if fails:
            reasons.append(f"{b}: " + "; ".join(fails))
            helps.append("ajustar o isolamento (bubblewrap ou usuário Linux separado, sem login)")
            continue
        chosen = b
        break
    if canary:
        try:
            os.remove(canary)
        except OSError:
            pass
    import datetime
    _GATE.update(checked_at=datetime.datetime.now(datetime.timezone.utc).isoformat())
    if chosen:
        _GATE.update(enabled=True, backend=chosen, reason=f"isolamento verificado ({chosen}: sem rede, home escondido, processos isolados)", help="", detail="")
    else:
        _GATE.update(enabled=False, backend="", reason="; ".join(reasons) or "isolamento indisponível",
                     help="; ".join(dict.fromkeys(h for h in helps if h)), detail="")
    return gate()


def execute(code, args=None, tests=None, timeout=None):
    """Roda `code` (e, se vier, `tests`) isolado. Devolve {"ok", "result"|"error", "stdout", "ms"}. Desligado = recusa."""
    if not enabled():
        return {"ok": False, "error": OFF_MSG, "disabled": True, "ms": 0}
    timeout = timeout or config.TOOL_TIMEOUT_SECONDS
    payload = json.dumps({"code": code, "args": args or {}, "tests": tests})
    t0 = time.time()
    p, err = _spawn(_GATE["backend"], RUNNER, "runner.py", payload, timeout)
    if err:
        res = {"ok": False, "error": err}
    else:
        raw = p.stdout[:100000].decode("utf-8", "replace")
        try:
            res = json.loads(raw)
        except ValueError:
            sig = f" (sinal {-p.returncode})" if p.returncode < 0 else ""
            res = {"ok": False, "error": f"o processo terminou sem resposta{sig}; tempo ou memória esgotados"}
    res["ms"] = int((time.time() - t0) * 1000)
    if res.get("ok"):
        shown = json.dumps(res.get("result"), ensure_ascii=False)
        if len(shown) > MAX_OUT:
            res = {"ok": False, "error": f"saída maior que {MAX_OUT} caracteres", "ms": res["ms"]}
    if "stdout" in res:
        res["stdout"] = res["stdout"][:MAX_OUT]
    return res
