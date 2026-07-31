# TWnetvalue — 台股每日淨值追蹤

台股沒有像嘉信那種「打開就看到整戶淨值曲線」的原生體驗,因為券商戶和交割銀行戶
是分離的,券商 App 只看得到證券資產,看不到現金和負債。這支工具就是補這塊:

每天收盤後自動記錄 **淨值 = 證券市值 + 期貨部位 + 現金 − 負債**,存進 SQLite,
用 Streamlit 看淨值曲線、回撤、現金比重、槓桿比,並跟加權指數 / 0050 比相對績效。

支援永豐(Shioaji)、富邦(Neo)證券與期貨帳戶自動抓庫存,也可以純手動維護持股表。

---

## 快速開始(手動模式,5 分鐘)

```powershell
pip install -r requirements.txt
```

1. **拿 FinMind token**(免費,抓收盤價用):到 <https://finmindtrade.com/> 註冊,
   在專案根目錄建立 `finmind_token.json`:
   ```json
   { "api_token": "你的 token" }
   ```
2. **填持股**:編輯 `positions_manual.csv`(代號, 股數),零股直接寫股數
3. **填現金/負債**:編輯 `config.yaml` 的 `cash` / `debt` 區塊
4. 跑起來:
   ```powershell
   python snapshot.py            # 記錄一筆快照
   streamlit run dashboard.py    # 看儀表板
   .\register_task.ps1           # 註冊每日自動排程(平日 14:45)
   ```

曲線是從第一筆快照開始累積的,所以越早開始跑越好。

---

## 日常維護

| 情境 | 要做什麼 |
|---|---|
| 買賣股票(手動模式) | 更新 `positions_manual.csv` |
| 買賣股票(API 模式) | **什麼都不用做**,下次快照自動抓最新庫存 |
| 現金 / 負債變動 | 改 `config.yaml`,然後跑 `python update_cash.py`(秒開,不碰券商 API) |

- 同一天重跑 `snapshot.py` 會覆蓋當天資料,放心重跑。
- 快照日期以「最近交易日」為準(週末跑也不會出錯)。
- **賣股後記得更新交割戶餘額**——賣出的股票會從市值消失,但錢進交割戶是手動維護的,
  忘了更新的話淨值曲線會出現一個假摔。

---

## 檔案

| 檔案 | 用途 |
|---|---|
| `config.yaml` | 帳戶開關、現金、負債、基準指數 |
| `positions_manual.csv` | 手動持股表 |
| `snapshot.py` | 每日快照主程式(排程跑這支) |
| `update_cash.py` | 只更新現金/負債,不重抓持股 |
| `dashboard.py` | Streamlit 儀表板 |
| `brokers.py` | 券商 adapter(manual / shioaji / fubon / fubon_futures) |
| `networth.db` | SQLite 資料(自動建立) |
| `register_task.ps1` | 註冊 Windows 排程 |
| `debug_fubon_*.py` | 診斷腳本,富邦欄位對不上時印出原始結構 |
| `shioaji_test_flow.py` | 永豐新 key 的一次性模擬測試(見下方) |

---

## 升級成全自動(券商 API)

### 永豐 Shioaji

1. 到 <https://sinotrade.github.io/> 申請 API key(需永豐帳戶 + 簽署同意書)
2. **申請時務必勾選這些權限**(這裡最容易卡):
   - **帳務(Accounting)**——查庫存的 `list_positions` 屬於帳務類 API,
     沒勾會噴 `401 Token doesn't have permission`
   - **正式環境(Production)**——沒勾只能連模擬環境,噴
     `Token doesn't have production permission`
   - **交易(Trading)**——只有跑下面的測試流程需要,測完可以取消勾選
3. `pip install shioaji`
4. `.env` 填 `SHIOAJI_API_KEY` / `SHIOAJI_SECRET_KEY`(範本見 `.env.example`)
5. **新 key 必須先跑一次模擬測試**(永豐規定,不跑的話正式環境會噴
   `406 Account Not Acceptable`):
   ```powershell
   python shioaji_test_flow.py
   ```
   全程 `simulation=True`,不會動到真錢。需要 CA 憑證(下單簽章用),
   所以 `.env` 要一併填 `SHIOAJI_CA_PATH` / `SHIOAJI_CA_PASSWORD` /
   `SHIOAJI_CA_PERSON_ID`。跑完等約 5 分鐘審核。
   限平日 08:00–20:00,18:00–20:00 限台灣 IP。
6. `config.yaml` 把 `sinopac.enabled` 改 `true`

> 純查詢庫存不需要 CA;`fetch_shioaji` 把 CA 設計成可選,`.env` 有填就啟用,
> 沒填就照原本流程跑。

### 富邦 Neo

1. 到 <https://www.fbs.com.tw/TradeAPI/> 申請 TradeAPI 與憑證(.pfx)
2. **`fubon-neo` 不在 PyPI**,不能 `pip install fubon-neo`。到
   [SDK Download 頁面](https://www.fbs.com.tw/TradeAPI/en/docs/download/download-sdk/)
   下載對應 Python 版本的 `.whl`(如 Python 3.11 對應
   `fubon_neo-<版本>-cp311-abi3-win_amd64.whl`),再 `pip install <檔名>.whl`
3. `.env` 填 `FUBON_ID` / `FUBON_PASSWORD` / `FUBON_CERT_PATH` / `FUBON_CERT_PASSWORD`
   (`FUBON_CERT_PATH` 是憑證**檔案路徑**,建議放專案外)
4. `config.yaml` 把 `fubon.enabled` 改 `true`

> **零股要另外抓**:`inventories()` 回傳的整股數量在頂層(`today_qty`),
> 零股包在巢狀的 `inv.odd` 子物件裡,兩個要分開讀再加總,否則零股會整個漏算。
> 另外數量一律以 `today_qty` 為準,**不能寫成 `today_qty or lastday_qty`**——
> 今天賣光時 `today_qty` 是 0(Python falsy),會退回昨日庫存把賣掉的部位復活。

### 富邦期貨

1. `config.yaml` 把 `fubon_futures.enabled` 改 `true`,並設定 `underlying` /
   `multiplier`(範例是小型台灣50期貨:標的 `0050`、每口 `1000` 單位)
2. 期貨帳戶記的不是「股數 × 現價」,而是拆成兩塊:
   - **實質價值** = 標的收盤 × 乘數 × 淨口數 → 算投資部位
   - **現金部分** = 帳戶權益數 − 實質價值 → 算現金

   這樣「停在期貨帳戶沒在做事的錢」才會正確反映在現金比重上。口數自動從帳戶抓,
   加減口不用手動改。

> 目前實質價值用的是**標的現股收盤價**,不是期貨報價,所以會有基差的誤差。
> 這不影響淨值(權益數本來就是券商用期貨結算價 mark-to-market 算的),
> 只影響「投資部位 vs 現金」之間的切分,金額約等於基差 × 乘數 × 口數。
>
> 欄位已用真實帳戶驗證:`account_type == "futopt"` 辨識期貨帳戶;
> `query_margin_equity()` 的 `result.data` 是 **list**(一幣別一筆),要先取 `[0]`;
> 欄位是 snake_case 的 `today_balance` / `today_equity` / `fut_unrealized_pnl` /
> `opt_pnl` / `buy_lot` / `sell_lot`。
>
> 若你的 SDK 版本欄位對不上,跑 `debug_fubon_inv.py` 或 `debug_fubon_futures.py`
> 印出原始結構來對照。

---

## 安全

- **API key / 憑證只放 `.env`**,已列入 `.gitignore`,不會進版控。
- 本工具**只查詢,不下單**。唯一會下單的是 `shioaji_test_flow.py`,而它寫死
  `simulation=True`(模擬環境),且只在你手動執行時跑;永豐測試通過後可以刪掉。
- 建議測試流程跑完後,回券商後台把 key 的「交易」權限取消勾選,
  只留「行情/資料 + 帳務 + 正式環境」——這樣就算 key 外洩也只能看不能下單。
- 憑證檔(`.pfx`)建議放專案資料夾外面,避免整包資料夾被分享時一起外流。
