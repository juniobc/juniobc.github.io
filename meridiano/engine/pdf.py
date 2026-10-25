"""PDF simples, sem dependências: texto em Helvetica (WinAnsi), títulos em negrito, quebra de linha e páginas numeradas.
Entende o Markdown que os produtos usam: # / ## / ### títulos, - itens, linha em branco separa parágrafos."""
import textwrap
import zlib

W, H, MARGIN = 595, 842, 56
BODY, LEAD = 10.5, 15
COLS = 88          # caracteres por linha de corpo (largura média da Helvetica, com folga na margem)


def _esc(text):
    b = text.encode("cp1252", errors="replace")   # o marcador de lista existe em cp1252 (0x95)
    return b.replace(b"\\", b"\\\\").replace(b"(", b"\\(").replace(b")", b"\\)")


def _blocks(markdown):
    """Lista de (fonte, tamanho, texto, espaço antes) já quebrada em linhas."""
    out = []
    for raw in markdown.replace("\r", "").split("\n"):
        line = raw.rstrip()
        if not line.strip():
            out.append(("F1", BODY, "", 0))
            continue
        if line.startswith("### "):
            out.append(("F2", 11.5, line[4:], 8))
        elif line.startswith("## "):
            out.append(("F2", 14, line[3:], 14))
        elif line.startswith("# "):
            out.append(("F2", 18, line[2:], 18))
        else:
            bullet = line.lstrip().startswith(("- ", "* "))
            text = ("• " + line.lstrip()[2:]) if bullet else line
            for i, part in enumerate(textwrap.wrap(text, COLS - (4 if bullet else 0), subsequent_indent="  " if bullet else "") or [""]):
                out.append(("F1", BODY, part, 0))
            continue
        size = out[-1][1]
        wrapped = textwrap.wrap(out[-1][2], int(COLS * BODY / size)) or [""]
        before = out.pop()[3]
        for i, part in enumerate(wrapped):
            out.append((out_font(size), size, part, before if i == 0 else 0))
    return out


def out_font(size):
    return "F2" if size != BODY else "F1"


def render_pdf(title, markdown, footer=""):
    """Devolve os bytes de um PDF com `title` na capa e depois o conteúdo."""
    lines = _blocks(f"# {title}\n\n" + markdown)
    pages, cur, y = [], [], H - MARGIN
    for font, size, text, before in lines:
        need = LEAD * (size / BODY) + before
        if y - need < MARGIN + 20:
            pages.append(cur)
            cur, y = [], H - MARGIN
        y -= need
        cur.append((font, size, text, y))
    pages.append(cur)
    objs = {1: b"<< /Type /Catalog /Pages 2 0 R >>", 3: b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>",
            4: b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica-Bold /Encoding /WinAnsiEncoding >>"}
    kids, n = [], 5
    for i, page in enumerate(pages, 1):
        parts = []
        for font, size, text, y0 in page:
            if text:
                parts.append(b"BT /%s %.1f Tf %d %.1f Td (" % (font.encode(), size, MARGIN, y0) + _esc(text) + b") Tj ET")
        foot = (footer + "  ·  " if footer else "") + f"página {i} de {len(pages)}"
        parts.append(b"BT /F1 8 Tf %d 30 Td (" % MARGIN + _esc(foot) + b") Tj ET")
        stream = zlib.compress(b"\n".join(parts))
        objs[n] = b"<< /Length %d /Filter /FlateDecode >>\nstream\n" % len(stream) + stream + b"\nendstream"
        objs[n + 1] = (b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 %d %d] /Contents %d 0 R /Resources << /Font << /F1 3 0 R /F2 4 0 R >> >> >>" % (W, H, n))
        kids.append(n + 1)
        n += 2
    objs[2] = b"<< /Type /Pages /Kids [" + b" ".join(b"%d 0 R" % k for k in kids) + b"] /Count %d >>" % len(kids)
    out = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets = {}
    for num in sorted(objs):
        offsets[num] = len(out)
        out += b"%d 0 obj\n" % num + objs[num] + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objs) + 1)
    for num in sorted(objs):
        out += b"%010d 00000 n \n" % offsets[num]
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objs) + 1, xref)
    return bytes(out)
