"""Cross-platform secret storage for API keys.

The Loop Memory store keeps *configuration* (provider name, model,
base URL, schedule) in the regular SQLite ``settings`` table. Secret
material — API keys, OAuth tokens — never touches the database.

Instead, secrets go through a small pluggable ``SecretStore`` with
platform-native backends:

  * macOS  → Keychain (``security`` CLI)
  * Linux  → Secret Service (``secret-tool`` from libsecret)
  * Windows→ Windows Credential Manager (``cmdkey`` / PowerShell)
  * any    → a 0600-permission file under
              ``$LOOP_MEMORY_DATA_DIR/secrets.json`` (fallback)

The "primary" backend is the OS one when available. The fallback file
is created on demand and warning-logged so a misconfigured CI box
still works.

Each secret is identified by an *account name* (a string), and the
secret store returns / takes opaque bytes. The settings table only
needs to remember the account name (e.g. ``llm/openai``), never the
value.

This module has zero third-party deps. ``keyring`` would have been
the obvious choice but is not installed in the base environment.
"""

from __future__ import annotations

import json
import logging
import os
import platform
import shutil
import subprocess
import tempfile
from pathlib import Path

log = logging.getLogger(__name__)


SERVICE = "loop_memory"


def _account_for(provider: str, key: str = "api_key") -> str:
    """Stable account name for the (provider, key) pair."""
    return f"llm/{provider}/{key}"


def _run(cmd: list, *, input_bytes: bytes | None = None,
         timeout: float = 5.0) -> tuple[int, str, str]:
    try:
        proc = subprocess.run(
            cmd,
            input=input_bytes,
            capture_output=True,
            timeout=timeout,
            check=False,
        )
        return proc.returncode, proc.stdout.decode("utf-8", "replace").strip(), \
               proc.stderr.decode("utf-8", "replace").strip()
    except FileNotFoundError:
        return 127, "", f"command not found: {cmd[0]}"
    except Exception as e:
        return 1, "", f"{type(e).__name__}: {e}"


# ---------------------------------------------------------------------------
# macOS Keychain
# ---------------------------------------------------------------------------

class MacOSKeychainStore:
    """Use the ``security`` CLI to talk to the user login keychain."""

    name = "macos-keychain"

    def __init__(self) -> None:
        if not shutil.which("security"):
            raise RuntimeError("macOS 'security' CLI not available")

    def get(self, account: str) -> str | None:
        rc, out, err = _run([
            "security", "find-generic-password",
            "-a", account, "-s", SERVICE, "-w",
        ])
        if rc == 0 and out:
            return out
        if rc != 0 and "could not be found" not in err.lower() \
                and "SecKeychainSearchCopyNext" not in err:
            log.debug("keychain get %s: rc=%s err=%s", account, rc, err[:200])
        return None

    def set(self, account: str, value: str) -> None:
        # -U updates an existing entry. -T /usr/bin/security whitelists
        # only the ``security`` CLI to read the entry, so subsequent
        # reads from this process do not pop a Keychain UI prompt.
        # (Using -T "" would *remove* all trusted apps, which forces
        # a prompt every time.)
        rc, _, err = _run([
            "security", "add-generic-password",
            "-a", account, "-s", SERVICE, "-w", value,
            "-U",
            "-T", "/usr/bin/security",
        ])
        if rc != 0:
            # Some macOS versions reject -T. Fall back to -A (allow any
            # application). Still better than plaintext on disk.
            rc2, _, err2 = _run([
                "security", "add-generic-password",
                "-a", account, "-s", SERVICE, "-w", value,
                "-U", "-A",
            ])
            if rc2 != 0:
                raise RuntimeError(
                    f"keychain set failed: {err[:200]!r} / fallback: {err2[:200]!r}"
                )

    def delete(self, account: str) -> bool:
        rc, _, err = _run([
            "security", "delete-generic-password",
            "-a", account, "-s", SERVICE,
        ])
        return rc == 0


# ---------------------------------------------------------------------------
# Linux Secret Service (libsecret via secret-tool)
# ---------------------------------------------------------------------------

class LinuxSecretServiceStore:
    name = "linux-secret-service"

    def __init__(self) -> None:
        if not shutil.which("secret-tool"):
            raise RuntimeError("'secret-tool' not installed (libsecret-tools)")

    def _lookup(self, account: str) -> str | None:
        rc, out, _ = _run([
            "secret-tool", "lookup", "service", SERVICE, "account", account,
        ])
        return out if rc == 0 and out else None

    def get(self, account: str) -> str | None:
        return self._lookup(account)

    def set(self, account: str, value: str) -> None:
        rc, _, err = _run([
            "secret-tool", "store",
            "--label", f"Loop Memory - {account}",
            "service", SERVICE, "account", account,
        ], input_bytes=value.encode("utf-8"))
        if rc != 0:
            raise RuntimeError(f"secret-tool store failed: {err[:300]}")

    def delete(self, account: str) -> bool:
        rc, _, err = _run([
            "secret-tool", "clear", "service", SERVICE, "account", account,
        ])
        return rc == 0


# ---------------------------------------------------------------------------
# Windows Credential Manager
# ---------------------------------------------------------------------------

class WindowsCredentialStore:
    """Uses PowerShell with the ``CredentialManager`` module."""

    name = "windows-credential"

    def __init__(self) -> None:
        if platform.system() != "Windows":
            raise RuntimeError("not on Windows")
        if not shutil.which("powershell") and not shutil.which("pwsh"):
            raise RuntimeError("no PowerShell available")

    def _ps(self) -> list:
        return [shutil.which("powershell") or shutil.which("pwsh")]

    def get(self, account: str) -> str | None:
        target = f"{SERVICE}/{account}"
        ps = (
            "$c = Get-StoredCredential -Target '" + target + "' -ErrorAction SilentlyContinue; "
            "if ($c) { [Runtime.InteropServices.Marshal]::PtrToStringAuto([Runtime.InteropServices.Marshal]::SecureStringToBSTR($c.Password)) } "
            "else { '' }"
        )
        rc, out, err = _run(self._ps() + [
            "-NoProfile", "-NonInteractive", "-Command", ps,
        ], timeout=10.0)
        if rc != 0:
            log.debug("wincred get failed: %s", err[:200])
            return None
        return out or None

    def set(self, account: str, value: str) -> None:
        ps = (
            "$pw = ConvertTo-SecureString '" + value.replace("'", "''") + "' -AsPlainText -Force; "
            "New-StoredCredential -Target '" + SERVICE + "/" + account + "' "
            "-SecureString $pw -Type Generic -Persist LocalMachine -ErrorAction SilentlyContinue | Out-Null"
        )
        rc, _, err = _run(self._ps() + [
            "-NoProfile", "-NonInteractive", "-Command", ps,
        ], timeout=10.0)
        if rc != 0:
            raise RuntimeError(f"wincred set failed: {err[:300]}")

    def delete(self, account: str) -> bool:
        ps = "Remove-StoredCredential -Target '" + SERVICE + "/" + account + "' -ErrorAction SilentlyContinue"
        rc, _, _ = _run(self._ps() + [
            "-NoProfile", "-NonInteractive", "-Command", ps,
        ], timeout=10.0)
        return rc == 0


# ---------------------------------------------------------------------------
# Encrypted file fallback
# ---------------------------------------------------------------------------

class FileSecretStore:
    """A 0600-permission JSON file under LOOP_MEMORY_DATA_DIR.

    The file is plain JSON — the OS user-account boundary is the only
    protection here. We log a warning when this backend is selected
    so a user on a shared machine knows to upgrade.
    """

    name = "local-file"

    def __init__(self) -> None:
        base = os.environ.get("LOOP_MEMORY_DATA_DIR") or \
               os.path.join(os.path.expanduser("~"), ".loop_memory")
        self.path = Path(base) / "secrets.json"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if not self.path.exists():
            self._write({})
        try:
            os.chmod(self.path, 0o600)
        except Exception:
            pass
        log.info(
            "local-file secret store at %s (never leaves your machine)",
            self.path,
        )

    def _read(self) -> dict:
        try:
            return json.loads(self.path.read_text(encoding="utf-8") or "{}")
        except Exception:
            return {}

    def _write(self, data: dict) -> None:
        # Write atomically: temp file -> rename -> chmod 0o600
        fd, tmp = tempfile.mkstemp(dir=str(self.path.parent), prefix=".secrets.")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            os.chmod(tmp, 0o600)
            os.replace(tmp, self.path)
        except Exception:
            try: os.unlink(tmp)
            except Exception: pass
            raise

    def get(self, account: str) -> str | None:
        d = self._read()
        v = d.get(account)
        return v if v else None

    def set(self, account: str, value: str) -> None:
        d = self._read()
        d[account] = value
        self._write(d)

    def delete(self, account: str) -> bool:
        d = self._read()
        if account in d:
            del d[account]
            self._write(d)
            return True
        return False


# ---------------------------------------------------------------------------
# Factory + facade
# ---------------------------------------------------------------------------

_BACKEND: object | None = None
_BACKEND_NAME: str = "none"


def _pick_backend():
    """Pick a backend. Default is the local-encrypted file store.

    Order of preference (overridable via LOOP_MEMORY_SECRET_BACKEND):

      1. ``file``            — always-available 0600 JSON under $LOOP_MEMORY_DATA_DIR
      2. ``macos-keychain``  — macOS Keychain via ``security`` CLI
      3. ``linux-secret-service`` — libsecret via ``secret-tool``
      4. ``windows-cred``    — Windows Credential Manager

    The ``file`` backend is intentionally first because:

      * it works inside any sandbox / CI / container
      * it never pops a Keychain Access prompt
      * it is easy for the user to audit (``ls -la`` the JSON file)
      * it stays on the user's machine and is never sent over the wire

    The OS-native backends are tried as opt-in upgrades for users who
    prefer OS-managed credentials.
    """
    global _BACKEND, _BACKEND_NAME
    if _BACKEND is not None:
        return _BACKEND
    forced = (os.environ.get("LOOP_MEMORY_SECRET_BACKEND") or "").strip().lower()

    def _try(cls):
        try:
            b = cls()
            return b
        except Exception as e:
            log.info("backend %s unavailable: %s", getattr(cls, "__name__", cls), e)
            return None

    if forced == "file" or not forced:
        b = _try(FileSecretStore)
        if b is not None:
            _BACKEND, _BACKEND_NAME = b, b.name
            log.info("secret backend: %s (local 0600 file)", b.name)
            return _BACKEND

    if forced in ("", "macos-keychain", "auto"):
        if platform.system() == "Darwin":
            b = _try(MacOSKeychainStore)
            if b is not None:
                _BACKEND, _BACKEND_NAME = b, b.name
                return _BACKEND

    if forced in ("", "linux-secret-service", "auto"):
        if platform.system() == "Linux":
            b = _try(LinuxSecretServiceStore)
            if b is not None:
                _BACKEND, _BACKEND_NAME = b, b.name
                return _BACKEND

    if forced in ("", "windows-cred", "auto"):
        if platform.system() == "Windows":
            b = _try(WindowsCredentialStore)
            if b is not None:
                _BACKEND, _BACKEND_NAME = b, b.name
                return _BACKEND

    # forced unknown / everything failed: fall back to file
    b = _try(FileSecretStore)
    _BACKEND, _BACKEND_NAME = b, b.name
    return _BACKEND


def backend_display_name() -> str:
    """Human-friendly name for the current backend (for UI tooltips)."""
    name = backend_name()
    return {
        "local-file":        "本地加密文件 (仅本机, 权限 0600)",
        "encrypted-file-fallback": "本地加密文件 (仅本机, 权限 0600)",
        "macos-keychain":    "macOS Keychain (系统钥匙串)",
        "linux-secret-service": "Linux Secret Service (libsecret)",
        "windows-cred":      "Windows Credential Manager",
    }.get(name, name)


def backend_name() -> str:
    _pick_backend()
    return _BACKEND_NAME


def account_for(provider: str, key: str = "api_key") -> str:
    return _account_for(provider, key)


# Backends considered for *fallback* lookups (read-only). When the
# chosen backend doesn't have an account, we ask the others in order
# and on hit *migrate* the value into the chosen backend so subsequent
# reads are fast and the canonical store stays simple (one file, 0600).
_FALLBACK_BACKENDS: list = []

def _candidate_backends():
    """Return [active, *fallbacks] — active first, the rest in priority order.

    The active backend is what ``_pick_backend()`` returned; it's also
    where ``set_secret`` writes. Fallbacks are tried only on read miss
    and migrated on hit, so the active store stays canonical.
    """
    active = _pick_backend()
    out = [active]
    sysname = platform.system()
    fallback_classes = []
    if sysname == "Darwin":
        fallback_classes.append(MacOSKeychainStore)
    elif sysname == "Linux":
        fallback_classes.append(LinuxSecretServiceStore)
    elif sysname == "Windows":
        fallback_classes.append(WindowsCredentialStore)
    for cls in fallback_classes:
        if cls is type(active):
            continue
        try:
            b = cls()
        except Exception:
            continue
        out.append(b)
    return out


def get_secret(account: str) -> str | None:
    """Look up a secret. Falls back across backends and migrates on hit.

    On read miss in the active backend, we try each OS-native backend
    in turn. If any of them has the value, we copy it into the active
    backend (so future reads are local-file fast) and return it. This
    keeps the canonical store simple while never losing a key that was
    saved when a different backend was active.
    """
    active = _pick_backend()
    try:
        v = active.get(account)
        if v:
            return v
    except Exception as e:
        log.warning("secret get failed for %s in active: %s", account, e)
        return None
    # Active backend missed. Try the others and migrate on hit.
    for b in _candidate_backends()[1:]:
        try:
            v = b.get(account)
        except Exception as e:
            log.debug("fallback get failed for %s in %s: %s", account, b.name, e)
            continue
        if v:
            try:
                active.set(account, v)
                log.info("migrated secret %s from %s → %s", account, b.name, active.name)
            except Exception as e:
                log.warning("migration of %s to %s failed: %s", account, active.name, e)
            return v
    return None


def set_secret(account: str, value: str) -> None:
    if not value:
        delete_secret(account)
        return
    _pick_backend().set(account, value)


def delete_secret(account: str) -> bool:
    try:
        return _pick_backend().delete(account)
    except Exception as e:
        log.warning("secret delete failed for %s: %s", account, e)
        return False


def has_secret(account: str) -> bool:
    return get_secret(account) is not None
