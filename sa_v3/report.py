"""Self-contained HTML reports with optional matplotlib charts."""
from __future__ import annotations
import base64
import io
import os
from datetime import datetime, timezone
import pandas as pd

CHARTS_DISABLED = os.environ.get("MATPLOTLIB_DISABLED", "0") == "1"
try:
    if CHARTS_DISABLED:
        raise ImportError("disabled by env")
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    CHARTS = True
except ImportError:
    CHARTS = False
    plt = None

_TINY_PNG = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNkYAAAAAYAAjCB0C8AAAAASUVORK5CYII="

def _ts_to_str(ms):
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M")

def _png_b64(fig):
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=80, bbox_inches="tight")
    plt.close(fig)
    return base64.b64encode(buf.getvalue()).decode("ascii")

def _equity_chart(equity_df):
    if not CHARTS:
        return _TINY_PNG
    fig, ax = plt.subplots(figsize=(10, 3.2))
    if len(equity_df):
        x = pd.to_datetime(equity_df["ts"], unit="ms", utc=True)
        ax.plot(x, equity_df["balance"], color="#4d6bfe", linewidth=1.4)
        ax.fill_between(x, equity_df["balance"], equity_df["balance"].min(),
                        alpha=0.1, color="#4d6bfe")
    ax.set_title("Equity curve", fontsize=10)
    ax.set_ylabel("Balance (USD)", fontsize=8)
    ax.tick_params(labelsize=7)
    ax.grid(alpha=0.2)
    return _png_b64(fig)

def _dd_chart(equity_df):
    if not CHARTS:
        return _TINY_PNG
    fig, ax = plt.subplots(figsize=(10, 2.6))
    if len(equity_df):
        x = pd.to_datetime(equity_df["ts"], unit="ms", utc=True)
        eq = equity_df["balance"].values
        peak = pd.Series(eq).cummax().values
        dd = (eq - peak) / peak * 100
        ax.fill_between(x, dd, 0, color="#ea3943", alpha=0.4)
        ax.plot(x, dd, color="#ea3943", linewidth=1.0)
    ax.set_title("Drawdown (%)", fontsize=10)
    ax.set_ylabel("%", fontsize=8)
    ax.tick_params(labelsize=7)
    ax.grid(alpha=0.2)
    return _png_b64(fig)

def _metrics_table(m):
    rows = [
        ("Trades", m.get("trades")),
        ("Wins / Losses / Flat", f"{m.get('wins')} / {m.get('losses')} / {m.get('flat')}"),
        ("Win rate", f"{m.get('win_rate_pct')}%" if m.get('win_rate_pct') is not None else "—"),
        ("Profit factor", m.get("profit_factor") if m.get("profit_factor") is not None else "—"),
        ("Net P&L (USD)", f"${m.get('net_pnl_usd')}"),
        ("Net P&L (bps)", m.get("net_pnl_bps")),
        ("Net P&L (%)", f"{m.get('net_pnl_pct')}%"),
        ("Starting / Ending balance", f"${m.get('starting_balance')} → ${m.get('ending_balance')}"),
        ("Sharpe (daily)", m.get("sharpe_daily")),
        ("Max drawdown (%)", f"{m.get('max_drawdown_pct')}%"),
        ("Calmar", m.get("calmar")),
        ("Avg holding (candles)", m.get("avg_holding_candles")),
        ("Avg win / loss (USD)", f"${m.get('avg_win_usd')} / ${m.get('avg_loss_usd')}"),
        ("Expectancy per trade", f"${m.get('expectancy_usd')}"),
    ]
    return "".join(f"<tr><th>{k}</th><td>{v}</td></tr>" for k, v in rows)

def _exits_table(m):
    src = m.get("exit_reasons", {})
    if not src:
        return "<tr><td colspan='2'>—</td></tr>"
    return "".join(f"<tr><td>{k}</td><td>{v}</td></tr>" for k, v in sorted(src.items()))

def _by_sym_table(m):
    src = m.get("by_symbol", {})
    if not src:
        return "<tr><td colspan='4'>—</td></tr>"
    rows = []
    for sym, s in sorted(src.items()):
        wr = (s["wins"] / s["count"] * 100) if s["count"] else 0
        rows.append(f"<tr><td>{sym}</td><td>{s['count']}</td><td>{wr:.1f}%</td>"
                    f"<td>${s['net_usd']:.2f}</td></tr>")
    return "".join(rows)

def _trades_table(trades, limit=200):
    if not trades:
        return "<tr><td colspan='10'>No trades</td></tr>"
    rows = []
    for t in trades[-limit:]:
        cls = "pos" if t["net_pnl_usd"] > 0 else ("neg" if t["net_pnl_usd"] < 0 else "")
        rows.append(
            f'<tr class="{cls}"><td>{_ts_to_str(t["entry_ts"])}</td>'
            f'<td>{t["symbol"]}</td><td>{t["mode"]}</td>'
            f'<td class="{"buy" if t["action"]=="BUY" else "sell"}">{t["action"]}</td>'
            f'<td>{t["entry_price"]:.6g}</td><td>{t["exit_price"]:.6g}</td>'
            f'<td>{t["exit_reason"]}</td><td>{t["holding_candles"]}</td>'
            f'<td>{t["net_pnl_bps"]:.1f}</td><td>${t["net_pnl_usd"]:.2f}</td></tr>'
        )
    return "".join(rows)

_TEMPLATE = """<!doctype html><html><head><meta charset="utf-8"><title>{title}</title>
<style>
body {{ font-family: -apple-system, Segoe UI, Roboto, sans-serif; background:#0b0e11; color:#e8ecef; margin:0 auto; padding:16px; max-width:1100px; }}
h1 {{ font-size:18px; margin:0 0 4px; }}
h2 {{ font-size:13px; margin:20px 0 8px; color:#8b95a1; text-transform:uppercase; letter-spacing:0.5px; }}
.sub {{ color:#8b95a1; font-size:11px; margin-bottom:16px; }}
table {{ width:100%; border-collapse:collapse; font-size:12px; margin-bottom:12px; }}
th, td {{ text-align:left; padding:5px 8px; border-bottom:1px solid #23282f; }}
th {{ color:#8b95a1; font-weight:500; }}
table.metrics th {{ width:45%; }}
tr.pos td {{ color:#16c784; }}
tr.neg td {{ color:#ea3943; }}
td.buy {{ color:#16c784; font-weight:600; }}
td.sell {{ color:#ea3943; font-weight:600; }}
img {{ width:100%; background:#12161b; border-radius:6px; padding:6px; box-sizing:border-box; }}
</style></head><body>
<h1>{title}</h1><div class="sub">{subtitle}</div>
<h2>Summary</h2><table class="metrics">{summary}</table>
<h2>Equity curve</h2><img src="data:image/png;base64,{equity_png}">
<h2>Drawdown</h2><img src="data:image/png;base64,{dd_png}">
<h2>Exit reasons</h2><table>{exits}</table>
<h2>By symbol</h2>
<table><thead><tr><th>Symbol</th><th>Trades</th><th>Win%</th><th>Net USD</th></tr></thead>
<tbody>{by_sym}</tbody></table>
<h2>Trades (recent 200)</h2>
<table><thead><tr><th>Entry</th><th>Symbol</th><th>Mode</th><th>Action</th>
<th>Entry px</th><th>Exit px</th><th>Reason</th><th>Held</th>
<th>Net bps</th><th>Net $</th></tr></thead><tbody>{trades}</tbody></table>
</body></html>"""

def write_report(result, config, out_dir="data/reports"):
    os.makedirs(out_dir, exist_ok=True)
    mode = result["mode"]
    ts_tag = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    path = os.path.join(out_dir, f"{mode}_{ts_tag}.html")
    cost = config["costs"]["fee_roundtrip_bps"] + config["costs"]["slippage_roundtrip_bps"]
    html = _TEMPLATE.format(
        title=f"Backtest — {mode}",
        subtitle=(f"{_ts_to_str(result['start_ms'])} → {_ts_to_str(result['end_ms'])} "
                  f"· {len(config['symbols'])} symbols · cost={cost}bps round-trip"),
        summary=_metrics_table(result["metrics"]),
        equity_png=_equity_chart(result["equity"]),
        dd_png=_dd_chart(result["equity"]),
        exits=_exits_table(result["metrics"]),
        by_sym=_by_sym_table(result["metrics"]),
        trades=_trades_table(result["trades"]),
    )
    with open(path, "w") as f:
        f.write(html)
    return path

def write_walkforward_report(wf, config, out_dir="data/reports"):
    os.makedirs(out_dir, exist_ok=True)
    mode = wf["mode"]
    ts_tag = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    path = os.path.join(out_dir, f"walkforward_{mode}_{ts_tag}.html")
    window_rows = "".join(
        f"<tr><td>{w['window']}</td>"
        f"<td>{_ts_to_str(w['train_start'])} → {_ts_to_str(w['train_end'])}</td>"
        f"<td>{_ts_to_str(w['test_start'])} → {_ts_to_str(w['test_end'])}</td>"
        f"<td>{w['oos_metrics'].get('trades')}</td>"
        f"<td>{w['oos_metrics'].get('net_pnl_usd')}</td>"
        f"<td>{w['oos_metrics'].get('sharpe_daily')}</td>"
        f"<td>{w['oos_metrics'].get('max_drawdown_pct')}%</td></tr>"
        for w in wf["windows"]
    )
    html = _TEMPLATE.format(
        title=f"Walk-forward — {mode}",
        subtitle=(f"OOS across {len(wf['windows'])} windows · "
                  f"{_ts_to_str(wf['start_ms'])} → {_ts_to_str(wf['end_ms'])}"),
        summary=_metrics_table(wf["oos_metrics"]),
        equity_png=_equity_chart(wf["oos_equity"]),
        dd_png=_dd_chart(wf["oos_equity"]),
        exits=_exits_table(wf["oos_metrics"]),
        by_sym=_by_sym_table(wf["oos_metrics"]),
        trades=_trades_table(wf["oos_trades"]),
    )
    windows_block = ("<h2>Windows</h2><table><thead><tr><th>#</th><th>Train</th>"
                     "<th>Test</th><th>Trades</th><th>Net $</th><th>Sharpe</th>"
                     f"<th>MaxDD</th></tr></thead><tbody>{window_rows}</tbody></table>")
    html = html.replace("<h2>Equity curve</h2>", windows_block + "\n<h2>Equity curve</h2>", 1)
    with open(path, "w") as f:
        f.write(html)
    return path

def summary_line(result):
    m = result["metrics"]
    return (f"trades={m['trades']}  net=${m['net_pnl_usd']}  "
            f"win={m['win_rate_pct']}%  sharpe={m['sharpe_daily']}  "
            f"maxDD={m['max_drawdown_pct']}%")

def wf_summary_line(wf):
    m = wf["oos_metrics"]
    return (f"OOS: trades={m['trades']}  net=${m['net_pnl_usd']}  "
            f"win={m['win_rate_pct']}%  sharpe={m['sharpe_daily']}  "
            f"maxDD={m['max_drawdown_pct']}%")
