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
MAX_OUT = 20000
_net_isolation = None


def net_isolation():
    """True se `unshare -rn` funciona neste servidor (sem rede dentro do subprocesso). Resultado em cache."""
    global _net_isolation
    if _net_isolation is None:
        exe = shutil.which("unshare")
        _net_isolation = False
        if exe:
            try:
                r = subprocess.run([exe, "-r", "-n", sys.executable, "-I", "-c", "print(1)"], capture_output=True, timeout=10, env={})
                _net_isolation = r.returncode == 0 and r.stdout.strip() == b"1"
            except Exception:
                _net_isolation = False
    return _net_isolation


def isolation_report():
    return ("subprocesso isolado (python -I, ambiente vazio, limites de CPU/memória/arquivos/processos), "
            + ("rede BLOQUEADA via unshare" if net_isolation() else "rede NÃO bloqueada (unshare indisponível neste servidor)"))


def execute(code, args=None, tests=None, timeout=None):
    """Roda `code` (e, se vier, `tests`) isolado. Devolve {"ok", "result"|"error", "stdout", "ms"}."""
    timeout = timeout or config.TOOL_TIMEOUT_SECONDS
    payload = json.dumps({"code": code, "args": args or {}, "tests": tests})
    cmd = [sys.executable, "-I", RUNNER]
    if net_isolation():
        cmd = [shutil.which("unshare"), "-r", "-n"] + cmd
    t0 = time.time()
    tmp = tempfile.mkdtemp(prefix="meridiano_tool_")
    try:
        p = subprocess.run(cmd, input=payload.encode(), capture_output=True, timeout=timeout, cwd=tmp, env={})
        raw = p.stdout[:100000].decode("utf-8", "replace")
        try:
            res = json.loads(raw)
        except ValueError:
            sig = f" (sinal {-p.returncode})" if p.returncode < 0 else ""
            res = {"ok": False, "error": f"o processo terminou sem resposta{sig}; tempo ou memória esgotados"}
    except subprocess.TimeoutExpired:
        res = {"ok": False, "error": f"tempo esgotado ({timeout}s)"}
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    res["ms"] = int((time.time() - t0) * 1000)
    if res.get("ok"):
        shown = json.dumps(res.get("result"), ensure_ascii=False)
        if len(shown) > MAX_OUT:
            res = {"ok": False, "error": f"saída maior que {MAX_OUT} caracteres", "ms": res["ms"]}
    if "stdout" in res:
        res["stdout"] = res["stdout"][:MAX_OUT]
    return res
