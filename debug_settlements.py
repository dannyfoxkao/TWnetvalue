"""一次性診斷:印出永豐與富邦的交割款(settlement)原始資料。
用來確認未交割款的欄位、正負號語意、以及 T+0/1/2 的對應。
純查詢,不下單。輸出寫到 _settle.txt。
"""
import os

from dotenv import load_dotenv

load_dotenv()
out = []

# ---------- 永豐 ----------
try:
    import shioaji as sj

    api = sj.Shioaji()
    api.login(api_key=os.environ["SHIOAJI_API_KEY"],
              secret_key=os.environ["SHIOAJI_SECRET_KEY"])
    out.append("=== 永豐 api.settlements() ===")
    for s in api.settlements(api.stock_account):
        out.append(f"  {s!r}")
    out.append("")
    out.append("=== 永豐 帳戶餘額 account_balance() ===")
    try:
        out.append(f"  {api.account_balance()!r}")
    except Exception as e:
        out.append(f"  (查詢失敗: {e})")
    try:
        api.logout()
    except Exception:
        pass
except Exception as e:
    out.append(f"[永豐錯誤] {e}")

out.append("")

# ---------- 富邦 ----------
try:
    from fubon_neo.sdk import FubonSDK

    sdk = FubonSDK()
    accounts = sdk.login(os.environ["FUBON_ID"], os.environ["FUBON_PASSWORD"],
                         os.environ["FUBON_CERT_PATH"],
                         os.environ.get("FUBON_CERT_PASSWORD", ""))
    stock_acc = next(a for a in accounts.data
                     if str(getattr(a, "account_type", "")).lower() == "stock")
    for period in ("0d", "3d"):
        out.append(f"=== 富邦 query_settlement(account, '{period}') ===")
        try:
            r = sdk.accounting.query_settlement(stock_acc, period)
            out.append(f"  is_success={r.is_success}")
            out.append(f"  data={r.data!r}")
        except Exception as e:
            out.append(f"  (查詢失敗: {e})")
        out.append("")
    out.append("=== 富邦 銀行餘額 bank_remain() ===")
    try:
        out.append(f"  {sdk.accounting.bank_remain(stock_acc)!r}")
    except Exception as e:
        out.append(f"  (查詢失敗: {e})")
    sdk.logout()
except Exception as e:
    out.append(f"[富邦錯誤] {e}")

open("_settle.txt", "w", encoding="utf-8").write("\n".join(out))
print("已寫入 _settle.txt")
