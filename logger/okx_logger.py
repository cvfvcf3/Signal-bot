"""Forward logger for the 'E' signal (alert + log only, NO trading), run once per invocation (GitHub Actions cron).
Signal source: OKX perpetual swaps (5m candles + taker buy/sell volume).  Execution check: MEXC futures order book.
E signal = 5m quote-volume spike (>= 8x the prior 24h average) + taker-flow imbalance (12-bar mean >= +/-0.1), first bar of the event."""
import os, sys, time, json, csv, urllib.request, urllib.parse
COINS = ["ETH", "SOL", "XRP", "BNB", "DOGE", "ADA", "LINK", "AVAX", "LTC"]
OKX, MEXC = "https://www.okx.com", "https://contract.mexc.com"
VOLX, VOLX_NEAR, IMB, HOLD, K, NOTIONAL = 8.0, 5.0, 0.10, 36, 2.0, 200.0
BAR, SCAN, MAX_AGE_S = 300000, 8, 40 * 60
CHECK_MAX_S = 65 * 60
DIR = os.environ.get("LOG_DIR", "e_logs"); os.makedirs(DIR, exist_ok=True)
SIGF, NEARF, OUTF, STF = f"{DIR}/signals.csv", f"{DIR}/near.csv", f"{DIR}/outcomes.csv", f"{DIR}/state.json"
TG_TOKEN, TG_CHAT = os.environ.get("TG_TOKEN"), os.environ.get("TG_CHAT")

def api(url, params=None):
    if params: url += "?" + urllib.parse.urlencode(params)
    last = None
    for i in range(3):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "okx-logger/1.0"}), timeout=15) as r:
                return json.loads(r.read().decode())
        except Exception as e:
            last = e; time.sleep(1 + i)
    raise last

def mean(a): return sum(a) / len(a)
def inst(coin): return f"{coin}-USDT-SWAP"

def okx_candles(coin, now_ms):
    d = api(OKX + "/api/v5/market/candles", {"instId": inst(coin), "bar": "5m", "limit": 300})
    if d.get("code") != "0": raise RuntimeError("okx candles: " + str(d.get("msg")))
    rows = [x for x in d["data"] if int(x[0]) + BAR <= now_ms]          # closed bars only
    rows.sort(key=lambda x: int(x[0]))
    ts = [int(x[0]) for x in rows]; h = [float(x[2]) for x in rows]; l = [float(x[3]) for x in rows]; c = [float(x[4]) for x in rows]
    qv = [float(x[7]) if float(x[7]) > 0 else float(x[6]) * float(x[4]) for x in rows]
    return ts, h, l, c, qv

def okx_taker(coin):
    d = api(OKX + "/api/v5/rubik/stat/taker-volume-contract", {"instId": inst(coin), "period": "5m"})
    if d.get("code") != "0": raise RuntimeError("okx taker: " + str(d.get("msg")))
    return {int(x[0]): (float(x[2]), float(x[1])) for x in d["data"]}     # ts -> (buy, sell); rows are [ts, sell, buy]

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

def events(coin, ts, h, l, c, qv, tk):
    """all event-start bars among the last SCAN closed bars: (index, side, tier, volx, imb12)"""
    out = []; n = len(ts)
    for i in range(max(289, n - SCAN), n):
        f1, f0 = feats(qv, ts, tk, i), feats(qv, ts, tk, i - 1)
        if f1 is None: continue
        for side in (1, -1):
            def cond(f, vx):
                return f is not None and f[0] > vx and ((f[1] > IMB) if side == 1 else (f[1] < -IMB))
            for vx, tier in ((VOLX, "v12" if f1[0] > 12 else "v8"), (VOLX_NEAR, "v5")):
                if cond(f1, vx) and not cond(f0, vx):
                    out.append((i, side, tier if vx == VOLX else "v5", f1[0], f1[1]))
                    break
    return out

_csize = {}
def contract_size(coin):
    if not _csize:
        d = api(MEXC + "/api/v1/contract/detail")
        for x in d.get("data", []):
            if x.get("symbol", "").endswith("_USDT"): _csize[x["symbol"]] = float(x.get("contractSize") or 0)
    return _csize.get(f"{coin}_USDT")

def mexc_book(coin, now=None):
    d = api(MEXC + f"/api/v1/contract/depth/{coin}_USDT", {"limit": 20})["data"]
    asks, bids = d["asks"], d["bids"]
    return float(bids[0][0]), float(asks[0][0]), asks, bids

def vwap_fill(levels, cs, notional):
    if not cs: return None
    left, qty = notional, 0.0
    for p, q, *_ in levels:
        p, q = float(p), float(q) * cs; take = min(left, p * q); qty += take / p; left -= take
        if left <= 1e-9: break
    return (notional - left) / qty if qty > 0 and left <= 1e-9 else None

def tg(msg):
    if TG_TOKEN and TG_CHAT:
        try:
            req = urllib.request.Request(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage", data=urllib.parse.urlencode({"chat_id": TG_CHAT, "text": msg}).encode())
            urllib.request.urlopen(req, timeout=15)
        except Exception as e: print("telegram failed", e)

def load_state():
    try: return json.load(open(STF))
    except Exception: return {"pending": [], "busy": {}, "seen": []}
def save_state(s):
    s["seen"] = s["seen"][-400:]; json.dump(s, open(STF, "w"))
def append(path, header, row):
    new = not os.path.exists(path)
    with open(path, "a", newline="") as f:
        w = csv.writer(f)
        if new: w.writerow(header)
        w.writerow(row)
def utc(ms): return time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(ms / 1000))

def record(coin, ts, h, l, c, i, side, tier, volx, imb, now, st):
    ct = ts[i] + BAR; e = c[i]; a = atr15(ts[:i + 1], h[:i + 1], l[:i + 1], c[:i + 1])
    if a is None or a <= 0: return
    sd = K * a; sl = e - side * sd; sid = f"{coin}-{ct}"
    bid = ask = fill = slip = drift = spread = None
    try:
        bid, ask, asks, bids = mexc_book(coin); mid = (bid + ask) / 2
        spread = (ask - bid) / mid * 1e4
        fill = vwap_fill(asks if side == 1 else bids, contract_size(coin), NOTIONAL)
        if fill: slip = ((fill / mid - 1) if side == 1 else (mid / fill - 1)) * 1e4
        drift = side * (mid / e - 1) * 1e4
    except Exception as ex: print("mexc book failed", coin, ex)
    r = lambda v, n=2: None if v is None else round(v, n)
    append(SIGF, ["id", "signal_close_utc", "logged_utc", "latency_s", "coin", "side", "tier", "okx_close", "volx", "imb12", "stop_pct", "sl",
                  "mexc_bid", "mexc_ask", "spread_bps", "mexc_vwap_fill_200usd", "slip_vs_mid_bps", "drift_since_close_bps"],
           [sid, utc(ct), utc(now), round(now / 1000 - ct / 1000), coin, "LONG" if side == 1 else "SHORT", tier, e, r(volx), r(imb, 3),
            r(sd / e * 100, 3), r(sl, 6), bid, ask, r(spread), fill, r(slip), r(drift)])
    st["pending"].append(dict(id=sid, coin=coin, side=side, close_ts=ct, entry=e, sl=sl, path=[]))
    st["busy"][coin] = ct + HOLD * BAR
    msg = f'E signal {"LONG" if side == 1 else "SHORT"} {coin} ({tier}) ref {e} SL {r(sl, 6)} stop {r(sd / e * 100)}% | volx {volx:.1f} imb {imb:+.2f} | latency {round(now / 1000 - ct / 1000)}s | MEXC spread {r(spread)}bps slip {r(slip)}bps drift {r(drift)}bps (alert only)'
    print(msg); tg(msg)

def finish(p, now):
    d = api(OKX + "/api/v5/market/candles", {"instId": inst(p["coin"]), "bar": "5m", "limit": 300})
    rows = sorted([x for x in d["data"] if int(x[0]) >= p["close_ts"]], key=lambda x: int(x[0]))[:HOLD]
    if len(rows) < HOLD: return False
    side, e, sl = p["side"], p["entry"], p["sl"]; xp, why = float(rows[-1][4]), "TIME"
    for x in rows:
        hi, lo = float(x[2]), float(x[3])
        if (side == 1 and lo <= sl) or (side == -1 and hi >= sl): xp, why = sl, "SL"; break
    gross = side * (xp - e) / e * 1e4
    def fills(nb, thr):
        for x in rows[:nb]:
            if (side == 1 and float(x[3]) <= e * (1 - thr)) or (side == -1 and float(x[2]) >= e * (1 + thr)): return 1
        return 0
    append(OUTF, ["id", "exit_reason", "gross_bps_from_close", "limit_fill_1bar_0bps", "limit_fill_3bar_0bps", "limit_fill_3bar_2bps", "limit_fill_3bar_5bps", "mexc_mid_path"],
           [p["id"], why, round(gross, 1), fills(1, 0), fills(3, 0), fills(3, 0.0002), fills(3, 0.0005), json.dumps(p["path"])])
    print("outcome", p["id"], why, round(gross, 1), "bps")
    return True

def process_pending(st, now):
    keep = []
    for p in st["pending"]:
        el = now / 1000 - p["close_ts"] / 1000
        if el <= CHECK_MAX_S:
            try:
                b, a, _, _ = mexc_book(p["coin"]); p["path"].append([round(el), round((b + a) / 2, 6)])
            except Exception as ex: print("path failed", ex)
        if el >= HOLD * 300 + 600:
            try:
                if finish(p, now): continue
            except Exception as ex: print("finish failed", p["id"], ex)
        keep.append(p)
    st["pending"] = keep

def run(now=None):
    now = now or int(time.time() * 1000); st = load_state(); ok, newc, top, lag = 0, 0, ("-", 0.0), []
    for coin in COINS:
        try:
            ts, h, l, c, qv = okx_candles(coin, now); tk = okx_taker(coin); ok += 1
            if ts and tk:
                done = [t for t in tk if t + BAR <= now]; lag.append(round((ts[-1] - max(done)) / BAR)) if done else lag.append(99)
            n = len(ts)
            if n >= 292:
                f = feats(qv, ts, tk, n - 1)
                if f and f[0] > top[1]: top = (coin, f[0])
            for (i, side, tier, volx, imb) in events(coin, ts, h, l, c, qv, tk):
                ct = ts[i] + BAR; sid = f"{coin}-{ct}"
                if sid in st["seen"] or now - ct > MAX_AGE_S * 1000: continue
                st["seen"].append(sid)
                if tier == "v5":
                    append(NEARF, ["id", "signal_close_utc", "coin", "side", "volx", "imb12", "okx_close"], [sid, utc(ct), coin, "LONG" if side == 1 else "SHORT", round(volx, 2), round(imb, 3), c[i]])
                    continue
                if st["busy"].get(coin, 0) > ct: continue
                record(coin, ts, h, l, c, i, side, tier, volx, imb, now, st); newc += 1
        except Exception as ex: print("error", coin, ex)
        time.sleep(0.25)
    process_pending(st, now); save_state(st)
    print(f"{utc(now)} UTC | coins ok {ok}/{len(COINS)} | max volx {top[0]} {top[1]:.1f} | taker lag (bars) {lag} | new signals {newc} | pending {len(st['pending'])}")
    return ok

if __name__ == "__main__":
    ok = run()
    sys.exit(0 if ok else 0)
