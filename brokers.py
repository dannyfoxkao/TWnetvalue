"""帳戶 adapter 層:每個 adapter 回傳該帳戶的持股清單。

回傳格式統一為 list[dict]:
    {"code": "2330", "shares": 1000, "price": 1080.0 或 None, "market_value": ... 或 None}
price/market_value 為 None 時,由 snapshot.py 用 FinMind 收盤價補上。
"""
import csv
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
    return positions


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
        return positions
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
        return positions
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
        # 淨口數(多單口數 − 空單口數)。單一商品帳戶用這個即可;若同時持有
        # 多種期貨商品才需要另查未平倉明細分商品。
        net_lots = float(getattr(data, "buy_lot", 0) or 0) - \
            float(getattr(data, "sell_lot", 0) or 0)
        return {
            "cash_balance": cash_balance,
            "unrealized_pnl": unrealized_pnl,
            "equity": equity,
            "net_lots": net_lots,
        }
    finally:
        sdk.logout()


ADAPTERS = {
    "manual": fetch_manual,
    "shioaji": fetch_shioaji,
    "fubon": fetch_fubon,
}
