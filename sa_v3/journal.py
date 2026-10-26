"""SQLite journal for paper-live."""
from __future__ import annotations
import json, os, sqlite3, threading, uuid
from datetime import datetime, timezone

_lock = threading.Lock()

def _connect(path):
    conn = sqlite3.connect(path, timeout=10)
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA busy_timeout=5000;")
    conn.row_factory = sqlite3.Row
    return conn

def init_db(path):
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with _lock:
        conn = _connect(path)
        try:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS signals (
                    signal_id TEXT PRIMARY KEY, symbol TEXT NOT NULL, mode TEXT NOT NULL,
                    action TEXT NOT NULL, created_at TEXT NOT NULL, entry_ts INTEGER NOT NULL,
                    entry_price REAL NOT NULL, tp_price REAL NOT NULL, sl_price REAL NOT NULL,
                    size_usd REAL NOT NULL, confidence REAL, layers_json TEXT,
                    status TEXT NOT NULL DEFAULT 'OPEN', exit_ts INTEGER, exit_price REAL,
                    exit_reason TEXT, gross_pnl_pct REAL, net_pnl_pct REAL,
                    gross_pnl_usd REAL, net_pnl_usd REAL
                );
                CREATE INDEX IF NOT EXISTS idx_status ON signals(status);
                CREATE INDEX IF NOT EXISTS idx_sym_mode ON signals(symbol, mode);
                CREATE TABLE IF NOT EXISTS state (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            """)
            conn.commit()
        finally:
            conn.close()

def get_state(path, key, default=None):
    conn = _connect(path)
    try:
        row = conn.execute("SELECT value FROM state WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else default
    finally:
        conn.close()

def set_state(path, key, value):
    with _lock:
        conn = _connect(path)
        try:
            conn.execute("INSERT INTO state (key, value) VALUES (?, ?) "
                         "ON CONFLICT(key) DO UPDATE SET value = excluded.value", (key, value))
            conn.commit()
        finally:
            conn.close()

def record_signal_open(path, symbol, mode, action, entry_ts, entry_price,
                      tp_price, sl_price, size_usd, confidence, layers):
    signal_id = str(uuid.uuid4())
    created_at = datetime.now(timezone.utc).isoformat()
    with _lock:
        conn = _connect(path)
        try:
            conn.execute(
                "INSERT INTO signals (signal_id, symbol, mode, action, created_at, "
                "entry_ts, entry_price, tp_price, sl_price, size_usd, confidence, "
                "layers_json, status) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,'OPEN')",
                (signal_id, symbol, mode, action, created_at, entry_ts, entry_price,
                 tp_price, sl_price, size_usd, confidence, json.dumps(layers)),
            )
            conn.commit()
        finally:
            conn.close()
    return signal_id

def record_signal_close(path, signal_id, exit_ts, exit_price, exit_reason,
                       gross_pnl_pct, net_pnl_pct, gross_pnl_usd, net_pnl_usd):
    with _lock:
        conn = _connect(path)
        try:
            conn.execute(
                "UPDATE signals SET status='CLOSED', exit_ts=?, exit_price=?, "
                "exit_reason=?, gross_pnl_pct=?, net_pnl_pct=?, gross_pnl_usd=?, "
                "net_pnl_usd=? WHERE signal_id=?",
                (exit_ts, exit_price, exit_reason, gross_pnl_pct, net_pnl_pct,
                 gross_pnl_usd, net_pnl_usd, signal_id),
            )
            conn.commit()
        finally:
            conn.close()

def get_open_positions(path):
    conn = _connect(path)
    try:
        return [dict(r) for r in conn.execute(
            "SELECT * FROM signals WHERE status='OPEN'").fetchall()]
    finally:
        conn.close()

def get_all_trades(path, limit=500):
    conn = _connect(path)
    try:
        return [dict(r) for r in conn.execute(
            "SELECT * FROM signals WHERE status='CLOSED' ORDER BY exit_ts DESC LIMIT ?",
            (limit,)).fetchall()]
    finally:
        conn.close()

def get_summary(path):
    conn = _connect(path)
    try:
        closed = conn.execute("SELECT * FROM signals WHERE status='CLOSED'").fetchall()
        open_c = conn.execute("SELECT COUNT(*) as c FROM signals WHERE status='OPEN'").fetchone()
        total = len(closed)
        wins = sum(1 for r in closed if (r["net_pnl_usd"] or 0) > 0)
        net_usd = sum((r["net_pnl_usd"] or 0) for r in closed)
        net_pct = sum((r["net_pnl_pct"] or 0) for r in closed)
        return {"open_positions": open_c["c"] if open_c else 0, "closed_trades": total,
                "wins": wins, "losses": total - wins,
                "win_rate_pct": round(wins / total * 100, 2) if total else None,
                "net_pnl_usd": round(net_usd, 2), "net_pnl_pct": round(net_pct, 3)}
    finally:
        conn.close()

def print_status(path):
    if not os.path.exists(path):
        print(f"No journal at {path}")
        return
    s = get_summary(path)
    print("=== Paper-live status ===")
    for k, v in s.items():
        print(f"  {k}: {v}")
    for p in get_open_positions(path):
        print(f"  OPEN: {p['symbol']} {p['mode']} {p['action']} "
              f"entry={p['entry_price']:.6g} tp={p['tp_price']:.6g} sl={p['sl_price']:.6g}")
