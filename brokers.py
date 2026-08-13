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


def fetch_manual(acc_cfg: dict):
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


def fetch_manual_foreign(acc_cfg: dict):
    """海外持股(複委託)手動表:富邦 Neo API 查不到複委託庫存,只能自己維護。

    CSV 欄位:ticker, shares, note
      ticker 用 Yahoo Finance 代號(日股加 .T,如 7203.T 豐田;美股直接寫 AAPL)
    價格與匯率都用 yfinance 自動抓,換算成台幣計價,所以只要維護股數。
    """
    import yfinance as yf  # 延遲載入

    path = Path(__file__).parent / acc_cfg["positions_file"]
    raw_rows, warn = _read_csv_rows(path)
    if warn:
        print(f"  [警告] {warn}")
    rows = []
    for row in raw_rows:
        ticker = (row.get("ticker") or "").strip()
        if not ticker or ticker.startswith("#"):
            continue
        rows.append((ticker, float(row["shares"])))
    if not rows:
        return {"positions": [], "settlement_cash": 0.0, "unsettled": 0.0}

    fx_cache = {}

    def _last_close(tk_symbol, label):
        """取最後一筆有效收盤價。

        yfinance 對「當日進行中」或休市日常回傳 NaN 收盤價,直接取 iloc[-1]
        會拿到 NaN,一路污染總市值(NaN 不是 None,任何 is None 檢查都抓不到,
        最後在寫入資料庫時才以難懂的 NOT NULL 錯誤爆掉)。先 dropna 再取。
        """
        hist = yf.Ticker(tk_symbol).history(period="10d")
        if hist.empty or "Close" not in hist:
            raise RuntimeError(f"{label} 抓不到報價({tk_symbol})")
        closes = hist["Close"].dropna()
        if closes.empty:
            raise RuntimeError(f"{label} 近十日收盤價全為空值({tk_symbol})")
        val = float(closes.iloc[-1])
        if not math.isfinite(val) or val <= 0:
            raise RuntimeError(f"{label} 取得的價格不合理:{val}({tk_symbol})")
        return val

    def to_twd(amount, currency):
        """外幣換台幣。TWD 直接回傳,其他幣別抓 <CUR>TWD=X 匯率。"""
        if currency in (None, "", "TWD"):
            return amount
        if currency not in fx_cache:
            fx_cache[currency] = _last_close(f"{currency}TWD=X", f"{currency}/TWD 匯率")
        return amount * fx_cache[currency]

    positions = []
    for ticker, shares in rows:
        tk = yf.Ticker(ticker)
        price_local = _last_close(ticker, f"海外持股 {ticker}")
        currency = (tk.fast_info.get("currency") if hasattr(tk, "fast_info") else None) or "USD"
        price_twd = to_twd(price_local, currency)
        positions.append({
            "code": ticker,
            "shares": shares,
            "price": price_twd,                 # 已換算台幣,與台股同單位
            "market_value": price_twd * shares,
            "name": f"{ticker}({currency} {price_local:,.2f})",
        })
    return {"positions": positions, "settlement_cash": 0.0, "unsettled": 0.0}


def fetch_shioaji(acc_cfg: dict):
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

        # 未交割款:只算 T+1、T+2(T+0 當天已完成交割,餘額已反映,算了會重複)
        unsettled = 0.0
        try:
            for s in api.settlements(api.stock_account):
                if int(getattr(s, "T", 0)) > 0:
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


def fetch_fubon(acc_cfg: dict):
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

        # 未交割款:取交割日「今天(含)以後」還掛在券商帳上的金額。
        # total_settlement_amount 負數=應付(買)、正數=應收(賣);
        # 沒成交的日期整筆欄位都是 None,要略過。
        #
        # 注意條件是 >= today 而不是 > today:交割當天的扣款約上午才會跑,
        # 在那之前錢還在交割戶、應付款也還掛著,兩邊都要算。若寫成 > today,
        # 交割日凌晨到扣款前這段會把應付款憑空抹掉,淨值被高估一整筆
        # (實測:南亞 08/12 買進、08/14 交割,當天凌晨查詢該筆仍在清單上)。
        unsettled = 0.0
        try:
            st = sdk.accounting.query_settlement(account, "3d")
            if st.is_success:
                today = dt.date.today()
                for d in getattr(st.data, "details", []) or []:
                    sd = getattr(d, "settlement_date", None)
                    amt = getattr(d, "total_settlement_amount", None)
                    if not sd or amt is None:
                        continue
                    if dt.datetime.strptime(sd, "%Y/%m/%d").date() >= today:
                        unsettled += float(amt)
        except Exception as e:
            print(f"  [警告] 富邦未交割款抓取失敗,以 0 計:{e}")

        return {"positions": positions, "settlement_cash": settlement_cash,
                "unsettled": unsettled}
    finally:
        sdk.logout()


def fetch_fubon_futures(acc_cfg: dict):
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
        # 乘數必須「逐商品」查表:不同期貨契約規格不同
        # (小型台灣50 ETF 期貨 1 口 = 1000 單位;小型個股期貨 1 口 = 100 股),
        # 全部套同一個乘數會把個股期貨的部位價值放大 10 倍。
        mult_map = acc_cfg.get("multipliers") or {}
        default_mult = acc_cfg.get("default_multiplier")
        net_lots = 0.0
        notional = 0.0
        pos_result = sdk.futopt_accounting.query_single_position(futures_acc)
        if not pos_result.is_success:
            raise RuntimeError(f"富邦期貨未平倉查詢失敗: {pos_result.message}")
        for p in pos_result.data or []:
            lots = float(getattr(p, "orig_lots", 0) or 0)
            if str(getattr(p, "buy_sell", "")).lower().endswith("sell"):
                lots = -lots
            net_lots += lots
            symbol = str(getattr(p, "symbol", "") or "")
            if symbol in mult_map:
                mult = float(mult_map[symbol])
            elif default_mult is not None:
                mult = float(default_mult)
                print(f"  [警告] 期貨商品 {symbol} 不在 multipliers 設定中,"
                      f"暫用 default_multiplier={mult:g};請到 config.yaml 補上正確乘數")
            else:
                raise RuntimeError(
                    f"期貨商品 {symbol} 沒有設定契約乘數。請在 config.yaml 的 "
                    f"fubon_futures.multipliers 加上 {symbol},或設 default_multiplier。"
                )
            mkt = getattr(p, "market_price", None)
            if mkt:
                notional += float(mkt) * mult * lots

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
