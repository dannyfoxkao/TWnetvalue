"""每日淨值快照:抓各帳戶持股 → 補收盤價 → 算淨值 → 寫入 SQLite。

用法:
    python snapshot.py            # 正常執行(建議收盤後跑)
排程後每天自動執行,同一天重跑會覆蓋當天資料。
"""
import datetime as dt
import math
import sys
from pathlib import Path

import yaml
from dotenv import load_dotenv

import cfgutil
import db
import prices
from brokers import ADAPTERS, fetch_fubon_futures

ROOT = Path(__file__).parent


def load_config():
    with open(ROOT / "config.yaml", encoding="utf-8") as f:
        return yaml.safe_load(f)


SETTLED_HOUR = 14      # 交割扣款完成的保守時點(台股約上午完成)


def check_run_time(force: bool):
    """平日太早跑會抓到「帳務還沒結算完」的中間狀態,先擋下來。

    交割日的扣款約在上午完成,而券商的交割清單在扣款後仍會保留該筆
    (分辨不出扣款與否),所以未交割款只能靠「交割日是否已過」判斷。
    這個判斷在扣款前是錯的:錢還在交割戶、應付款也還在,會少算一筆義務。
    收盤後再跑就沒有這個問題,排程設在 16:32 本來就安全。
    """
    now = dt.datetime.now()
    if force or now.weekday() >= 5 or now.hour >= SETTLED_HOUR:
        return
    print(f"[中止] 現在是 {now:%H:%M},交割扣款可能還沒完成。")
    print(f"  這時候跑會抓到中間狀態:錢還在交割戶、應付款卻已被視為交割完成,")
    print(f"  淨值會失真。請在 {SETTLED_HOUR}:00 之後再跑(排程 16:32 不受影響)。")
    print(f"  確定要跑請加 --force。")
    sys.exit(1)


def main():
    check_run_time("--force" in sys.argv)
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
    broker_cash = 0.0      # 各券商交割戶餘額(自動抓)
    unsettled = 0.0        # 未交割款淨額(T+2 修正)
    account_cash = {}      # 逐帳戶現金明細,供儀表板分開列出
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
            futures_values[name] = fut
            account_values[name] = fut["equity"]
            print(f"帳戶 {name}(期貨): 權益 {fut['equity']:,.0f}"
                  f",淨口數 {fut['net_lots']:+.0f}"
                  f"(未實現損益 {fut['unrealized_pnl']:+,.0f})")
            continue

        adapter = ADAPTERS[acc_cfg["type"]]
        try:
            res = adapter(acc_cfg)
        except Exception as e:
            print(f"[錯誤] 帳戶 {name} 抓取失敗:{e}")
            print("為避免淨值失真(少算一個帳戶),本次中止,不寫入資料。")
            sys.exit(1)
        positions = res["positions"]
        for p in positions:
            p["account"] = name
        all_positions.extend(positions)
        broker_cash += res.get("settlement_cash", 0.0)
        unsettled += res.get("unsettled", 0.0)
        account_cash[name] = {
            "settlement_cash": res.get("settlement_cash", 0.0),
            "unsettled": res.get("unsettled", 0.0),
        }
        msg = f"帳戶 {name}: {len(positions)} 檔持股"
        if res.get("settlement_cash"):
            msg += f",交割戶 {res['settlement_cash']:,.0f}"
        if res.get("unsettled"):
            msg += f",未交割款 {res['unsettled']:+,.0f}"
        print(msg)

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
            # adapter 已給名稱的(如海外持股)不覆蓋
            if not p.get("name"):
                p["name"] = name_map.get(p["code"], "")

    for name in {p["account"] for p in all_positions}:
        account_values[name] = sum(
            p["market_value"] for p in all_positions if p["account"] == name
        )
    # 有啟用但零持股的帳戶也記 0,曲線才連續
    for name, acc_cfg in cfg["accounts"].items():
        if acc_cfg.get("enabled") and name not in account_values:
            account_values[name] = 0.0

    # 4. 期貨:實質價值由 adapter 用「期貨即時報價 × 乘數 × 口數」算好,
    #    權益數扣掉實質價值後的餘額歸類為現金。
    #    注意:槓桿部位的實質價值可能大於權益數,此時 free_cash 為負,
    #    那是正確的——負現金代表你的曝險超過自有資金(借來的部位)。
    for acc_name, f in futures_values.items():
        f["free_cash"] = f["equity"] - f["notional"]
        # 期貨帳戶的「現金」就是權益數扣掉部位後的閒置保證金
        account_cash[acc_name] = {"settlement_cash": f["free_cash"], "unsettled": 0.0}

    # 5. 現金與負債
    #    現金 = 券商交割戶(自動) + config 手動現金 + 期貨(權益數 − 實質價值)
    #    另外加未交割款(T+2 修正),投資部位 = 證券市值 + 期貨實質價值
    try:
        config_cash = cfgutil.sum_amounts(cfg.get("cash"), "cash")
        debt = cfgutil.sum_amounts(cfg.get("debt"), "debt")
    except cfgutil.ConfigError as e:
        print(f"[錯誤] {e}")
        sys.exit(1)

    # 防呆:NaN 不是 None,任何 is None 檢查都抓不到,放著會一路污染總市值,
    # 最後在寫入資料庫時才以難懂的 NOT NULL 錯誤爆掉。在這裡就擋下並指名哪一檔。
    bad = [p["code"] for p in all_positions
           if p["market_value"] is None or not math.isfinite(p["market_value"])]
    if bad:
        print(f"[錯誤] 這些持股的市值無效(None/NaN):{', '.join(bad)}")
        print("為避免寫入錯誤的淨值,本次中止。請檢查該檔的報價來源。")
        sys.exit(1)

    stock_mv = sum(p["market_value"] for p in all_positions)
    futures_position = sum(f["notional"] for f in futures_values.values())
    futures_free = sum(f["free_cash"] for f in futures_values.values())
    market_value = stock_mv + futures_position
    cash = broker_cash + config_cash + futures_free

    # 6. 寫入
    conn = db.connect()
    prev = db.previous_total(conn, snap_date)
    net_value = db.save_snapshot(
        conn, snap_date, market_value, account_values, all_positions,
        cash, debt, bench_closes, unsettled, broker_cash, account_cash
    )
    if futures_values:
        db.save_futures_snapshot(conn, snap_date, futures_values)

    # 7. 摘要
    mv = market_value
    cash_pct = cash / (net_value + debt) * 100 if (net_value + debt) else 0
    print("-" * 46)
    print(f"投資部位(證券+期貨實質價值) {mv:>12,.0f}")
    print(f"現金(交割戶+手動+期貨閒置) {cash:>12,.0f}({cash_pct:.1f}%)")
    if unsettled:
        print(f"未交割款(T+2)          {unsettled:>+15,.0f}")
    print(f"負債      {debt:>16,.0f}")
    print(f"淨值      {net_value:>16,.0f}")
    if prev:
        chg = net_value - prev[1]
        pct = chg / prev[1] * 100 if prev[1] else 0
        print(f"vs {prev[0]}: {chg:+,.0f} ({pct:+.2f}%)")
    print(f"已寫入 networth.db(日期 {snap_date})")


if __name__ == "__main__":
    main()
