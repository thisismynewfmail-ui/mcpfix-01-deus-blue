#!/usr/bin/env python3
"""
CHATSGI / TERMINAL — local browser MCP server
Section 9 · self-hosted Playwright · stdio JSON-RPC 2.0

A fully local Model-Context-Protocol server that drives a real, headed Chromium
through Playwright running ON THIS MACHINE — no npx, no Docker, no external
Playwright server. It speaks the same line-delimited JSON-RPC over stdio that
the rest of the terminal's MCP servers use, so the existing MCP bridge in
``start.py`` spawns, toggles and tears it down exactly like any other server.

Highlights
----------
  • Persistent browser context — cookies / storage / logins survive across runs
    (stored project-local under ./.browser/profile).
  • ALWAYS opens with an explicit viewport. Headed (visible) by default; the
    resolution + headless flag + idle auto-close duration come from the terminal
    Settings panel (passed in as BROWSER_* env vars by start.py).
  • Accessibility-style page snapshots with stable element refs, plus a COMPACT
    snapshot variant for context-frugal agent loops.
  • Self-healing install: if the Chromium engine is missing it runs
    ``browser_setup.ensure()`` on first use.
  • Clean shutdown: closes the browser on stdin-EOF (parent went away), on
    SIGTERM/SIGINT, and at interpreter exit — so toggling the server off in the
    UI fully tears the browser down on Windows and Linux alike.

Configuration (environment, all optional)
-----------------------------------------
  BROWSER_WIDTH / BROWSER_HEIGHT   viewport size            (default 1280×800)
  BROWSER_HEADLESS                 "1" hides the window     (default "0" = visible)
  BROWSER_DURATION                 idle seconds before auto-closing the window;
                                   0 = stay open until toggled off  (default 0)
  BROWSER_SNAPSHOT                 auto snapshot after actions:
                                   "compact" | "full" | "off"   (default compact)
  BROWSER_USER_DATA_DIR            persistent profile dir
  BROWSER_SCREENSHOT_DIR           where screenshots are saved
  PLAYWRIGHT_BROWSERS_PATH         engine location (project-local)
  BROWSER_EXECUTABLE               explicit Chromium path override (advanced)
"""

from __future__ import annotations

import atexit
import json
import os
import queue
import signal
import sys
import threading
import time
import traceback

# browser_setup is stdlib-only at import time (playwright is imported lazily),
# so importing it here keeps this module importable even before install.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import browser_setup  # noqa: E402

PROTOCOL_VERSION = "2024-11-05"
SERVER_NAME = "chatsgi-browser"
SERVER_VERSION = "1.0"


def log(*a) -> None:
    """Diagnostics go to stderr — the MCP bridge surfaces the tail in the UI."""
    try:
        sys.stderr.write("[browser] " + " ".join(str(x) for x in a) + "\n")
        sys.stderr.flush()
    except Exception:
        pass


# ───────────────────────── Bot-detection prevention ─────────────────────────
# A persistent profile already does most of the heavy lifting (real cookies,
# storage, a warmed-up fingerprint, surviving logins). On top of that we strip
# the obvious automation tells so account / secure sites don't block the agent.
# The init script runs in EVERY page/frame before any site script, so coverage
# is adaptive across navigations and new tabs.

STEALTH_ARGS = [
    "--disable-blink-features=AutomationControlled",
    "--disable-features=IsolateOrigins,site-per-process,AutomationControlled",
    "--disable-infobars",
    "--no-first-run",
    "--no-default-browser-check",
    "--password-store=basic",
    "--use-mock-keychain",
]

STEALTH_JS = r"""
() => {
  const patch = (obj, prop, val) => { try { Object.defineProperty(obj, prop, { get: () => val, configurable: true }); } catch(e){} };
  // The single biggest tell.
  patch(navigator, 'webdriver', undefined);
  // Plausible language + plugin surface.
  patch(navigator, 'languages', ['en-US', 'en']);
  patch(navigator, 'plugins', [1,2,3,4,5]);
  patch(navigator, 'hardwareConcurrency', navigator.hardwareConcurrency || 8);
  patch(navigator, 'deviceMemory', navigator.deviceMemory || 8);
  // chrome runtime object expected on real Chrome.
  if (!window.chrome) window.chrome = {};
  if (!window.chrome.runtime) window.chrome.runtime = {};
  // Notifications permission consistency.
  try {
    const q = window.navigator.permissions && window.navigator.permissions.query;
    if (q) window.navigator.permissions.query = (p) =>
      (p && p.name === 'notifications')
        ? Promise.resolve({ state: (typeof Notification!=='undefined' ? Notification.permission : 'default') })
        : q(p);
  } catch(e){}
  // WebGL vendor/renderer (headless leaks "Google SwiftShader").
  try {
    const spoof = (proto) => {
      if (!proto) return;
      const gp = proto.getParameter;
      proto.getParameter = function(p){
        if (p === 37445) return 'Intel Inc.';
        if (p === 37446) return 'Intel Iris OpenGL Engine';
        return gp.call(this, p);
      };
    };
    spoof(window.WebGLRenderingContext && WebGLRenderingContext.prototype);
    spoof(window.WebGL2RenderingContext && WebGL2RenderingContext.prototype);
  } catch(e){}
}
"""


def _default_user_agent() -> str:
    """A clean, modern desktop-Chrome UA for the host OS (used in headless mode
    where the engine otherwise leaks 'HeadlessChrome'). Override with BROWSER_UA."""
    override = os.environ.get("BROWSER_UA")
    if override:
        return override
    ver = "131.0.0.0"
    if sys.platform.startswith("win"):
        plat = "Windows NT 10.0; Win64; x64"
    elif sys.platform == "darwin":
        plat = "Macintosh; Intel Mac OS X 10_15_7"
    else:
        plat = "X11; Linux x86_64"
    return (f"Mozilla/5.0 ({plat}) AppleWebKit/537.36 (KHTML, like Gecko) "
            f"Chrome/{ver} Safari/537.36")


# ───────────────────────── Page snapshot (in-page JS) ─────────────────────────
# Walks the rendered DOM and emits an indented, accessibility-flavoured outline.
# Interactive elements get a stable [ref=eN] (written as a data-attribute) that
# the action tools resolve back to a selector. Refs are reset at the start of
# every snapshot, so the workflow is: snapshot → act on a ref → re-snapshot.

SNAPSHOT_JS = r"""
(opts) => {
  const compact = !!(opts && opts.compact);
  const maxNodes = (opts && opts.max) || (compact ? 250 : 1600);
  for (const el of document.querySelectorAll('[data-mcpref]')) el.removeAttribute('data-mcpref');
  const lines = [];
  let refCount = 0, nodeCount = 0;
  const SKIP = new Set(['SCRIPT','STYLE','NOSCRIPT','TEMPLATE','SVG','PATH','LINK','META','HEAD']);
  const INTERACTIVE_TAGS = new Set(['A','BUTTON','INPUT','SELECT','TEXTAREA','SUMMARY','OPTION']);
  const INTERACTIVE_ROLES = new Set(['button','link','checkbox','radio','tab','menuitem','menuitemcheckbox','menuitemradio','switch','textbox','combobox','option','slider','searchbox']);
  const LANDMARK_TAGS = new Set(['NAV','MAIN','HEADER','FOOTER','ASIDE','FORM','SECTION','ARTICLE']);
  function visible(el){
    const s = getComputedStyle(el);
    if (s.display==='none' || s.visibility==='hidden') return false;
    if (!el.getClientRects().length) return false;
    const b = el.getBoundingClientRect();
    if (b.width<=0 && b.height<=0) return false;
    return true;
  }
  function roleOf(el){
    const explicit = el.getAttribute('role');
    if (explicit) return explicit;
    const tag = el.tagName;
    if (/^H[1-6]$/.test(tag)) return 'heading';
    switch(tag){
      case 'A': return el.hasAttribute('href') ? 'link' : 'generic';
      case 'BUTTON': return 'button';
      case 'SELECT': return 'combobox';
      case 'TEXTAREA': return 'textbox';
      case 'IMG': return 'image';
      case 'NAV': return 'navigation';
      case 'MAIN': return 'main';
      case 'HEADER': return 'banner';
      case 'FOOTER': return 'contentinfo';
      case 'FORM': return 'form';
      case 'OPTION': return 'option';
      case 'LI': return 'listitem';
      case 'UL': case 'OL': return 'list';
      case 'TABLE': return 'table';
      case 'INPUT': {
        const t = (el.getAttribute('type')||'text').toLowerCase();
        if (t==='checkbox') return 'checkbox';
        if (t==='radio') return 'radio';
        if (t==='submit'||t==='button'||t==='reset') return 'button';
        if (t==='range') return 'slider';
        if (t==='search') return 'searchbox';
        if (t==='hidden') return 'hidden';
        return 'textbox';
      }
    }
    return 'generic';
  }
  // Container/landmark roles enumerate their children, so don't also fold the
  // concatenated descendant text into their name — that's just noise.
  const CONTAINER_ROLES = new Set(['navigation','main','banner','contentinfo','form','list','listitem',
    'region','article','table','row','rowgroup','group','generic','search','dialog','tablist','menu','menubar']);
  function nameOf(el, role){
    let t = el.getAttribute('aria-label') || '';
    if (!t && el.getAttribute('aria-labelledby')){
      t = el.getAttribute('aria-labelledby').split(/\s+/).map(id => {
        const e = document.getElementById(id); return e ? e.textContent : '';
      }).join(' ').trim();
    }
    if (!t) t = el.getAttribute('alt') || el.getAttribute('title') || '';
    if (!t && (role==='textbox'||role==='searchbox'||role==='combobox')) t = el.getAttribute('placeholder') || '';
    // Container roles WITH children skip the folded descendant text (noise);
    // leaf elements (no children) still surface their own text.
    if (!t && !(CONTAINER_ROLES.has(role) && el.childElementCount > 0)){
      const tag = el.tagName;
      if (tag==='INPUT'){
        const ty=(el.getAttribute('type')||'').toLowerCase();
        if (ty==='submit'||ty==='button'||ty==='reset') t = el.value||'';
      } else if (tag!=='IMG'){
        t = (el.textContent||'').replace(/\s+/g,' ').trim();
      }
    }
    t = (t||'').replace(/\s+/g,' ').trim();
    if (t.length>100) t = t.slice(0,99)+'…';
    return t;
  }
  function interactive(el, role){
    if (INTERACTIVE_TAGS.has(el.tagName)) return true;
    if (INTERACTIVE_ROLES.has(role)) return true;
    if (el.hasAttribute('onclick')) return true;
    if (el.getAttribute('contenteditable')==='true') return true;
    const ti = el.getAttribute('tabindex');
    if (ti!==null && ti!=='-1') return true;
    return false;
  }
  function walk(el, depth){
    if (nodeCount >= maxNodes) return;
    const kids = el.children;
    for (let i=0;i<kids.length;i++){
      if (nodeCount >= maxNodes) break;
      const child = kids[i];
      if (SKIP.has(child.tagName)) continue;
      if (!visible(child)) continue;
      const role = roleOf(child);
      if (role==='hidden') continue;
      const isInteractive = interactive(child, role);
      const isHeading = role==='heading';
      const isLandmark = LANDMARK_TAGS.has(child.tagName) ||
        ['navigation','main','banner','contentinfo','form','search','region','dialog'].includes(role);
      const leafText = child.childElementCount===0 && (child.textContent||'').trim().length>0;
      const include = compact ? (isInteractive || isHeading || isLandmark)
                              : (role!=='generic' || isInteractive || leafText);
      let childDepth = depth;
      if (include){
        const name = nameOf(child, role);
        let ref = '';
        if (isInteractive){ ref = 'e'+(++refCount); child.setAttribute('data-mcpref', ref); }
        let line = '  '.repeat(Math.min(depth,14)) + '- ' + role;
        if (name) line += ' "'+name+'"';
        if (ref) line += ' [ref='+ref+']';
        if (child.tagName==='A'){ const h=child.getAttribute('href'); if (h) line += ' href='+h.slice(0,120); }
        if (child.tagName==='INPUT'){
          const ty=(child.getAttribute('type')||'text').toLowerCase();
          if (ty==='checkbox'||ty==='radio') line += child.checked?' [checked]':' [unchecked]';
          else if (child.value) line += ' value="'+String(child.value).slice(0,40)+'"';
          if (child.disabled) line += ' [disabled]';
        }
        lines.push(line);
        nodeCount++;
        childDepth = depth+1;
      }
      walk(child, childDepth);
    }
  }
  walk(document.body || document.documentElement, 0);
  const head = (nodeCount >= maxNodes)
    ? '(snapshot truncated at '+maxNodes+' nodes)\n' : '';
  return head + lines.join('\n');
}
"""


# ─────────────────────────── Tool catalogue ───────────────────────────

def _tool(name, description, props=None, required=None):
    return {
        "name": name,
        "description": description,
        "inputSchema": {
            "type": "object",
            "properties": props or {},
            "required": required or [],
            "additionalProperties": False,
        },
    }


TOOLS = [
    _tool("browser_navigate",
          "Open a URL in the local browser (launches a real headed Chromium with a viewport on first use). "
          "Returns a compact page snapshot whose interactive elements carry [ref=eN] handles for the other tools.",
          {"url": {"type": "string", "description": "Absolute URL, e.g. https://example.com"},
           "wait": {"type": "string", "enum": ["load", "domcontentloaded", "networkidle"],
                    "description": "When to consider navigation done (default domcontentloaded)."}},
          ["url"]),
    _tool("browser_snapshot",
          "Capture a FULL accessibility-style snapshot of the current page: roles, names and [ref=eN] handles "
          "for every visible element. Use this to discover what is on the page before acting.",
          {}),
    _tool("browser_snapshot_compact",
          "Capture a COMPACT snapshot — only interactive controls, headings and landmarks — to save context "
          "tokens while still exposing [ref=eN] handles you can click / type into.",
          {}),
    _tool("browser_click",
          "Click an element by its snapshot ref.",
          {"ref": {"type": "string", "description": "Element ref from a snapshot, e.g. e12"},
           "doubleClick": {"type": "boolean", "description": "Double-click instead of single (default false)."}},
          ["ref"]),
    _tool("browser_type",
          "Type text into an input/textbox/contenteditable by ref. Replaces existing content. "
          "Set submit=true to press Enter afterwards (e.g. to run a search).",
          {"ref": {"type": "string"},
           "text": {"type": "string"},
           "submit": {"type": "boolean", "description": "Press Enter after typing (default false)."}},
          ["ref", "text"]),
    _tool("browser_press_key",
          "Press a single keyboard key on the page (e.g. Enter, Escape, ArrowDown, PageDown, Tab).",
          {"key": {"type": "string"}},
          ["key"]),
    _tool("browser_hover",
          "Hover the mouse over an element by ref (reveals menus / tooltips).",
          {"ref": {"type": "string"}},
          ["ref"]),
    _tool("browser_select_option",
          "Select option(s) in a <select> by ref, matching value or visible label.",
          {"ref": {"type": "string"},
           "values": {"type": "array", "items": {"type": "string"}}},
          ["ref", "values"]),
    _tool("browser_scroll",
          "Scroll the page.",
          {"direction": {"type": "string", "enum": ["down", "up", "top", "bottom"],
                         "description": "Default down."},
           "amount": {"type": "integer", "description": "Pixels for up/down (default 600)."}}),
    _tool("browser_navigate_back", "Go back to the previous page in history.", {}),
    _tool("browser_navigate_forward", "Go forward to the next page in history.", {}),
    _tool("browser_wait",
          "Wait — either a fixed number of seconds, or until some text appears on the page.",
          {"seconds": {"type": "number", "description": "Seconds to wait (max 30)."},
           "text": {"type": "string", "description": "Wait until this text becomes visible."}}),
    _tool("browser_get_text",
          "Return the visible plain-text content of the current page (truncated).",
          {}),
    _tool("browser_screenshot",
          "Save a PNG screenshot of the current page to the local screenshots folder and return its path.",
          {"fullPage": {"type": "boolean", "description": "Capture the full scrollable page (default false)."}}),
    _tool("browser_eval",
          "Run a JavaScript expression in the page and return its JSON result. The script is the body of "
          "a function, e.g. 'return document.title'. Advanced / debugging use.",
          {"script": {"type": "string"}},
          ["script"]),
    _tool("browser_tabs",
          "Manage browser tabs: list them, open a new one, switch to one, or close one.",
          {"action": {"type": "string", "enum": ["list", "new", "select", "close"]},
           "index": {"type": "integer", "description": "Tab index for select/close."},
           "url": {"type": "string", "description": "URL for the new tab."}},
          ["action"]),
    _tool("browser_close",
          "Close the browser window/context now (frees resources). It will relaunch automatically on the next "
          "navigate. Use this when finished browsing.",
          {}),
]

TOOL_NAMES = {t["name"] for t in TOOLS}


# ─────────────────────────── Browser manager ───────────────────────────

class Browser:
    """Owns the Playwright instance + persistent context. Single-threaded:
    every method here runs on the main thread (Playwright sync API is thread
    bound), driven by the request loop in main()."""

    def __init__(self):
        self.width = _int_env("BROWSER_WIDTH", 1280)
        self.height = _int_env("BROWSER_HEIGHT", 800)
        self.headless = os.environ.get("BROWSER_HEADLESS", "0") in ("1", "true", "True", "yes")
        self.idle_seconds = _int_env("BROWSER_DURATION", 0)
        self.snapshot_mode = (os.environ.get("BROWSER_SNAPSHOT", "compact") or "compact").lower()
        if self.snapshot_mode not in ("compact", "full", "off"):
            self.snapshot_mode = "compact"
        self.profile = os.environ.get("BROWSER_USER_DATA_DIR") or browser_setup.paths()["profile"]
        self.shot_dir = os.environ.get("BROWSER_SCREENSHOT_DIR") or browser_setup.paths()["screenshots"]
        self.executable = os.environ.get("BROWSER_EXECUTABLE") or None

        self._pw = None
        self.ctx = None
        self.page = None
        self.deadline = None              # idle auto-close epoch (None = never)
        self._headless_fallback = False

    # ---- lifecycle ----
    def _ensure_engine(self):
        if browser_setup.is_ready() or (self.executable and os.path.exists(self.executable)):
            return
        log("Chromium engine missing — running browser_setup.ensure() …")
        browser_setup.ensure(log=log)
        # playwright may have just been pip-installed into this live interpreter.
        import importlib
        importlib.invalidate_caches()
        if not browser_setup.is_ready() and not (self.executable and os.path.exists(self.executable)):
            raise RuntimeError(
                "Chromium engine is not installed. Open Settings → BROWSER → INSTALL ENGINE, "
                "or run `python browser_setup.py` in the project folder.")

    def _launch_kwargs(self, headless: bool, exe: str | None) -> dict:
        args = list(STEALTH_ARGS)
        # --no-sandbox is intentionally NOT used by default: Chromium warns that
        # "stability and security will suffer", and on a normal user account the
        # sandbox works fine. Opt in only via BROWSER_NO_SANDBOX=1 for the rare
        # case of running as root in a container where Chromium won't otherwise start.
        if os.environ.get("BROWSER_NO_SANDBOX", "") in ("1", "true", "yes"):
            args.append("--no-sandbox")
        if not headless:
            args.append(f"--window-size={self.width},{self.height}")
        kw = dict(
            user_data_dir=self.profile,
            headless=headless,
            viewport={"width": self.width, "height": self.height},
            args=args,
            ignore_default_args=["--enable-automation"],
            locale=os.environ.get("BROWSER_LOCALE", "en-US"),
            bypass_csp=True,
            # Headed Chromium already reports a real Chrome UA; only override in
            # headless (where it leaks "HeadlessChrome") or when forced via env.
            user_agent=(_default_user_agent() if (headless or os.environ.get("BROWSER_UA")) else None),
            extra_http_headers={"Accept-Language": "en-US,en;q=0.9"},
        )
        tz = os.environ.get("BROWSER_TZ")
        if tz:
            kw["timezone_id"] = tz
        if exe and os.path.exists(exe):
            kw["executable_path"] = exe
        return {k: v for k, v in kw.items() if v is not None}

    def ensure(self):
        """Launch the persistent context if it isn't running, and refresh the idle timer."""
        if self.ctx is not None:
            self._touch()
            return
        self._ensure_engine()
        from playwright.sync_api import sync_playwright

        os.makedirs(self.profile, exist_ok=True)
        os.makedirs(self.shot_dir, exist_ok=True)
        if self._pw is None:
            self._pw = sync_playwright().start()

        exe = self.executable or browser_setup.find_chromium()
        try:
            self.ctx = self._pw.chromium.launch_persistent_context(**self._launch_kwargs(self.headless, exe))
            mode = "headless" if self.headless else "headed"
            log(f"launched {mode} {self.width}x{self.height} · profile={self.profile} · exe={exe or 'bundled'}")
        except Exception as e:
            # No display (e.g. a headless Linux box)? Fall back to headless so the
            # tool still functions, but keep the explicit viewport as required.
            if not self.headless:
                log(f"headed launch failed ({e}); retrying headless with same viewport…")
                self.ctx = self._pw.chromium.launch_persistent_context(**self._launch_kwargs(True, exe))
                self._headless_fallback = True
                log(f"launched headless (fallback) {self.width}x{self.height}")
            else:
                raise
        # Adaptive anti-detection: applied to every current and future page/frame.
        try:
            self.ctx.add_init_script(STEALTH_JS)
        except Exception as e:
            log(f"could not install stealth init script: {e}")
        self.ctx.set_default_timeout(20000)
        self.page = self.ctx.pages[0] if self.ctx.pages else self.ctx.new_page()
        self._touch()

    def _touch(self):
        self.deadline = (time.time() + self.idle_seconds) if self.idle_seconds and self.idle_seconds > 0 else None

    def tick(self):
        """Called from the idle loop — close the window after the idle duration."""
        if self.ctx is not None and self.deadline is not None and time.time() >= self.deadline:
            log(f"idle for {self.idle_seconds}s — auto-closing browser window.")
            self._close_browser()

    def _close_browser(self):
        self.deadline = None
        if self.ctx is not None:
            try:
                self.ctx.close()
            except Exception:
                pass
        self.ctx = None
        self.page = None

    def shutdown(self):
        self._close_browser()
        if self._pw is not None:
            try:
                self._pw.stop()
            except Exception:
                pass
            self._pw = None

    # ---- helpers ----
    def _loc(self, ref: str):
        if not ref:
            raise ValueError("missing 'ref'")
        return self.page.locator(f"[data-mcpref={json.dumps(ref)}]")

    def _snapshot(self, compact: bool) -> str:
        try:
            return self.page.evaluate(SNAPSHOT_JS, {"compact": compact}) or "(empty page)"
        except Exception as e:
            return f"(snapshot unavailable: {e})"

    def _page_header(self) -> list[str]:
        url = title = ""
        try:
            url = self.page.url
        except Exception:
            pass
        try:
            title = self.page.title()
        except Exception:
            pass
        out = [f"- URL: {url}", f"- Title: {title}"]
        n = 0
        try:
            n = len(self.ctx.pages)
        except Exception:
            pass
        if n > 1:
            out.append(f"- Tabs open: {n}")
        if self._headless_fallback:
            out.append("- NOTE: running headless (no display available); viewport still applied.")
        return out

    def _result(self, header: str, want_snapshot: bool = True) -> str:
        parts = [header] + self._page_header()
        if want_snapshot and self.snapshot_mode != "off":
            parts.append("")
            parts.append("### Page snapshot")
            parts.append(self._snapshot(self.snapshot_mode == "compact"))
        return "\n".join(parts)

    # ---- tools ----
    def navigate(self, args):
        url = (args.get("url") or "").strip()
        if not url:
            raise ValueError("missing 'url'")
        url = _normalize_url(url)
        self.ensure()
        wait = args.get("wait") or "domcontentloaded"
        try:
            self.page.goto(url, wait_until=wait, timeout=30000)
        except Exception as e:
            # Soft-fail: navigation may still have committed (slow assets); report and snapshot.
            return self._result(f"⚠ navigation to {url} reported: {e}")
        return self._result(f"✓ navigated to {url}")

    def snapshot(self, args):
        self.ensure()
        return "\n".join([f"✓ full snapshot"] + self._page_header() + ["", self._snapshot(False)])

    def snapshot_compact(self, args):
        self.ensure()
        return "\n".join([f"✓ compact snapshot"] + self._page_header() + ["", self._snapshot(True)])

    def click(self, args):
        self.ensure()
        ref = args.get("ref")
        loc = self._loc(ref)
        if args.get("doubleClick"):
            loc.dblclick(timeout=15000)
            verb = "double-clicked"
        else:
            loc.click(timeout=15000)
            verb = "clicked"
        self.page.wait_for_timeout(250)
        return self._result(f"✓ {verb} {ref}")

    def type(self, args):
        self.ensure()
        ref = args.get("ref")
        text = args.get("text", "")
        loc = self._loc(ref)
        try:
            loc.fill(text, timeout=15000)
        except Exception:
            # contenteditable / non-fillable: click then type
            loc.click(timeout=10000)
            self.page.keyboard.type(text)
        if args.get("submit"):
            loc.press("Enter")
            self.page.wait_for_timeout(400)
            return self._result(f"✓ typed into {ref} and pressed Enter")
        return self._result(f"✓ typed into {ref}")

    def press_key(self, args):
        self.ensure()
        key = args.get("key") or ""
        if not key:
            raise ValueError("missing 'key'")
        self.page.keyboard.press(key)
        self.page.wait_for_timeout(200)
        return self._result(f"✓ pressed {key}")

    def hover(self, args):
        self.ensure()
        ref = args.get("ref")
        self._loc(ref).hover(timeout=15000)
        self.page.wait_for_timeout(200)
        return self._result(f"✓ hovered {ref}")

    def select_option(self, args):
        self.ensure()
        ref = args.get("ref")
        values = args.get("values") or []
        chosen = self._loc(ref).select_option(values, timeout=15000)
        return self._result(f"✓ selected {chosen} in {ref}")

    def scroll(self, args):
        self.ensure()
        direction = (args.get("direction") or "down").lower()
        amount = int(args.get("amount") or 600)
        if direction == "top":
            self.page.evaluate("() => window.scrollTo(0, 0)")
        elif direction == "bottom":
            self.page.evaluate("() => window.scrollTo(0, document.body.scrollHeight)")
        elif direction == "up":
            self.page.evaluate("(y) => window.scrollBy(0, -y)", amount)
        else:
            self.page.evaluate("(y) => window.scrollBy(0, y)", amount)
        self.page.wait_for_timeout(200)
        return self._result(f"✓ scrolled {direction}")

    def navigate_back(self, args):
        self.ensure()
        self.page.go_back(timeout=20000)
        return self._result("✓ navigated back")

    def navigate_forward(self, args):
        self.ensure()
        self.page.go_forward(timeout=20000)
        return self._result("✓ navigated forward")

    def wait(self, args):
        self.ensure()
        text = args.get("text")
        if text:
            self.page.get_by_text(text, exact=False).first.wait_for(timeout=30000)
            return self._result(f"✓ text appeared: {text!r}")
        secs = float(args.get("seconds") or 1)
        secs = max(0.0, min(30.0, secs))
        self.page.wait_for_timeout(int(secs * 1000))
        return self._result(f"✓ waited {secs}s")

    def get_text(self, args):
        self.ensure()
        txt = self.page.evaluate("() => document.body ? document.body.innerText : ''") or ""
        if len(txt) > 6000:
            txt = txt[:6000] + f"\n…(+{len(txt) - 6000} more chars truncated)"
        return "\n".join(["✓ page text"] + self._page_header() + ["", txt])

    def screenshot(self, args):
        self.ensure()
        os.makedirs(self.shot_dir, exist_ok=True)
        path = os.path.join(self.shot_dir, f"shot-{int(time.time() * 1000)}.png")
        self.page.screenshot(path=path, full_page=bool(args.get("fullPage")))
        return "\n".join([f"✓ screenshot saved", f"- File: {path}"] + self._page_header())

    def eval(self, args):
        self.ensure()
        script = args.get("script") or ""
        if not script.strip():
            raise ValueError("missing 'script'")
        body = script if script.strip().startswith("return") or "\n" in script else f"return ({script})"
        try:
            result = self.page.evaluate(f"() => {{ {body} }}")
        except Exception:
            result = self.page.evaluate(f"() => ({script})")
        try:
            out = json.dumps(result, ensure_ascii=False, default=str)
        except Exception:
            out = str(result)
        if len(out) > 4000:
            out = out[:4000] + "…(truncated)"
        return f"✓ eval result:\n{out}"

    def tabs(self, args):
        self.ensure()
        action = args.get("action") or "list"
        if action == "new":
            self.page = self.ctx.new_page()
            url = args.get("url")
            if url:
                self.page.goto(_normalize_url(url), wait_until="domcontentloaded", timeout=30000)
            return self._result("✓ opened new tab")
        if action == "select":
            idx = int(args.get("index", 0))
            pages = self.ctx.pages
            if idx < 0 or idx >= len(pages):
                raise ValueError(f"tab index {idx} out of range (0..{len(pages)-1})")
            self.page = pages[idx]
            self.page.bring_to_front()
            return self._result(f"✓ switched to tab {idx}")
        if action == "close":
            idx = int(args.get("index", 0))
            pages = self.ctx.pages
            if idx < 0 or idx >= len(pages):
                raise ValueError(f"tab index {idx} out of range (0..{len(pages)-1})")
            pages[idx].close()
            self.page = self.ctx.pages[0] if self.ctx.pages else self.ctx.new_page()
            return self._result(f"✓ closed tab {idx}")
        # list
        lines = ["✓ open tabs:"]
        for i, p in enumerate(self.ctx.pages):
            mark = "→" if p is self.page else " "
            t = ""
            try:
                t = p.title()
            except Exception:
                pass
            lines.append(f" {mark} [{i}] {t}  {p.url}")
        return "\n".join(lines)

    def close(self, args):
        self._close_browser()
        return "✓ browser closed. It will relaunch on the next navigate."

    DISPATCH = {
        "browser_navigate": navigate,
        "browser_snapshot": snapshot,
        "browser_snapshot_compact": snapshot_compact,
        "browser_click": click,
        "browser_type": type,
        "browser_press_key": press_key,
        "browser_hover": hover,
        "browser_select_option": select_option,
        "browser_scroll": scroll,
        "browser_navigate_back": navigate_back,
        "browser_navigate_forward": navigate_forward,
        "browser_wait": wait,
        "browser_get_text": get_text,
        "browser_screenshot": screenshot,
        "browser_eval": eval,
        "browser_tabs": tabs,
        "browser_close": close,
    }

    def call(self, name: str, args: dict) -> str:
        fn = self.DISPATCH.get(name)
        if fn is None:
            raise ValueError(f"unknown tool '{name}'")
        return fn(self, args or {})


def _int_env(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, "") or default)
    except Exception:
        return default


_SCHEME_RE = __import__("re").compile(r"^[a-zA-Z][a-zA-Z0-9+.\-]*:")

def _normalize_url(url: str) -> str:
    """Add https:// to bare hosts, but leave real schemes (data:, file:, about:, http:) alone."""
    url = (url or "").strip()
    if not url:
        return url
    if _SCHEME_RE.match(url):
        return url
    return "https://" + url


# ─────────────────────────── JSON-RPC plumbing ───────────────────────────

def _write(obj: dict) -> None:
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()


def _result_msg(rid, result):
    return {"jsonrpc": "2.0", "id": rid, "result": result}


def _error_msg(rid, code, message):
    return {"jsonrpc": "2.0", "id": rid, "error": {"code": code, "message": message}}


def handle(browser: Browser, msg: dict):
    """Return a response dict for requests (those with an id), or None for notifications."""
    method = msg.get("method")
    rid = msg.get("id")
    params = msg.get("params") or {}

    if method == "initialize":
        return _result_msg(rid, {
            "protocolVersion": params.get("protocolVersion", PROTOCOL_VERSION),
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
        })
    if method in ("notifications/initialized", "initialized"):
        return None
    if method == "ping":
        return _result_msg(rid, {})
    if method == "tools/list":
        return _result_msg(rid, {"tools": TOOLS})
    if method == "tools/call":
        name = params.get("name")
        args = params.get("arguments") or {}
        if name not in TOOL_NAMES:
            return _result_msg(rid, {"content": [{"type": "text", "text": f"unknown tool '{name}'"}], "isError": True})
        try:
            text = browser.call(name, args)
            return _result_msg(rid, {"content": [{"type": "text", "text": text}]})
        except Exception as e:
            tb = traceback.format_exc(limit=3)
            log(f"tool error in {name}: {e}\n{tb}")
            hint = ""
            if "data-mcpref" in str(e) or "strict mode" in str(e) or "Timeout" in str(e):
                hint = " · the ref may be stale — call browser_snapshot to refresh element refs."
            return _result_msg(rid, {
                "content": [{"type": "text", "text": f"[browser error] {name}: {e}{hint}"}],
                "isError": True,
            })
    if method in ("shutdown", "exit"):
        return _result_msg(rid, {}) if rid is not None else None
    if rid is not None:
        return _error_msg(rid, -32601, f"method not found: {method}")
    return None


def main():
    browser = Browser()
    _idle = f"{browser.idle_seconds}s" if browser.idle_seconds and browser.idle_seconds > 0 else "never"
    log(f"online · viewport {browser.width}x{browser.height} · "
        f"{'headless' if browser.headless else 'headed'} · "
        f"idle-close {_idle} · snapshot={browser.snapshot_mode}")

    q: queue.Queue = queue.Queue()

    def reader():
        try:
            for raw in sys.stdin.buffer:
                line = raw.decode("utf-8", errors="replace").strip()
                if not line:
                    continue
                try:
                    q.put(("msg", json.loads(line)))
                except Exception:
                    continue
        except Exception:
            pass
        finally:
            q.put(("eof", None))

    threading.Thread(target=reader, name="stdin-reader", daemon=True).start()

    def _graceful(*_):
        q.put(("eof", None))

    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            signal.signal(sig, _graceful)
        except Exception:
            pass
    atexit.register(browser.shutdown)

    while True:
        try:
            kind, msg = q.get(timeout=1.0)
        except queue.Empty:
            try:
                browser.tick()    # idle auto-close watchdog
            except Exception:
                pass
            continue
        if kind == "eof":
            break
        try:
            resp = handle(browser, msg)
        except Exception as e:
            resp = _error_msg(msg.get("id"), -32603, f"internal error: {e}")
        if resp is not None:
            try:
                _write(resp)
            except (BrokenPipeError, OSError):
                break

    log("shutting down — closing browser…")
    browser.shutdown()


if __name__ == "__main__":
    main()
