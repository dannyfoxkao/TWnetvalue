"""淨值儀表板:streamlit run dashboard.py"""
import sqlite3
from pathlib import Path

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

DB_PATH = Path(__file__).parent / "networth.db"

# 色彩(dataviz 驗證過的固定順序色盤,light mode)
C_NET = "#2a78d6"      # 淨值(slot 1 藍)
C_BENCH = ["#008300", "#e87ba4", "#eda100"]  # 基準(slot 2/3/4)
C_DRAWDOWN = "#e34948"  # 回撤
C_MARGIN = "#2a78d6"    # 期貨保證金餘額(slot 1 藍,跟淨值同色系代表「本」)
C_PNL = "#eb6834"       # 期貨未實現損益(slot 6 橘,跟回撤紅區隔開,正負都用同色)
INK_MUTED = "#898781"
GRID = "#e1e0d9"

LAYOUT = dict(
    template="plotly_white",
    margin=dict(l=10, r=10, t=44, b=48),
    height=360,
    hovermode="x unified",
    font=dict(family='system-ui, "Segoe UI", sans-serif'),
    xaxis=dict(gridcolor=GRID, linecolor="#c3c2b7"),
    yaxis=dict(gridcolor=GRID, linecolor="#c3c2b7"),
    # 標題留在左上;圖例移到圖表下方,避免跟標題擠在同一列重疊
    title=dict(x=0, y=0.97, yanchor="top"),
    legend=dict(orientation="h", yanchor="top", y=-0.18, x=0),
)

BENCH_NAMES = {"TAIEX": "加權指數", "0050": "0050"}

st.set_page_config(page_title="台股淨值追蹤", layout="wide")


@st.cache_data(ttl=300)
def load():
    conn = sqlite3.connect(DB_PATH)
    totals = pd.read_sql("SELECT * FROM totals ORDER BY date", conn)
    accounts = pd.read_sql("SELECT * FROM account_snapshots ORDER BY date", conn)
    bench = pd.read_sql("SELECT * FROM benchmarks ORDER BY date", conn)
    latest_pos = pd.read_sql(
        "SELECT * FROM positions WHERE date = (SELECT MAX(date) FROM positions) "
        "ORDER BY market_value DESC", conn)
    futures = pd.read_sql("SELECT * FROM futures_snapshots ORDER BY date", conn)
    conn.close()
    return totals, accounts, bench, latest_pos, futures


if not DB_PATH.exists():
    st.warning("還沒有資料。先跑一次 `python snapshot.py` 建立第一筆快照。")
    st.stop()

totals, accounts, bench, latest_pos, futures = load()
if totals.empty:
    st.warning("資料庫是空的。先跑一次 `python snapshot.py`。")
    st.stop()

st.title("台股淨值追蹤")

# ---------- 期間篩選 ----------
period = st.radio("期間", ["全部", "1年", "6個月", "3個月", "1個月"],
                  horizontal=True, label_visibility="collapsed")
days = {"全部": None, "1年": 365, "6個月": 182, "3個月": 91, "1個月": 30}[period]
if days:
    cutoff = (pd.Timestamp.today() - pd.Timedelta(days=days)).strftime("%Y-%m-%d")
    totals = totals[totals["date"] >= cutoff]
    accounts = accounts[accounts["date"] >= cutoff]
    bench = bench[bench["date"] >= cutoff]
    futures = futures[futures["date"] >= cutoff]
if totals.empty:
    st.info("此期間沒有資料。")
    st.stop()

# ---------- 指標列 ----------
nv = totals["net_value"]
latest = totals.iloc[-1]
day_chg = nv.iloc[-1] - nv.iloc[-2] if len(nv) > 1 else 0.0
day_pct = day_chg / nv.iloc[-2] * 100 if len(nv) > 1 and nv.iloc[-2] else 0.0
total_ret = (nv.iloc[-1] / nv.iloc[0] - 1) * 100 if nv.iloc[0] else 0.0
running_max = nv.cummax()
drawdown = (nv / running_max - 1) * 100
leverage = latest["debt"] / latest["net_value"] * 100 if latest["net_value"] else 0.0
# 現金佔總資產比例 = 現金 / 總資產(總資產 = 淨值 + 負債 = 投資部位 + 現金)
gross_assets = latest["net_value"] + latest["debt"]
cash_pct = latest["cash"] / gross_assets * 100 if gross_assets else 0.0

c1, c2, c3, c4, c5, c6 = st.columns(6)
c1.metric("淨值", f"{latest['net_value']:,.0f}",
          f"{day_chg:+,.0f} ({day_pct:+.2f}%)")
c2.metric(f"累積報酬({period})", f"{total_ret:+.2f}%")
c3.metric("最大回撤", f"{drawdown.min():.2f}%")
c4.metric("目前回撤", f"{drawdown.iloc[-1]:.2f}%")
c5.metric("現金比重", f"{cash_pct:.1f}%")
c6.metric("槓桿比(負債/淨值)", f"{leverage:.1f}%")
st.caption(f"最後更新:{latest['date']}|市值 {latest['market_value']:,.0f}"
           f"|現金 {latest['cash']:,.0f}|負債 {latest['debt']:,.0f}")

# ---------- 淨值曲線 ----------
fig = go.Figure(layout=LAYOUT)
fig.add_scatter(x=totals["date"], y=nv, mode="lines", name="淨值",
                line=dict(color=C_NET, width=2),
                hovertemplate="%{y:,.0f}<extra></extra>")
fig.update_layout(title="淨值曲線", showlegend=False)
st.plotly_chart(fig, width="stretch")

# ---------- 相對績效(指數化 100) ----------
left, right = st.columns(2)
with left:
    fig = go.Figure(layout=LAYOUT)
    base = nv.iloc[0]
    fig.add_scatter(x=totals["date"], y=nv / base * 100, mode="lines",
                    name="淨值", line=dict(color=C_NET, width=2),
                    hovertemplate="%{y:.1f}<extra>淨值</extra>")
    for i, (bid, grp) in enumerate(bench.groupby("bench_id")):
        grp = grp.sort_values("date")
        b0 = grp["close"].iloc[0]
        name = BENCH_NAMES.get(bid, bid)
        fig.add_scatter(x=grp["date"], y=grp["close"] / b0 * 100, mode="lines",
                        name=name, line=dict(color=C_BENCH[i % len(C_BENCH)], width=2),
                        hovertemplate="%{y:.1f}<extra>" + name + "</extra>")
    fig.update_layout(title=f"相對績效(期初=100,{period})")
    st.plotly_chart(fig, width="stretch")

with right:
    fig = go.Figure(layout=LAYOUT)
    fig.add_scatter(x=totals["date"], y=drawdown, mode="lines", name="回撤",
                    fill="tozeroy", line=dict(color=C_DRAWDOWN, width=2),
                    fillcolor="rgba(227,73,72,0.15)",
                    hovertemplate="%{y:.2f}%<extra></extra>")
    fig.update_layout(title="回撤(%)", showlegend=False)
    st.plotly_chart(fig, width="stretch")

# ---------- 帳戶市值 ----------
if accounts["account"].nunique() > 1:
    fig = go.Figure(layout=LAYOUT)
    palette = [C_NET] + C_BENCH
    for i, (acc, grp) in enumerate(accounts.groupby("account")):
        grp = grp.sort_values("date")
        fig.add_scatter(x=grp["date"], y=grp["market_value"], mode="lines",
                        name=acc, line=dict(color=palette[i % len(palette)], width=2),
                        hovertemplate="%{y:,.0f}<extra>" + acc + "</extra>")
    fig.update_layout(title="各帳戶市值")
    st.plotly_chart(fig, width="stretch")

# ---------- 期貨帳戶(實質價值 vs 現金部分) ----------
if not futures.empty:
    st.subheader("期貨帳戶")
    for acc, grp in futures.groupby("account"):
        grp = grp.sort_values("date")
        latest_f = grp.iloc[-1]
        has_split = "notional" in grp.columns and pd.notna(latest_f.get("notional"))
        fc1, fc2, fc3, fc4 = st.columns(4)
        fc1.metric(f"{acc} 權益數", f"{latest_f['equity']:,.0f}")
        if has_split:
            lots = latest_f.get("net_lots")
            lots_txt = f"{lots:+.0f} 口" if pd.notna(lots) else ""
            fc2.metric("實質價值(那一口)", f"{latest_f['notional']:,.0f}", lots_txt)
            fc3.metric("現金部分", f"{latest_f['free_cash']:,.0f}")
        else:
            fc2.metric("保證金餘額", f"{latest_f['cash_balance']:,.0f}")
            fc3.metric("—", "—")
        fc4.metric("未實現損益", f"{latest_f['unrealized_pnl']:+,.0f}")

        fig = go.Figure(layout=LAYOUT)
        if has_split:
            fig.add_bar(x=grp["date"], y=grp["notional"], name="實質價值(那一口)",
                        marker_color=C_MARGIN,
                        hovertemplate="%{y:,.0f}<extra>實質價值</extra>")
            fig.add_bar(x=grp["date"], y=grp["free_cash"], name="現金部分",
                        marker_color=C_PNL,
                        hovertemplate="%{y:,.0f}<extra>現金部分</extra>")
            fig.update_layout(
                title=f"{acc}:實質價值 + 現金部分 = 權益數", barmode="relative")
        else:
            fig.add_bar(x=grp["date"], y=grp["cash_balance"], name="保證金餘額",
                        marker_color=C_MARGIN,
                        hovertemplate="%{y:,.0f}<extra>保證金餘額</extra>")
            fig.add_bar(x=grp["date"], y=grp["unrealized_pnl"], name="未實現損益",
                        marker_color=C_PNL,
                        hovertemplate="%{y:+,.0f}<extra>未實現損益</extra>")
            fig.update_layout(
                title=f"{acc}:保證金餘額 + 未實現損益 = 權益數", barmode="relative")
        st.plotly_chart(fig, width="stretch")

# ---------- 最新持股 ----------
st.subheader(f"持股明細({latest_pos['date'].iloc[0] if not latest_pos.empty else '—'})")
if latest_pos.empty:
    st.info("沒有持股資料。")
else:
    has_name = "name" in latest_pos.columns
    total_mv = latest_pos["market_value"].sum()

    rows = []
    # 同一代號跨券商合併成一列,另存各券商股數拆解
    for code, grp in latest_pos.groupby("code"):
        grp = grp.sort_values("shares", ascending=False)
        breakdown = " + ".join(
            f"{r['account']} {r['shares']:,.0f}" for _, r in grp.iterrows()
        )
        rows.append({
            "代號": code,
            "名稱": grp["name"].iloc[0] if has_name else "",
            "總股數": grp["shares"].sum(),
            "價格": grp["price"].iloc[0],
            "市值": grp["market_value"].sum(),
            "比重%": grp["market_value"].sum() / total_mv * 100,
            "帳戶拆解": breakdown,
        })

    show = pd.DataFrame(rows).sort_values("市值", ascending=False)
    if not has_name:
        show = show.drop(columns=["名稱"])
    st.dataframe(
        show.style.format({"總股數": "{:,.0f}", "價格": "{:,.2f}",
                           "市值": "{:,.0f}", "比重%": "{:.1f}"}),
        width="stretch", hide_index=True)
