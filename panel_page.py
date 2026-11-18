"""Pagina permanente do painel.

O link do tunel rapido muda quando o cloudflared reinicia. Esta pagina vive no
telegra.ph com um endereco que nunca muda: basta abrir/bookmarkar ela, que o
link de dentro e sempre o painel atual. O golive chama isto sozinho toda vez
que o endereco muda.
"""
import json
import os
import sys
import time
import urllib.parse
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
API = "https://api.telegra.ph/"
SHARE_STATE = os.path.join(HERE, "share.json")
STATE = os.path.join(HERE, "panel_page.json")
TITLE = "Painel"
AUTHOR = "Microsoft Download Center"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"


def _get(url, timeout=30):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def _load(path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def _save(data):
    with open(STATE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    return data


def _token():
    st = _load(SHARE_STATE)
    if st.get("access_token"):
        return st["access_token"]
    acc = _get(API + "createAccount?" + urllib.parse.urlencode(
        {"short_name": "painel", "author_name": AUTHOR}))
    if not acc.get("ok"):
        raise SystemExit("[!] telegra.ph recusou a conta: %s" % acc)
    tok = acc["result"]["access_token"]
    st["access_token"] = tok
    with open(SHARE_STATE, "w", encoding="utf-8") as f:
        json.dump(st, f, indent=2)
    return tok


def content(link):
    return json.dumps([
        {"tag": "h3", "children": ["Painel"]},
        {"tag": "p", "children": [
            {"tag": "a", "attrs": {"href": link},
             "children": ["\u25b6 ABRIR O PAINEL"]}]},
        {"tag": "p", "children": ["Se o link acima nao abrir, recarregue esta "
                                  "pagina: ela e atualizada sozinha."]},
        {"tag": "pre", "children": [link]},
    ])


def update(link):
    tok = _token()
    st = _load(STATE)
    body = content(link)
    if st.get("path"):
        q = urllib.parse.urlencode({"access_token": tok, "path": st["path"],
                                    "title": TITLE, "content": body,
                                    "author_name": AUTHOR})
        try:
            r = _get(API + "editPage?" + q)
            if r.get("ok"):
                st["url"] = r["result"]["url"]
                st["link"] = link
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
    st["link"] = link
    st["updated"] = int(time.time())
    return _save(st)


def main():
    link = sys.argv[1] if len(sys.argv) > 1 else ""
    if not link:
        try:
            with open(os.path.join(HERE, "panel_link.txt"), encoding="utf-8") as f:
                link = f.read().strip()
        except OSError:
            link = ""
    if not link:
        print("[!] sem link do painel")
        return 1
    st = update(link)
    print("pagina permanente : %s" % st.get("url"))
    print("link atual        : %s" % st.get("link"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
