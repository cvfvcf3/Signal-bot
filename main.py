#!/usr/bin/env python3
"""Signal Advisor v3 — CLI entrypoint."""
import argparse
import os
import sys
from datetime import datetime, timezone

from dotenv import load_dotenv
load_dotenv()

from sa_v3.config import load_config
from sa_v3 import data as data_mod
from sa_v3 import engine as engine_mod
from sa_v3 import optimize as optimize_mod
from sa_v3 import report as report_mod
from sa_v3 import paper as paper_mod


def _parse_symbols(arg, config):
    if arg:
        return [s.strip() for s in arg.split(",") if s.strip()]
    return config["symbols"]

def cmd_download(args, config):
    days = args.days or config["data"]["default_days"]
    symbols = _parse_symbols(args.symbols, config)
    timeframes = args.timeframes.split(",") if args.timeframes else ["15m", "1h", "4h"]

    store = data_mod.make_store(config)
    end_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    start_ms = end_ms - days * 86_400_000

    print(f"Downloading {days} days for {len(symbols)} symbols × {len(timeframes)} timeframes")
    for sym in symbols:
        for tf in timeframes:
            print(f"  {sym} {tf} ...", end="", flush=True)
            try:
                df = store.get(sym, tf, start_ms, end_ms)
                print(f" {len(df)} candles")
            except Exception as e:
                print(f" ERROR: {e}")

    print("\nFundings ...")
    for sym in symbols:
        try:
            n = len(store.get_funding(sym, start_ms, end_ms))
            print(f"  {sym}: {n} records")
        except Exception as e:
            print(f"  {sym}: ERROR {e}")

def cmd_backtest(args, config):
    symbols = _parse_symbols(args.symbols, config)
    modes = config["active_modes"] if args.mode == "all" else [args.mode]
    days = args.days or config["data"]["default_days"]
    start_ms, end_ms = engine_mod.default_window(days)

    store = data_mod.make_store(config)
    for mode in modes:
        print(f"\n=== Backtest: {mode} ===")
        result = engine_mod.run_single_backtest(
            config=config, mode=mode, symbols=symbols, store=store,
            start_ms=start_ms, end_ms=end_ms, verbose=True,
        )
        path = report_mod.write_report(result, config, out_dir="data/reports")
        print(f"Report: {path}")
        print(report_mod.summary_line(result))

def cmd_walkforward(args, config):
    symbols = _parse_symbols(args.symbols, config)
    modes = config["active_modes"] if args.mode == "all" else [args.mode]
    days = args.days or config["data"]["default_days"]
    start_ms, end_ms = engine_mod.default_window(days)

    if args.windows is not None:
        config["walkforward"]["n_windows"] = args.windows
    if args.budget is not None:
        config["walkforward"]["search_budget"] = args.budget

    store = data_mod.make_store(config)
    for mode in modes:
        print(f"\n=== Walk-forward: {mode} ===")
        wf_result = optimize_mod.run_walk_forward(
            config=config, mode=mode, symbols=symbols, store=store,
            start_ms=start_ms, end_ms=end_ms, verbose=True,
        )
        path = report_mod.write_walkforward_report(wf_result, config, out_dir="data/reports")
        best_path = optimize_mod.save_best_params(wf_result, config, out_dir="data/results")
        print(f"Report: {path}")
        print(f"Best params: {best_path}")
        print(report_mod.wf_summary_line(wf_result))

def cmd_paper(args, config):
    symbols = _parse_symbols(args.symbols, config)
    modes = config["active_modes"] if args.mode == "all" else [args.mode]
    if args.poll_seconds is not None:
        config["paper"]["poll_interval_seconds"] = args.poll_seconds
    runner = paper_mod.PaperRunner(config=config, symbols=symbols, modes=modes)
    runner.run_forever()

def cmd_dashboard(args, config):
    import sa_v3.dashboard; sa_v3.dashboard.serve(config, port=args.port or config["paper"]["dashboard_port"])

def cmd_status(args, config):
    from sa_v3 import journal
    db = config["paper"]["journal_db"]
    if not os.path.exists(db):
        print(f"No paper journal at {db}. Run `python main.py paper` first.")
        return
    journal.print_status(db)

def main():
    parser = argparse.ArgumentParser(description="Signal Advisor v3")
    parser.add_argument("--config", default="config.yaml")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("download")
    p.add_argument("--days", type=int, default=None)
    p.add_argument("--symbols", default=None)
    p.add_argument("--timeframes", default=None)

    p = sub.add_parser("backtest")
    p.add_argument("--mode", default="all")
    p.add_argument("--symbols", default=None)
    p.add_argument("--days", type=int, default=None)

    p = sub.add_parser("walkforward")
    p.add_argument("--mode", default="all")
    p.add_argument("--symbols", default=None)
    p.add_argument("--days", type=int, default=None)
    p.add_argument("--windows", type=int, default=None)
    p.add_argument("--budget", type=int, default=None)

    p = sub.add_parser("paper")
    p.add_argument("--mode", default="all")
    p.add_argument("--symbols", default=None)
    p.add_argument("--poll-seconds", type=int, default=None)

    p = sub.add_parser("dashboard")
    p.add_argument("--port", type=int, default=None)

    sub.add_parser("status")

    args = parser.parse_args()
    config = load_config(args.config)

    cmd = {
        "download": cmd_download,
        "backtest": cmd_backtest,
        "walkforward": cmd_walkforward,
        "paper": cmd_paper,
        "dashboard": cmd_dashboard,
        "status": cmd_status,
    }[args.command]
    cmd(args, config)

if __name__ == "__main__":
    main()
