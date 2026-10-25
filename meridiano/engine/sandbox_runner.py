"""Executa código de ferramenta de agente. Roda como `python -I sandbox_runner.py` em subprocesso, com ambiente vazio.
O pai também importa `validate` daqui. Arquivo único de propósito: no modo -I o diretório do script não entra no sys.path.

AVISO HONESTO: sandbox em Python NÃO é uma barreira de segurança por si só. As camadas são: (1) validação por AST,
(2) builtins e imports restritos, (3) processo separado, ambiente vazio, limites de CPU/memória/arquivos/processos,
(4) sem rede quando `unshare -rn` funciona no servidor. Se (4) não existir, a rede NÃO é bloqueada (ver /api/engine)."""
import ast
import json
import sys

ALLOWED_MODULES = {
    "json", "re", "math", "datetime", "statistics", "csv", "textwrap", "itertools", "collections",
    "decimal", "hashlib", "base64", "unicodedata", "string", "html", "urllib.parse",
}
# `string.Formatter` devolve objetos por nome de atributo (get_field) e furaria a proibição de "_": fica de fora.
MODULE_ATTR_DENY = {"string": {"Formatter"}}
BANNED_NAMES = {
    "eval", "exec", "compile", "open", "input", "breakpoint", "getattr", "setattr", "delattr", "vars", "globals",
    "locals", "dir", "type", "memoryview", "help", "exit", "quit", "os", "sys", "subprocess", "socket", "ctypes",
    "importlib", "builtins", "super", "classmethod", "staticmethod", "property",
}
BANNED_ATTRS = {
    "gi_frame", "gi_code", "gi_yieldfrom", "f_globals", "f_locals", "f_back", "f_builtins", "f_code", "tb_frame", "tb_next",
    "cr_frame", "cr_code", "cr_await", "ag_frame", "ag_code", "func_globals", "mro",
    # str.format/format_map navegam atributos a partir de TEXTO em tempo de execução (ex.: "{0.__globals__}"), furando a
    # proibição de "_" da AST. f-strings continuam valendo: a expressão dentro delas passa pela AST.
    "format", "format_map",
}
BANNED_STRINGS = ("/proc", "environ", "/etc", "/home", "/root", "/sys", "/dev", ".env", "..")
HELPER_NAMES = {"http_get", "read_file", "write_file"}   # helpers de rede/arquivo ainda não existem: nível C
MAX_CODE = 30000


def validate(code, need_run=True):
    """(ok, motivos, usa_helpers). Rejeita antes de executar qualquer coisa."""
    problems = []
    if len(code) > MAX_CODE:
        return False, [f"código maior que {MAX_CODE} caracteres"], False
    try:
        tree = ast.parse(code)
    except SyntaxError as e:
        return False, [f"erro de sintaxe: {e.msg} (linha {e.lineno})"], False
    helpers = False
    has_run = False
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                if a.name not in ALLOWED_MODULES:
                    problems.append(f"import proibido: {a.name}")
        elif isinstance(node, ast.ImportFrom):
            if node.level or (node.module or "") not in ALLOWED_MODULES:
                problems.append(f"import proibido: {node.module}")
            for a in node.names:
                if a.name.startswith("_") or a.name == "*":
                    problems.append(f"import proibido: {a.name}")
        elif isinstance(node, ast.Attribute):
            if node.attr.startswith("_") or node.attr in BANNED_ATTRS:
                problems.append(f"atributo proibido: {node.attr}")
        elif isinstance(node, ast.Name):
            if node.id in HELPER_NAMES:
                helpers = True
            elif node.id in BANNED_NAMES or node.id.startswith("__"):
                problems.append(f"nome proibido: {node.id}")
        elif isinstance(node, (ast.AsyncFunctionDef, ast.Await, ast.AsyncFor, ast.AsyncWith, ast.Global, ast.Nonlocal)):
            problems.append(f"construção proibida: {type(node).__name__}")
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            low = node.value.lower()
            for b in BANNED_STRINGS:
                if b in low:
                    problems.append(f"texto proibido: {b!r}")
                    break
        elif isinstance(node, ast.FunctionDef) and node.name == "run" and node.col_offset == 0:
            has_run = True
    if need_run and not has_run:
        problems.append("o código precisa definir `def run(args):` no nível principal")
    return not problems, sorted(set(problems)), helpers


def _run(payload):
    import builtins
    import importlib
    import io
    import types

    out = io.StringIO()

    def safe_print(*a, sep=" ", end="\n", **_k):
        if out.tell() < 20000:
            out.write(sep.join(str(x) for x in a) + end)

    class SafeModule:
        __slots__ = ("_m",)

        def __init__(self, m):
            object.__setattr__(self, "_m", m)

        def __getattr__(self, n):
            if n.startswith("_") or n in MODULE_ATTR_DENY.get(self._m.__name__, ()):
                raise AttributeError(n)
            v = getattr(self._m, n)
            if isinstance(v, types.ModuleType):
                if v.__name__ in ALLOWED_MODULES:
                    return SafeModule(v)
                raise AttributeError(n)
            return v

    def safe_import(name, globals=None, locals=None, fromlist=(), level=0):
        if level or name not in ALLOWED_MODULES:
            raise ImportError(f"import proibido: {name}")
        mod = importlib.import_module(name)
        if fromlist:
            return SafeModule(mod)
        return SafeModule(importlib.import_module(name.split(".")[0]))

    names = ("abs all any bin bool bytes callable chr dict divmod enumerate filter float format frozenset hash hex int "
             "isinstance issubclass iter len list map max min next object oct ord pow range repr reversed round set slice "
             "sorted str sum tuple zip Exception ValueError TypeError KeyError IndexError ZeroDivisionError AssertionError "
             "StopIteration ArithmeticError AttributeError RuntimeError OverflowError NotImplementedError LookupError").split()
    safe = {n: getattr(builtins, n) for n in names}
    safe.update({"print": safe_print, "__import__": safe_import, "__build_class__": builtins.__build_class__, "__name__": "tool"})
    g = {"__builtins__": safe, "__name__": "tool"}

    exec(compile(payload["code"], "<tool>", "exec"), g)
    if payload.get("tests"):
        tg = dict(g)
        exec(compile(payload["tests"], "<tests>", "exec"), tg)
        ran = 0
        for n, f in list(tg.items()):
            if n.startswith("test_") and callable(f):
                f()
                ran += 1
        if ran == 0:
            raise AssertionError("os testes não definem nenhuma função test_*")
        return {"testes": ran}, out.getvalue()
    result = g["run"](payload.get("args") or {})
    json.dumps(result)   # precisa ser serializável
    return result, out.getvalue()


def _limits():
    import resource
    for lim, val in ((resource.RLIMIT_CPU, (8, 9)), (resource.RLIMIT_AS, (400 * 2 ** 20, 400 * 2 ** 20)),
                     (resource.RLIMIT_FSIZE, (1 << 20, 1 << 20)), (resource.RLIMIT_NOFILE, (32, 32)),
                     (resource.RLIMIT_NPROC, (0, 0)), (resource.RLIMIT_CORE, (0, 0))):
        try:
            resource.setrlimit(lim, val)
        except (ValueError, OSError):
            pass


def main():
    payload = json.loads(sys.stdin.read())
    ok_v, problems, _ = validate(payload["code"], need_run=not payload.get("tests"))
    if ok_v and payload.get("tests"):
        ok_v, problems, _ = validate(payload["tests"], need_run=False)
    if not ok_v:
        res = {"ok": False, "error": "validação: " + "; ".join(problems)}
    else:
        _limits()
        try:
            result, printed = _run(payload)
            res = {"ok": True, "result": result, "stdout": printed}
        except MemoryError:
            res = {"ok": False, "error": "memória esgotada"}
        except BaseException as e:   # inclui AssertionError dos testes
            res = {"ok": False, "error": f"{type(e).__name__}: {str(e)[:300]}"}
    sys.stdout.write(json.dumps(res)[:100000])


if __name__ == "__main__":
    main()
