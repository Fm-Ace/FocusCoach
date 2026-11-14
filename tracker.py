"""Everything that looks at the computer.

- which window is active, and for how long the user hasn't touched anything
- the optional browser extension that reports the exact tab title and site
- making sure only one copy of FocusCoach runs

Nothing in this file writes to the database. It only answers the question
"what is the user doing right now?" (see get_current_activity).
"""

from __future__ import annotations

import json
import logging
import os
import re
import secrets
import socketserver
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

import psutil
import pywintypes
import win32api
import win32event
import win32gui
import win32process
import winerror

from activity_log import Activity

logger = logging.getLogger(__name__)

HOST = "127.0.0.1"
PORT = 8765
IDLE_AFTER = 120  # Seconds without keyboard/mouse input before we call it idle.
MAX_REQUEST_SIZE = 16_384
MAX_TITLE_LENGTH = 300
MAX_DOMAIN_LENGTH = 253
MUTEX_NAME = "FocusCoach_SingleInstance"

# The extension must send this token. Set the FOCUSCOACH_TOKEN environment
# variable to use your own; the default is only fine on a single-user PC.
API_TOKEN = os.environ.get("FOCUSCOACH_TOKEN", "focuscoach-local-dev-token")

# Only extensions may read our replies from a web page's point of view.
EXTENSION_ORIGINS = ("moz-extension://", "chrome-extension://")

PROTECTED_APP = "Protected app"  # Windows won't tell us the name (admin apps).
UWP_HOST = "applicationframehost.exe"

# Process name -> the name we log.
BROWSERS = {
    "zen.exe": "Zen",
    "firefox.exe": "Firefox",
    "chrome.exe": "Chrome",
    "msedge.exe": "Edge",
    "brave.exe": "Brave",
}

# What each browser adds to the end of its window title ("Page — Zen Browser").
WINDOW_TITLE_SUFFIX = {
    "Zen": "Zen Browser",
    "Firefox": "Mozilla Firefox",
    "Chrome": "Google Chrome",
    "Edge": "Microsoft Edge",
    "Brave": "Brave",
}


# ------------------------------------------------------------ single instance

_instance_mutex = None  # Kept alive on purpose: Windows frees it when we exit.


def another_copy_is_running() -> bool:
    """True if FocusCoach is already running.

    Two copies would both write to the database and the second one would
    close the first one's open row, so main() refuses to start.
    """
    global _instance_mutex

    try:
        _instance_mutex = win32event.CreateMutex(None, False, MUTEX_NAME)
        return win32api.GetLastError() == winerror.ERROR_ALREADY_EXISTS
    except pywintypes.error:
        logger.warning("Could not check for another running copy.")
        return False


# ------------------------------------------------------------ the active window


def clean_text(text: str) -> str:
    """One line, single spaces, not absurdly long."""
    return " ".join(text.split())[:MAX_TITLE_LENGTH]


def get_idle_seconds() -> float:
    """Seconds since the last keyboard or mouse input."""
    last_input = win32api.GetLastInputInfo()
    now = win32api.GetTickCount()

    # The tick counter is 32-bit and wraps around, so mask the difference.
    return ((now - last_input) & 0xFFFFFFFF) / 1000


def find_real_uwp_pid(window, host_pid: int) -> int:
    """
    Store apps (Settings, Calculator...) run inside ApplicationFrameHost.
    The real app lives in a child window owned by a different process.
    """
    pids = []

    def collect(child, _):
        _, pid = win32process.GetWindowThreadProcessId(child)

        if pid != host_pid:
            pids.append(pid)

        return True

    win32gui.EnumChildWindows(window, collect, None)

    return pids[0] if pids else host_pid


def get_process_name(pid: int) -> str:
    try:
        return psutil.Process(pid).name()
    except psutil.AccessDenied:
        return PROTECTED_APP


def get_active_window() -> tuple[str, str] | None:
    """Returns (process name, window title), or None if Windows won't say."""
    try:
        window = win32gui.GetForegroundWindow()

        if not window:
            return None

        title = clean_text(win32gui.GetWindowText(window))
        _, process_id = win32process.GetWindowThreadProcessId(window)
        process_name = get_process_name(process_id)

        if process_name.casefold() == UWP_HOST:
            process_name = get_process_name(find_real_uwp_pid(window, process_id))

        return process_name, title or "Untitled window"

    except (pywintypes.error, psutil.Error):
        # The window closed while we were looking at it, or Windows refused.
        return None


def strip_browser_suffix(browser: str, window_title: str) -> str:
    """"Page — Zen Browser" -> "Page". Keeps the title if nothing is left."""
    suffix = re.escape(WINDOW_TITLE_SUFFIX[browser])
    stripped = re.sub(rf"\s+[-—–]\s+{suffix}\s*$", "", window_title, flags=re.IGNORECASE)

    return stripped or window_title


# ------------------------------------------------------- browser extension tabs


class TabStore:
    """The last tab each browser's extension told us about (thread-safe)."""

    def __init__(self):
        self._tabs: dict[str, tuple[str, str]] = {}
        self._lock = threading.Lock()

    def update(self, browser: str, title: str, domain: str) -> None:
        with self._lock:
            self._tabs[browser] = (title, domain)

    def get(self, browser: str) -> tuple[str, str] | None:
        with self._lock:
            return self._tabs.get(browser)


tabs = TabStore()


def tab_matches_window(tab_title: str, window_title: str) -> bool:
    """
    Browsers show the tab title at the start of the window title, followed by
    the browser's name. If the window doesn't start with the stored tab
    title, the stored tab is outdated.
    """
    tab = tab_title.casefold()
    window = window_title.casefold()

    if not tab or not window.startswith(tab):
        return False

    rest = window[len(tab) :]
    return rest == "" or re.match(r"\s+[-—–]\s", rest) is not None


def get_browser_page(browser: str, window_title: str) -> tuple[str, str]:
    """Returns (title, domain) for what the browser is showing."""
    tab = tabs.get(browser)

    if tab and tab_matches_window(tab[0], window_title):
        return tab

    # No fresh tab info, so use what the window itself says.
    return strip_browser_suffix(browser, window_title), ""


# ------------------------------------------------------------- the main answer


def get_current_activity() -> Activity | None:
    """What the user is looking at right now, or None if we can't tell.

    When the user hasn't touched the keyboard or mouse for IDLE_AFTER seconds
    the window is still recorded, but marked idle. That keeps "watching a
    lecture" (idle, but the video is on screen) apart from "away from the
    desk" (idle, nothing useful on screen) for later.
    """
    window = get_active_window()

    if window is None:
        return None

    process_name, window_title = window
    idle = get_idle_seconds() >= IDLE_AFTER
    browser = BROWSERS.get(process_name.casefold())

    if browser:
        title, domain = get_browser_page(browser, window_title)
        return Activity(browser, title, domain, idle)

    app = (
        process_name[:-4]
        if process_name.casefold().endswith(".exe")
        else process_name
    )

    return Activity(app, window_title, "", idle)


# ------------------------------------------------------------------- receiver


class BadRequest(Exception):
    """The request is refused. `status` is the HTTP code sent back."""

    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


def get_domain(url: str) -> str:
    """The site's host name, or "" for pages that aren't websites."""
    try:
        parts = urlsplit(url.strip())

        if parts.scheme not in ("http", "https"):
            return ""

        domain = parts.hostname or ""
        _ = parts.port  # Also checks that a supplied port is valid.
    except ValueError:
        raise BadRequest(400, "invalid URL") from None

    if not domain or len(domain) > MAX_DOMAIN_LENGTH:
        raise BadRequest(400, "invalid URL host")

    return domain


def parse_tab(data) -> tuple[str, str, str]:
    """Check the extension's JSON and return (browser, title, domain)."""
    if not isinstance(data, dict):
        raise BadRequest(400, "expected an object")

    browser = data.get("browser")
    title = data.get("title")
    url = data.get("url")

    if not all(isinstance(value, str) for value in (browser, title, url)):
        raise BadRequest(400, "invalid fields")

    if browser not in WINDOW_TITLE_SUFFIX:
        raise BadRequest(400, "unsupported browser")

    if not url.strip():
        raise BadRequest(400, "invalid URL")

    return browser, clean_text(title) or "Untitled tab", get_domain(url)


class TabReceiver(BaseHTTPRequestHandler):
    """A tiny local web server the browser extension sends the open tab to."""

    timeout = 5

    def reply(self, status: int, data: dict | None = None, headers: dict | None = None):
        body = json.dumps(data).encode("utf-8") if data is not None else b""

        try:
            self.send_response(status)

            if data is not None:
                self.send_header("Content-Type", "application/json; charset=utf-8")

            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")

            for name, value in {**self.cors_headers(), **(headers or {})}.items():
                self.send_header(name, value)

            self.end_headers()
            self.wfile.write(body)
        except OSError:
            pass  # The client went away; nothing to do.

    def cors_headers(self) -> dict:
        """Let extensions (and nothing else) read our replies."""
        origin = self.headers.get("Origin", "")

        if origin.startswith(EXTENSION_ORIGINS):
            return {"Access-Control-Allow-Origin": origin, "Vary": "Origin"}

        return {}

    def check_host(self) -> None:
        """Refuse requests meant for another site (DNS rebinding attacks)."""
        port = self.server.server_address[1]
        allowed = {f"127.0.0.1:{port}", f"localhost:{port}"}

        if self.headers.get("Host", "").lower() not in allowed:
            raise BadRequest(403, "invalid host")

    def check_token(self) -> None:
        sent = self.headers.get("X-FocusCoach-Token", "").encode("utf-8")

        if not secrets.compare_digest(sent, API_TOKEN.encode("utf-8")):
            raise BadRequest(403, "invalid token")

    def read_json(self):
        try:
            length = int(self.headers.get("Content-Length", ""))
        except ValueError:
            raise BadRequest(400, "invalid content length") from None

        if length <= 0:
            raise BadRequest(400, "empty request body")

        if length > MAX_REQUEST_SIZE:
            raise BadRequest(413, "request body too large")

        try:
            body = self.rfile.read(length)
        except OSError:
            raise BadRequest(400, "could not read request body") from None

        if len(body) != length:
            raise BadRequest(400, "incomplete request body")

        try:
            return json.loads(body)
        except (ValueError, RecursionError):  # ValueError covers bad JSON/UTF-8.
            raise BadRequest(400, "invalid JSON") from None

    def refuse(self, error: BadRequest) -> None:
        logger.warning("Refused request from %s: %s", self.client_address[0], error.message)
        self.reply(error.status, {"error": error.message})

    def do_OPTIONS(self):
        """The "may I send this?" question browsers ask before a POST."""
        headers = {}

        if self.cors_headers():
            headers = {
                "Access-Control-Allow-Methods": "GET, POST",
                "Access-Control-Allow-Headers": "Content-Type, X-FocusCoach-Token",
                "Access-Control-Max-Age": "600",
            }

        self.reply(204, headers=headers)

    def do_GET(self):
        try:
            self.check_host()

            if self.path != "/health":
                raise BadRequest(404, "not found")
        except BadRequest as error:
            self.refuse(error)
            return

        self.reply(200, {"status": "running"})

    def do_POST(self):
        try:
            if self.path != "/active-tab":
                raise BadRequest(404, "not found")

            self.check_host()
            self.check_token()
            browser, title, domain = parse_tab(self.read_json())
        except BadRequest as error:
            self.refuse(error)
            return

        tabs.update(browser, title, domain)
        self.reply(200, {"received": True})

    def log_message(self, format_string, *args):
        pass  # Normal requests aren't interesting; refusals are logged above.


class ReceiverServer(ThreadingHTTPServer):
    """ThreadingHTTPServer without the slow host-name lookup.

    The standard server_bind() asks DNS for the "real" name of 127.0.0.1
    (socket.getfqdn). On some Windows setups that lookup stalls for a long
    time, and the program seems frozen at startup. We never use the name.
    """

    def server_bind(self):
        socketserver.TCPServer.server_bind(self)
        self.server_name = HOST
        self.server_port = self.server_address[1]


def start_receiver(port: int = PORT) -> tuple[ThreadingHTTPServer, threading.Thread]:
    """Start the receiver in a background thread. Returns (server, thread).

    Raises OSError if the port is taken.
    """
    server = ReceiverServer((HOST, port), TabReceiver)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    return server, thread


def stop_receiver(server: ThreadingHTTPServer, thread: threading.Thread) -> None:
    try:
        server.shutdown()
    except KeyboardInterrupt:
        logger.warning("Shutdown interrupted; closing the receiver.")
    finally:
        server.server_close()
        thread.join(timeout=1)


if __name__ == "__main__":
    raise SystemExit("Run focuscoach.py instead. This file only holds the tracking code.")
