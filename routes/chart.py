import json
import os
import time
from datetime import datetime
from typing import Any, Callable, Dict, Optional
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends

from bot import TradingBot


def create_chart_router(
    auth_dependency: Callable[..., str],
    get_bot: Callable[[], Optional[TradingBot]],
) -> APIRouter:
    router = APIRouter()
    chart_cache: Dict[str, Any] = {}
    quote_cache: Dict[str, Any] = {}
    trade_cache: Dict[str, Any] = {"mtime": 0.0, "items": []}

    valid_sessions = {"all", "daytime", "pre", "regular", "after"}
    tz_et = ZoneInfo("America/New_York")

    def classify_us_session(ts_epoch: int) -> str:
        dt_et = datetime.fromtimestamp(ts_epoch, tz=tz_et)
        minute_of_day = dt_et.hour * 60 + dt_et.minute
        if 4 * 60 <= minute_of_day < (9 * 60 + 30):
            return "pre"
        if (9 * 60 + 30) <= minute_of_day < 16 * 60:
            return "regular"
        if 16 * 60 <= minute_of_day < 20 * 60:
            return "after"
        return "off"

    def resolve_live_session(bot: Optional[TradingBot]) -> str:
        if not bot:
            return "off"
        now_kst = bot.get_korean_time()
        if bot.is_daytime_market_open(now_kst):
            return "daytime"
        now_et = bot.get_eastern_time()
        if not bot.is_active_trading_time(now_et):
            return "off"
        minute_of_day = now_et.hour * 60 + now_et.minute
        if 4 * 60 <= minute_of_day < (9 * 60 + 30):
            return "pre"
        if (9 * 60 + 30) <= minute_of_day < 16 * 60:
            return "regular"
        if 16 * 60 <= minute_of_day < 20 * 60:
            return "after"
        return "off"

    def get_trade_entries() -> list:
        trade_file = "trade_log.json"
        if not os.path.exists(trade_file):
            trade_cache["mtime"] = 0.0
            trade_cache["items"] = []
            return []

        try:
            mtime = os.path.getmtime(trade_file)
        except OSError:
            return trade_cache.get("items", [])
        if trade_cache["items"] and trade_cache["mtime"] == mtime:
            return trade_cache["items"]

        try:
            with open(trade_file, "r", encoding="utf-8") as file:
                raw = json.load(file)
        except Exception:
            return trade_cache.get("items", [])

        normalized = []
        for trade in raw if isinstance(raw, list) else []:
            side = str(trade.get("side", ""))
            if side not in ("매수", "매도"):
                continue
            normalized.append(
                {
                    "symbol": str(trade.get("symbol", "")),
                    "time": trade.get("timestamp", ""),
                    "side": side,
                    "price": trade.get("price", 0),
                    "qty": trade.get("qty", 0),
                }
            )
        trade_cache["mtime"] = mtime
        trade_cache["items"] = normalized
        return normalized

    @router.get("/api/chart-data")
    async def get_chart_data(
        symbol: str = "",
        period: str = "5d",
        interval: str = "5m",
        session: str = "all",
        username: str = Depends(auth_dependency),
    ) -> Dict[str, Any]:
        import pandas as pd
        import yfinance as yf

        bot = get_bot()
        if not symbol and bot and bot.symbols:
            symbol = bot.symbols[0]
        if not symbol:
            return {"candles": [], "symbol": "", "trades": []}

        session_filter = session.strip().lower()
        if session_filter not in valid_sessions:
            session_filter = "all"

        cache_key: str = f"{symbol}_{period}_{interval}_{session_filter}"
        now: float = time.time()
        ttl: float = 15.0 if interval == "1m" else (60.0 if interval == "5m" else 300.0)
        cached = chart_cache.get(cache_key)
        if cached and (now - cached["ts"]) < ttl:
            return cached["data"]

        try:
            candles: list = []
            us_candles: list = []
            daytime_candles: list = []
            source: str = "yfinance"
            is_intraday: bool = interval.endswith("m")
            session_applied: bool = is_intraday

            use_daytime_data = bool(bot and period == "1d" and is_intraday and session_filter in ("all", "daytime"))
            if use_daytime_data:
                try:
                    interval_min: int = int(interval[:-1])
                    daytime_candles = bot.api.get_intraday_candles(
                        symbol=symbol,
                        interval_min=interval_min,
                        nrec=120,
                        prefer_daytime=True,
                    )
                except Exception:
                    daytime_candles = []
                if daytime_candles:
                    for candle in daytime_candles:
                        candle["session"] = "daytime"

            use_yf_data = session_filter != "daytime"
            if use_yf_data:
                ticker = yf.Ticker(symbol)
                use_prepost: bool = interval not in ("1d", "1wk", "1mo")
                hist = ticker.history(period=period, interval=interval, prepost=use_prepost)
                if not hist.empty:
                    if is_intraday:
                        for ts, row in hist.iterrows():
                            ts_epoch = int(pd.Timestamp(ts).timestamp())
                            us_session = classify_us_session(ts_epoch)
                            if us_session == "off":
                                continue
                            if session_filter in ("pre", "regular", "after") and us_session != session_filter:
                                continue
                            us_candles.append(
                                {
                                    "time": ts_epoch,
                                    "open": round(float(row.get("Open", 0.0)), 2),
                                    "high": round(float(row.get("High", 0.0)), 2),
                                    "low": round(float(row.get("Low", 0.0)), 2),
                                    "close": round(float(row.get("Close", 0.0)), 2),
                                    "volume": int(float(row.get("Volume", 0) or 0)),
                                    "session": us_session,
                                }
                            )
                    else:
                        timestamps: pd.Series = hist.index.astype("int64") // 10**9
                        us_candles = (
                            pd.DataFrame(
                                {
                                    "time": timestamps,
                                    "open": hist["Open"].round(2),
                                    "high": hist["High"].round(2),
                                    "low": hist["Low"].round(2),
                                    "close": hist["Close"].round(2),
                                    "volume": hist["Volume"].astype(int),
                                    "session": "daily",
                                }
                            )
                            .to_dict("records")
                        )
                        session_applied = False

            if session_filter == "daytime":
                candles = daytime_candles
                source = "kis_daytime" if candles else "none"
            elif session_filter == "all" and is_intraday and daytime_candles:
                merged_by_time: Dict[int, Dict[str, Any]] = {}
                for item in us_candles:
                    merged_by_time[item["time"]] = item
                for item in daytime_candles:
                    merged_by_time[item["time"]] = item
                candles = [merged_by_time[key] for key in sorted(merged_by_time.keys())]
                source = "kis_daytime+yfinance"
            else:
                candles = us_candles
                source = "yfinance"

            trades = [trade for trade in get_trade_entries() if trade.get("symbol") == symbol]
            result: Dict[str, Any] = {
                "candles": candles,
                "symbol": symbol,
                "trades": trades,
                "source": source,
                "session": session_filter,
                "session_applied": session_applied,
            }
            chart_cache[cache_key] = {"data": result, "ts": now}
            return result
        except Exception as error:
            return {"candles": [], "symbol": symbol, "error": str(error)}

    @router.get("/api/chart-quote")
    async def get_chart_quote(
        symbol: str = "",
        username: str = Depends(auth_dependency),
    ) -> Dict[str, Any]:
        bot = get_bot()
        if not symbol and bot and bot.symbols:
            symbol = bot.symbols[0]
        if not symbol:
            return {"symbol": "", "price": 0.0, "session": "off"}

        session = resolve_live_session(bot)
        cache_key = f"{symbol}_{session}"
        now = time.time()
        ttl = 3.0
        cached = quote_cache.get(cache_key)
        if cached and (now - cached["ts"]) < ttl:
            return cached["data"]

        price = 0.0
        if bot:
            try:
                price = float(bot.api.get_current_price(symbol, prefer_daytime=(session == "daytime")))
            except Exception:
                price = 0.0
        result = {"symbol": symbol, "price": price, "session": session, "source": "kis_quote", "ts": int(now)}
        quote_cache[cache_key] = {"data": result, "ts": now}
        return result

    @router.get("/api/equity-history")
    async def get_equity_history(username: str = Depends(auth_dependency)) -> Dict[str, Any]:
        try:
            equity_file: str = "equity_log.json"
            if os.path.exists(equity_file):
                with open(equity_file, "r", encoding="utf-8") as file:
                    history = json.load(file)
                return {"history": history}
            return {"history": []}
        except Exception as error:
            return {"history": [], "error": str(error)}

    @router.get("/api/trade-history")
    async def get_trade_history(username: str = Depends(auth_dependency)) -> Dict[str, Any]:
        try:
            trade_file: str = "trade_log.json"
            if os.path.exists(trade_file):
                with open(trade_file, "r", encoding="utf-8") as file:
                    trades = json.load(file)
                trades.reverse()
                return {"trades": trades}
            return {"trades": []}
        except Exception as error:
            return {"trades": [], "error": str(error)}

    return router
