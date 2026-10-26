"""Random-search walk-forward optimization."""
from __future__ import annotations
import json, os, random
from copy import deepcopy
import pandas as pd
from . import engine
from .metrics import compute_metrics

def _apply_params(config, mode, params):
    c = deepcopy(config)
    m = c["modes"][mode]
    for k, v in params.items():
        if k == "min_score_to_signal_buy":
            if isinstance(m["min_score_to_signal"], dict):
                m["min_score_to_signal"]["buy"] = v
            else:
                m["min_score_to_signal"] = {"buy": v, "sell": m["min_score_to_signal"]}
        elif k == "min_score_to_signal_sell":
            if isinstance(m["min_score_to_signal"], dict):
                m["min_score_to_signal"]["sell"] = v
            else:
                m["min_score_to_signal"] = {"buy": m["min_score_to_signal"], "sell": v}
        elif k == "atr_multiplier":
            c["adaptive_target"]["atr_multiplier"] = v
        else:
            m[k] = v
    return c

def _sample(space, rng):
    out = {}
    for name, spec in space.items():
        out[name] = rng.choice(spec["choices"])
    return out

def _score(metrics, objective):
    if objective == "sharpe_daily":
        return metrics.get("sharpe_daily", 0.0)
    if objective == "calmar":
        return metrics.get("calmar", 0.0)
    if objective == "net_pnl_bps":
        return metrics.get("net_pnl_bps", 0.0)
    return 0.0

def run_walk_forward(config, mode, symbols, store, start_ms, end_ms, verbose=False):
    wf_cfg = config["walkforward"]
    space = config["parameter_space"][mode]
    n_windows = wf_cfg["n_windows"]
    train_frac = wf_cfg["train_frac"]
    budget = wf_cfg["search_budget"]
    min_trades = wf_cfg["min_trades_in_train"]
    objective = wf_cfg["objective"]
    rng = random.Random(wf_cfg.get("random_seed", 42))

    data, funding, btc_regimes = engine.load_data_for_mode(
        config, mode, symbols, store, start_ms, end_ms
    )

    total_ms = end_ms - start_ms
    test_span = total_ms / n_windows
    train_span = int(total_ms * train_frac)

    windows = []
    oos_trades = []
    oos_equity = []
    cum_balance = config["risk"]["starting_balance_usd"]

    for w in range(n_windows):
        test_start = int(start_ms + w * test_span)
        test_end = int(start_ms + (w + 1) * test_span)
        train_start = max(start_ms, test_start - train_span)
        train_end = test_start

        if verbose:
            print(f"\n[wf] window {w+1}/{n_windows}")

        best_score = float("-inf")
        best_params = None
        for i in range(budget):
            params = _sample(space, rng)
            cfg_try = _apply_params(config, mode, params)
            res = engine.run_mode_backtest(cfg_try, mode, symbols, data, funding,
                                          btc_regimes, train_start, train_end)
            m = compute_metrics(res["trades"], res["equity"])
            if m["trades"] < min_trades:
                continue
            sc = _score(m, objective)
            if sc > best_score:
                best_score = sc; best_params = params

        if best_params is None:
            if verbose:
                print("     no valid params — using defaults")
            best_params = {}
            best_score = 0.0

        cfg_best = _apply_params(config, mode, best_params)
        res_test = engine.run_mode_backtest(cfg_best, mode, symbols, data, funding,
                                           btc_regimes, test_start, test_end)
        m_test = compute_metrics(res_test["trades"], res_test["equity"])

        windows.append({
            "window": w + 1, "train_start": train_start, "train_end": train_end,
            "test_start": test_start, "test_end": test_end,
            "best_params": best_params, "train_score": best_score, "oos_metrics": m_test,
        })

        for t in res_test["trades"]:
            oos_trades.append(t)
        if len(res_test["equity"]):
            eq = res_test["equity"].copy()
            eq["balance"] = eq["balance"] - eq["balance"].iloc[0] + cum_balance
            oos_equity.extend(list(zip(eq["ts"].tolist(), eq["balance"].tolist())))
            cum_balance = float(eq["balance"].iloc[-1])

        if verbose:
            print(f"     OOS: trades={m_test['trades']} net=${m_test['net_pnl_usd']} "
                  f"sharpe={m_test['sharpe_daily']}")

    equity_df = pd.DataFrame(oos_equity, columns=["ts", "balance"]) if oos_equity \
        else pd.DataFrame(columns=["ts", "balance"])
    oos_metrics = compute_metrics(oos_trades, equity_df)
    return {"mode": mode, "windows": windows, "oos_trades": oos_trades,
            "oos_equity": equity_df, "oos_metrics": oos_metrics,
            "start_ms": start_ms, "end_ms": end_ms}

def save_best_params(wf_result, config, out_dir="data/results"):
    os.makedirs(out_dir, exist_ok=True)
    mode = wf_result["mode"]
    keys = set()
    for w in wf_result["windows"]:
        keys.update(w["best_params"].keys())
    agg = {}
    for k in keys:
        vals = [w["best_params"].get(k) for w in wf_result["windows"] if k in w["best_params"]]
        if vals:
            agg[k] = max(set(vals), key=vals.count)
    out = {"mode": mode, "oos_metrics": wf_result["oos_metrics"],
           "windows_count": len(wf_result["windows"]), "suggested_params": agg}
    path = os.path.join(out_dir, f"walkforward_{mode}_best_params.json")
    with open(path, "w") as f:
        json.dump(out, f, indent=2, default=str)
    return path
