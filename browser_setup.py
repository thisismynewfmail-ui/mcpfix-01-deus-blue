#!/usr/bin/env python3
"""
CHATSGI / TERMINAL — local browser subsystem setup
Section 9 · self-hosted Playwright auto-installer

This script ONLY sets up the local browser (Playwright) subsystem that powers
the in-terminal BROWSER tool (see ``browser_mcp_server.py``). It does NOT touch
the LLM endpoint, MCP config, Piper TTS, chats, or any other part of the
terminal — its sole job is to make a fully local, self-hosted headed browser
available with no npx, no Docker and no external Playwright server.

What it does
------------
  1. ``pip install playwright`` (into the interpreter running this script).
  2. ``playwright install chromium`` — downloads the Chromium engine into a
     PROJECT-LOCAL directory (``./.browser/ms-playwright``) so nothing leaks
     into the system and the whole browser ships next to the app.
  3. (optional, ``--with-deps``) Linux OS library deps, best-effort.

It is idempotent and safe to run repeatedly. A lock file guards against two
processes installing at once (the backend may auto-run this on boot while the
user also clicks "INSTALL ENGINE").

Cross-platform: Windows, Linux and macOS. The Chromium download is a Node
process; behind a TLS-terminating proxy it needs the proxy CA — this script
auto-detects a CA bundle (``NODE_EXTRA_CA_CERTS`` / ``SSL_CERT_FILE`` / common
locations) and feeds it to the downloader so corporate / sandbox proxies work.

Usage
-----
    python browser_setup.py            # install playwright + chromium
    python browser_setup.py --check    # print readiness as JSON, install nothing
    python browser_setup.py --with-deps  # also try OS lib deps (Linux, needs root)

Importable API (used by start.py and browser_mcp_server.py)
-----------------------------------------------------------
    paths()                -> dict of canonical local paths (all strings)
    ca_bundle()            -> str | None   (CA bundle for the Node downloader)
    playwright_installed() -> bool
    chromium_installed()   -> bool
    find_chromium()        -> str | None   (path to the chromium executable)
    is_ready()             -> bool
    status()               -> dict
    ensure(log=print, with_deps=False, want_browser=True) -> dict
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

# ───────────────────────────── Paths ─────────────────────────────
# Everything browser-related lives under ./.browser next to this file so the
# whole subsystem is project-local and trivially removable.

ROOT = Path(__file__).resolve().parent
BROWSER_DIR = ROOT / ".browser"
BROWSERS_PATH = BROWSER_DIR / "ms-playwright"   # PLAYWRIGHT_BROWSERS_PATH (engine binaries)
PROFILE_DIR = BROWSER_DIR / "profile"           # persistent user-data dir (cookies, storage…)
SCREENSHOT_DIR = BROWSER_DIR / "screenshots"    # saved screenshots
LOCK_FILE = BROWSER_DIR / ".install.lock"
LOG_FILE = BROWSER_DIR / "setup.log"

# Package spec to install. Unpinned so the downloaded Chromium build always
# matches whatever Playwright version pip resolves (they ship in lock-step).
PLAYWRIGHT_PIP_SPEC = "playwright"


def paths() -> dict:
    """Canonical local paths as strings (safe to hand to subprocess env)."""
    return {
        "root": str(ROOT),
        "dir": str(BROWSER_DIR),
        "browsers": str(BROWSERS_PATH),
        "profile": str(PROFILE_DIR),
        "screenshots": str(SCREENSHOT_DIR),
        "lock": str(LOCK_FILE),
        "log": str(LOG_FILE),
    }


def _mkdirs() -> None:
    for p in (BROWSER_DIR, BROWSERS_PATH, PROFILE_DIR, SCREENSHOT_DIR):
        try:
            p.mkdir(parents=True, exist_ok=True)
        except Exception:
            pass


# ─────────────────────────── CA bundle ───────────────────────────
# The Chromium download runs in Node (bundled with Playwright). Behind a
# TLS-terminating proxy it must trust the proxy CA or the download aborts. We
# look at the usual env vars first, then a few well-known bundle locations.

_CA_ENV_VARS = ("NODE_EXTRA_CA_CERTS", "SSL_CERT_FILE", "REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE")
_CA_KNOWN_PATHS = (
    "/root/.ccr/ca-bundle.crt",
    "/etc/ssl/certs/ca-certificates.crt",
    "/etc/pki/tls/certs/ca-bundle.crt",
)


def ca_bundle() -> str | None:
    for var in _CA_ENV_VARS:
        v = os.environ.get(var)
        if v and Path(v).exists():
            return v
    for p in _CA_KNOWN_PATHS:
        if Path(p).exists():
            return p
    return None


# ─────────────────────── Readiness detection ─────────────────────

def playwright_installed() -> bool:
    """True if the playwright Python package is importable."""
    try:
        import importlib.util
        return importlib.util.find_spec("playwright") is not None
    except Exception:
        return False


def _chromium_exe_names() -> tuple[str, ...]:
    if sys.platform.startswith("win"):
        return ("chrome.exe",)
    if sys.platform == "darwin":
        return ("Chromium", "Google Chrome for Testing", "chrome")
    return ("chrome", "headless_shell", "chrome-wrapper")


def find_chromium() -> str | None:
    """Locate the Chromium executable inside the project-local browsers dir.

    An explicit ``BROWSER_EXECUTABLE`` override always wins (lets advanced users
    point at a system Chrome / a sandbox-provided build).
    """
    override = os.environ.get("BROWSER_EXECUTABLE")
    if override and Path(override).exists():
        return override
    if not BROWSERS_PATH.exists():
        return None
    names = _chromium_exe_names()
    # Playwright layout: <browsers>/chromium-<rev>/chrome-<os>/<exe>
    for d in sorted(BROWSERS_PATH.glob("chromium*")):
        if not d.is_dir():
            continue
        for cand in d.rglob("*"):
            if cand.is_file() and cand.name in names:
                # skip obvious non-launchers
                if cand.name in ("chrome-wrapper",):
                    continue
                return str(cand)
    return None


def chromium_installed() -> bool:
    return find_chromium() is not None


def is_ready() -> bool:
    return playwright_installed() and chromium_installed()


def status() -> dict:
    return {
        "ready": is_ready(),
        "playwright": playwright_installed(),
        "chromium": chromium_installed(),
        "executable": find_chromium(),
        "caBundle": ca_bundle(),
        "installing": _lock_is_active(),
        "paths": paths(),
        "python": sys.executable,
    }


# ───────────────────────────── Lock ──────────────────────────────
# Cross-platform best-effort lock so the boot auto-installer and a manual
# "INSTALL ENGINE" click don't run two downloads at once.

_LOCK_TTL = 1800  # seconds; a stale lock older than this is ignored


def _lock_is_active() -> bool:
    try:
        if not LOCK_FILE.exists():
            return False
        age = time.time() - LOCK_FILE.stat().st_mtime
        return age < _LOCK_TTL
    except Exception:
        return False


def _acquire_lock() -> bool:
    _mkdirs()
    try:
        fd = os.open(str(LOCK_FILE), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.write(fd, f"{os.getpid()} {int(time.time())}\n".encode())
        os.close(fd)
        return True
    except FileExistsError:
        if not _lock_is_active():          # stale → steal it
            try:
                LOCK_FILE.unlink()
            except Exception:
                pass
            return _acquire_lock()
        return False
    except Exception:
        return False


def _release_lock() -> None:
    try:
        LOCK_FILE.unlink()
    except Exception:
        pass


# ─────────────────────────── Install ─────────────────────────────

def _run(cmd: list[str], log, env: dict | None = None, timeout: int = 1800) -> int:
    log("$ " + " ".join(cmd))
    try:
        proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            env=env, cwd=str(ROOT), bufsize=1, universal_newlines=True,
        )
    except FileNotFoundError as e:
        log(f"  ! cannot launch: {e}")
        return 127
    start = time.time()
    assert proc.stdout is not None
    for line in proc.stdout:
        log("  " + line.rstrip())
        if time.time() - start > timeout:
            proc.kill()
            log("  ! timed out")
            return 124
    return proc.wait()


def _install_env() -> dict:
    env = os.environ.copy()
    env["PLAYWRIGHT_BROWSERS_PATH"] = str(BROWSERS_PATH)
    # never let an upstream "skip download" flag block us
    env.pop("PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD", None)
    ca = ca_bundle()
    if ca:
        env["NODE_EXTRA_CA_CERTS"] = ca
    return env


def ensure(log=print, with_deps: bool = False, want_browser: bool = True) -> dict:
    """Make the local browser subsystem ready. Idempotent.

    Returns a status dict (see ``status()``) augmented with ``ok``/``error``.
    """
    _mkdirs()

    if is_ready():
        log("browser subsystem already ready.")
        s = status(); s["ok"] = True; return s

    # Another install in flight? Wait for it rather than racing the download.
    if not _acquire_lock():
        log("another install is in progress — waiting…")
        deadline = time.time() + _LOCK_TTL
        while time.time() < deadline:
            time.sleep(2)
            if is_ready():
                s = status(); s["ok"] = True; return s
            if not _lock_is_active():
                break
        if not _acquire_lock():
            s = status(); s["ok"] = is_ready()
            s["error"] = "install already in progress"
            return s

    try:
        env = _install_env()
        ca = env.get("NODE_EXTRA_CA_CERTS")
        log(f"python   : {sys.executable}")
        log(f"browsers : {BROWSERS_PATH}")
        log(f"ca bundle: {ca or '(none — direct TLS)'}")

        if not playwright_installed():
            log("installing playwright package…")
            rc = _run([sys.executable, "-m", "pip", "install", "--upgrade", PLAYWRIGHT_PIP_SPEC], log, env=env)
            if rc != 0:
                raise RuntimeError(f"pip install playwright failed (exit {rc})")
        else:
            log("playwright package present.")

        if want_browser and not chromium_installed():
            log("downloading Chromium engine (this can take a minute)…")
            rc = _run([sys.executable, "-m", "playwright", "install", "chromium"], log, env=env)
            if rc != 0:
                raise RuntimeError(
                    f"'playwright install chromium' failed (exit {rc}). "
                    "If you are behind a proxy, ensure a CA bundle is available."
                )
        elif chromium_installed():
            log("chromium engine present.")

        if with_deps and sys.platform.startswith("linux"):
            log("installing Linux OS deps (best-effort)…")
            _run([sys.executable, "-m", "playwright", "install-deps", "chromium"], log, env=env)

        ok = is_ready()
        log("✓ browser subsystem ready." if ok else "! setup finished but subsystem still not ready.")
        s = status(); s["ok"] = ok
        if not ok:
            s["error"] = "playwright/chromium still missing after setup"
        return s
    except Exception as e:
        log(f"ERROR: {e}")
        s = status(); s["ok"] = False; s["error"] = str(e); return s
    finally:
        _release_lock()


# ───────────────────────────── CLI ───────────────────────────────

def main() -> int:
    ap = argparse.ArgumentParser(description="CHATSGI — local browser (Playwright) setup")
    ap.add_argument("--check", action="store_true", help="print readiness as JSON and exit (install nothing)")
    ap.add_argument("--with-deps", action="store_true", help="also install Linux OS library deps (best-effort, needs root)")
    ap.add_argument("--no-browser", action="store_true", help="install the playwright package only, skip the Chromium download")
    args = ap.parse_args()

    if args.check:
        print(json.dumps(status(), indent=2))
        return 0 if is_ready() else 1

    print("┌─ CHATSGI · LOCAL BROWSER SETUP ─────────────────────────────")
    res = ensure(log=lambda m: print("│ " + str(m)), with_deps=args.with_deps, want_browser=not args.no_browser)
    print("└─────────────────────────────────────────────────────────────")
    print(json.dumps({k: res.get(k) for k in ("ready", "ok", "executable", "error")}, indent=2))
    return 0 if res.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
