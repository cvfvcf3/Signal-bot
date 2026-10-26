"""Paper-live runner — no orders, public data only."""
from __future__ import annotations
import time
import pandas as pd
from . import data as data_mod
from . import engine
from . import journal
from .strategy import Strategy
from .utils import tf_to_ms

class PaperRunner:
    def __init__(self, config, symbols, modes):
        self.config = config
        self.symbols = symbols
        self.modes = modes
        self.db = config["paper"]["journal_db"]
        self.poll = config["paper"]["poll_interval_seconds"]
        self.strategy = Strategy(config)
        self.store = data_mod.make_store(config)
        self.cost_bps = (config["costs"]["fee_roundtrip_bps"]
                         + config["costs"]["slippage_roundtrip_bps"])
        journal.init_db(self.db)

    def run_forever(self):
        print(f"Paper-live (DB: {self.db}, poll: {self.poll}s). Ctrl-C to stop.")
        try:
            while True:
                try:
                    self.tick()
                except Exception as e:
                    print(f"[tick error] {e}")
                time.sleep(self.poll)
        except KeyboardInterrupt:
            print("\nStopped.")

    def _now(self):
        return int(time.time() * 1000)

    def _fetch(self, symbol, tf, lookback=250):
        end = self._now()
        start = end - lookback * tf_to_ms(tf)
        df = self.store.get(symbol, tf, start, end)
        if len(df):
            df = df[df["ts"] + tf_to_ms(tf) <= end].reset_index(drop=True)
        return df

    def tick(self):
        for mode in self.modes:
            self._tick_mode(mode)

    def _tick_mode(self, mode):
        mode_cfg = self.config["modes"][mode]
        primary_tf = mode_cfg["primary_timeframe"]
        confirm_tfs = mode_cfg["confirm_timeframes"]
        for sym in self.symbols:
            try:
                self._tick_symbol(mode, sym, primary_tf, confirm_tfs)
            except Exception as e:
                print(f"[{mode}/{sym}] error: {e}")

    def _tick_symbol(self, mode, sym, primary_tf, confirm_tfs):
        mode_cfg = self.config["modes"][mode]
        primary = self._fetch(sym, primary_tf)
        if len(primary) < 100:
            return

        for pos_row in [p for p in journal.get_open_positions(self.db)
                        if p["symbol"] == sym and p["mode"] == mode]:
            self._resolve(pos_row, primary, mode_cfg)

        still = [p for p in journal.get_open_positions(self.db)
                 if p["symbol"] == sym and p["mode"] == mode]
        if still:
            return

        cooldown_ms = mode_cfg.get("cooldown_minutes", 0) * 60_000
        last = int(journal.get_state(self.db, f"last_sig_{sym}_{mode}", "0") or "0")
        newest = int(primary["ts"].iloc[-1])
        if last and newest - last < cooldown_ms:
            return
        open_in_mode = [p for p in journal.get_open_positions(self.db) if p["mode"] == mode]
        if len(open_in_mode) >= mode_cfg.get("max_concurrent_positions", 1):
            return

        confirm = {}
        for tf in confirm_tfs:
            df = self._fetch(sym, tf)
            confirm[tf] = df if len(df) else pd.DataFrame()
        funding = self._funding(sym)
        ctx = {"mode": mode, "primary_ohlcv": primary, "confirm_ohlcv": confirm,
               "funding": funding, "btc_regime": self._btc_regime(),
               "is_btc": sym == self.config["btc_symbol"]}
        decision = self.strategy.evaluate(ctx)
        if decision["action"] not in ("BUY", "SELL"):
            return
        entry = float(primary["close"].iloc[-1])
        sizing = self.strategy.compute_tp_sl(decision["action"], entry, primary, mode)
        if sizing is None:
            return
        size_usd = engine.position_size_usd(self.config, self._balance(), entry, sizing["sl_price"])
        if size_usd <= 0:
            return
        layers = {k: {"bull": v["bullish_score"], "bear": v["bearish_score"]}
                  for k, v in decision["layers"].items()}
        journal.record_signal_open(self.db, sym, mode, decision["action"], newest,
                                   entry, sizing["tp_price"], sizing["sl_price"],
                                   size_usd, decision["confidence"], layers)
        journal.set_state(self.db, f"last_sig_{sym}_{mode}", str(newest))
        print(f"[OPEN] {mode} {sym} {decision['action']} @ {entry:.6g} "
              f"tp={sizing['tp_price']:.6g} sl={sizing['sl_price']:.6g} ${size_usd:.2f}")

    def _resolve(self, pos_row, primary, mode_cfg):
        p = engine.Position(
            signal_id=pos_row["signal_id"], symbol=pos_row["symbol"],
            mode=pos_row["mode"], action=pos_row["action"],
            entry_ts=int(pos_row["entry_ts"]), entry_index=0,
            entry_price=float(pos_row["entry_price"]),
            tp_price=float(pos_row["tp_price"]), sl_price=float(pos_row["sl_price"]),
            size_usd=float(pos_row["size_usd"]), target_bps=0.0, stop_bps=0.0,
            peak=float(pos_row["entry_price"]),
        )
        pk = f"peak_{pos_row['signal_id']}"; ak = f"armed_{pos_row['signal_id']}"
        p.peak = float(journal.get_state(self.db, pk, str(pos_row["entry_price"])) or pos_row["entry_price"])
        p.armed = journal.get_state(self.db, ak, "0") == "1"

        newer = primary[primary["ts"] > pos_row["entry_ts"]].reset_index(drop=True)
        for i, row in newer.iterrows():
            res = engine.resolve_exit(p, row, mode_cfg, self.cost_bps)
            journal.set_state(self.db, pk, str(p.peak))
            journal.set_state(self.db, ak, "1" if p.armed else "0")
            if res is not None:
                self._close(p, int(row["ts"]), res["price"], res["reason"], i + 1)
                return
            if i + 1 >= mode_cfg["evaluation_candles"]:
                self._close(p, int(row["ts"]), float(row["close"]), "TIMEOUT", i + 1)
                return

    def _close(self, p, exit_ts, exit_price, reason, held):
        trade = engine.make_trade(p, exit_ts, exit_price, reason, self.cost_bps)
        trade.holding_candles = held
        journal.record_signal_close(self.db, p.signal_id, exit_ts, exit_price, reason,
                                    trade.gross_pnl_pct, trade.net_pnl_pct,
                                    trade.gross_pnl_usd, trade.net_pnl_usd)
        print(f"[CLOSE] {p.mode} {p.symbol} {p.action} {reason} @ {exit_price:.6g} "
              f"net={trade.net_pnl_pct:.3f}% (${trade.net_pnl_usd:.2f})")

    def _funding(self, symbol):
        end = self._now()
        start = end - 30 * 86_400_000
        try:
            return self.store.get_funding(symbol, start, end)
        except Exception:
            return pd.DataFrame(columns=["ts", "rate"])

    def _btc_regime(self):
        tf = self.config["btc_macro"]["htf_timeframe"]
        df = self._fetch(self.config["btc_symbol"], tf, 250)
        if len(df) < 60:
            return "neutral"
        from . import layers
        r = layers.technical(df)
        thr = self.config["btc_macro"]["strong_threshold"]
        if r["bullish_score"] >= thr:
            return "bullish"
        if r["bearish_score"] >= thr:
            return "bearish"
        return "neutral"

    def _balance(self):
        return self.config["risk"]["starting_balance_usd"] + journal.get_summary(self.db)["net_pnl_usd"]
