"""只更新現金/負債,不重抓持股(不碰券商 API,秒開)。

改完 config.yaml 的 cash / debt 後,跑這支就好,不用跑完整的 snapshot.py。
它讀 config 現值,重算指定日期的淨值 = 已存市值 + 現金 − 負債。

用法:
    python update_cash.py              # 更新「最新一天」
    python update_cash.py 2026-07-22   # 更新指定某一天(補登用)
    python update_cash.py --all        # 把所有日期都套成目前 config 的現金/負債
"""
import sqlite3
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).parent
DB_PATH = ROOT / "networth.db"


def load_cash_debt():
    with open(ROOT / "config.yaml", encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    cash = sum(item["amount"] for item in cfg.get("cash", []) or [])
    debt = sum(item["amount"] for item in cfg.get("debt", []) or [])
    return cash, debt


def update_one(conn, date, config_cash, debt):
    row = conn.execute(
        "SELECT market_value, net_value FROM totals WHERE date=?", (date,)
    ).fetchone()
    if row is None:
        print(f"[略過] {date} 沒有快照資料")
        return
    mv, old_nv = row
    # 現金 = config 手動現金 + 該日期貨(權益數 − 實質價值)= free_cash。
    # 舊資料 free_cash 為 NULL(當天市值已含完整期貨權益),視為 0 不會重複計算,
    # 兩種情況淨值都正確。
    futures_free = conn.execute(
        "SELECT COALESCE(SUM(free_cash), 0) FROM futures_snapshots WHERE date=?",
        (date,),
    ).fetchone()[0]
    cash = config_cash + futures_free
    new_nv = mv + cash - debt
    conn.execute(
        "UPDATE totals SET cash=?, debt=?, net_value=? WHERE date=?",
        (cash, debt, new_nv, date),
    )
    extra = f"(含期貨閒置 {futures_free:,.0f})" if futures_free else ""
    print(f"{date}: 現金 {cash:,.0f}{extra} 負債 {debt:,.0f} | "
          f"淨值 {old_nv:,.0f} → {new_nv:,.0f}")


def main():
    cash, debt = load_cash_debt()
    conn = sqlite3.connect(DB_PATH)

    args = sys.argv[1:]
    if args and args[0] == "--all":
        dates = [r[0] for r in conn.execute("SELECT date FROM totals ORDER BY date")]
    elif args:
        dates = args
    else:
        row = conn.execute("SELECT MAX(date) FROM totals").fetchone()
        dates = [row[0]] if row and row[0] else []

    if not dates:
        print("資料庫還沒有任何快照,先跑 python snapshot.py")
        return

    with conn:
        for d in dates:
            update_one(conn, d, cash, debt)
    print("完成。重新整理 dashboard 即可看到更新。")


if __name__ == "__main__":
    main()
