#!/usr/bin/env python3
"""
AKDIVEX <-> MetaTrader 5 bridge  (Windows + MT5 terminal running)
  pip install MetaTrader5 websockets
  python akdivex_bridge.py
First run creates bridge_config.json and prints a pairing TOKEN -> paste it in the site (Live tab).
"""
import asyncio, hashlib, json, math, os, secrets, sys, time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
try:
    import MetaTrader5 as mt5
    import websockets
except ImportError:
    sys.exit("Run:  pip install MetaTrader5 websockets")

CFG_FILE = Path(__file__).with_name("bridge_config.json")
CFG = {"host": "127.0.0.1", "port": 8765, "token": "", "allow_trading": True,
       "allowed_origins": [], "poll_ms": 150, "mt5_path": "", "deviation": 30}
if CFG_FILE.exists():
    CFG.update(json.loads(CFG_FILE.read_text("utf-8")))
if os.environ.get("AKDIVEX_TOKEN"): CFG["token"] = os.environ["AKDIVEX_TOKEN"]   # pairing values come from the website's launcher
if os.environ.get("AKDIVEX_PORT", "").isdigit(): CFG["port"] = int(os.environ["AKDIVEX_PORT"])
if os.environ.get("AKDIVEX_ORIGINS"): CFG["allowed_origins"] = [o for o in os.environ["AKDIVEX_ORIGINS"].split(",") if o]
if not CFG["token"]:
    CFG["token"] = secrets.token_urlsafe(16)
CFG_FILE.write_text(json.dumps(CFG, indent=2), "utf-8")

EX = ThreadPoolExecutor(1)                       # all MT5 calls run serialized in one thread
async def mt(fn, *a, **k):
    return await asyncio.get_running_loop().run_in_executor(EX, lambda: fn(*a, **k))

CLIENTS, KNOWN, EMITTED = set(), {}, set()
S = {"ok": False, "offset": 0, "err": "", "login": None}
REASON = {0: "MANUAL", 1: "MANUAL", 2: "MANUAL", 3: "EA", 4: "SL", 5: "TP", 6: "STOPOUT"}
OUT = (1, 2, 3)                                  # DEAL_ENTRY_OUT / INOUT / OUT_BY

def init():
    ok = mt5.initialize(CFG["mt5_path"]) if CFG["mt5_path"] else mt5.initialize()
    S["ok"], S["err"] = bool(ok), ("" if ok else str(mt5.last_error()))
    if ok:
        a = mt5.account_info(); S["login"] = a.login if a else None
    return ok

def calc_offset(sym):
    t = mt5.symbol_info_tick(sym)
    if t and t.time:
        d = round((t.time - time.time()) / 900) * 900
        if abs(d) <= 14 * 3600: S["offset"] = d

def pos_dict(p):
    info = mt5.symbol_info(p.symbol)
    return {"ticket": p.ticket, "symbol": p.symbol, "side": "BUY" if p.type == 0 else "SELL",
            "volume": p.volume, "open": p.price_open, "price": p.price_current,
            "sl": p.sl or None, "tp": p.tp or None, "profit": round(p.profit + p.swap, 2),
            "open_ts": p.time - S["offset"], "digits": info.digits if info else 5}

def account():
    a, t = mt5.account_info(), mt5.terminal_info()
    if not a: return None
    S["login"] = a.login
    return {"login": a.login, "name": a.name, "server": a.server, "currency": a.currency,
            "leverage": a.leverage, "balance": a.balance, "equity": a.equity, "profit": a.profit,
            "margin": a.margin, "free": a.margin_free, "level": a.margin_level,
            "broker_ok": bool(t and t.connected), "terminal": str(Path(t.path)) if t and t.path else ""}

def build_trade(pid, deals, sl=None, tp=None, first_sl=None, side=None):
    ins = [d for d in deals if d.entry == 0]
    outs = [d for d in deals if d.entry in OUT]
    if not ins or not outs: return None
    vin, vout = sum(d.volume for d in ins), sum(d.volume for d in outs)
    if vout < vin - 1e-9: return None            # still partially open
    w = lambda ds: sum(d.price * d.volume for d in ds) / sum(d.volume for d in ds)
    gross = sum(d.profit for d in deals)
    net = gross + sum(d.commission + d.swap + getattr(d, "fee", 0) for d in deals)
    if sl is None and tp is None:                # history path: read levels from orders
        for o in (mt5.history_orders_get(position=pid) or ()):
            sl, tp = (o.sl or sl), (o.tp or tp)
    last = max(outs, key=lambda d: d.time)
    return {"id": f"mt_{pid}", "ticket": pid, "acct": S.get("login"), "symbol": ins[0].symbol,
            "side": side or ("BUY" if ins[0].type == 0 else "SELL"), "lot": round(vin, 2),
            "entry": w(ins), "exit": w(outs), "sl": sl, "tp": tp, "orig_sl": first_sl or sl,
            "pnl": round(net, 2), "reason": REASON.get(last.reason, "MANUAL"),
            "open_ts": min(d.time for d in ins) - S["offset"], "close_ts": last.time - S["offset"]}

async def broadcast(msg):
    data = json.dumps(msg)
    for ws in list(CLIENTS):
        try: await ws.send(data)
        except Exception: CLIENTS.discard(ws)

async def finalize(c, t0):
    pid = c["last"]["ticket"]
    for _ in range(25):                          # deal appears within ms; wait up to ~1.2s
        deals = await mt(mt5.history_deals_get, position=pid)
        tr = deals and await mt(build_trade, pid, deals, c["last"]["sl"], c["last"]["tp"], c["first_sl"], c["last"]["side"])
        if tr: break
        await asyncio.sleep(0.05)
    else:
        return
    EMITTED.add(pid)
    tr["latency_ms"] = round((time.perf_counter() - t0) * 1000)
    await broadcast({"type": "closed", "trade": tr, "live": True})

async def poll():
    last_sig, last_sent, tick = None, 0, 0
    while True:
        try:
            if S.get("busy"): await asyncio.sleep(.1); continue
            if not S["ok"] and not await mt(init):
                await broadcast({"type": "state", "mt5_ok": False, "error": S["err"]})
                await asyncio.sleep(3); continue
            t0 = time.perf_counter()
            ps = await mt(mt5.positions_get)
            if ps is None:                       # terminal lost -> re-init next loop
                S["ok"] = False; continue
            cur = {p.ticket: p for p in ps}
            for tk in [k for k in KNOWN if k not in cur]:
                asyncio.create_task(finalize(KNOWN.pop(tk), t0))
            for p in ps:
                d = pos_dict(p)
                k = KNOWN.setdefault(p.ticket, {"first_sl": d["sl"], "last": d})
                if k["first_sl"] is None and d["sl"]: k["first_sl"] = d["sl"]
                k["last"] = d
            tick += 1
            if tick % 400 == 1 and ps: await mt(calc_offset, ps[0].symbol)
            if CLIENTS:
                acc = await mt(account)
                lst = [k["last"] for k in KNOWN.values()]
                sig = (acc and (acc["balance"], acc["equity"]), tuple((x["ticket"], x["price"], x["sl"], x["tp"], x["volume"]) for x in lst))
                now = time.time()
                if (sig != last_sig and now - last_sent > .2) or now - last_sent > 2:
                    last_sig, last_sent = sig, now
                    await broadcast({"type": "state", "mt5_ok": True, "account": acc, "positions": lst})
        except Exception as e:
            print("poll error:", e)
        await asyncio.sleep(CFG["poll_ms"] / 1000)

# ---------- trading commands ----------
def _send(req):
    r = mt5.order_send(req)
    if r is None: return False, str(mt5.last_error())
    if r.retcode in (mt5.TRADE_RETCODE_DONE, 10025): return True, r.comment or "OK"
    return False, f"{r.comment} ({r.retcode})"

def _get(ticket):
    ps = mt5.positions_get(ticket=int(ticket))
    return ps[0] if ps else None

def do_modify(ticket, sl, tp):
    p = _get(ticket)
    if not p: return False, "Position not found"
    dg = mt5.symbol_info(p.symbol).digits
    return _send({"action": mt5.TRADE_ACTION_SLTP, "position": p.ticket, "symbol": p.symbol,
                  "sl": round(float(sl), dg) if sl else 0.0, "tp": round(float(tp), dg) if tp else 0.0})

def do_close(ticket, volume=None):
    p = _get(ticket)
    if not p: return False, "Position not found"
    info, tick = mt5.symbol_info(p.symbol), mt5.symbol_info_tick(p.symbol)
    buy = p.type == 0
    vol = p.volume
    if volume:
        step = info.volume_step or 0.01
        v = round(math.floor(min(float(volume), p.volume) / step) * step, 8)
        if v >= info.volume_min and p.volume - v >= info.volume_min: vol = v
    modes = [mt5.ORDER_FILLING_IOC, mt5.ORDER_FILLING_FOK, mt5.ORDER_FILLING_RETURN]
    for f in modes:
        ok, msg = _send({"action": mt5.TRADE_ACTION_DEAL, "position": p.ticket, "symbol": p.symbol,
                         "volume": vol, "type": mt5.ORDER_TYPE_SELL if buy else mt5.ORDER_TYPE_BUY,
                         "price": tick.bid if buy else tick.ask, "deviation": CFG["deviation"],
                         "comment": "AKDIVEX", "type_filling": f})
        if ok or "10030" not in msg: break       # 10030 = unsupported filling mode -> try next
    return ok, msg

def history(days):
    now = int(time.time()) + 86400
    deals = mt5.history_deals_get(now - int(days) * 86400 - 86400, now) or ()
    by = {}
    for d in deals:
        if d.position_id and d.type in (0, 1): by.setdefault(d.position_id, []).append(d)
    out = [build_trade(pid, ds) for pid, ds in by.items()]
    return sorted([t for t in out if t], key=lambda t: t["close_ts"])

def discover():
    """Installed MT5 terminals (only these can be selected from the website)."""
    seen, out = set(), []
    def add(exe):
        exe = Path(exe)
        if exe.name.lower() == "terminal64.exe" and exe.exists() and str(exe).lower() not in seen:
            seen.add(str(exe).lower())
            out.append({"id": hashlib.sha1(str(exe).lower().encode()).hexdigest()[:10], "name": exe.parent.name, "path": str(exe), "dir": str(exe.parent)})
    for env in ("ProgramFiles", "ProgramFiles(x86)"):
        base = os.environ.get(env)
        if base and Path(base).exists():
            for d in Path(base).iterdir(): add(d / "terminal64.exe")
    ap = os.environ.get("APPDATA")
    if ap:
        for f in Path(ap, "MetaQuotes", "Terminal").glob("*/origin.txt"):
            for enc in ("utf-16", "utf-8"):
                try: add(Path(f.read_text(enc).strip()) / "terminal64.exe"); break
                except Exception: pass
    if CFG["mt5_path"]: add(CFG["mt5_path"])
    return out

def terminals():
    ti = mt5.terminal_info() if S["ok"] else None
    cur = str(Path(ti.path)).lower() if ti and ti.path else ""
    out = discover()
    if cur and not any(str(Path(x["dir"])).lower() == cur for x in out):
        exe = Path(ti.path) / "terminal64.exe"
        out.append({"id": hashlib.sha1(str(exe).lower().encode()).hexdigest()[:10], "name": Path(ti.path).name, "path": str(exe), "dir": str(Path(ti.path))})
    for x in out: x["active"] = str(Path(x["dir"])).lower() == cur
    return out

def do_switch(tid):
    m = [x for x in terminals() if x["id"] == tid]
    if not m: return False, "Unknown terminal"
    mt5.shutdown()
    CFG["mt5_path"] = m[0]["path"]; CFG_FILE.write_text(json.dumps(CFG, indent=2), "utf-8")
    ok = init()
    return ok, "OK" if ok else S["err"]

async def handle(ws):
    try:
        auth = json.loads(await asyncio.wait_for(ws.recv(), 5))
        if auth.get("type") != "auth" or not secrets.compare_digest(str(auth.get("token", "")), CFG["token"]):
            await ws.send(json.dumps({"type": "auth_fail"})); await asyncio.sleep(1); return
    except Exception:
        return
    CLIENTS.add(ws)
    await ws.send(json.dumps({"type": "hello", "trading": CFG["allow_trading"]}))
    try:
        async for raw in ws:
            m = json.loads(raw); t = m.get("type"); rid = m.get("id")
            if t == "ping":
                await ws.send(json.dumps({"type": "pong", "ts": m.get("ts")})); continue
            if t == "sync":
                await ws.send(json.dumps({"type": "history", "trades": await mt(history, m.get("days", 7))})); continue
            if t == "terminals":
                await ws.send(json.dumps({"type": "result", "id": rid, "ok": True, "list": await mt(terminals)})); continue
            if t == "switch":
                S["busy"] = True
                try: res = await mt(do_switch, m.get("id"))
                finally: KNOWN.clear(); S["offset"] = 0; S["busy"] = False
                await ws.send(json.dumps({"type": "result", "id": rid, "ok": res[0], "msg": res[1]})); continue
            if t in ("modify", "close"):
                if not CFG["allow_trading"]: res = (False, "Read-only mode (allow_trading=false)")
                elif t == "modify": res = await mt(do_modify, m["ticket"], m.get("sl"), m.get("tp"))
                else: res = await mt(do_close, m["ticket"], m.get("volume"))
                await ws.send(json.dumps({"type": "result", "id": rid, "ok": res[0], "msg": res[1]}))
    except Exception:
        pass
    finally:
        CLIENTS.discard(ws)

async def main():
    kw = {"origins": CFG["allowed_origins"] + [None]} if CFG["allowed_origins"] else {}
    async with websockets.serve(handle, CFG["host"], CFG["port"], **kw):
        print(f"\nAKDIVEX bridge listening on ws://{CFG['host']}:{CFG['port']}\nTOKEN: {CFG['token']}\n\nKeep this window open (you can minimize it). Close it to stop the bridge.\n")
        await poll()

if __name__ == "__main__":
    try: asyncio.run(main())
    except KeyboardInterrupt: mt5.shutdown()
    except OSError as e:
        if getattr(e, "errno", None) in (10048, 10013, 98, 48):
            print(f"\nPort {CFG['port']} is already in use - the AKDIVEX bridge is probably already running in another window.\n"
                  "Close the other bridge window (or just use it) and start again.")
            sys.exit(3)
        raise
