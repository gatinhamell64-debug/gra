#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
RAF launcher -- one command, server + local agent, session already selected.

    python run.py

Starts the C2 console, spawns agent.py against it, waits for the check-in and
drops you straight into the prompt with that session active. Type 'help' for
commands, 'quit' to leave (the agent process is killed on exit).
"""

import argparse
import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import server as S


def main():
    ap = argparse.ArgumentParser(description="RAF launcher (server + agent)")
    ap.add_argument("--port", type=int, default=4444)
    ap.add_argument("--key", default=S.DEFAULT_KEY)
    ap.add_argument("--host", default="127.0.0.1", help="address the agent dials")
    ap.add_argument("--no-agent", action="store_true", help="server only")
    a = ap.parse_args()

    S._harden_console()

    srv = S.Server("127.0.0.1", a.port, a.key.encode())
    try:
        srv.start()
    except OSError as exc:
        print("[!] cannot bind 127.0.0.1:%d -- %s" % (a.port, exc))
        print("    another instance running? try --port %d" % (a.port + 1))
        return 1

    agent = None
    if not a.no_agent:
        agent = subprocess.Popen(
            [sys.executable, os.path.join(HERE, "agent.py"),
             "--host", a.host, "--port", str(a.port),
             "--key", a.key, "--visible"],
            cwd=HERE,
        )
        print("agent spawned (pid %d), waiting for check-in..." % agent.pid)

    sess = None
    deadline = time.time() + 20
    while time.time() < deadline:
        found = srv.all()
        if found:
            sess = found[0]
            break
        time.sleep(0.15)

    if sess is None:
        print("[!] no check-in after 20s")
        print("    is 127.0.0.1:%d reachable? is the key the same?" % a.port)
        if agent:
            agent.kill()
        return 1

    print("\nsession %s ready  (%s@%s)" % (
        sess.id, sess.info.get("user", "?"), sess.info.get("host", "?")))
    print("commands: help | sessions | shell <cmd> | ls | get/put | screenshot | quit\n")

    try:
        S.repl(srv, initial=sess)
    finally:
        if agent:
            try:
                agent.kill()
            except Exception:
                pass
        for s in srv.all():
            s.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
