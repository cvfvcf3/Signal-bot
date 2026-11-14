"""Railway worker: E-signal forward logger (alert + log only, NO trading). Runs forever.
Signal E = 5m quote-volume spike (>= 8x the prior 24h mean) + taker-flow imbalance (12-bar mean >= +/-0.1), first bar of the event.
Data source: SOURCE=binance | okx | auto (auto = Binance if reachable from this server, else OKX).  Execution check: MEXC order book."""
import os, sys, time, json, csv, urllib.request, urllib.parse
COINS = ["ETH", "SOL", "XRP", "BNB", "DOGE", "ADA", "LINK", "AVAX", "LTC"]
BIN, OKX, MEXC = "https://fapi.binance.com", "https://www.okx.com", "https://contract.mexc.com"
VOLX, VOLX_NEAR, IMB, HOLD, K, NOTIONAL = 8.0, 5.0, 0.10, 36, 2.0, 200.0
BAR, SCAN, MAX_AGE_S = 300000, 3, 15 * 60
CHECK = [60, 300, 900, 3600]
SOURCE = os.environ.get("SOURCE", "auto").lower()
DIR = os.environ.get("LOG_DIR") or ("/data/e_logs" if os.path.isdir("/data") else "e_logs"); os.makedirs(DIR, exist_ok=True)
SIGF, NEARF, OUTF, STF = f"{DIR}/signals.csv", f"{DIR}/near.csv", f"{DIR}/outcomes.csv", f"{DIR}/state.json"
TG_TOKEN, TG_CHAT = os.environ.get("TG_TOKEN"), os.environ.get("TG_CHAT")

def log(*a): print(time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime()), *a, flush=True)

def api(url, params=None):
    if params: url += "?" + urllib.parse.urlencode(params)
    last = None
    for i in range(3):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "e-logger/1.0"}), timeout=15) as r:
                return json.loads(r.read().decode())
        except Exception as e:
            last = e; time.sleep(1 + i)
    raise last

def mean(a): return sum(a) / len(a)

def bars_binance(coin, now_ms):
    k = [x for x in api(BIN + "/fapi/v1/klines", {"symbol": coin + "USDT", "interval": "5m", "limit": 300}) if int(x[0]) + BAR <= now_ms]
    ts = [int(x[0]) for x in k]; h = [float(x[2]) for x in k]; l = [float(x[3]) for x in k]; c = [float(x[4]) for x in k]
    qv = [float(x[7]) for x in k]
    tk = {int(x[0]): (float(x[10]), float(x[7]) - float(x[10])) for x in k}     # ts -> (taker buy, taker sell) in quote volume
    return ts, h, l, c, qv, tk

def bars_okx(coin, now_ms):
    d = api(OKX + "/api/v5/market/candles", {"instId": f"{coin}-USDT-SWAP", "bar": "5m", "limit": 300})
    if d.get("code") != "0": raise RuntimeError("okx candles: " + str(d.get("msg")))
    rows = sorted([x for x in d["data"] if int(x[0]) + BAR <= now_ms], key=lambda x: int(x[0]))
    ts = [int(x[0]) for x in rows]; h = [float(x[2]) for x in rows]; l = [float(x[3]) for x in rows]; c = [float(x[4]) for x in rows]
    qv = [float(x[7]) if float(x[7]) > 0 else float(x[6]) * float(x[4]) for x in rows]
    t = api(OKX + "/api/v5/rubik/stat/taker-volume-contract", {"instId": f"{coin}-USDT-SWAP", "period": "5m"})
    if t.get("code") != "0": raise RuntimeError("okx taker: " + str(t.get("msg")))
    tk = {int(x[0]): (float(x[2]), float(x[1])) for x in t["data"]}                # rows are [ts, sell, buy]
    return ts, h, l, c, qv, tk

def get_bars(coin, now_ms, src): return bars_binance(coin, now_ms) if src == "binance" else bars_okx(coin, now_ms)

def after_rows(coin, close_ts, src):
    """first HOLD closed 5m bars starting at close_ts as (high, low, close), or None if not available yet"""
    if src == "binance":
        k = api(BIN + "/fapi/v1/klines", {"symbol": coin + "USDT", "interval": "5m", "startTime": close_ts, "limit": HOLD + 2})
        rows = [(float(x[2]), float(x[3]), float(x[4])) for x in k if int(x[0]) >= close_ts][:HOLD]
    else:
        d = api(OKX + "/api/v5/market/candles", {"instId": f"{coin}-USDT-SWAP", "bar": "5m", "limit": 300})
        rows = [(float(x[2]), float(x[3]), float(x[4])) for x in sorted([x for x in d["data"] if int(x[0]) >= close_ts], key=lambda x: int(x[0]))][:HOLD]
    return rows if len(rows) >= HOLD else None

def feats(qv, ts, tk, i):
    if i < 288 or qv[i] <= 0: return None
    base = mean(qv[i - 288:i])
    if base <= 0: return None
    imb = []
    for j in range(i - 11, i + 1):
        r = tk.get(ts[j])
        if r is None or r[0] + r[1] <= 0: return None
        imb.append((r[0] - r[1]) / (r[0] + r[1]))
    return qv[i] / base, mean(imb)

def atr15(ts, h, l, c):
    g = {}
    for t, hh, ll, cc in zip(ts, h, l, c): g.setdefault(t // 900000, []).append((hh, ll, cc))
    full = [g[k] for k in sorted(g) if len(g[k]) == 3]
    if len(full) < 15: return None
    cand = [(max(x[0] for x in b), min(x[1] for x in b), b[-1][2]) for b in full[-15:]]
    trs = [max(cand[j][0] - cand[j][1], abs(cand[j][0] - cand[j - 1][2]), abs(cand[j][1] - cand[j - 1][2])) for j in range(1, 15)]
    return mean(trs)

def events(ts, qv, tk):
    out = []; n = len(ts)
    for i in range(max(289, n - SCAN), n):
        f1, f0 = feats(qv, ts, tk, i), feats(qv, ts, tk, i - 1)
        if f1 is None: continue
        for side in (1, -1):
            def cond(f, vx): return f is not None and f[0] > vx and ((f[1] > IMB) if side == 1 else (f[1] < -IMB))
            for vx, tier in ((VOLX, "v12" if f1[0] > 12 else "v8"), (VOLX_NEAR, "v5")):
                if cond(f1, vx) and not cond(f0, vx):
                    out.append((i, side, tier if vx == VOLX else "v5", f1[0], f1[1])); break
    return out

_csize = {}
def contract_size(coin):
    if not _csize:
        for x in api(MEXC + "/api/v1/contract/detail").get("data", []):
            if x.get("symbol", "").endswith("_USDT"): _csize[x["symbol"]] = float(x.get("contractSize") or 0)
    return _csize.get(f"{coin}_USDT")

def mexc_book(coin):
    d = api(MEXC + f"/api/v1/contract/depth/{coin}_USDT", {"limit": 20})["data"]
    return float(d["bids"][0][0]), float(d["asks"][0][0]), d["asks"], d["bids"]

def vwap_fill(levels, cs, notional):
    if not cs: return None
    left, qty = notional, 0.0
    for p, q, *_ in levels:
        p, q = float(p), float(q) * cs; take = min(left, p * q); qty += take / p; left -= take
        if left <= 1e-9: break
    return (notional - left) / qty if qty > 0 and left <= 1e-9 else None

def tg(msg):
    if TG_TOKEN and TG_CHAT:
        try: urllib.request.urlopen(urllib.request.Request(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage", data=urllib.parse.urlencode({"chat_id": TG_CHAT, "text": msg}).encode()), timeout=15)
        except Exception as e: log("telegram failed", e)

def tg_doc(path):
    if not (TG_TOKEN and TG_CHAT and os.path.exists(path)): return
    try:
        b = "----eboundary7f3a"
        head = (f"--{b}\r\nContent-Disposition: form-data; name=\"chat_id\"\r\n\r\n{TG_CHAT}\r\n--{b}\r\n"
                f"Content-Disposition: form-data; name=\"document\"; filename=\"{os.path.basename(path)}\"\r\nContent-Type: text/csv\r\n\r\n").encode()
        body = head + open(path, "rb").read() + f"\r\n--{b}--\r\n".encode()
        urllib.request.urlopen(urllib.request.Request(f"https://api.telegram.org/bot{TG_TOKEN}/sendDocument", data=body,
                               headers={"Content-Type": f"multipart/form-data; boundary={b}"}), timeout=30)
    except Exception as e: log("telegram doc failed", e)

def load_state():
    try: return json.load(open(STF))
    except Exception: return {"pending": [], "busy": {}, "seen": []}
def save_state(s): s["seen"] = s["seen"][-400:]; json.dump(s, open(STF, "w"))
def append(path, header, row):
    new = not os.path.exists(path)
    with open(path, "a", newline="") as f:
        w = csv.writer(f)
        if new: w.writerow(header)
        w.writerow(row)
    log("ROW", os.path.basename(path), ",".join("" if v is None else str(v) for v in row))
def utc(ms): return time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(ms / 1000))

def record(coin, ts, h, l, c, i, side, tier, volx, imb, now, st, src):
    ct = ts[i] + BAR; e = c[i]; a = atr15(ts[:i + 1], h[:i + 1], l[:i + 1], c[:i + 1])
    if a is None or a <= 0: return
    sd = K * a; sl = e - side * sd; sid = f"{coin}-{ct}"
    bid = ask = fill = slip = drift = spread = None
    try:
        bid, ask, asks, bids = mexc_book(coin); mid = (bid + ask) / 2; spread = (ask - bid) / mid * 1e4
        fill = vwap_fill(asks if side == 1 else bids, contract_size(coin), NOTIONAL)
        if fill: slip = ((fill / mid - 1) if side == 1 else (mid / fill - 1)) * 1e4
        drift = side * (mid / e - 1) * 1e4
    except Exception as ex: log("mexc book failed", coin, ex)
    r = lambda v, n=2: None if v is None else round(v, n)
    append(SIGF, ["id", "signal_close_utc", "logged_utc", "latency_s", "source", "coin", "side", "tier", "ref_close", "volx", "imb12", "stop_pct", "sl",
                  "mexc_bid", "mexc_ask", "spread_bps", "mexc_vwap_fill_200usd", "slip_vs_mid_bps", "drift_since_close_bps"],
           [sid, utc(ct), utc(now), round(now / 1000 - ct / 1000), src, coin, "LONG" if side == 1 else "SHORT", tier, e, r(volx), r(imb, 3), r(sd / e * 100, 3), r(sl, 6),
            bid, ask, r(spread), fill, r(slip), r(drift)])
    st["pending"].append(dict(id=sid, coin=coin, side=side, close_ts=ct, entry=e, sl=sl, src=src, chk={}))
    st["busy"][coin] = ct + HOLD * BAR
    msg = f'E signal {"LONG" if side == 1 else "SHORT"} {coin} ({tier}, {src}) ref {e} SL {r(sl, 6)} stop {r(sd / e * 100)}% | volx {volx:.1f} imb {imb:+.2f} | latency {round(now / 1000 - ct / 1000)}s | MEXC spread {r(spread)}bps slip {r(slip)}bps drift {r(drift)}bps (alert only)'
    log(msg); tg(msg)

def finish(p):
    rows = after_rows(p["coin"], p["close_ts"], p["src"])
    if rows is None: return False
    side, e, sl = p["side"], p["entry"], p["sl"]; xp, why = rows[-1][2], "TIME"
    for hi, lo, cl in rows:
        if (side == 1 and lo <= sl) or (side == -1 and hi >= sl): xp, why = sl, "SL"; break
    gross = side * (xp - e) / e * 1e4
    def fills(nb, thr):
        for hi, lo, cl in rows[:nb]:
            if (side == 1 and lo <= e * (1 - thr)) or (side == -1 and hi >= e * (1 + thr)): return 1
        return 0
    cp = p["chk"]
    append(OUTF, ["id", "exit_reason", "gross_bps_from_close", "limit_fill_1bar_0bps", "limit_fill_3bar_0bps", "limit_fill_3bar_2bps", "limit_fill_3bar_5bps",
                  "mid_1m", "mid_5m", "mid_15m", "mid_60m"],
           [p["id"], why, round(gross, 1), fills(1, 0), fills(3, 0), fills(3, 0.0002), fills(3, 0.0005), cp.get("60"), cp.get("300"), cp.get("900"), cp.get("3600")])
    tg(f'outcome {p["id"]}: {why} {round(gross, 1)} bps (gross, from signal close)')
    return True

def process_pending(st, now):
    keep = []
    for p in st["pending"]:
        el = now / 1000 - p["close_ts"] / 1000
        for s in CHECK:
            if str(s) not in p["chk"] and el >= s:
                try: b, a, _, _ = mexc_book(p["coin"]); p["chk"][str(s)] = round((b + a) / 2, 6)
                except Exception as ex: log("checkpoint failed", ex)
        if el >= HOLD * 300 + 600:
            try:
                if finish(p): continue
            except Exception as ex: log("finish failed", p["id"], ex)
        keep.append(p)
    st["pending"] = keep

def cycle(st, src, now=None):
    now = now or int(time.time() * 1000); ok, newc, top, lag = 0, 0, ("-", 0.0), []
    for coin in COINS:
        try:
            ts, h, l, c, qv, tk = get_bars(coin, now, src); ok += 1
            if ts and tk:
                done = [t for t in tk if t + BAR <= now]; lag.append(round((ts[-1] - max(done)) / BAR) if done else 99)
            if len(ts) >= 292:
                f = feats(qv, ts, tk, len(ts) - 1)
                if f and f[0] > top[1]: top = (coin, f[0])
            for (i, side, tier, volx, imb) in events(ts, qv, tk):
                ct = ts[i] + BAR; sid = f"{coin}-{ct}"
                if sid in st["seen"] or now - ct > MAX_AGE_S * 1000: continue
                st["seen"].append(sid)
                if tier == "v5":
                    append(NEARF, ["id", "signal_close_utc", "coin", "side", "volx", "imb12", "ref_close"], [sid, utc(ct), coin, "LONG" if side == 1 else "SHORT", round(volx, 2), round(imb, 3), c[i]]); continue
                if st["busy"].get(coin, 0) > ct: continue
                record(coin, ts, h, l, c, i, side, tier, volx, imb, now, st, src); newc += 1
        except Exception as ex: log("error", coin, ex)
    process_pending(st, now); save_state(st)
    log(f"{src} | coins ok {ok}/{len(COINS)} | max volx {top[0]} {top[1]:.1f} | taker lag {lag} | new signals {newc} | pending {len(st['pending'])}")
    return ok

def pick_source():
    if SOURCE in ("binance", "okx"): return SOURCE
    try: api(BIN + "/fapi/v1/ticker/bookTicker", {"symbol": "ETHUSDT"}); return "binance"
    except Exception as e: log("binance not reachable from this server:", e); return "okx"

def main():
    src = pick_source(); st = load_state(); last_hb = 0; last_doc = time.time()
    msg = f"E logger started | source={src} | log dir={DIR} | telegram={'ON' if TG_TOKEN and TG_CHAT else 'off'}"; log(msg); tg(msg)
    while True:
        nxt = (int(time.time() * 1000) // BAR + 1) * BAR + 6000
        while time.time() * 1000 < nxt:
            time.sleep(min(30, max(0.5, nxt / 1000 - time.time())))
            if st["pending"]: process_pending(st, int(time.time() * 1000)); save_state(st)
        cycle(st, src)
        if time.time() - last_hb > 6 * 3600:
            last_hb = time.time(); n = sum(1 for _ in open(SIGF)) - 1 if os.path.exists(SIGF) else 0
            tg(f"E logger alive | source={src} | signals so far {max(n, 0)} | pending {len(st['pending'])}")
        if time.time() - last_doc > 24 * 3600:
            last_doc = time.time()
            for f in (SIGF, OUTF, NEARF): tg_doc(f)

if __name__ == "__main__":
    main()
