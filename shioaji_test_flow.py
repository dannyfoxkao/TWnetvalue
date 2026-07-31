"""永豐 Shioaji API 一次性模擬環境測試流程。

新申請的 API Key 第一次要用正式環境前,永豐規定必須先在「模擬模式」完成
一次登入 + 一次下單測試,審核通過(約 5 分鐘)後正式環境才能用。這支腳本
就是做這件事:全程 simulation=True,不會動到真錢、不會影響正式帳戶。

用法:
    python shioaji_test_flow.py

跑完後等 5 分鐘,再跑 python snapshot.py 測試正式環境是否已通過。

限制(永豐規定):平日 08:00-20:00 才能測試,18:00-20:00 限台灣 IP。
"""
import os
import time

import shioaji as sj
from dotenv import load_dotenv

load_dotenv()

api_key = os.environ["SHIOAJI_API_KEY"]
secret = os.environ["SHIOAJI_SECRET_KEY"]
ca_path = os.environ.get("SHIOAJI_CA_PATH")
ca_passwd = os.environ.get("SHIOAJI_CA_PASSWORD")
ca_person_id = os.environ.get("SHIOAJI_CA_PERSON_ID")

if not ca_path:
    raise SystemExit(
        "下單測試需要 CA 憑證,請先在 .env 填 SHIOAJI_CA_PATH / "
        "SHIOAJI_CA_PASSWORD / SHIOAJI_CA_PERSON_ID"
    )

print("1. 模擬環境登入...")
api = sj.Shioaji(simulation=True)
accounts = api.login(api_key=api_key, secret_key=secret)
print(f"   登入成功,帳戶: {accounts}")

print("2. 啟用 CA 憑證...")
ok = api.activate_ca(ca_path=ca_path, ca_passwd=ca_passwd, person_id=ca_person_id)
if not ok:
    raise SystemExit("CA 啟用失敗,檢查 .env 裡的 CA 路徑/密碼/身分證字號")

print("3. 下單測試(模擬模式,以平盤價買進 2890 一張,限價 ROD)...")
contract = api.Contracts.Stocks["2890"]
order = api.Order(
    price=contract.reference,
    quantity=1,
    action=sj.constant.Action.Buy,
    price_type=sj.constant.StockPriceType.LMT,
    order_type=sj.constant.OrderType.ROD,
    account=api.stock_account,
)
trade = api.place_order(contract, order)
print(f"   下單完成: {trade}")

time.sleep(2)
api.update_status(api.stock_account)
print(f"4. 委託狀態: {trade.status}")

api.logout()
print("-" * 50)
print("測試流程跑完了。等約 5 分鐘讓永豐審核通過,再跑 python snapshot.py 測正式環境。")
