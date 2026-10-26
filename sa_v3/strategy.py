"""Strategy — single source of truth for signal decisions."""
from __future__ import annotations
import pandas as pd
from . import layers
from .utils import atr_pct, frac_to_bps

class Strategy:
    def __init__(self, config):
        self.config = config

    def _run_layers(self, context):
        mode_cfg = self.config["modes"][context["mode"]]
        layer_cfg = self.config["layers"]
        primary = context["primary_ohlcv"]

        scores = {
            "technical": layers.technical(primary),
            "multi_timeframe": layers.multi_timeframe(context["confirm_ohlcv"]),
            "volume_imbalance": layers.volume_imbalance(
                primary,
                lookback=layer_cfg["volume_imbalance"].get("lookback", 20),
                vol_multiplier=layer_cfg["volume_imbalance"].get("vol_multiplier", 1.5),
            ),
            "funding": layers.funding(
                context["funding"],
                rolling_window=layer_cfg["funding"].get("rolling_window", 100),
                min_history=layer_cfg["funding"].get("min_history", 20),
            ),
            "smc": layers.smc(
                primary,
                fractal_left=layer_cfg["smc"]["fractal_left"],
                fractal_right=layer_cfg["smc"]["fractal_right"],
            ),
        }

        weights = dict(mode_cfg["weights"])
        cap = mode_cfg.get("smc_weight_cap_if_htf_disagrees")
        if cap is not None and weights.get("smc", 0) > cap:
            smc_r = scores["smc"]; mtf_r = scores["multi_timeframe"]
            smc_bull = smc_r["bullish_score"] > smc_r["bearish_score"]
            mtf_bull = mtf_r["bullish_score"] > mtf_r["bearish_score"]
            if smc_bull != mtf_bull:
                weights["smc"] = cap
        return scores, weights

    def _composite(self, scores, weights):
        bull = sum(scores[k]["bullish_score"] * weights.get(k, 0) / 100 for k in scores)
        bear = sum(scores[k]["bearish_score"] * weights.get(k, 0) / 100 for k in scores)
        return round(min(bull, 100), 2), round(min(bear, 100), 2)

    def _apply_btc_filter(self, bull, bear, btc_regime, is_btc):
        cfg = self.config["btc_macro"]
        if is_btc or not cfg.get("enabled", True):
            return bull, bear
        if btc_regime == "bullish":
            m = cfg["bullish"]; bull *= m["buy_multiplier"]; bear *= m["sell_multiplier"]
        elif btc_regime == "bearish":
            m = cfg["bearish"]; bull *= m["buy_multiplier"]; bear *= m["sell_multiplier"]
        return round(max(0.0, min(100.0, bull)), 2), round(max(0.0, min(100.0, bear)), 2)

    def _decide(self, bull, bear, mode_cfg):
        m = mode_cfg["min_score_to_signal"]
        if isinstance(m, dict):
            buy_t, sell_t = m["buy"], m["sell"]
        else:
            buy_t = sell_t = m
        gap = mode_cfg["min_score_gap"]
        if bull >= buy_t and (bull - bear) >= gap:
            return {"action": "BUY", "confidence": bull}
        if bear >= sell_t and (bear - bull) >= gap:
            return {"action": "SELL", "confidence": bear}
        return {"action": "WAIT", "confidence": max(bull, bear)}

    def evaluate(self, context):
        mode_cfg = self.config["modes"][context["mode"]]
        scores, weights = self._run_layers(context)
        bull, bear = self._composite(scores, weights)
        bull, bear = self._apply_btc_filter(bull, bear, context["btc_regime"], context["is_btc"])
        decision = self._decide(bull, bear, mode_cfg)
        return {"action": decision["action"], "confidence": decision["confidence"],
                "bullish_score": bull, "bearish_score": bear, "layers": scores}

    def compute_tp_sl(self, action, entry_price, primary_ohlcv, mode_name):
        mode_cfg = self.config["modes"][mode_name]
        atr_mult = self.config["adaptive_target"]["atr_multiplier"]
        atr = atr_pct(primary_ohlcv, period=mode_cfg.get("atr_period", 14))
        target_bps = max(float(mode_cfg["success_move_bps"]), frac_to_bps(atr) * atr_mult)
        stop_bps = target_bps / float(mode_cfg["rr"])
        min_stop = float(mode_cfg["min_stop_bps"])
        if stop_bps < min_stop:
            stop_bps = min_stop
            target_bps = stop_bps * float(mode_cfg["rr"])

        min_net_rr = self.config["risk"].get("min_net_rr")
        if min_net_rr is not None:
            cost = (self.config["costs"]["fee_roundtrip_bps"]
                    + self.config["costs"]["slippage_roundtrip_bps"])
            net_reward = target_bps - cost
            net_risk = stop_bps + cost
            if net_risk <= 0 or (net_reward / net_risk) < min_net_rr:
                return None

        if action == "BUY":
            tp = entry_price * (1 + target_bps / 10_000)
            sl = entry_price * (1 - stop_bps / 10_000)
        else:
            tp = entry_price * (1 - target_bps / 10_000)
            sl = entry_price * (1 + stop_bps / 10_000)
        return {"tp_price": round(tp, 8), "sl_price": round(sl, 8),
                "target_bps": target_bps, "stop_bps": stop_bps}
