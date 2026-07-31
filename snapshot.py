"""每日淨值快照:抓各帳戶持股 → 補收盤價 → 算淨值 → 寫入 SQLite。

用法:
    python snapshot.py            # 正常執行(建議收盤後跑)
排程後每天自動執行,同一天重跑會覆蓋當天資料。
"""
import sys
from pathlib import Path

import yaml
from dotenv import load_dotenv

import db
import prices
from brokers import ADAPTERS, fetch_fubon_futures

ROOT = Path(__file__).parent


def load_config():
    with open(ROOT / "config.yaml", encoding="utf-8") as f:
        return yaml.safe_load(f)


def main():
    load_dotenv(ROOT / ".env")
    cfg = load_config()
    token = prices.load_token(cfg["finmind_token_file"])

    # 1. 基準指數 —— 同時用來決定「今天的快照日期」(最近一個交易日)
    bench_ids = [str(b["id"]) for b in cfg.get("benchmarks", [])]
    bench = prices.latest_closes(bench_ids, token)
    if not bench:
        print("抓不到基準指數,FinMind 可能斷線,中止。")
        sys.exit(1)
    snap_date = max(d for d, _ in bench.values())
    bench_closes = {bid: close for bid, (d, close) in bench.items() if d == snap_date}
    print(f"快照日期(最近交易日): {snap_date}")

    # 2. 各帳戶持股(股票類)+ 期貨帳戶(權益數,非股數 x 現價)
    all_positions = []
    account_values = {}
    futures_values = {}
    for name, acc_cfg in cfg["accounts"].items():
        if not acc_cfg.get("enabled"):
            continue

        if acc_cfg["type"] == "fubon_futures":
            try:
                fut = fetch_fubon_futures(acc_cfg)
            except Exception as e:
                print(f"[錯誤] 帳戶 {name} 抓取失敗:{e}")
                print("為避免淨值失真(少算一個帳戶),本次中止,不寫入資料。")
                sys.exit(1)
            fut["underlying"] = str(acc_cfg.get("underlying", ""))
            fut["multiplier"] = float(acc_cfg.get("multiplier", 0) or 0)
            futures_values[name] = fut
            account_values[name] = fut["equity"]
            print(f"帳戶 {name}(期貨): 權益 {fut['equity']:,.0f}"
                  f",淨口數 {fut['net_lots']:+.0f}"
                  f"(未實現損益 {fut['unrealized_pnl']:+,.0f})")
            continue

        adapter = ADAPTERS[acc_cfg["type"]]
        try:
            positions = adapter(acc_cfg)
        except Exception as e:
            print(f"[錯誤] 帳戶 {name} 抓取失敗:{e}")
            print("為避免淨值失真(少算一個帳戶),本次中止,不寫入資料。")
            sys.exit(1)
        for p in positions:
            p["account"] = name
        all_positions.extend(positions)
        print(f"帳戶 {name}: {len(positions)} 檔持股")

    # 3. 補收盤價(adapter 沒給價格的,用 FinMind 收盤價)
    need_price = sorted({p["code"] for p in all_positions if p["price"] is None})
    if need_price:
        closes = prices.latest_closes(need_price, token)
        for p in all_positions:
            if p["price"] is None:
                got = closes.get(p["code"])
                if got is None:
                    print(f"[錯誤] {p['code']} 抓不到收盤價,中止(避免市值少算)。")
                    sys.exit(1)
                p["price"] = got[1]
                p["market_value"] = got[1] * p["shares"]

    # 補股票名稱(全市場對照表,本地快取,一個月才重抓)
    if all_positions:
        name_map = prices.stock_names(token)
        for p in all_positions:
            p["name"] = name_map.get(p["code"], "")

    for name in {p["account"] for p in all_positions}:
        account_values[name] = sum(
            p["market_value"] for p in all_positions if p["account"] == name
        )
    # 有啟用但零持股的帳戶也記 0,曲線才連續
    for name, acc_cfg in cfg["accounts"].items():
        if acc_cfg.get("enabled") and name not in account_values:
            account_values[name] = 0.0

    # 4. 期貨部位實質價值(= 標的收盤 × 乘數 × 淨口數),權益數扣掉它後算現金
    fut_underlyings = sorted({
        f["underlying"] for f in futures_values.values() if f["underlying"]
    })
    und_closes = prices.latest_closes(fut_underlyings, token) if fut_underlyings else {}
    for f in futures_values.values():
        und = f["underlying"]
        got = und_closes.get(und) if und else None
        if und and f["multiplier"] and f["net_lots"] and got is None:
            print(f"[錯誤] 期貨標的 {und} 抓不到收盤價,中止(避免實質價值算錯)。")
            sys.exit(1)
        und_close = got[1] if got else 0.0
        f["notional"] = und_close * f["multiplier"] * f["net_lots"]
        f["free_cash"] = f["equity"] - f["notional"]  # 權益數扣掉那一口 = 現金部分

    # 5. 現金與負債
    #    現金 = config 手動現金 + 期貨(權益數 − 實質價值)
    #    投資部位 = 證券市值 + 期貨實質價值(那一口以 0050 實質價值計)
    config_cash = sum(item["amount"] for item in cfg.get("cash", []) or [])
    debt = sum(item["amount"] for item in cfg.get("debt", []) or [])

    stock_mv = sum(p["market_value"] for p in all_positions)
    futures_position = sum(f["notional"] for f in futures_values.values())
    futures_free = sum(f["free_cash"] for f in futures_values.values())
    market_value = stock_mv + futures_position
    cash = config_cash + futures_free

    # 6. 寫入
    conn = db.connect()
    prev = db.previous_total(conn, snap_date)
    net_value = db.save_snapshot(
        conn, snap_date, market_value, account_values, all_positions,
        cash, debt, bench_closes
    )
    if futures_values:
        db.save_futures_snapshot(conn, snap_date, futures_values)

    # 7. 摘要
    mv = market_value
    cash_pct = cash / (net_value + debt) * 100 if (net_value + debt) else 0
    print("-" * 46)
    print(f"投資部位(證券+期貨實質價值) {mv:>12,.0f}")
    print(f"現金(含期貨閒置)     {cash:>15,.0f}({cash_pct:.1f}%)")
    print(f"負債      {debt:>16,.0f}")
    print(f"淨值      {net_value:>16,.0f}")
    if prev:
        chg = net_value - prev[1]
        pct = chg / prev[1] * 100 if prev[1] else 0
        print(f"vs {prev[0]}: {chg:+,.0f} ({pct:+.2f}%)")
    print(f"已寫入 networth.db(日期 {snap_date})")


if __name__ == "__main__":
    main()
