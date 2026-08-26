# TWnetvalue — 台股每日淨值追蹤

台股沒有像嘉信那種「打開就看到整戶淨值曲線」的原生體驗,因為券商戶和交割銀行戶
是分離的,券商 App 只看得到證券資產,看不到現金和負債。這支工具就是補這塊:

每天收盤後自動記錄
**淨值 = 證券市值 + 期貨部位 + 現金 + 未交割款 − 負債**,存進 SQLite,
用 Streamlit 看淨值曲線、回撤、現金比重、槓桿與曝險倍數,並跟加權指數 /
櫃買指數 / 0050 比相對績效。儀表板最上方另有大盤指標卡(點數、漲跌點、漲跌%)。

台股是 **T+2 交割**,賣出當天持股就從庫存消失、但錢兩個交易日後才進交割戶;
買進當天持股就進庫存、但錢還沒扣。中間這兩天如果不補未交割款,淨值會被低估
(賣)或高估(買),曲線上會出現不存在的假摔或假高點。這工具會自動抓券商的
未交割款把它補平。

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
| 買賣股票(API 模式) | **什麼都不用做**,持股、交割戶餘額、未交割款都自動抓 |
| 交割戶餘額變動(API 模式) | **什麼都不用做**,自動抓 |
| 其他銀行帳戶 / 負債變動 | 改 `config.yaml`,然後跑 `python update_cash.py`(秒開,不碰券商 API) |

- 同一天重跑 `snapshot.py` 會覆蓋當天資料,放心重跑。
- 快照日期以「最近交易日」為準(週末跑也不會出錯)。
- **手動跑 `snapshot.py` 要在下午 14:00 之後**(交割扣款完成、當日收盤已發布)。
  太早跑會抓到帳務結算到一半的中間狀態,程式會直接擋下來並提示。
- **不要把券商交割戶餘額寫進 `config.yaml` 的 `cash`**——那是自動抓的,寫了會重複計算。
  該區只填券商 API 看不到的錢(個人銀行帳戶、沒串接的券商等)。

---

## 檔案

| 檔案 | 用途 |
|---|---|
| `config.yaml` | 帳戶開關、現金、負債、基準指數 |
| `positions_manual.csv` | 手動持股表(台股) |
| `positions_foreign.csv` | 海外持股表(複委託,API 查不到只能手動) |
| `snapshot.py` | 每日快照主程式(排程跑這支) |
| `update_cash.py` | 只更新現金/負債,不重抓持股 |
| `cfgutil.py` | config 金額驗證(YAML 不會做運算,金額必須是純數字) |
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

1. `config.yaml` 把 `fubon_futures.enabled` 改 `true`,並設定 `multiplier`
   (契約乘數,小型台灣50期貨是 `1000`)
2. 期貨帳戶記的不是「股數 × 現價」,而是拆成兩塊:
   - **實質價值** = 期貨報價 × 乘數 × 淨口數 → 算投資部位
   - **現金部分** = 帳戶權益數 − 實質價值 → 算現金

   這樣「停在期貨帳戶沒在做事的錢」才會正確反映在現金比重上。口數與報價
   都自動抓,加減口不用手動改。
   槓桿部位的現金部分會是**負數**,那是正確的——代表曝險大於自有資金。

> ⚠️ **契約乘數要逐商品設定**:小型台灣50 ETF 期貨(`FISRF`)每口 1000 單位,
> 小型個股期貨(如小台達電 `FIRVF`)每口只有 100 股。全部套同一個乘數會把
> 個股期貨的部位價值放大 10 倍。沒設定到的 symbol 會用 `default_multiplier`
> 並印警告。
>
> ⚠️ **口數一定要用 `query_single_position()`,不能用 `query_margin_equity()`
> 的 `buy_lot` / `sell_lot`**——後者是「**當日**成交口數」,沒交易的日子會是 0,
> 會把在手部位誤判成空手(實測:帳上 2 口但兩個欄位都是 0,`initial_margin`
> 15,800 才揭露真相)。
>
> `query_single_position()` 回傳每筆未平倉,含 `orig_lots`、`buy_sell`,
> 以及 **`market_price`(期貨即時報價)**——直接拿它算實質價值,不必用現股價
> 替代,沒有基差誤差。
>
> 其他已驗證欄位:`account_type == "futopt"` 辨識期貨帳戶;
> `query_margin_equity()` 的 `result.data` 是 **list**(一幣別一筆),要先取 `[0]`;
> 權益數欄位是 `today_balance` / `today_equity` / `fut_unrealized_pnl` / `opt_pnl`。
>
> 若你的 SDK 版本欄位對不上,跑 `debug_fubon_inv.py`、`debug_fubon_futures.py`
> 或 `debug_settlements.py` 印出原始結構來對照。

### 海外持股(複委託)

**富邦 Neo API 查不到複委託庫存**——SDK 只有 `stock` 和 `futopt` 兩個模組,
登入也只回傳這兩個帳戶。所以海外部位只能手動維護股數:

1. 複製 `positions_foreign.example.csv` 成 `positions_foreign.csv`,填入
   Yahoo Finance 代號與股數(日股加 `.T`,如 `7203.T`;美股直接寫 `AAPL`)
2. `config.yaml` 把 `fubon_foreign.enabled` 改 `true`

價格與匯率都由 yfinance 自動抓並換算成台幣,只需要維護股數。持股明細會在
名稱欄顯示原幣價格(如 `7203.T(JPY 2,161.00)`)方便對帳。

`cost` 欄位選填,填**原幣**的每股成本(日股填日圓、美股填美元),留空就不顯示
該檔的成本損益。成本與現價都用**當天匯率**換算台幣,匯率在兩者的比值裡抵銷,
所以報酬率等於原幣計價的漲跌幅——看的是股票本身的表現,不受匯率干擾。

進階(不填就用不到):

- **`cost_date` 欄**:填買進日期 `YYYY-MM-DD`,該批成本改用**那天**的匯率換算,
  得到含匯率損益的真台幣成本。匯率市場遇假日沒報價,程式會往前找最近的交易日。
- **同一檔分多列**:代表分批建倉,各批用自己的日期換匯後加權平均,合併成一筆
  部位顯示。任一批沒填成本,整檔就留空(不拿半套資料算出誤導的均價)。

> ⚠️ **買賣海外持股後一定要更新這個檔案**。錢從交割戶出去是自動抓到的,但買到的
> 股票不會自動出現——只記到支出、沒記到資產,淨值會憑空少一截看起來像暴跌。

> CSV 檔容易被 Excel / 記事本存成 Big5 導致中文註解損毀。讀取器會依序嘗試
> `utf-8-sig` → `cp950` → `big5`,再退回 `errors="replace"`,所以註解壞掉
> 不會讓整份快照失敗(代號與股數是 ASCII 不受影響),但會印警告提醒你重存。

---

## 成本與損益

持股明細會顯示**成本均價、成本市值、損益、報酬%**,資料來源:

- **永豐**:`list_positions()` 的 `price` 欄位就是均價成本
  (容易誤會——現價在 `last_price`)
- **富邦**:`inventories()` 沒有成本欄位,要另外呼叫
  `unrealized_gains_and_loses()` 拿 `cost_price`,以 `stock_no` 對回庫存
- **手動表**:`cost` 欄位選填

同一檔跨券商持有時,成本用**加權平均**合併(同一檔在不同券商的買進成本
往往差很多)。任一邊缺成本就整檔留空顯示「—」,不會拿部分資料算出誤導的均價。

---

## 風險指標:槓桿比 vs 曝險倍數

儀表板上有兩個看起來像、但意義不同的指標:

| 指標 | 公式 | 看的是 |
|---|---|---|
| **槓桿比** | 負債 ÷ 淨值 | 借了多少錢 |
| **曝險倍數** | 投資部位 ÷ 淨值 | 市場波動的放大倍數 |

**槓桿比看不到期貨槓桿** —— 期貨不是借款,但它用保證金撐起數倍的標的曝險。
曝險倍數把期貨實質價值算進去,才是「市場動 10%,我的淨值動幾 %」的答案。

實例:2 口個股期貨,初始保證金合計約 3.9 萬,實質價值 23 萬 → 槓桿約 5.8 倍。
這部分完全不會出現在負債欄位裡。

兩者都有歷史走勢圖,可以看出槓桿是何時加上去的。

---

## 大盤指標卡

`config.yaml` 的 `market_cards` 決定儀表板最上方顯示哪些指數(點數、漲跌點、
漲跌%),清單裡的 id 必須先出現在 `benchmarks`。

FinMind 的 id:加權指數 `TAIEX`、**櫃買指數 `TPEx`**(`TWO` / `OTC` 都查不到資料)。

> 漲跌是拿**未經期間篩選**的完整資料算的。若用篩選後的資料,切到「1 個月」時
> 期初那天會找不到前一交易日,漲跌會變成空值。
>
> 新增基準指數後,歷史收盤**不會自動回填**,只會從下次快照開始累積。要補歷史
> 得單獨抓 FinMind 寫進 `benchmarks` 表(指數是純市場資料,不受
> `snapshot.py` 的執行時間限制)。

---

## T+2 交割與現金追蹤

台股 T+2 制度會讓「持股變動」和「現金變動」差兩個交易日,不處理的話淨值曲線
會出現假摔(賣出後)或假高點(買進後)。這工具用兩個機制補平:

**1. 交割戶餘額自動抓**(API 模式)
- 永豐:`api.account_balance()` → `acc_balance`
- 富邦:`sdk.accounting.bank_remain()` → `data.balance`

**2. 未交割款自動計入**

兩家都取「**交割日晚於 `snap_date`**」的金額,`snap_date` 是這筆快照代表的
交易日(由 FinMind 最新收盤日決定):

- 富邦:`query_settlement(account, "3d")` 的 `details`,看 `settlement_date`
  與 `total_settlement_amount`(負數=應付/買進、正數=應收/賣出;
  沒成交的日期欄位全是 `None`,要略過)
- 永豐:`api.settlements()` 的 `date` 與 `amount`

> ⚠️ **基準必須是 `snap_date`,不能用 `date.today()`。**
>
> 券商在交割後**不會**把該筆移出清單——它是「哪天成交、哪天交割」的歷史
> 紀錄,會留在三天的查詢視窗內,所以清單本身分辨不出交割與否,只能靠日期判斷。
> 而用牆上時鐘當基準會壞在跨午夜執行:
>
> | | 用 `date.today()` | 用 `snap_date` |
> |---|---|---|
> | 08/26 收盤的快照在 08/27 凌晨跑 | 08/27 > 08/27 ✗ 濾掉 | 08/27 > 08/26 ✓ 計入 |
>
> 實際踩過:一筆 25 號賣出、27 號交割的應收款在 27 號凌晨跑快照時被濾掉,
> 但那時錢根本還沒入帳(交割扣付款約在交割日上午),整筆賣股金額憑空消失,
> 淨值看起來像暴跌。改用 `snap_date` 後,不論幾點跑都對應同一個交易日的收盤狀態。
>
> 另外仍保留執行時間檢查(平日 14:00 前中止,可用 `--force` 略過),
> 主要是避免 FinMind 尚未發布當日收盤時把快照寫到前一天;排程 16:32 不受影響。

**3. 借錢買股票的記帳時序**(不然同一筆錢會被算兩次)
- **買進當天**:應付款已記在「未交割款」,`config.yaml` 的 `debt` **先不要加**
- **借款撥入交割戶時**:現金增加,這時 `debt` 才要加上借款
- **交割日扣款後**:未交割款歸零、交割戶被扣,`debt` 維持

判斷原則:`debt` 填的是**當下實際欠款**,錢還沒撥下來就還不是負債。

淨值公式因此是 `投資部位 + 現金 + 未交割款 − 負債`。買進當天就認列應付、
賣出當天就認列應收,兩天的失真窗口消失。

---

## 安全

- **API key / 憑證只放 `.env`**,已列入 `.gitignore`,不會進版控。
- 本工具**只查詢,不下單**。唯一會下單的是 `shioaji_test_flow.py`,而它寫死
  `simulation=True`(模擬環境),且只在你手動執行時跑;永豐測試通過後可以刪掉。
- 建議測試流程跑完後,回券商後台把 key 的「交易」權限取消勾選,
  只留「行情/資料 + 帳務 + 正式環境」——這樣就算 key 外洩也只能看不能下單。
- 憑證檔(`.pfx`)建議放專案資料夾外面,避免整包資料夾被分享時一起外流。
