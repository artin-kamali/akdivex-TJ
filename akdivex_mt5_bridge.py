# -*- coding: utf-8 -*-
"""
AKDIVEX — Local MetaTrader 5 Bridge  (v4: multi-terminal switching)
========================================================================
Run this script next to your MetaTrader 5 terminal (on the SAME Windows
computer). No login/password or account number is needed — MetaTrader 5
just needs to be open and logged in. This script connects to that already
open terminal using the official MetaTrader5 package, and shares your
account info, open positions and closed-trade history with the AKDIVEX
journal (in your browser) over a local address (http://127.0.0.1:8787).
From here you can also close a trade or change its Stop Loss / Take
Profit directly from the journal.

EASIEST WAY TO RUN THIS (recommended, no typing required):
    Just double-click "AKDIVEX_Start.bat" in this same folder.
    It checks everything for you and opens this script automatically.

MANUAL WAY (if you prefer the command line):
    Install once:   pip install MetaTrader5
    Run:            python akdivex_mt5_bridge.py
    (different port: python akdivex_mt5_bridge.py --port 9000)

Note: you don't even need to install the MetaTrader5 package yourself —
if it's missing, this script installs it automatically the first time
you run it.

RUN AUTOMATICALLY EVERY TIME WINDOWS STARTS (optional):
    The first time you run this script, it will ask you if you'd like
    this. Answer "y" and you're done — you'll never need to open this
    window again (as long as MetaTrader 5 itself is open).
    You can also set this up manually at any time:
        python akdivex_mt5_bridge.py --install-startup
    To remove it again:
        python akdivex_mt5_bridge.py --uninstall-startup

    Important: no website (not this journal, not any other) is allowed
    to — or technically able to — launch a desktop program on your
    computer just because you opened a page in your browser. That's a
    browser security rule, not a limitation of this journal. Installing
    automatic startup is the closest alternative: set it up once, and
    from then on the journal can always connect whenever MetaTrader is
    open.

SECURITY & TECHNICAL NOTES:
  - Windows only (the official MetaTrader5 package is Windows-only), and
    only works with MetaTrader 5.
  - This address is only reachable from this same computer (127.0.0.1),
    never from your network or the internet.
  - On first run, a random "security token" is created and saved next to
    this script (in akdivex_token.txt), and also printed in this window
    AND copied to your clipboard automatically. Paste it once into the
    MetaTrader connection settings inside the journal. Without this
    token, no request (reading data or closing a trade) is accepted —
    this stops some other website you have open from accessing your
    account without permission.
  - For close-trade / change SL-TP commands to actually execute, the
    "AutoTrading" button in the MetaTrader 5 toolbar must be turned on
    (green).

AUTO-SCREENSHOTS (entry / exit chart snapshots for the journal):
  When the journal asks for a screenshot, this script opens the right
  chart for that symbol/timeframe and captures it as-is — it does not
  draw anything itself. So the Stop Loss / Take Profit / entry-price
  lines only show up in the picture if MetaTrader is already drawing
  them, which needs "Show Trade Levels" turned on (Tools > Options >
  Charts tab) — this is MetaTrader's default, so usually nothing to
  change. Once a position is closed, MetaTrader removes those lines by
  itself, so an "exit" screenshot taken after that point won't have them.

MULTIPLE METATRADER INSTALLS ON ONE COMPUTER:
  If you have more than one MetaTrader 5 terminal installed (e.g. one per
  broker), this bridge can see all of them and the journal lets you switch
  between them with one click — no need to close/reopen anything by hand.
"""

import argparse
import base64
import json
import os
import re
import secrets
import subprocess
import sys
import tempfile
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit, parse_qs


def ensure_mt5_package():
    """Import the MetaTrader5 package, installing it automatically if needed."""
    try:
        import MetaTrader5 as mt5_module
        return mt5_module
    except ImportError:
        pass

    print("The 'MetaTrader5' package isn't installed yet.")
    print("Installing it automatically now — this only happens once, please wait...")
    try:
        subprocess.check_call([sys.executable, "-m", "pip", "install", "--quiet", "MetaTrader5"])
    except Exception as e:  # noqa: BLE001
        print(f"Automatic installation failed ({e}).")
        print("Please open Command Prompt and run this yourself:")
        print("    pip install MetaTrader5")
        sys.exit(1)

    try:
        import MetaTrader5 as mt5_module
        print("Installed successfully ✅\n")
        return mt5_module
    except ImportError:
        print("The installation seemed to finish, but the package still can't be found.")
        print("Please close this window, reopen it, and try running the script again.")
        sys.exit(1)


mt5 = ensure_mt5_package()

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
TOKEN_FILE = os.path.join(SCRIPT_DIR, "akdivex_token.txt")
STARTUP_ASKED_FILE = os.path.join(SCRIPT_DIR, ".akdivex_startup_asked")
AUTH_TOKEN = ""  # set inside main()
ACTIVE_TERMINAL_PATH = None  # terminal64.exe path the user picked from the journal, if any


# ---- multiple MetaTrader 5 installs on this computer ------------------------

def _guess_terminal_label(exe_path):
    """Best-effort friendly name for a terminal64.exe, from its install folder
    (e.g. ".../XM Global MT5/terminal64.exe" -> "XM Global MT5")."""
    folder = os.path.basename(os.path.dirname(exe_path))
    if folder and folder.strip().lower() not in ("terminal", "terminal64", ""):
        return folder
    return "MetaTrader 5"


def find_terminal_installations():
    """Scans this Windows computer for every installed MetaTrader 5 terminal —
    each broker's own copy counts separately — and returns [{path, name}, ...].
    'path' is what gets passed to mt5.initialize(path=...) to attach to that
    specific terminal instead of whichever one happens to already be open.
    Read-only: only looks at the filesystem, never launches anything."""
    found = {}
    if os.name != "nt":
        return []

    # 1) Every MetaQuotes "data folder" keeps an origin.txt pointing at the
    #    real install folder of the terminal it belongs to — this is how
    #    MetaQuotes itself tells multiple copies on one PC apart.
    appdata = os.environ.get("APPDATA", "")
    mq_root = os.path.join(appdata, "MetaQuotes", "Terminal")
    if os.path.isdir(mq_root):
        try:
            for entry in os.listdir(mq_root):
                origin_file = os.path.join(mq_root, entry, "origin.txt")
                if not os.path.isfile(origin_file):
                    continue
                install_dir = ""
                for enc in ("utf-16", "utf-8"):
                    try:
                        with open(origin_file, "r", encoding=enc, errors="ignore") as f:
                            install_dir = f.read().strip().strip("\x00")
                        if install_dir:
                            break
                    except Exception:
                        continue
                exe = os.path.join(install_dir, "terminal64.exe") if install_dir else ""
                if exe and os.path.isfile(exe):
                    found[os.path.normcase(exe)] = exe
        except OSError:
            pass

    # 2) Fall back to common Program Files locations too, in case a terminal
    #    was installed but never logged into yet (so it has no data folder).
    program_dirs = [
        os.environ.get("ProgramFiles", r"C:\Program Files"),
        os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"),
    ]
    for base in program_dirs:
        if not base or not os.path.isdir(base):
            continue
        try:
            for entry in os.listdir(base):
                exe = os.path.join(base, entry, "terminal64.exe")
                if os.path.isfile(exe):
                    found[os.path.normcase(exe)] = exe
        except OSError:
            pass

    return [{"path": p, "name": _guess_terminal_label(p)} for p in found.values()]


def terminals_payload():
    term_info = mt5.terminal_info()
    current_dir = os.path.normcase(term_info.path) if term_info and term_info.path else ""
    out = []
    for t in find_terminal_installations():
        out.append({
            "path": t["path"],
            "name": t["name"],
            "active": bool(current_dir) and os.path.normcase(os.path.dirname(t["path"])) == current_dir,
        })
    return out


# ---- security token ---------------------------------------------------------

def load_or_create_token(custom_token=None):
    if custom_token:
        token = custom_token.strip()
        with open(TOKEN_FILE, "w", encoding="utf-8") as f:
            f.write(token)
        return token
    if os.path.exists(TOKEN_FILE):
        with open(TOKEN_FILE, "r", encoding="utf-8") as f:
            existing = f.read().strip()
            if existing:
                return existing
    token = secrets.token_hex(16)
    with open(TOKEN_FILE, "w", encoding="utf-8") as f:
        f.write(token)
    return token


def copy_to_clipboard(text):
    """Best-effort copy to the Windows clipboard. Returns True on success."""
    if os.name != "nt":
        return False
    try:
        subprocess.run("clip", input=text.strip().encode("utf-16"), check=True, shell=True)
        return True
    except Exception:  # noqa: BLE001
        return False


# ---- run automatically at Windows startup -----------------------------------

def startup_vbs_path():
    appdata = os.environ.get("APPDATA", "")
    startup_dir = os.path.join(appdata, "Microsoft", "Windows", "Start Menu", "Programs", "Startup")
    return os.path.join(startup_dir, "AKDIVEX_MT5_Bridge.vbs"), startup_dir


def install_startup(quiet=False):
    if os.name != "nt":
        print("This feature only works on Windows.")
        return
    vbs_path, startup_dir = startup_vbs_path()
    if not os.path.isdir(startup_dir):
        print("Couldn't find the Windows Startup folder — automatic startup isn't possible.")
        return
    pythonw = sys.executable
    if pythonw.lower().endswith("python.exe"):
        candidate = pythonw[:-len("python.exe")] + "pythonw.exe"
        if os.path.exists(candidate):
            pythonw = candidate
    script_path = os.path.abspath(__file__)
    vbs_content = (
        'CreateObject("Wscript.Shell").Run "" & Chr(34) & "%s" & Chr(34) & '
        '" " & Chr(34) & "%s" & Chr(34), 0, False'
    ) % (pythonw, script_path)
    with open(vbs_path, "w", encoding="utf-8") as f:
        f.write(vbs_content)
    print("✅ Done — set up successfully.")
    print("   From now on, the AKDIVEX bridge will start automatically and silently")
    print("   every time you log into Windows. You won't need to run this again.")
    if not quiet:
        print(f"   File installed: {vbs_path}")
    print("   (MetaTrader 5 itself still needs to be opened separately.)")
    if os.path.exists(STARTUP_ASKED_FILE):
        try:
            os.remove(STARTUP_ASKED_FILE)
        except OSError:
            pass


def uninstall_startup():
    vbs_path, _ = startup_vbs_path()
    if os.path.exists(vbs_path):
        os.remove(vbs_path)
        print("✅ Automatic startup has been removed.")
    else:
        print("Nothing to remove — automatic startup wasn't set up.")


def maybe_offer_autostart():
    """Ask, once, whether to enable automatic startup — so most users never
    need to know this feature (or its command-line flag) exists at all."""
    if os.name != "nt":
        return
    vbs_path, _ = startup_vbs_path()
    if os.path.exists(vbs_path) or os.path.exists(STARTUP_ASKED_FILE):
        return
    print()
    try:
        answer = input(
            "Start this bridge automatically every time you log into Windows,\n"
            "so you never have to open this window again? [Y/n]: "
        ).strip().lower()
    except (EOFError, KeyboardInterrupt):
        answer = "n"
    if answer in ("", "y", "yes"):
        install_startup(quiet=True)
    else:
        print("Okay — you can turn this on later by running:")
        print("    python akdivex_mt5_bridge.py --install-startup")
        try:
            with open(STARTUP_ASKED_FILE, "w", encoding="utf-8") as f:
                f.write("declined")
        except OSError:
            pass


# ---- helpers ------------------------------------------------------------

def ensure_connected():
    """Connect to the terminal if we aren't already (no login/password — the
    already-open session is reused). If the user picked a specific terminal
    from the journal's terminal switcher (ACTIVE_TERMINAL_PATH), we make sure
    we're attached to THAT one — not just any MetaTrader window that's open."""
    if ACTIVE_TERMINAL_PATH:
        info = mt5.terminal_info()
        wanted_dir = os.path.normcase(os.path.dirname(ACTIVE_TERMINAL_PATH))
        already_there = info is not None and mt5.account_info() is not None and \
            os.path.normcase(info.path or "") == wanted_dir
        if already_there:
            return True
        return mt5.initialize(path=ACTIVE_TERMINAL_PATH)
    if mt5.terminal_info() is not None and mt5.account_info() is not None:
        return True
    return mt5.initialize()


def deal_type_str(t):
    # 0 = BUY, 1 = SELL; other values (balance/credit ops, etc.) don't apply to the journal
    return "DEAL_TYPE_SELL" if t == 1 else "DEAL_TYPE_BUY"


def deal_entry_str(e):
    return {0: "DEAL_ENTRY_IN", 1: "DEAL_ENTRY_OUT", 2: "DEAL_ENTRY_INOUT", 3: "DEAL_ENTRY_OUT_BY"}.get(e, "DEAL_ENTRY_IN")


# MT5's DEAL_REASON_* constants — tells the journal *why* a position closed,
# so it can label an auto-registered trade as "hit SL" / "hit TP" instead of
# just "closed". 4 = SL, 5 = TP, 6 = stop-out; everything else counts as manual.
def deal_reason_str(r):
    return {4: "SL", 5: "TP", 6: "SO"}.get(r, "MANUAL")


def unix_to_iso(ts):
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def account_information_payload():
    info = mt5.account_info()
    if info is None:
        raise RuntimeError("account_info() returned nothing — make sure you're logged in on MetaTrader")
    d = info._asdict()
    return {
        "broker": d.get("company", ""),
        "currency": d.get("currency", ""),
        "server": d.get("server", ""),
        "balance": d.get("balance", 0),
        "equity": d.get("equity", 0),
        "margin": d.get("margin", 0),
        "freeMargin": d.get("margin_free", 0),
        "leverage": d.get("leverage", 0),
        "marginLevel": d.get("margin_level", 0),
        "tradeAllowed": bool(d.get("trade_allowed", False)),
        "name": d.get("name", ""),
        "login": d.get("login", 0),
        "credit": d.get("credit", 0),
    }


def positions_payload():
    rows = mt5.positions_get()
    out = []
    for p in rows or []:
        d = p._asdict()
        out.append({
            "id": str(d.get("ticket")),
            "symbol": d.get("symbol", ""),
            "type": "POSITION_TYPE_SELL" if d.get("type") == 1 else "POSITION_TYPE_BUY",
            "volume": d.get("volume", 0),
            "openPrice": d.get("price_open", 0),
            "currentPrice": d.get("price_current", 0),
            "stopLoss": d.get("sl", 0) or None,
            "takeProfit": d.get("tp", 0) or None,
            "profit": d.get("profit", 0),
            "swap": d.get("swap", 0),
            "commission": 0,
            "time": unix_to_iso(d.get("time", 0)),
        })
    return out


def history_deals_payload(start_dt, end_dt):
    rows = mt5.history_deals_get(start_dt, end_dt)
    out = []
    for dl in rows or []:
        d = dl._asdict()
        if d.get("type", 0) not in (0, 1):
            continue  # 0/1 = buy/sell; the rest (deposits/withdrawals, etc.) aren't relevant to the journal
        out.append({
            "id": str(d.get("ticket")),
            "positionId": str(d.get("position_id")),
            "orderId": str(d.get("order")),
            "symbol": d.get("symbol", ""),
            "type": deal_type_str(d.get("type", 0)),
            "entryType": deal_entry_str(d.get("entry", 0)),
            "reason": deal_reason_str(d.get("reason", 0)),
            "price": d.get("price", 0),
            "volume": d.get("volume", 0),
            "profit": d.get("profit", 0),
            "swap": d.get("swap", 0),
            "commission": d.get("commission", 0),
            "time": unix_to_iso(d.get("time", 0)),
        })
    return out


# ---- CHART SCREENSHOTS (entry / exit snapshots for the journal) --------------
# MetaTrader draws the Stop Loss / Take Profit / entry-price lines on a chart by
# itself for any symbol with an open position (Tools > Options > Charts > "Show
# Trade Levels", on by default) — so we don't draw anything ourselves, we just
# open the right chart and let MT5's own screenshot function capture it. Only
# one screenshot is taken at a time (SCREENSHOT_LOCK) since opening/switching
# charts is a shared, terminal-wide action.
TIMEFRAME_MAP = {
    "M1": "TIMEFRAME_M1", "M5": "TIMEFRAME_M5", "M15": "TIMEFRAME_M15",
    "M30": "TIMEFRAME_M30", "H1": "TIMEFRAME_H1", "H4": "TIMEFRAME_H4", "D1": "TIMEFRAME_D1",
}
SCREENSHOT_LOCK = threading.Lock()


def capture_screenshot(symbol, timeframe_key):
    symbol = (symbol or "").strip().upper()
    if not symbol:
        raise RuntimeError("نماد نامعتبر است")
    tf_const_name = TIMEFRAME_MAP.get((timeframe_key or "M15").upper(), "TIMEFRAME_M15")
    tf_const = getattr(mt5, tf_const_name)

    with SCREENSHOT_LOCK:
        if not mt5.symbol_select(symbol, True):
            raise RuntimeError(f"نماد {symbol} پیدا نشد")
        chart_id = mt5.chart_open(symbol, tf_const)
        if not chart_id:
            raise RuntimeError("باز کردن نمودار در متاتریدر ممکن نشد")
        try:
            time.sleep(0.7)  # let the chart actually render before capturing it
            fd, path = tempfile.mkstemp(suffix=".png", prefix="akdivex_shot_")
            os.close(fd)
            ok = mt5.chart_screenshot(chart_id, path, 900, 480)
            if not ok:
                raise RuntimeError("گرفتن اسکرین‌شات از نمودار ناموفق بود")
            with open(path, "rb") as f:
                data = f.read()
            return "data:image/png;base64," + base64.b64encode(data).decode("ascii")
        finally:
            try:
                mt5.chart_close(chart_id)
            except Exception:
                pass
            try:
                os.remove(path)
            except Exception:
                pass


def find_position(ticket):
    rows = mt5.positions_get(ticket=ticket)
    for p in rows or []:
        return p._asdict()
    return None


def close_position(ticket, requested_volume=None):
    """Closes a position at market price (fully or partially)."""
    d = find_position(ticket)
    if d is None:
        raise RuntimeError(f"Position #{ticket} not found — it may already be closed")
    symbol = d["symbol"]
    is_buy = d.get("type", 0) == 0
    full_volume = d.get("volume", 0)
    volume = full_volume
    if requested_volume:
        try:
            volume = min(float(requested_volume), full_volume)
        except (TypeError, ValueError):
            volume = full_volume
    if volume <= 0:
        raise RuntimeError("Invalid close volume")

    mt5.symbol_select(symbol, True)
    tick = mt5.symbol_info_tick(symbol)
    if tick is None:
        raise RuntimeError(f"No live price available for {symbol}")
    price = tick.bid if is_buy else tick.ask

    request = {
        "action": mt5.TRADE_ACTION_DEAL,
        "symbol": symbol,
        "volume": volume,
        "type": mt5.ORDER_TYPE_SELL if is_buy else mt5.ORDER_TYPE_BUY,
        "position": ticket,
        "price": price,
        "deviation": 20,
        "magic": 990099,
        "comment": "AKDIVEX journal close",
        "type_time": mt5.ORDER_TIME_GTC,
        "type_filling": mt5.ORDER_FILLING_IOC,
    }
    result = mt5.order_send(request)
    if result is None or result.retcode != mt5.TRADE_RETCODE_DONE:
        code = result.retcode if result is not None else mt5.last_error()
        raise RuntimeError(
            f"Failed to close the trade (code {code}) — make sure \"AutoTrading\" is turned on in MetaTrader"
        )
    return {"ok": True, "ticket": ticket, "closedVolume": volume}


def modify_position(ticket, sl, tp):
    """Changes the Stop Loss / Take Profit of an open position. 0 clears that level."""
    d = find_position(ticket)
    if d is None:
        raise RuntimeError(f"Position #{ticket} not found — it may already be closed")

    def to_float(v, fallback):
        if v is None or v == "":
            return fallback
        try:
            return float(v)
        except (TypeError, ValueError):
            return fallback

    new_sl = to_float(sl, d.get("sl", 0) or 0)
    new_tp = to_float(tp, d.get("tp", 0) or 0)

    request = {
        "action": mt5.TRADE_ACTION_SLTP,
        "symbol": d["symbol"],
        "position": ticket,
        "sl": new_sl,
        "tp": new_tp,
    }
    result = mt5.order_send(request)
    if result is None or result.retcode != mt5.TRADE_RETCODE_DONE:
        code = result.retcode if result is not None else mt5.last_error()
        raise RuntimeError(
            f"Failed to set SL/TP (code {code}) — make sure \"AutoTrading\" is turned on in MetaTrader"
        )
    return {"ok": True, "ticket": ticket, "sl": new_sl, "tp": new_tp}


def open_position(symbol, side, volume, sl=None, tp=None):
    """Opens a new market order (BUY or SELL) on the given symbol."""
    symbol = (symbol or "").strip().upper()
    if not symbol:
        raise RuntimeError("نماد را وارد کنید")
    try:
        volume = float(volume)
    except (TypeError, ValueError):
        raise RuntimeError("حجم معامله نامعتبر است")
    if volume <= 0:
        raise RuntimeError("حجم معامله باید بزرگ‌تر از صفر باشد")
    is_buy = (side or "").upper() == "BUY"

    if not mt5.symbol_select(symbol, True):
        raise RuntimeError(f"نماد {symbol} پیدا نشد یا در Market Watch قابل انتخاب نیست")
    tick = mt5.symbol_info_tick(symbol)
    if tick is None:
        raise RuntimeError(f"قیمت زنده‌ای برای {symbol} در دسترس نیست")
    price = tick.ask if is_buy else tick.bid

    def to_float_or_none(v):
        if v is None or v == "":
            return 0.0
        try:
            return float(v)
        except (TypeError, ValueError):
            return 0.0

    request = {
        "action": mt5.TRADE_ACTION_DEAL,
        "symbol": symbol,
        "volume": volume,
        "type": mt5.ORDER_TYPE_BUY if is_buy else mt5.ORDER_TYPE_SELL,
        "price": price,
        "sl": to_float_or_none(sl),
        "tp": to_float_or_none(tp),
        "deviation": 20,
        "magic": 990099,
        "comment": "AKDIVEX journal open",
        "type_time": mt5.ORDER_TIME_GTC,
        "type_filling": mt5.ORDER_FILLING_IOC,
    }
    result = mt5.order_send(request)
    if result is None or result.retcode != mt5.TRADE_RETCODE_DONE:
        code = result.retcode if result is not None else mt5.last_error()
        raise RuntimeError(
            f"باز کردن معامله ناموفق بود (کد {code}) — مطمئن شوید \"AutoTrading\" در متاتریدر روشن است و حجم/نماد درست است"
        )
    return {"ok": True, "ticket": result.order, "symbol": symbol, "side": "BUY" if is_buy else "SELL", "volume": volume, "price": price}


# ---- HTTP server --------------------------------------------------------------

CLOSE_PATH_RE = re.compile(r"^/positions/(\d+)/close$")
MODIFY_PATH_RE = re.compile(r"^/positions/(\d+)/modify$")


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass  # keep the console quiet; our own status messages are printed separately

    def _send_json(self, status, payload):
        body = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "auth-token, Content-Type, Accept")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _check_auth(self):
        got = self.headers.get("auth-token", "")
        if not got or got != AUTH_TOKEN:
            self._send_json(401, {
                "error": "توکن امنیتی نامعتبر یا خالی است — توکن چاپ‌شده در پنجره‌ی این اسکریپت را داخل تنظیمات اتصال ژورنال وارد کنید"
            })
            return False
        return True

    def do_OPTIONS(self):
        self._send_json(204, {})

    def do_GET(self):
        path = urlsplit(self.path).path.rstrip("/")
        try:
            if path == "" or path == "/ping":
                self._send_json(200, {"ok": True, "service": "akdivex-mt5-bridge"})
                return
            if not self._check_auth():
                return
            if path.endswith("/terminals"):
                # Listing installed terminals doesn't need an active MT5 session —
                # this is what lets the journal offer a switch before one is picked.
                self._send_json(200, terminals_payload())
                return
            if not ensure_connected():
                self._send_json(503, {"error": "به متاتریدر وصل نشد — مطمئن شوید متاتریدر ۵ باز و لاگین است"})
                return
            if path.endswith("/account-information"):
                self._send_json(200, account_information_payload())
            elif path.endswith("/positions"):
                self._send_json(200, positions_payload())
            elif path == "/screenshot":
                qs = parse_qs(urlsplit(self.path).query)
                symbol = (qs.get("symbol", [""])[0])
                tf = (qs.get("timeframe", ["M15"])[0])
                self._send_json(200, {"image": capture_screenshot(symbol, tf)})
            elif path == "/price":
                qs = parse_qs(urlsplit(self.path).query)
                symbol = (qs.get("symbol", [""])[0]).strip().upper()
                if not symbol or not mt5.symbol_select(symbol, True):
                    self._send_json(404, {"error": f"نماد {symbol} پیدا نشد"})
                    return
                tick = mt5.symbol_info_tick(symbol)
                if tick is None:
                    self._send_json(404, {"error": "قیمت زنده‌ای در دسترس نیست"})
                    return
                self._send_json(200, {"symbol": symbol, "bid": tick.bid, "ask": tick.ask})
            elif "/history-deals/time/" in path:
                tail = path.split("/history-deals/time/", 1)[1]
                parts = tail.split("/")
                if len(parts) != 2:
                    self._send_json(400, {"error": "بازه‌ی زمانی نامعتبر است"})
                    return
                start_dt = datetime.fromisoformat(parts[0].replace("Z", "+00:00"))
                end_dt = datetime.fromisoformat(parts[1].replace("Z", "+00:00"))
                self._send_json(200, history_deals_payload(start_dt, end_dt))
            else:
                self._send_json(404, {"error": "مسیر ناشناخته"})
        except Exception as e:  # noqa: BLE001
            self._send_json(500, {"error": str(e)})

    def do_POST(self):
        global ACTIVE_TERMINAL_PATH
        path = urlsplit(self.path).path.rstrip("/")
        try:
            if not self._check_auth():
                return
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length else b"{}"
            try:
                body = json.loads(raw.decode("utf-8")) if raw else {}
            except Exception:
                body = {}

            if path.endswith("/terminals/switch"):
                target = (body.get("path") or "").strip()
                if not target or not os.path.isfile(target):
                    self._send_json(400, {"error": "مسیر ترمینال معتبر نیست"})
                    return
                mt5.shutdown()
                if not mt5.initialize(path=target):
                    code = mt5.last_error()
                    self._send_json(503, {
                        "error": f"اتصال به این ترمینال ممکن نشد (کد {code}) — مطمئن شوید همان ترمینال باز و لاگین است"
                    })
                    return
                ACTIVE_TERMINAL_PATH = target
                self._send_json(200, {"ok": True, "account": account_information_payload()})
                return

            if not ensure_connected():
                self._send_json(503, {"error": "به متاتریدر وصل نشد — مطمئن شوید متاتریدر ۵ باز و لاگین است"})
                return

            if path.endswith("/positions/open"):
                self._send_json(200, open_position(body.get("symbol"), body.get("side"), body.get("volume"), body.get("sl"), body.get("tp")))
                return

            m = CLOSE_PATH_RE.match(path)
            if m:
                self._send_json(200, close_position(int(m.group(1)), body.get("volume")))
                return

            m = MODIFY_PATH_RE.match(path)
            if m:
                self._send_json(200, modify_position(int(m.group(1)), body.get("sl"), body.get("tp")))
                return

            self._send_json(404, {"error": "مسیر ناشناخته"})
        except RuntimeError as e:
            self._send_json(400, {"error": str(e)})
        except Exception as e:  # noqa: BLE001
            self._send_json(500, {"error": str(e)})


def main():
    global AUTH_TOKEN
    parser = argparse.ArgumentParser(description="AKDIVEX MT5 local bridge")
    parser.add_argument("--port", type=int, default=8787, help="local port to run on (default: 8787)")
    parser.add_argument("--token", type=str, default=None, help="manually set the security token (optional)")
    parser.add_argument("--install-startup", action="store_true", help="run automatically and silently every time Windows starts")
    parser.add_argument("--uninstall-startup", action="store_true", help="remove automatic startup from Windows")
    args = parser.parse_args()

    if args.install_startup:
        install_startup()
        return
    if args.uninstall_startup:
        uninstall_startup()
        return

    AUTH_TOKEN = load_or_create_token(args.token)

    print("Connecting to the MetaTrader 5 terminal...")
    if not ensure_connected():
        print("Couldn't connect to MetaTrader. Make sure MetaTrader 5 is open and logged in, then try again.")
        sys.exit(1)
    acc = mt5.account_info()
    print(f"Connected ✅  Account #{acc.login if acc else '?'}  —  Server: {acc.server if acc else '?'}")
    print("=" * 60)
    copied = copy_to_clipboard(AUTH_TOKEN)
    print(f"  Security token: {AUTH_TOKEN}")
    if copied:
        print("  (already copied to your clipboard — just paste it, Ctrl+V, into the journal)")
    else:
        print("  Copy this token into the MetaTrader connection settings in the journal.")
    print("=" * 60)
    print(f"Running on http://127.0.0.1:{args.port}  (keep this window open)")
    print("To close a trade or change SL/TP from the journal, the \"AutoTrading\" button in MetaTrader must be turned on (green).")

    maybe_offer_autostart()

    print("\nTo stop this bridge: press Ctrl+C, or just close this window.")

    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        mt5.shutdown()


if __name__ == "__main__":
    main()
