import json
import os
import time
from typing import Any, Callable, Dict, Optional

from fastapi import APIRouter, Depends

from bot import TradingBot


def create_chart_router(
    auth_dependency: Callable[..., str],
    get_bot: Callable[[], Optional[TradingBot]],
) -> APIRouter:
    router = APIRouter()
    chart_cache: Dict[str, Any] = {}

    @router.get("/api/chart-data")
    async def get_chart_data(
        symbol: str = "",
        period: str = "5d",
        interval: str = "5m",
        username: str = Depends(auth_dependency),
    ) -> Dict[str, Any]:
        import pandas as pd
        import yfinance as yf

        bot = get_bot()
        if not symbol and bot and bot.symbols:
            symbol = bot.symbols[0]
        if not symbol:
            return {"candles": [], "symbol": "", "trades": []}

        cache_key: str = f"{symbol}_{period}_{interval}"
        now: float = time.time()
        ttl: float = 30.0 if interval in ("1m", "5m") else 300.0
        cached = chart_cache.get(cache_key)
        if cached and (now - cached["ts"]) < ttl:
            return cached["data"]

        try:
            ticker = yf.Ticker(symbol)
            use_prepost: bool = interval not in ("1d", "1wk", "1mo")
            hist = ticker.history(period=period, interval=interval, prepost=use_prepost)
            if hist.empty:
                return {"candles": [], "symbol": symbol}

            timestamps: pd.Series = hist.index.astype("int64") // 10**9
            candles: list = (
                pd.DataFrame(
                    {
                        "time": timestamps,
                        "open": hist["Open"].round(2),
                        "high": hist["High"].round(2),
                        "low": hist["Low"].round(2),
                        "close": hist["Close"].round(2),
                        "volume": hist["Volume"].astype(int),
                    }
                )
                .to_dict("records")
            )

            trades: list = []
            trade_file: str = "trade_log.json"
            if os.path.exists(trade_file):
                with open(trade_file, "r", encoding="utf-8") as file:
                    all_trades = json.load(file)
                for trade in all_trades:
                    if trade.get("symbol") == symbol:
                        trades.append(
                            {
                                "time": trade.get("timestamp", ""),
                                "side": trade.get("side", ""),
                                "price": trade.get("price", 0),
                                "qty": trade.get("qty", 0),
                            }
                        )

            result: Dict[str, Any] = {"candles": candles, "symbol": symbol, "trades": trades}
            chart_cache[cache_key] = {"data": result, "ts": now}
            return result
        except Exception as error:
            return {"candles": [], "symbol": symbol, "error": str(error)}

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
