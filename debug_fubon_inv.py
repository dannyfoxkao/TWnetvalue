"""一次性診斷:印出富邦證券庫存每一檔的原始數量欄位。
用來確認賣出後 today_qty / lastday_qty / sell_qty 的實際變化。
純查詢,不下單。
"""
import os

from dotenv import load_dotenv
from fubon_neo.sdk import FubonSDK

load_dotenv()

sdk = FubonSDK()
accounts = sdk.login(
    os.environ["FUBON_ID"],
    os.environ["FUBON_PASSWORD"],
    os.environ["FUBON_CERT_PATH"],
    os.environ.get("FUBON_CERT_PASSWORD", ""),
)
stock_acc = next(a for a in accounts.data
                 if str(getattr(a, "account_type", "")).lower() == "stock")
result = sdk.accounting.inventories(stock_acc)

lines = []
for inv in result.data:
    lines.append(repr(inv))
    lines.append("-" * 60)

open("_inv_dump.txt", "w", encoding="utf-8").write("\n".join(lines))
print(f"已寫入 _inv_dump.txt({len(result.data)} 筆)")
sdk.logout()
