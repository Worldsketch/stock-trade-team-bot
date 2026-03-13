import json
import os
import time
from datetime import datetime
from typing import Any, Callable, Dict, Optional
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends

from bot import TradingBot
from services.live_data_cache import LiveDataCache


def create_chart_router(
    auth_dependency: Callable[..., str],
    get_bot: Callable[[], Optional[TradingBot]],
    live_data_cache: Optional[LiveDataCache] = None,
) -> APIRouter:
    router = APIRouter()
    chart_cache: Dict[str, Any] = {}
    quote_cache: Dict[str, Any] = {}
    trade_cache: Dict[str, Any] = {"mtime": 0.0, "items": []}

    valid_sessions = {"all", "daytime", "pre", "regular", "after"}
    tz_et = ZoneInfo("America/New_York")

    def estimate_intraday_nrec(period: str, interval_min: int) -> int:
        if interval_min <= 0:
            return 120
        period_map = {
            "1d": 24 * 60,
            "5d": 5 * 24 * 60,
            "1mo": 30 * 24 * 60,
        }
        total_minutes = period_map.get(period, 24 * 60)
        return max(1, min(int(total_minutes / interval_min) + 20, 120))

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
    def get_chart_data(
        symbol: str = "",
        period: str = "5d",
        interval: str = "5m",
        session: str = "all",
        username: str = Depends(auth_dependency),
    ) -> Dict[str, Any]:
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
            source: str = "none"
            is_intraday: bool = interval.endswith("m")
            session_applied: bool = is_intraday

            if bot and is_intraday:
                try:
                    interval_min: int = int(interval[:-1])
                    live_session = resolve_live_session(bot)
                    prefer_daytime = session_filter == "daytime" or (session_filter == "all" and live_session == "daytime")
                    kis_candles = bot.api.get_intraday_candles(
                        symbol=symbol,
                        interval_min=interval_min,
                        nrec=estimate_intraday_nrec(period, interval_min),
                        prefer_daytime=prefer_daytime,
                    )
                    normalized: list = []
                    for candle in kis_candles:
                        ts_epoch = int(candle.get("time", 0) or 0)
                        if ts_epoch <= 0:
                            continue
                        if prefer_daytime:
                            candle_session = "daytime"
                        else:
                            candle_session = classify_us_session(ts_epoch)
                            if candle_session == "off":
                                continue
                        if session_filter in ("pre", "regular", "after") and candle_session != session_filter:
                            continue
                        normalized.append(
                            {
                                "time": ts_epoch,
                                "open": round(float(candle.get("open", 0.0)), 2),
                                "high": round(float(candle.get("high", 0.0)), 2),
                                "low": round(float(candle.get("low", 0.0)), 2),
                                "close": round(float(candle.get("close", 0.0)), 2),
                                "volume": int(float(candle.get("volume", 0) or 0)),
                                "session": candle_session,
                            }
                        )
                    candles = normalized
                    source = "kis_daytime" if prefer_daytime else "kis_intraday"
                except Exception:
                    candles = []
                    source = "none"

            if bot and not is_intraday and not candles:
                try:
                    daily = bot.api.get_daily_candles(symbol=symbol, period=period)
                    candles = [
                        {
                            "time": int(c.get("time", 0)),
                            "open": round(float(c.get("open", 0.0)), 2),
                            "high": round(float(c.get("high", 0.0)), 2),
                            "low": round(float(c.get("low", 0.0)), 2),
                            "close": round(float(c.get("close", 0.0)), 2),
                            "volume": int(float(c.get("volume", 0) or 0)),
                            "session": "daily",
                        }
                        for c in daily
                        if int(c.get("time", 0)) > 0
                    ]
                    session_applied = False
                    source = "kis_daily" if candles else "none"
                except Exception:
                    candles = []
                    source = "none"

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
    def get_chart_quote(
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
        source = "kis_quote"
        if bot:
            if live_data_cache:
                price = live_data_cache.get_price_from_portfolio(symbol, ttl_sec=3.0)
                if price > 0:
                    source = "shared_portfolio"
            if price <= 0:
                bot_snapshot = bot.get_live_snapshot(max_age_sec=12.0)
                if bot_snapshot:
                    for pos in bot_snapshot.get("positions", []) or []:
                        if str(pos.get("symbol", "")).upper() != symbol.upper():
                            continue
                        try:
                            snap_price = float(pos.get("current_price", 0.0) or 0.0)
                        except Exception:
                            snap_price = 0.0
                        if snap_price > 0:
                            price = snap_price
                            source = "bot_snapshot"
                            break
            try:
                if price <= 0:
                    price = float(bot.api.get_current_price(symbol, prefer_daytime=(session == "daytime")))
            except Exception:
                price = 0.0
        result = {"symbol": symbol, "price": price, "session": session, "source": source, "ts": int(now)}
        quote_cache[cache_key] = {"data": result, "ts": now}
        return result

    @router.get("/api/equity-history")
    def get_equity_history(username: str = Depends(auth_dependency)) -> Dict[str, Any]:
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
    def get_trade_history(username: str = Depends(auth_dependency)) -> Dict[str, Any]:
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
