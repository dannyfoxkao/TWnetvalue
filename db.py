"""SQLite 儲存層:每日快照、持股明細、基準指數。"""
import sqlite3
from pathlib import Path

DB_PATH = Path(__file__).parent / "networth.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS totals (
    date TEXT PRIMARY KEY,          -- 交易日 YYYY-MM-DD
    market_value REAL NOT NULL,     -- 各帳戶證券市值合計
    cash REAL NOT NULL,             -- 交割戶餘額 + 手動現金 + 期貨閒置
    debt REAL NOT NULL,
    net_value REAL NOT NULL,        -- market_value + cash + unsettled - debt
    unsettled REAL,                 -- 未交割款淨額(T+2 制度修正,應收正/應付負)
    broker_cash REAL                -- 其中來自券商交割戶的部分(自動抓,供重算用)
);
CREATE TABLE IF NOT EXISTS account_snapshots (
    date TEXT NOT NULL,
    account TEXT NOT NULL,
    market_value REAL NOT NULL,
    PRIMARY KEY (date, account)
);
CREATE TABLE IF NOT EXISTS positions (
    date TEXT NOT NULL,
    account TEXT NOT NULL,
    code TEXT NOT NULL,
    name TEXT,
    shares REAL NOT NULL,
    price REAL,
    market_value REAL,
    PRIMARY KEY (date, account, code)
);
CREATE TABLE IF NOT EXISTS benchmarks (
    date TEXT NOT NULL,
    bench_id TEXT NOT NULL,
    close REAL NOT NULL,
    PRIMARY KEY (date, bench_id)
);
CREATE TABLE IF NOT EXISTS futures_snapshots (
    date TEXT NOT NULL,
    account TEXT NOT NULL,
    cash_balance REAL NOT NULL,     -- 保證金餘額(不含未實現損益)
    unrealized_pnl REAL NOT NULL,   -- 未實現損益(期貨+選擇權)
    equity REAL NOT NULL,           -- 權益數 = cash_balance + unrealized_pnl
    net_lots REAL,                  -- 淨口數(多−空)
    notional REAL,                  -- 部位實質價值 = 標的收盤 × 乘數 × 口數(那一口的價值)
    free_cash REAL,                 -- 權益數 − 實質價值,歸類為現金
    PRIMARY KEY (date, account)
);
"""


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.executescript(SCHEMA)
    _migrate(conn)
    return conn


def _migrate(conn):
    """對既有資料庫補上後來才加的欄位(CREATE TABLE IF NOT EXISTS 不會改舊表)。"""
    tot_cols = {row[1] for row in conn.execute("PRAGMA table_info(totals)")}
    for col in ("unsettled", "broker_cash"):
        if col not in tot_cols:
            conn.execute(f"ALTER TABLE totals ADD COLUMN {col} REAL")
    pos_cols = {row[1] for row in conn.execute("PRAGMA table_info(positions)")}
    if "name" not in pos_cols:
        conn.execute("ALTER TABLE positions ADD COLUMN name TEXT")
    fut_cols = {row[1] for row in conn.execute("PRAGMA table_info(futures_snapshots)")}
    for col in ("net_lots", "notional", "free_cash"):
        if col not in fut_cols:
            conn.execute(f"ALTER TABLE futures_snapshots ADD COLUMN {col} REAL")
    conn.commit()


def save_snapshot(conn, date, market_value, account_values, positions, cash, debt,
                  bench_closes, unsettled=0.0, broker_cash=0.0):
    """寫入一天的快照。同一天重跑會覆蓋(以最後一次為準)。

    market_value 由呼叫端算好傳入(證券市值 + 期貨部位實質價值),不再由
    account_values 直接加總——因為期貨的閒置保證金已改歸類到 cash。
    account_values 仍保留各帳戶總值,供「各帳戶市值」圖使用。

    unsettled 是未交割款淨額(T+2 制度):賣出後錢還沒進來(正數)、
    買進後錢還沒扣(負數),不加這項的話交易後兩天淨值會失真。
    """
    net_value = market_value + cash + unsettled - debt
    with conn:
        conn.execute(
            "INSERT OR REPLACE INTO totals"
            " (date, market_value, cash, debt, net_value, unsettled, broker_cash)"
            " VALUES (?,?,?,?,?,?,?)",
            (date, market_value, cash, debt, net_value, unsettled, broker_cash),
        )
        conn.execute("DELETE FROM account_snapshots WHERE date=?", (date,))
        conn.executemany(
            "INSERT INTO account_snapshots VALUES (?,?,?)",
            [(date, acc, mv) for acc, mv in account_values.items()],
        )
        conn.execute("DELETE FROM positions WHERE date=?", (date,))
        conn.executemany(
            "INSERT INTO positions (date, account, code, name, shares, price, market_value)"
            " VALUES (?,?,?,?,?,?,?)",
            [(date, p["account"], p["code"], p.get("name"), p["shares"],
              p["price"], p["market_value"])
             for p in positions],
        )
        conn.executemany(
            "INSERT OR REPLACE INTO benchmarks VALUES (?,?,?)",
            [(date, bid, close) for bid, close in bench_closes.items()],
        )
    return net_value


def save_futures_snapshot(conn, date, futures_values: dict):
    """寫入一天的期貨帳戶快照(保證金餘額 vs 未實現損益 vs 原始/可動用保證金)。"""
    with conn:
        conn.execute("DELETE FROM futures_snapshots WHERE date=?", (date,))
        conn.executemany(
            "INSERT INTO futures_snapshots"
            " (date, account, cash_balance, unrealized_pnl, equity,"
            "  net_lots, notional, free_cash) VALUES (?,?,?,?,?,?,?,?)",
            [(date, acc, f["cash_balance"], f["unrealized_pnl"], f["equity"],
              f.get("net_lots"), f.get("notional"), f.get("free_cash"))
             for acc, f in futures_values.items()],
        )


def previous_total(conn, before_date):
    """回傳 before_date 之前最近一筆 (date, net_value),沒有則 None。"""
    row = conn.execute(
        "SELECT date, net_value FROM totals WHERE date < ? ORDER BY date DESC LIMIT 1",
        (before_date,),
    ).fetchone()
    return row
