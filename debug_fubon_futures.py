"""一次性診斷腳本:印出富邦期貨帳戶物件與權益查詢的實際欄位結構,
用來對照 fetch_fubon_futures(brokers.py)裡猜錯的欄位名。
跑完把整段輸出貼回去對話,不會動到任何交易,純查詢。
"""
import os

from dotenv import load_dotenv
from fubon_neo.sdk import FubonSDK

load_dotenv()

uid = os.environ["FUBON_ID"]
pwd = os.environ["FUBON_PASSWORD"]
cert = os.environ["FUBON_CERT_PATH"]
cert_pwd = os.environ.get("FUBON_CERT_PASSWORD", "")

sdk = FubonSDK()
accounts = sdk.login(uid, pwd, cert, cert_pwd)
print("=== accounts.data(所有帳戶)===")
for i, acc in enumerate(accounts.data):
    print(f"[{i}] {acc!r}")
    print(f"    vars: {vars(acc) if hasattr(acc, '__dict__') else '(no __dict__)'}")

print()
print("=== 逐一嘗試 query_margin_equity ===")
for i, acc in enumerate(accounts.data):
    try:
        result = sdk.futopt_accounting.query_margin_equity(acc)
        print(f"[{i}] is_success={result.is_success}")
        if result.is_success:
            data = result.data
            print(f"    data repr: {data!r}")
            print(f"    data vars: {vars(data) if hasattr(data, '__dict__') else '(no __dict__)'}")
        else:
            print(f"    message: {result.message}")
    except Exception as e:
        print(f"[{i}] 例外: {e}")

sdk.logout()
