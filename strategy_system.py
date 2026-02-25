#!/usr/bin/env python3
"""贵金属（金/银）周频量化策略系统。

功能：
1) 基于周线行情构建多因子评分（趋势、动量、均值回归、波动率、金银比）
2) 输出买入/卖出/观望建议，并给出价格区间与理由
3) 记录用户手工交易，回放并统计收益
4) 生成基础报表（权益曲线、持仓与行情同表）
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Tuple

import numpy as np
import pandas as pd


SYMBOLS = ("XAUUSD", "XAGUSD")


@dataclass
class SignalResult:
    symbol: str
    action: str
    score: float
    current_price: float
    buy_zone: Tuple[float, float]
    sell_zone: Tuple[float, float]
    reasons: List[str]


def load_prices(path: Path) -> pd.DataFrame:
    df = pd.read_csv(path)
    required = {"date", "symbol", "close"}
    if not required.issubset(df.columns):
        raise ValueError(f"行情文件需包含列: {required}")
    df["date"] = pd.to_datetime(df["date"])
    df = df.sort_values(["symbol", "date"]).reset_index(drop=True)
    return df


def engineer_features(df: pd.DataFrame) -> pd.DataFrame:
    out = []
    for sym, g in df.groupby("symbol", sort=False):
        g = g.copy()
        g["ret_1w"] = g["close"].pct_change()
        g["ma_4"] = g["close"].rolling(4).mean()
        g["ma_12"] = g["close"].rolling(12).mean()
        g["mom_4"] = g["close"].pct_change(4)
        g["vol_8"] = g["ret_1w"].rolling(8).std()
        g["z_12"] = (g["close"] - g["close"].rolling(12).mean()) / g["close"].rolling(12).std()
        g["atr_proxy"] = g["ret_1w"].abs().rolling(6).mean() * g["close"]
        out.append(g)
    feat = pd.concat(out).sort_values(["symbol", "date"]).reset_index(drop=True)

    # 金银比因子：黄金对比白银的相对强弱
    pivot = feat.pivot(index="date", columns="symbol", values="close")
    ratio = (pivot["XAUUSD"] / pivot["XAGUSD"]).rename("gold_silver_ratio")
    ratio_z = ((ratio - ratio.rolling(20).mean()) / ratio.rolling(20).std()).rename("ratio_z_20")
    ratio_df = pd.concat([ratio, ratio_z], axis=1).reset_index()
    feat = feat.merge(ratio_df, on="date", how="left")
    return feat


def score_row(r: pd.Series) -> Tuple[float, List[str]]:
    reasons = []
    score = 0.0

    # 趋势因子
    if pd.notna(r["ma_4"]) and pd.notna(r["ma_12"]):
        if r["ma_4"] > r["ma_12"]:
            score += 1.0
            reasons.append("短期均线高于中期均线，趋势偏多")
        else:
            score -= 1.0
            reasons.append("短期均线低于中期均线，趋势偏空")

    # 动量因子
    if pd.notna(r["mom_4"]):
        if r["mom_4"] > 0.02:
            score += 1.0
            reasons.append("4周动量为正且较强")
        elif r["mom_4"] < -0.02:
            score -= 1.0
            reasons.append("4周动量为负且较强")
        else:
            reasons.append("4周动量中性")

    # 均值回归因子
    if pd.notna(r["z_12"]):
        if r["z_12"] < -1.0:
            score += 0.8
            reasons.append("价格位于12周均值下方较深，存在反弹概率")
        elif r["z_12"] > 1.2:
            score -= 0.8
            reasons.append("价格偏离12周均值过高，警惕回撤")

    # 金银比因子：对黄金和白银方向影响相反
    if pd.notna(r["ratio_z_20"]):
        if r["symbol"] == "XAUUSD":
            if r["ratio_z_20"] > 1.0:
                score -= 0.5
                reasons.append("金银比偏高，黄金相对估值偏贵")
            elif r["ratio_z_20"] < -1.0:
                score += 0.5
                reasons.append("金银比偏低，黄金相对估值改善")
        if r["symbol"] == "XAGUSD":
            if r["ratio_z_20"] > 1.0:
                score += 0.5
                reasons.append("金银比偏高，白银相对估值偏低")
            elif r["ratio_z_20"] < -1.0:
                score -= 0.5
                reasons.append("金银比偏低，白银相对估值偏贵")

    # 波动惩罚因子
    if pd.notna(r["vol_8"]):
        if r["vol_8"] > 0.045:
            score -= 0.4
            reasons.append("近8周波动偏高，降低仓位激进程度")

    return score, reasons


def build_signal(r: pd.Series, score: float, reasons: List[str]) -> SignalResult:
    px = float(r["close"])
    atr = float(r["atr_proxy"]) if pd.notna(r["atr_proxy"]) else px * 0.02
    buy_zone = (round(px - 0.8 * atr, 2), round(px - 0.2 * atr, 2))
    sell_zone = (round(px + 0.2 * atr, 2), round(px + 0.8 * atr, 2))

    if score >= 1.5:
        action = "买入/加仓"
        reasons.append("综合评分达到做多阈值（>=1.5）")
    elif score <= -1.5:
        action = "卖出/减仓"
        reasons.append("综合评分达到做空或减仓阈值（<=-1.5）")
    else:
        action = "观望/持有"
        reasons.append("综合评分位于中性区间，等待更清晰信号")

    return SignalResult(
        symbol=r["symbol"],
        action=action,
        score=round(score, 2),
        current_price=round(px, 2),
        buy_zone=buy_zone,
        sell_zone=sell_zone,
        reasons=reasons,
    )


def latest_signals(feat: pd.DataFrame) -> List[SignalResult]:
    res: List[SignalResult] = []
    for sym in SYMBOLS:
        row = feat[feat["symbol"] == sym].sort_values("date").iloc[-1]
        score, reasons = score_row(row)
        res.append(build_signal(row, score, reasons))
    return res


def read_trades(path: Path) -> pd.DataFrame:
    if not path.exists():
        return pd.DataFrame(columns=["datetime", "symbol", "side", "price", "qty"])
    t = pd.read_csv(path)
    if t.empty:
        return pd.DataFrame(columns=["datetime", "symbol", "side", "price", "qty"])
    t["datetime"] = pd.to_datetime(t["datetime"])
    t = t.sort_values("datetime").reset_index(drop=True)
    return t


def append_trade(path: Path, symbol: str, side: str, price: float, qty: float, dt: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    exists = path.exists()
    row = pd.DataFrame([
        {"datetime": dt, "symbol": symbol, "side": side.lower(), "price": price, "qty": qty}
    ])
    row.to_csv(path, mode="a", header=not exists, index=False)


def backtest_with_manual_trades(prices: pd.DataFrame, trades: pd.DataFrame, initial_cash: float = 100000.0) -> pd.DataFrame:
    weekly = prices.pivot(index="date", columns="symbol", values="close").sort_index().copy()
    weekly["cash"] = np.nan
    for sym in SYMBOLS:
        weekly[f"pos_{sym}"] = 0.0
        weekly[f"mkt_{sym}"] = 0.0
    cash = initial_cash
    pos: Dict[str, float] = {s: 0.0 for s in SYMBOLS}

    # 将交易按周合并（用户手工执行）
    if not trades.empty:
        trades = trades.copy()
        trades["week"] = trades["datetime"].dt.to_period("W-FRI").apply(lambda p: p.end_time.normalize())

    for dt in weekly.index:
        if not trades.empty:
            sub = trades[trades["week"] == dt]
            for _, tr in sub.iterrows():
                sym = tr["symbol"]
                qty = float(tr["qty"])
                val = float(tr["price"]) * qty
                if tr["side"] == "buy":
                    cash -= val
                    pos[sym] += qty
                elif tr["side"] == "sell":
                    cash += val
                    pos[sym] -= qty

        weekly.loc[dt, "cash"] = cash
        for sym in SYMBOLS:
            weekly.loc[dt, f"pos_{sym}"] = pos[sym]
            weekly.loc[dt, f"mkt_{sym}"] = pos[sym] * weekly.loc[dt, sym]

    weekly["equity"] = weekly["cash"] + weekly[[f"mkt_{s}" for s in SYMBOLS]].sum(axis=1)
    weekly["equity_ret"] = weekly["equity"].pct_change().fillna(0.0)
    weekly["cum_ret"] = (1 + weekly["equity_ret"]).cumprod() - 1
    return weekly.reset_index()


def render_signal_text(signals: List[SignalResult], as_of: pd.Timestamp) -> str:
    lines = [f"\n=== 贵金属周频策略建议 | 截止 {as_of.date()} ==="]
    for s in signals:
        lines.append(f"\n[{s.symbol}] 当前价: {s.current_price}")
        lines.append(f"- 动作建议: {s.action} (评分: {s.score})")
        lines.append(f"- 买入区间: {s.buy_zone[0]} ~ {s.buy_zone[1]}")
        lines.append(f"- 卖出区间: {s.sell_zone[0]} ~ {s.sell_zone[1]}")
        lines.append("- 理由:")
        lines.extend([f"  * {r}" for r in s.reasons])
    return "\n".join(lines)


def save_reports(report_df: pd.DataFrame, out_dir: Path) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    table_cols = [
        "date",
        "XAUUSD",
        "XAGUSD",
        "pos_XAUUSD",
        "pos_XAGUSD",
        "cash",
        "equity",
        "cum_ret",
    ]
    report_df[table_cols].to_csv(out_dir / "portfolio_report.csv", index=False)


def main() -> None:
    parser = argparse.ArgumentParser(description="黄金/白银周频量化策略系统")
    parser.add_argument("--price-file", default="data/weekly_prices.csv", help="周线价格CSV")
    parser.add_argument("--trade-file", default="data/manual_trades.csv", help="手工交易记录CSV")
    parser.add_argument("--out-dir", default="output", help="报表输出目录")

    parser.add_argument("--add-trade", action="store_true", help="追加一笔手工交易")
    parser.add_argument("--symbol", choices=SYMBOLS)
    parser.add_argument("--side", choices=["buy", "sell"])
    parser.add_argument("--price", type=float)
    parser.add_argument("--qty", type=float)
    parser.add_argument("--datetime", help="格式: YYYY-MM-DD HH:MM")

    args = parser.parse_args()

    trade_path = Path(args.trade_file)
    if args.add_trade:
        required = [args.symbol, args.side, args.price, args.qty, args.datetime]
        if any(v is None for v in required):
            raise ValueError("--add-trade 模式下需提供 --symbol --side --price --qty --datetime")
        append_trade(trade_path, args.symbol, args.side, args.price, args.qty, args.datetime)
        print(f"已记录交易: {args.symbol} {args.side} qty={args.qty} @ {args.price} ({args.datetime})")

    prices = load_prices(Path(args.price_file))
    feat = engineer_features(prices)
    signals = latest_signals(feat)
    as_of = feat["date"].max()

    print(render_signal_text(signals, as_of))

    trades = read_trades(trade_path)
    report = backtest_with_manual_trades(prices, trades)
    save_reports(report, Path(args.out_dir))

    latest = report.iloc[-1]
    print("\n=== 账户摘要 ===")
    print(f"- 最新权益: {latest['equity']:.2f}")
    print(f"- 累计收益率: {latest['cum_ret']*100:.2f}%")
    print(f"- 报表输出: {Path(args.out_dir) / 'portfolio_report.csv'}")


if __name__ == "__main__":
    main()
