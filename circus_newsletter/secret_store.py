"""Store local integration secrets with Windows machine DPAPI and a restricted ACL."""

from __future__ import annotations

import csv
import ctypes
import os
import subprocess
import tempfile
from pathlib import Path


SECRET_NAME = "imap-password.dpapi"
DISCORD_SECRET_NAME = "discord-webhook.dpapi"
_UI_FORBIDDEN = 0x1
_LOCAL_MACHINE = 0x4


class _Blob(ctypes.Structure):
    _fields_ = [("size", ctypes.c_ulong), ("data", ctypes.POINTER(ctypes.c_byte))]


def _windows_api():
    if os.name != "nt":
        raise RuntimeError("Mail secret storage requires Windows")
    crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    crypt32.CryptProtectData.argtypes = [ctypes.POINTER(_Blob), ctypes.c_wchar_p,
                                         ctypes.c_void_p, ctypes.c_void_p,
                                         ctypes.c_void_p, ctypes.c_ulong,
                                         ctypes.POINTER(_Blob)]
    crypt32.CryptProtectData.restype = ctypes.c_int
    crypt32.CryptUnprotectData.argtypes = [ctypes.POINTER(_Blob), ctypes.c_void_p,
                                           ctypes.c_void_p, ctypes.c_void_p,
                                           ctypes.c_void_p, ctypes.c_ulong,
                                           ctypes.POINTER(_Blob)]
    crypt32.CryptUnprotectData.restype = ctypes.c_int
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    kernel32.LocalFree.restype = ctypes.c_void_p
    return crypt32, kernel32


def _protect(data: bytes) -> bytes:
    crypt32, kernel32 = _windows_api()
    source_buffer = ctypes.create_string_buffer(data)
    source = _Blob(len(data), ctypes.cast(source_buffer, ctypes.POINTER(ctypes.c_byte)))
    result = _Blob()
    if not crypt32.CryptProtectData(ctypes.byref(source), "Circus Newsletter IMAP",
                                    None, None, None, _LOCAL_MACHINE | _UI_FORBIDDEN,
                                    ctypes.byref(result)):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        return ctypes.string_at(result.data, result.size)
    finally:
        kernel32.LocalFree(result.data)


def _unprotect(data: bytes) -> bytes:
    crypt32, kernel32 = _windows_api()
    source_buffer = ctypes.create_string_buffer(data)
    source = _Blob(len(data), ctypes.cast(source_buffer, ctypes.POINTER(ctypes.c_byte)))
    result = _Blob()
    if not crypt32.CryptUnprotectData(ctypes.byref(source), None, None, None, None,
                                      _UI_FORBIDDEN, ctypes.byref(result)):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        return ctypes.string_at(result.data, result.size)
    finally:
        kernel32.LocalFree(result.data)


def _restrict_directory(directory: Path) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    identity = subprocess.run(["whoami", "/user", "/fo", "csv", "/nh"],
                              check=True, capture_output=True, text=True).stdout.strip()
    sid = next(csv.reader([identity]))[1]
    subprocess.run(["icacls", str(directory), "/grant:r",
                    f"*{sid}:(OI)(CI)F", "*S-1-5-18:(OI)(CI)F"],
                   check=True, capture_output=True, text=True)
    subprocess.run(["icacls", str(directory), "/inheritance:r"],
                   check=True, capture_output=True, text=True)


def store_secret(root: Path, filename: str, value: str) -> None:
    if filename not in {SECRET_NAME, DISCORD_SECRET_NAME}:
        raise ValueError("Unknown secret filename")
    if not value:
        raise ValueError("Secret is empty")
    directory = root / "config" / "secrets"
    _restrict_directory(directory)
    encrypted = _protect(value.encode("utf-8"))
    handle, temporary = tempfile.mkstemp(prefix=".secret-", dir=directory)
    try:
        with os.fdopen(handle, "wb") as output:
            output.write(encrypted)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, directory / filename)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def read_secret(root: Path, filename: str) -> str:
    if filename not in {SECRET_NAME, DISCORD_SECRET_NAME}:
        raise ValueError("Unknown secret filename")
    path = root / "config" / "secrets" / filename
    if not path.exists():
        raise RuntimeError(f"No Circus secret stored: {filename}")
    return _unprotect(path.read_bytes()).decode("utf-8")


def store_password(root: Path, password: str) -> None:
    store_secret(root, SECRET_NAME, password)


def read_password(root: Path) -> str:
    path = root / "config" / "secrets" / SECRET_NAME
    if not path.exists():
        raise RuntimeError("No Circus app password is stored; run scripts/configure_imap.py")
    return read_secret(root, SECRET_NAME)
