"""帳戶 adapter 層。

每個證券 adapter 回傳 dict:
    {
      "positions": [ {"code","shares","price","market_value"}, ... ],
      "settlement_cash": 交割戶餘額(抓不到就 0),
      "unsettled": 未交割款淨額(應收為正、應付為負,抓不到就 0),
    }
price/market_value 為 None 時,由 snapshot.py 用 FinMind 收盤價補上。

未交割款是台股 T+2 制度的必要修正:賣出當天持股就從庫存消失、但錢兩個
交易日後才進交割戶;買進當天持股就進庫存、但錢還沒扣。中間這段如果不
補上未交割款,淨值會被低估(賣)或高估(買)。
"""
import csv
import datetime as dt
import math
import os
from pathlib import Path


def _read_csv_rows(path):
    """讀持股 CSV,對編碼容錯。

    這些檔案常被 Excel / 記事本開過再存,中文註解容易變成 Big5 或半毀的
    UTF-8。代號和股數欄位都是 ASCII,所以就算註解解不開也不該讓整份快照
    掛掉——依序嘗試常見編碼,最後退回 errors="replace" 硬讀。
    """
    for enc in ("utf-8-sig", "cp950", "big5"):
        try:
            with open(path, newline="", encoding=enc) as f:
                return list(csv.DictReader(f)), None
        except UnicodeDecodeError:
            continue
    with open(path, newline="", encoding="utf-8", errors="replace") as f:
        return list(csv.DictReader(f)), (
            f"{Path(path).name} 編碼已損毀,已略過無法解讀的字元(代號與股數不受影響)。"
            " 建議用 UTF-8 重存這個檔案。")


def _as_date(value):
    """把 'YYYY-MM-DD' 字串或 date 物件轉成 date;None/格式不符回傳 None。"""
    if value is None:
        return None
    if isinstance(value, dt.datetime):
        return value.date()
    if isinstance(value, dt.date):
        return value
    try:
        return dt.date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def fetch_manual(acc_cfg: dict, snap_date: str = None):
    """手動持股表:只維護代號+股數,價格交給 FinMind。"""
    path = Path(__file__).parent / acc_cfg["positions_file"]
    positions = []
    rows, warn = _read_csv_rows(path)
    if warn:
        print(f"  [警告] {warn}")
    for row in rows:
        code = (row.get("code") or "").strip()
        if not code or code.startswith("#"):
            continue
        cost = (row.get("cost") or "").strip()   # 選填:均價成本
        positions.append({
            "code": code,
            "shares": float(row["shares"]),
            "price": None,
            "market_value": None,
            "cost_price": float(cost) if cost else None,
        })
    return {"positions": positions, "settlement_cash": 0.0, "unsettled": 0.0}


def fetch_manual_foreign(acc_cfg: dict, snap_date: str = None):
    """海外持股(複委託)手動表:富邦 Neo API 查不到複委託庫存,只能自己維護。

    CSV 欄位:ticker, shares, cost, cost_date, note
      ticker    Yahoo Finance 代號(日股加 .T,如 7203.T 豐田;美股直接寫 AAPL)
      cost      選填,填「原幣」的每股成本(日股就填日圓價)
      cost_date 選填,買進日期 YYYY-MM-DD;填了就用**那天**的匯率把成本換成
                台幣,得到真正的台幣成本(含買進後的匯率變動);留空則用當前匯率。

    **同一檔可以分多列**代表分批建倉,各批用自己的日期換匯後加權平均,
    最後合併成一筆部位。任一批沒填成本,整檔的成本就留空(不拿半套資料
    算出誤導的均價)。

    現價與匯率都由 yfinance 自動抓並換算台幣,所以平時只要維護股數。
    """
    import yfinance as yf  # 延遲載入

    path = Path(__file__).parent / acc_cfg["positions_file"]
    raw_rows, warn = _read_csv_rows(path)
    if warn:
        print(f"  [警告] {warn}")
    lots = []
    for row in raw_rows:
        ticker = (row.get("ticker") or "").strip()
        if not ticker or ticker.startswith("#"):
            continue
        cost = (row.get("cost") or "").strip()
        cdate = (row.get("cost_date") or "").strip()
        lots.append({
            "ticker": ticker,
            "shares": float(row["shares"]),
            "cost": float(cost) if cost else None,
            "cost_date": cdate or None,
        })
    if not lots:
        return {"positions": [], "settlement_cash": 0.0, "unsettled": 0.0}

    fx_cache = {}

    def _fast_price(tk):
        """取 yfinance 的即時/最後成交價,拿不到回傳 None。

        FastInfo 在不同版本的存取方式不一致(dict 風格用 'lastPrice',
        屬性風格用 last_price),而且可能其中一種回 None,所以全部試一遍。
        """
        try:
            fi = tk.fast_info
        except Exception:
            return None
        for get in (lambda: fi["lastPrice"],
                    lambda: fi.get("lastPrice"),
                    lambda: getattr(fi, "last_price", None)):
            try:
                v = get()
            except Exception:
                continue
            if v is None:
                continue
            try:
                v = float(v)
            except (TypeError, ValueError):
                continue
            if math.isfinite(v) and v > 0:
                return v
        return None

    def _last_close(tk_symbol, label):
        """取最新價格:優先即時報價,歷史收盤檔當後備。

        Yahoo 的**歷史收盤檔更新很慢** —— 當日收盤後那一列的 Close 可能仍是
        NaN(實測日股收盤後數小時仍為空),dropna 後就退回前一個交易日,
        導致整份快照的海外持股永遠慢一個交易日。但 fast_info 的即時報價
        當下就有值,所以先讀它。

        歷史檔仍需保留當後備:fast_info 偶爾會拿不到(冷門標的、Yahoo 限流)。
        取歷史檔時一樣要先 dropna——NaN 不是 None,任何 is None 檢查都抓不到,
        會一路污染總市值,最後在寫入資料庫時才以難懂的 NOT NULL 錯誤爆掉。
        """
        tk = yf.Ticker(tk_symbol)
        val = _fast_price(tk)
        if val is not None:
            return val
        hist = tk.history(period="10d")
        if hist.empty or "Close" not in hist:
            raise RuntimeError(f"{label} 抓不到報價({tk_symbol})")
        closes = hist["Close"].dropna()
        if closes.empty:
            raise RuntimeError(f"{label} 近十日收盤價全為空值({tk_symbol})")
        val = float(closes.iloc[-1])
        if not math.isfinite(val) or val <= 0:
            raise RuntimeError(f"{label} 取得的價格不合理:{val}({tk_symbol})")
        print(f"  [提醒] {label} 用歷史收盤檔({tk_symbol}),即時報價取不到,"
              f"可能落後一個交易日")
        return val

    def fx_rate(currency, on_date=None):
        """取 <CUR>/TWD 匯率。on_date 為 None 時用最新,否則用該日期(含)之前
        最後一個有報價的交易日——匯率市場遇假日沒資料,不能只查當天。"""
        if currency in (None, "", "TWD"):
            return 1.0
        key = (currency, on_date)
        if key in fx_cache:
            return fx_cache[key]
        sym = f"{currency}TWD=X"
        if on_date is None:
            rate = _last_close(sym, f"{currency}/TWD 匯率")
        else:
            d = dt.date.fromisoformat(on_date)
            hist = yf.Ticker(sym).history(
                start=(d - dt.timedelta(days=10)).isoformat(),
                end=(d + dt.timedelta(days=1)).isoformat())
            closes = hist["Close"].dropna() if not hist.empty and "Close" in hist else None
            if closes is None or closes.empty:
                raise RuntimeError(f"抓不到 {on_date} 前後的 {currency}/TWD 匯率")
            rate = float(closes.iloc[-1])
            if not math.isfinite(rate) or rate <= 0:
                raise RuntimeError(f"{on_date} 的 {currency}/TWD 匯率不合理:{rate}")
        fx_cache[key] = rate
        return rate

    # 同一 ticker 的多列視為分批建倉,合併成一筆部位
    by_ticker = {}
    for lot in lots:
        by_ticker.setdefault(lot["ticker"], []).append(lot)

    positions = []
    for ticker, tlots in by_ticker.items():
        tk = yf.Ticker(ticker)
        price_local = _last_close(ticker, f"海外持股 {ticker}")
        currency = (tk.fast_info.get("currency") if hasattr(tk, "fast_info") else None) or "USD"
        price_twd = price_local * fx_rate(currency)

        shares = sum(l["shares"] for l in tlots)
        # 各批用自己的買進日匯率換台幣後加權平均;任一批沒成本就整檔留空
        if all(l["cost"] is not None for l in tlots) and shares:
            total_cost_twd = sum(
                l["cost"] * fx_rate(currency, l["cost_date"]) * l["shares"] for l in tlots
            )
            cost_twd = total_cost_twd / shares
        else:
            cost_twd = None

        positions.append({
            "code": ticker,
            "shares": shares,
            "price": price_twd,                 # 已換算台幣,與台股同單位
            "market_value": price_twd * shares,
            "cost_price": cost_twd,
            "name": f"{ticker}({currency} {price_local:,.2f})",
        })
    return {"positions": positions, "settlement_cash": 0.0, "unsettled": 0.0}


def fetch_shioaji(acc_cfg: dict, snap_date: str = None):
    """永豐 Shioaji:自動抓庫存,含即時價。

    CA 憑證原則上是下單簽章用的,查詢不一定需要,但官方 Quickstart 的標準
    .env 範本把 CA_PATH/CA_PASSWD 跟 API Key 放在一起當標準設定,不同帳戶權限
    層級的實際要求可能不同。這裡把 CA 設計成可選:填了就啟用,沒填就照原本
    查詢流程跑,兩種都支援,避免因為猜錯規則卡住。
    """
    import shioaji as sj  # 延遲載入,沒裝也不影響其他 adapter

    api_key = os.environ.get("SHIOAJI_API_KEY")
    secret = os.environ.get("SHIOAJI_SECRET_KEY")
    ca_path = os.environ.get("SHIOAJI_CA_PATH")
    ca_passwd = os.environ.get("SHIOAJI_CA_PASSWORD")
    ca_person_id = os.environ.get("SHIOAJI_CA_PERSON_ID")
    if not api_key or not secret:
        raise RuntimeError("缺 SHIOAJI_API_KEY / SHIOAJI_SECRET_KEY,請設定 .env")

    api = sj.Shioaji()
    api.login(api_key=api_key, secret_key=secret)
    if ca_path:
        result = api.activate_ca(
            ca_path=ca_path,
            ca_passwd=ca_passwd,
            person_id=ca_person_id,
        )
        if not result:
            raise RuntimeError("CA 憑證啟用失敗,檢查 SHIOAJI_CA_PATH/CA_PASSWORD/CA_PERSON_ID")
    try:
        raw = api.list_positions(api.stock_account, unit=sj.constant.Unit.Share)
        positions = []
        for p in raw:
            shares = float(p.quantity)
            last = float(p.last_price) if p.last_price else None
            positions.append({
                "code": p.code,
                "shares": shares,
                "price": last,
                "market_value": last * shares if last else None,
                # StockPosition.price 是「均價成本」,不是現價(現價在 last_price)
                "cost_price": float(p.price) if p.price else None,
            })

        # 交割戶餘額(自動抓,不用手動維護)
        settlement_cash = 0.0
        try:
            bal = api.account_balance()
            settlement_cash = float(getattr(bal, "acc_balance", 0) or 0)
        except Exception as e:
            print(f"  [警告] 永豐交割戶餘額抓取失敗,以 0 計:{e}")

        # 未交割款:取交割日晚於快照日的部分(理由同富邦,見 fetch_fubon)。
        # 用 settlements() 回傳的 date 欄位判斷,不用 T 值——T 是相對於「執行
        # 當下」的天數,跨午夜執行時會跟快照代表的交易日對不起來。
        unsettled = 0.0
        try:
            base = _as_date(snap_date) or dt.date.today()
            for s in api.settlements(api.stock_account):
                sd = _as_date(getattr(s, "date", None))
                if sd is None:
                    # 沒有日期就退回用 T 值(T>0 代表尚未交割)
                    if int(getattr(s, "T", 0)) > 0:
                        unsettled += float(getattr(s, "amount", 0) or 0)
                elif sd > base:
                    unsettled += float(getattr(s, "amount", 0) or 0)
        except Exception as e:
            print(f"  [警告] 永豐未交割款抓取失敗,以 0 計:{e}")

        return {"positions": positions, "settlement_cash": settlement_cash,
                "unsettled": unsettled}
    finally:
        # logout 失敗只印警告,不能讓它蓋掉上面 try 區塊真正的錯誤
        # (finally 裡的例外會取代 try 裡傳出來的例外)
        try:
            api.logout()
        except Exception as logout_err:
            print(f"  [警告] 永豐 logout 失敗(不影響已抓到的資料):{logout_err}")


def _fubon_qty(obj):
    """取富邦庫存物件的「當前」股數,一律以 today_qty(今日庫存)為準。

    不能寫成 `today_qty or lastday_qty`:今天賣光時 today_qty 是 0,而 0 在
    Python 是 falsy,會退回昨日庫存,把已賣掉的部位復活(例如整股全賣、
    零股續抱時,整張會被算回來)。只有欄位根本不存在(舊版 SDK)才退回
    lastday_qty。
    """
    if obj is None:
        return 0.0
    qty = getattr(obj, "today_qty", None)
    if qty is None:
        qty = getattr(obj, "lastday_qty", 0)
    return float(qty or 0)


def fetch_fubon(acc_cfg: dict, snap_date: str = None):
    """富邦 Neo API:自動抓庫存。需要憑證檔(.pfx)。"""
    from fubon_neo.sdk import FubonSDK  # 延遲載入

    uid = os.environ.get("FUBON_ID")
    pwd = os.environ.get("FUBON_PASSWORD")
    cert = os.environ.get("FUBON_CERT_PATH")
    cert_pwd = os.environ.get("FUBON_CERT_PASSWORD", "")
    if not uid or not pwd or not cert:
        raise RuntimeError("缺 FUBON_ID / FUBON_PASSWORD / FUBON_CERT_PATH,請設定 .env")

    sdk = FubonSDK()
    accounts = sdk.login(uid, pwd, cert, cert_pwd)
    if not accounts.is_success:
        raise RuntimeError(f"富邦登入失敗: {accounts.message}")
    try:
        account = accounts.data[0]
        result = sdk.accounting.inventories(account)
        if not result.is_success:
            raise RuntimeError(f"富邦庫存查詢失敗: {result.message}")
        # 成本均價:inventories() 沒有成本欄位,要另外查未實現損益,
        # 以 stock_no 對應回庫存。
        cost_map = {}
        try:
            ur = sdk.accounting.unrealized_gains_and_loses(account)
            if ur.is_success:
                for u in ur.data or []:
                    cp = getattr(u, "cost_price", None)
                    if cp:
                        cost_map[str(u.stock_no)] = float(cp)
        except Exception as e:
            print(f"  [警告] 富邦成本查詢失敗,成本欄位留空:{e}")

        positions = []
        for inv in result.data:
            # 整股(集保)股數在頂層;零股股數包在巢狀的 inv.odd 子物件裡,
            # 兩者結構相同(today_qty/lastday_qty),必須分開讀再加總,
            # 不然像定期定額買到的零股會整個漏算。
            shares = _fubon_qty(inv) + _fubon_qty(getattr(inv, "odd", None))
            if shares <= 0:
                continue
            positions.append({
                "code": inv.stock_no,
                "shares": shares,
                "price": None,          # 用 FinMind 收盤價統一計價
                "market_value": None,
                "cost_price": cost_map.get(str(inv.stock_no)),
            })

        # 交割戶餘額(自動抓,不用手動維護)
        settlement_cash = 0.0
        try:
            br = sdk.accounting.bank_remain(account)
            if br.is_success:
                settlement_cash = float(getattr(br.data, "balance", 0) or 0)
        except Exception as e:
            print(f"  [警告] 富邦交割戶餘額抓取失敗,以 0 計:{e}")

        # 未交割款:取交割日「晚於快照日」的,也就是該交易日收盤時還沒交割的。
        # total_settlement_amount 負數=應付(買)、正數=應收(賣);
        # 沒成交的日期整筆欄位都是 None,要略過。
        #
        # 基準是 snap_date(這筆快照代表的交易日)而不是 date.today()。
        # 券商在交割後**不會**把該筆移出清單(它是「哪天成交、哪天交割」的
        # 歷史紀錄,會留在三天查詢視窗內),所以只能靠日期判斷有沒有交割完。
        # 用牆上時鐘會壞在跨午夜執行:例如 08/26 收盤的快照在 08/27 凌晨才跑,
        # 08/25 賣出、08/27 交割的那筆會因為 08/27 > 08/27 不成立而被濾掉,
        # 但錢那時根本還沒入帳(交割扣付款約在交割日上午),應收款就憑空消失。
        # 改用 snap_date 後,不論幾點跑,答案都對應同一個交易日的收盤狀態。
        unsettled = 0.0
        try:
            st = sdk.accounting.query_settlement(account, "3d")
            if st.is_success:
                base = _as_date(snap_date) or dt.date.today()
                for d in getattr(st.data, "details", []) or []:
                    sd = getattr(d, "settlement_date", None)
                    amt = getattr(d, "total_settlement_amount", None)
                    if not sd or amt is None:
                        continue
                    if dt.datetime.strptime(sd, "%Y/%m/%d").date() > base:
                        unsettled += float(amt)
        except Exception as e:
            print(f"  [警告] 富邦未交割款抓取失敗,以 0 計:{e}")

        return {"positions": positions, "settlement_cash": settlement_cash,
                "unsettled": unsettled}
    finally:
        sdk.logout()


def fetch_fubon_futures(acc_cfg: dict, snap_date: str = None):
    """富邦期貨保證金帳戶:回傳權益數與淨口數。

    回傳 {"cash_balance": 本日餘額, "unrealized_pnl": 未實現損益,
          "equity": 權益數 = 本日餘額 + 未實現損益, "net_lots": 淨口數(多−空)}

    期貨部位的「實質價值」由 snapshot.py 用 config 的標的+乘數計算
    (實質價值 = 標的收盤 × 乘數 × net_lots),權益數扣掉實質價值後算現金。

    account_type 實測值是 "stock" / "futopt"(字串比對抓 futopt 帳戶)。
    query_margin_equity() 回傳的 result.data 是一個 list(一幣別一筆,NTD/TWD
    兩筆數值相同),要先取 [0] 才能拿到欄位,不是單一物件——這是原本抓到
    全部是 0 的真正原因。欄位是 snake_case:today_balance(對應券商軟體上顯示
    的「權益數」)、today_equity(= today_balance + 未沖銷損益,實際計入淨值
    用這個)、fut_unrealized_pnl、opt_pnl。
    """
    from fubon_neo.sdk import FubonSDK  # 延遲載入

    uid = os.environ.get("FUBON_ID")
    pwd = os.environ.get("FUBON_PASSWORD")
    cert = os.environ.get("FUBON_CERT_PATH")
    cert_pwd = os.environ.get("FUBON_CERT_PASSWORD", "")
    if not uid or not pwd or not cert:
        raise RuntimeError("缺 FUBON_ID / FUBON_PASSWORD / FUBON_CERT_PATH,請設定 .env")

    sdk = FubonSDK()
    accounts = sdk.login(uid, pwd, cert, cert_pwd)
    if not accounts.is_success:
        raise RuntimeError(f"富邦登入失敗: {accounts.message}")
    try:
        futures_acc = None
        for acc in accounts.data:
            if str(getattr(acc, "account_type", "")).lower() == "futopt":
                futures_acc = acc
                break
        if futures_acc is None:
            raise RuntimeError(
                "登入帳戶清單裡找不到 account_type == 'futopt' 的期貨帳戶。"
                "請確認這組憑證有連到期貨帳戶。"
            )

        result = sdk.futopt_accounting.query_margin_equity(futures_acc)
        if not result.is_success:
            raise RuntimeError(f"富邦期貨權益查詢失敗: {result.message}")
        data = result.data[0]
        cash_balance = float(data.today_balance)
        unrealized_pnl = float(data.fut_unrealized_pnl) + float(data.opt_pnl)
        equity = float(data.today_equity)

        # 未平倉口數與實質價值:一定要用 query_single_position(),
        # 不能用 margin_equity 的 buy_lot/sell_lot——那是「當日成交口數」,
        # 沒交易的日子會是 0,會誤判成空手(實測 08/07 兩口在手但都是 0)。
        # 每筆部位自帶 market_price = 期貨即時報價,直接拿它算實質價值,
        # 不必用現股收盤價替代,沒有基差誤差。
        #
        # 乘數必須「逐商品」查表:不同期貨契約規格差很多
        # (小型台灣50 ETF 期貨 1 口 = 1000 單位;小型個股期貨 1 口 = 100 股;
        # 微型台指期貨 1 點 = 10 元),套錯就是好幾倍的誤差。
        #
        # 刻意**沒有預設乘數**:曾經有 default_multiplier,結果新建倉的微台指
        # (實際 10)被默默套成 100,部位價值灌成 10 倍,曝險倍數跟著膨脹成
        # 好幾倍。程式雖有印警告,但排程在背景跑沒人看得到。現在遇到沒設定的
        # 商品直接中止,並用券商回報的損益反推乘數當提示。
        mult_map = acc_cfg.get("multipliers") or {}

        def _mult_hint(p, symbol):
            """乘數 = 損益 ÷ (市價 − 成本) ÷ 口數;剛建倉沒有價差時無法反推。"""
            try:
                pnl = float(getattr(p, "profit_or_loss", 0) or 0)
                diff = (float(getattr(p, "market_price", 0) or 0)
                        - float(getattr(p, "price", 0) or 0))
                n = float(getattr(p, "orig_lots", 0) or 0)
                if pnl and diff and n:
                    return f"{symbol}: {round(abs(pnl / (diff * n)), 2):g}    # 依券商損益反推"
            except (TypeError, ValueError):
                pass
            return f"{symbol}: ?    # 剛建倉無價差可反推,請查契約規格"

        net_lots = 0.0
        notional = 0.0
        missing = []
        pos_result = sdk.futopt_accounting.query_single_position(futures_acc)
        if not pos_result.is_success:
            raise RuntimeError(f"富邦期貨未平倉查詢失敗: {pos_result.message}")
        for p in pos_result.data or []:
            lots = float(getattr(p, "orig_lots", 0) or 0)
            if str(getattr(p, "buy_sell", "")).lower().endswith("sell"):
                lots = -lots
            net_lots += lots
            symbol = str(getattr(p, "symbol", "") or "")
            if symbol not in mult_map:
                missing.append(_mult_hint(p, symbol))
                continue
            mkt = getattr(p, "market_price", None)
            if mkt:
                notional += float(mkt) * float(mult_map[symbol]) * lots
        if missing:
            raise RuntimeError(
                "有期貨商品沒設定契約乘數,為避免寫入錯誤的部位價值,本次中止。\n"
                "  請在 config.yaml 的 fubon_futures.multipliers 補上:\n"
                + "\n".join(f"    {m}" for m in dict.fromkeys(missing))
            )

        return {
            "cash_balance": cash_balance,
            "unrealized_pnl": unrealized_pnl,
            "equity": equity,
            "net_lots": net_lots,
            "notional": notional,
        }
    finally:
        sdk.logout()


ADAPTERS = {
    "manual": fetch_manual,
    "manual_foreign": fetch_manual_foreign,
    "shioaji": fetch_shioaji,
    "fubon": fetch_fubon,
}
