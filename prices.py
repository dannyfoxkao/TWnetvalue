"""FinMind 價格層:個股收盤價與基準指數。"""
import datetime as dt
import json
import time
from pathlib import Path

import requests

API_URL = "https://api.finmindtrade.com/api/v4/data"
NAMES_CACHE = Path(__file__).parent / "stock_names.json"
NAMES_TTL_DAYS = 30  # 股票名稱極少變,快取一個月才重抓一次


def load_token(token_file: str) -> str:
    path = Path(__file__).parent / token_file
    return json.loads(path.read_text(encoding="utf-8"))["api_token"]


def _fetch(stock_id: str, token: str, days: int = 15):
    """抓最近 days 天的日K,回傳 list[dict](FinMind data rows)。"""
    start = (dt.date.today() - dt.timedelta(days=days)).isoformat()
    resp = requests.get(
        API_URL,
        params={
            "dataset": "TaiwanStockPrice",
            "data_id": stock_id,
            "start_date": start,
            "token": token,
        },
        timeout=30,
    )
    resp.raise_for_status()
    body = resp.json()
    if body.get("status") != 200:
        raise RuntimeError(f"FinMind error for {stock_id}: {body.get('msg')}")
    return body.get("data", [])


def latest_close(stock_id: str, token: str):
    """回傳 (date, close),抓不到回傳 None。"""
    rows = _fetch(stock_id, token)
    if not rows:
        return None
    last = rows[-1]
    return last["date"], float(last["close"])


def latest_closes(stock_ids, token) -> dict:
    """批次抓多檔的最新收盤價 → {stock_id: (date, close)}。"""
    out = {}
    for sid in stock_ids:
        result = latest_close(sid, token)
        if result is None:
            print(f"  [警告] {sid} 抓不到價格,略過")
            continue
        out[sid] = result
    return out


def stock_names(token: str) -> dict:
    """回傳 {stock_id: stock_name} 全市場對照表。

    用 FinMind TaiwanStockInfo,快取到本地 stock_names.json,一個月才重抓一次
    (名稱幾乎不變)。抓失敗且無快取時回傳空 dict,讓呼叫端可退回只顯示代號。
    """
    if NAMES_CACHE.exists():
        age_days = (time.time() - NAMES_CACHE.stat().st_mtime) / 86400
        if age_days < NAMES_TTL_DAYS:
            try:
                return json.loads(NAMES_CACHE.read_text(encoding="utf-8"))
            except (ValueError, OSError):
                pass  # 快取壞了就重抓

    try:
        resp = requests.get(
            API_URL,
            params={"dataset": "TaiwanStockInfo", "token": token},
            timeout=30,
        )
        resp.raise_for_status()
        rows = resp.json().get("data", [])
    except requests.RequestException:
        rows = []

    if not rows:
        # 抓不到:有舊快取就用舊的(過期總比沒有好),否則空 dict
        if NAMES_CACHE.exists():
            try:
                return json.loads(NAMES_CACHE.read_text(encoding="utf-8"))
            except (ValueError, OSError):
                return {}
        return {}

    names = {r["stock_id"]: r["stock_name"] for r in rows if r.get("stock_id")}
    try:
        NAMES_CACHE.write_text(
            json.dumps(names, ensure_ascii=False), encoding="utf-8"
        )
    except OSError:
        pass
    return names
