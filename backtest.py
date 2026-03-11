"""
Strategy E 백테스트: SMA200 필터 + DCA 357 + RSI 과매도 보너스 + 트레일링 스탑
- 5년 일봉 데이터 (yfinance)
- NVDL(2x NVDA), TSLL(2x TSLA), TQQQ(3x QQQ) 3종목 동시 운용
- 초기 자본 $100,000 (약 1.4억 원)
"""

import yfinance as yf
import pandas as pd
import numpy as np
from typing import Dict, List, Tuple
from dataclasses import dataclass, field
from datetime import datetime

LEVERAGED_ETF_MAP: Dict[str, str] = {
    "NVDL": "NVDA", "TSLL": "TSLA", "TQQQ": "QQQ",
}

INITIAL_CAPITAL: float = 100_000.0

# === Strategy Parameters (bot.py 동일) ===
SMA_PERIOD: int = 200
RSI_PERIOD: int = 14
RSI_OVERSOLD: float = 30.0

DCA_THRESHOLDS: List[Tuple[str, float]] = [
    ("t3", -0.03),
    ("t5", -0.05),
    ("t7", -0.07),
]

TRAILING_STOP_THRESHOLD: float = -0.40
TRAILING_SELL_PCT: float = 0.50

AGGRESSIVE: Dict[str, float] = {"base": 0.001, "w3": 0.025, "w5": 0.045, "w7": 0.065}
DEFENSIVE: Dict[str, float] = {"base": 0.001, "w3": 0.012, "w5": 0.022, "w7": 0.032}

CASH_DEFEND_RATIO: float = 0.35
SMA_DEFEND_COUNT: int = 2


@dataclass
class Position:
    symbol: str
    qty: float = 0.0
    avg_price: float = 0.0
    cost_basis: float = 0.0

    def buy(self, price: float, amount: float) -> float:
        shares: float = amount / price
        self.cost_basis += amount
        total_qty: float = self.qty + shares
        if total_qty > 0:
            self.avg_price = self.cost_basis / total_qty
        self.qty = total_qty
        return amount

    def sell(self, price: float, shares: float) -> float:
        shares = min(shares, self.qty)
        if shares <= 0:
            return 0.0
        proceeds: float = shares * price
        ratio: float = shares / self.qty if self.qty > 0 else 1.0
        self.cost_basis -= self.cost_basis * ratio
        self.qty -= shares
        return proceeds

    @property
    def market_value(self) -> float:
        return self.qty * self.avg_price


@dataclass
class TradeRecord:
    date: str
    symbol: str
    side: str
    price: float
    qty: float
    amount: float
    reason: str


@dataclass
class BacktestEngine:
    symbols: List[str]
    cash: float = INITIAL_CAPITAL
    positions: Dict[str, Position] = field(default_factory=dict)
    hwm: Dict[str, float] = field(default_factory=dict)
    trades: List[TradeRecord] = field(default_factory=list)
    equity_curve: List[Dict] = field(default_factory=list)
    mode: str = "aggressive"

    def total_equity(self, prices: Dict[str, float]) -> float:
        stock_val: float = sum(
            pos.qty * prices.get(sym, pos.avg_price)
            for sym, pos in self.positions.items()
        )
        return self.cash + stock_val

    def cash_ratio(self, prices: Dict[str, float]) -> float:
        te: float = self.total_equity(prices)
        return self.cash / te if te > 0 else 1.0

    def get_ratios(self) -> Dict[str, float]:
        return AGGRESSIVE if self.mode == "aggressive" else DEFENSIVE

    def place_buy(self, symbol: str, price: float, amount: float, reason: str, date: str) -> bool:
        amount = min(amount, self.cash * 0.99)
        if amount < 1.0 or price <= 0:
            return False
        spent: float = self.positions[symbol].buy(price, amount)
        self.cash -= spent
        self.trades.append(TradeRecord(date, symbol, "BUY", price, amount / price, spent, reason))
        return True

    def place_sell(self, symbol: str, price: float, shares: float, reason: str, date: str) -> bool:
        if shares <= 0:
            return False
        proceeds: float = self.positions[symbol].sell(price, shares)
        self.cash += proceeds
        self.trades.append(TradeRecord(date, symbol, "SELL", price, shares, proceeds, reason))
        return True


def compute_sma(series: pd.Series, period: int) -> pd.Series:
    return series.rolling(window=period, min_periods=period).mean()


def compute_rsi(series: pd.Series, period: int = 14) -> pd.Series:
    delta: pd.Series = series.diff()
    gain: pd.Series = delta.where(delta > 0, 0.0).ewm(alpha=1 / period, adjust=False).mean()
    loss_raw: pd.Series = (-delta.where(delta < 0, 0.0)).ewm(alpha=1 / period, adjust=False).mean()
    loss_safe: pd.Series = loss_raw.replace(0.0, 1e-10)
    rs: pd.Series = gain / loss_safe
    rsi: pd.Series = 100 - (100 / (1 + rs))
    return rsi.fillna(50.0)


def download_data(symbols: List[str], years: int = 5) -> Dict[str, pd.DataFrame]:
    all_tickers: set = set(symbols)
    for sym in symbols:
        base: str = LEVERAGED_ETF_MAP.get(sym, sym)
        all_tickers.add(base)

    data: Dict[str, pd.DataFrame] = {}
    period: str = f"{years}y"
    for ticker in sorted(all_tickers):
        print(f"  다운로드: {ticker}...", end=" ")
        df: pd.DataFrame = yf.Ticker(ticker).history(period=period, interval="1d")
        if df.empty:
            print("실패!")
            continue
        df.index = df.index.tz_localize(None)
        data[ticker] = df
        print(f"{len(df)}일")

    return data


def run_backtest(symbols: List[str], years: int = 5) -> BacktestEngine:
    print(f"\n{'='*60}")
    print(f"Strategy E 백테스트 ({years}년)")
    print(f"종목: {', '.join(symbols)}")
    print(f"초기 자본: ${INITIAL_CAPITAL:,.0f}")
    print(f"{'='*60}\n")

    print("[1/3] 데이터 다운로드 중...")
    data: Dict[str, pd.DataFrame] = download_data(symbols, years)

    print("\n[2/3] 기술지표 계산 중...")
    indicators: Dict[str, pd.DataFrame] = {}
    for sym in symbols:
        base: str = LEVERAGED_ETF_MAP.get(sym, sym)
        if base not in data or sym not in data:
            print(f"  ⚠️ {sym} 데이터 없음, 건너뜀")
            continue

        base_df: pd.DataFrame = data[base].copy()
        etf_df: pd.DataFrame = data[sym].copy()

        base_df["sma200"] = compute_sma(base_df["Close"], SMA_PERIOD)
        base_df["rsi"] = compute_rsi(base_df["Close"], RSI_PERIOD)

        common_idx: pd.DatetimeIndex = etf_df.index.intersection(base_df.index)
        merged: pd.DataFrame = pd.DataFrame(index=common_idx)
        merged["price"] = etf_df.loc[common_idx, "Close"]
        merged["open"] = etf_df.loc[common_idx, "Open"]
        merged["high"] = etf_df.loc[common_idx, "High"]
        merged["low"] = etf_df.loc[common_idx, "Low"]
        merged["base_close"] = base_df.loc[common_idx, "Close"]
        merged["base_sma200"] = base_df.loc[common_idx, "sma200"]
        merged["base_rsi"] = base_df.loc[common_idx, "rsi"]
        merged["prev_close"] = merged["price"].shift(1)
        merged["is_uptrend"] = merged["base_close"] > merged["base_sma200"]
        merged["is_rsi_oversold"] = merged["base_rsi"] < RSI_OVERSOLD

        indicators[sym] = merged.dropna(subset=["base_sma200", "prev_close"])
        print(f"  {sym}: {len(indicators[sym])}일 데이터 준비 완료")

    common_dates: pd.DatetimeIndex = indicators[symbols[0]].index
    for sym in symbols[1:]:
        if sym in indicators:
            common_dates = common_dates.intersection(indicators[sym].index)
    print(f"  공통 거래일: {len(common_dates)}일")

    print("\n[3/3] 백테스트 실행 중...")
    engine = BacktestEngine(symbols=symbols)
    for sym in symbols:
        engine.positions[sym] = Position(symbol=sym)

    initial_buy_pct: float = 0.15
    for sym in symbols:
        if sym in indicators and len(common_dates) > 0:
            first_price: float = float(indicators[sym].loc[common_dates[0], "price"])
            init_amount: float = INITIAL_CAPITAL * initial_buy_pct
            engine.place_buy(sym, first_price, init_amount, "초기 매수", str(common_dates[0].date()))
            engine.hwm[sym] = first_price

    for date in common_dates:
        date_str: str = str(date.date())
        prices: Dict[str, float] = {}
        for sym in symbols:
            if sym in indicators and date in indicators[sym].index:
                prices[sym] = float(indicators[sym].loc[date, "price"])

        te: float = engine.total_equity(prices)
        cr: float = engine.cash_ratio(prices)

        below_sma: int = sum(
            1 for sym in symbols
            if sym in indicators and date in indicators[sym].index
            and not bool(indicators[sym].loc[date, "is_uptrend"])
        )
        if cr <= CASH_DEFEND_RATIO or below_sma >= SMA_DEFEND_COUNT:
            engine.mode = "defensive"
        else:
            engine.mode = "aggressive"

        ratios: Dict[str, float] = engine.get_ratios()
        daily_state: Dict[str, Dict[str, bool]] = {sym: {"base": False, "t3": False, "t5": False, "t7": False, "rsi": False} for sym in symbols}

        for sym in symbols:
            if sym not in indicators or date not in indicators[sym].index:
                continue

            row = indicators[sym].loc[date]
            price: float = float(row["price"])
            low: float = float(row["low"])
            prev_close: float = float(row["prev_close"])
            is_uptrend: bool = bool(row["is_uptrend"])
            is_rsi_oversold: bool = bool(row["is_rsi_oversold"])
            pos: Position = engine.positions[sym]

            if price > engine.hwm.get(sym, 0.0):
                engine.hwm[sym] = price

            # Trailing Stop
            hwm_price: float = engine.hwm.get(sym, 0.0)
            if hwm_price > 0 and pos.qty > 0:
                drawdown: float = (price - hwm_price) / hwm_price
                if drawdown <= TRAILING_STOP_THRESHOLD:
                    sell_qty: float = pos.qty * TRAILING_SELL_PCT
                    engine.place_sell(sym, price, sell_qty, f"트레일링 스탑 ({drawdown*100:.1f}%)", date_str)

            # Daily base buy
            if is_uptrend and not daily_state[sym]["base"]:
                base_amt: float = te * ratios["base"]
                if engine.place_buy(sym, price, base_amt, "기본적립", date_str):
                    daily_state[sym]["base"] = True
                    te = engine.total_equity(prices)

            # DCA tiers
            if pos.qty > 0 and is_uptrend and prev_close > 0:
                intraday_drop: float = (low - prev_close) / prev_close
                tier_keys: List[str] = ["w3", "w5", "w7"]
                for i, (tier_name, threshold) in enumerate(DCA_THRESHOLDS):
                    if intraday_drop <= threshold and not daily_state[sym][tier_name]:
                        dca_price: float = prev_close * (1 + threshold)
                        dca_amt: float = te * ratios[tier_keys[i]]
                        if engine.place_buy(sym, dca_price, dca_amt, f"DCA {threshold*100:.0f}%", date_str):
                            daily_state[sym][tier_name] = True
                            te = engine.total_equity(prices)

            # RSI oversold bonus
            if pos.qty > 0 and is_rsi_oversold and not daily_state[sym]["rsi"]:
                rsi_amt: float = te * ratios["w5"]
                if engine.place_buy(sym, price, rsi_amt, "RSI 과매도 보너스", date_str):
                    daily_state[sym]["rsi"] = True
                    te = engine.total_equity(prices)

        engine.equity_curve.append({
            "date": date_str,
            "equity": engine.total_equity(prices),
            "cash": engine.cash,
            "mode": engine.mode,
        })

    return engine


def print_results(engine: BacktestEngine) -> None:
    if not engine.equity_curve:
        print("결과 없음")
        return

    eq: pd.DataFrame = pd.DataFrame(engine.equity_curve)
    eq["equity"] = eq["equity"].astype(float)
    eq["date"] = pd.to_datetime(eq["date"])

    final_equity: float = eq["equity"].iloc[-1]
    total_return: float = (final_equity / INITIAL_CAPITAL - 1) * 100
    years: float = (eq["date"].iloc[-1] - eq["date"].iloc[0]).days / 365.25
    cagr: float = ((final_equity / INITIAL_CAPITAL) ** (1 / years) - 1) * 100 if years > 0 else 0

    eq["peak"] = eq["equity"].cummax()
    eq["drawdown"] = (eq["equity"] - eq["peak"]) / eq["peak"]
    max_dd: float = eq["drawdown"].min() * 100
    max_dd_date: str = str(eq.loc[eq["drawdown"].idxmin(), "date"].date())

    eq["daily_return"] = eq["equity"].pct_change()
    sharpe: float = 0.0
    if eq["daily_return"].std() > 0:
        sharpe = (eq["daily_return"].mean() / eq["daily_return"].std()) * np.sqrt(252)

    neg_returns: pd.Series = eq["daily_return"][eq["daily_return"] < 0]
    sortino: float = 0.0
    if neg_returns.std() > 0:
        sortino = (eq["daily_return"].mean() / neg_returns.std()) * np.sqrt(252)

    trades_df: pd.DataFrame = pd.DataFrame([
        {"date": t.date, "symbol": t.symbol, "side": t.side, "price": t.price,
         "qty": t.qty, "amount": t.amount, "reason": t.reason}
        for t in engine.trades
    ])
    buy_trades: int = len(trades_df[trades_df["side"] == "BUY"]) if not trades_df.empty else 0
    sell_trades: int = len(trades_df[trades_df["side"] == "SELL"]) if not trades_df.empty else 0
    total_invested: float = trades_df[trades_df["side"] == "BUY"]["amount"].sum() if not trades_df.empty else 0

    defensive_days: int = len(eq[eq["mode"] == "defensive"])
    aggressive_days: int = len(eq[eq["mode"] == "aggressive"])

    # Yearly returns
    eq["year"] = eq["date"].dt.year
    yearly: List[Dict] = []
    for yr, grp in eq.groupby("year"):
        yr_start: float = grp["equity"].iloc[0]
        yr_end: float = grp["equity"].iloc[-1]
        yr_return: float = (yr_end / yr_start - 1) * 100
        yr_peak: float = grp["equity"].cummax().max()
        yr_dd: float = ((grp["equity"] - grp["equity"].cummax()) / grp["equity"].cummax()).min() * 100
        yearly.append({"year": yr, "return": yr_return, "max_dd": yr_dd, "end_equity": yr_end})

    print(f"\n{'='*60}")
    print("백테스트 결과")
    print(f"{'='*60}")
    print(f"\n📊 수익률 요약")
    print(f"  기간: {eq['date'].iloc[0].date()} ~ {eq['date'].iloc[-1].date()} ({years:.1f}년)")
    print(f"  초기 자본:   ${INITIAL_CAPITAL:>12,.2f}")
    print(f"  최종 자산:   ${final_equity:>12,.2f}")
    print(f"  총 수익률:   {total_return:>11.2f}%")
    print(f"  연환산(CAGR): {cagr:>10.2f}%")
    print(f"  최대 낙폭(MDD): {max_dd:>8.2f}% ({max_dd_date})")
    print(f"  Sharpe Ratio:  {sharpe:>9.2f}")
    print(f"  Sortino Ratio: {sortino:>9.2f}")

    print(f"\n📈 연도별 수익률")
    print(f"  {'연도':>6} {'수익률':>10} {'최대 낙폭':>10} {'연말 자산':>14}")
    print(f"  {'─'*6} {'─'*10} {'─'*10} {'─'*14}")
    for yr in yearly:
        print(f"  {yr['year']:>6} {yr['return']:>9.2f}% {yr['max_dd']:>9.2f}% ${yr['end_equity']:>12,.2f}")

    print(f"\n🔄 매매 통계")
    print(f"  총 매수: {buy_trades:>6}회")
    print(f"  총 매도: {sell_trades:>6}회 (트레일링 스탑)")
    print(f"  총 투자금: ${total_invested:>12,.2f}")

    if not trades_df.empty:
        for sym in engine.symbols:
            sym_trades: pd.DataFrame = trades_df[trades_df["symbol"] == sym]
            sym_buys: int = len(sym_trades[sym_trades["side"] == "BUY"])
            sym_sells: int = len(sym_trades[sym_trades["side"] == "SELL"])
            sym_invested: float = sym_trades[sym_trades["side"] == "BUY"]["amount"].sum()
            print(f"  {sym}: 매수 {sym_buys}회, 매도 {sym_sells}회, 투자 ${sym_invested:,.0f}")

    print(f"\n⚙️ 전략 모드 통계")
    print(f"  공격 모드: {aggressive_days}일 ({aggressive_days/len(eq)*100:.1f}%)")
    print(f"  방어 모드: {defensive_days}일 ({defensive_days/len(eq)*100:.1f}%)")

    if not trades_df.empty:
        dca_trades: pd.DataFrame = trades_df[trades_df["reason"].str.contains("DCA")]
        rsi_trades: pd.DataFrame = trades_df[trades_df["reason"].str.contains("RSI")]
        ts_trades: pd.DataFrame = trades_df[trades_df["reason"].str.contains("트레일링")]
        base_trades: pd.DataFrame = trades_df[trades_df["reason"].str.contains("기본적립")]
        print(f"\n📋 매수 사유별 통계")
        print(f"  기본적립:        {len(base_trades):>5}회  ${base_trades['amount'].sum():>12,.0f}")
        print(f"  DCA 물타기:      {len(dca_trades):>5}회  ${dca_trades['amount'].sum():>12,.0f}")
        print(f"  RSI 과매도 보너스: {len(rsi_trades):>5}회  ${rsi_trades['amount'].sum():>12,.0f}")
        print(f"  트레일링 스탑 매도: {len(ts_trades):>5}회  ${ts_trades['amount'].sum():>12,.0f}")

    # Buy & Hold comparison
    print(f"\n📊 Buy & Hold 비교 (동일 초기 비중)")
    bh_value: float = 0.0
    for sym in engine.symbols:
        if sym in data_cache:
            df: pd.DataFrame = data_cache[sym]
            start_price: float = df["Close"].iloc[0]
            end_price: float = df["Close"].iloc[-1]
            init_alloc: float = INITIAL_CAPITAL * 0.15
            remaining: float = INITIAL_CAPITAL * (1 - 0.15 * len(engine.symbols))
            shares: float = init_alloc / start_price
            sym_value: float = shares * end_price
            bh_return: float = (end_price / start_price - 1) * 100
            print(f"  {sym}: ${init_alloc:,.0f} → ${sym_value:,.0f} ({bh_return:+.1f}%)")
            bh_value += sym_value
    bh_value += INITIAL_CAPITAL * (1 - 0.15 * len(engine.symbols))
    bh_total_return: float = (bh_value / INITIAL_CAPITAL - 1) * 100
    print(f"  총 B&H: ${bh_value:,.0f} ({bh_total_return:+.1f}%) vs 전략: ${final_equity:,.0f} ({total_return:+.1f}%)")
    diff: float = total_return - bh_total_return
    print(f"  전략 초과 수익: {diff:+.1f}%p")


data_cache: Dict[str, pd.DataFrame] = {}


if __name__ == "__main__":
    symbols: List[str] = ["NVDL", "TSLL", "TQQQ"]

    print("데이터 다운로드 중...")
    raw_data: Dict[str, pd.DataFrame] = download_data(symbols, years=5)
    data_cache.update(raw_data)

    engine: BacktestEngine = run_backtest(symbols, years=5)
    print_results(engine)
