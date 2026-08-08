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
import os
from pathlib import Path


def fetch_manual(acc_cfg: dict):
    """手動持股表:只維護代號+股數,價格交給 FinMind。"""
    path = Path(__file__).parent / acc_cfg["positions_file"]
    positions = []
    with open(path, newline="", encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            code = row["code"].strip()
            if not code:
                continue
            positions.append({
                "code": code,
                "shares": float(row["shares"]),
                "price": None,
                "market_value": None,
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
            })

        # 交割戶餘額(自動抓,不用手動維護)
        settlement_cash = 0.0
        try:
            br = sdk.accounting.bank_remain(account)
            if br.is_success:
                settlement_cash = float(getattr(br.data, "balance", 0) or 0)
        except Exception as e:
            print(f"  [警告] 富邦交割戶餘額抓取失敗,以 0 計:{e}")

        # 未交割款:只算交割日尚未到的(已交割的餘額已反映,算了會重複)。
        # total_settlement_amount 負數=應付(買)、正數=應收(賣);
        # 沒成交的日期整筆欄位都是 None,要略過。
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
                    if dt.datetime.strptime(sd, "%Y/%m/%d").date() > today:
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
        multiplier = float(acc_cfg.get("multiplier", 0) or 0)
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
            mkt = getattr(p, "market_price", None)
            if mkt:
                notional += float(mkt) * multiplier * lots

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
    "shioaji": fetch_shioaji,
    "fubon": fetch_fubon,
}
