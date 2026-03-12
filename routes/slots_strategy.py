import time
from typing import Any, Callable, Dict, Optional

from fastapi import APIRouter, Depends, Request

from bot import TradingBot


def create_slots_strategy_router(
    auth_dependency: Callable[..., str],
    get_bot: Callable[[], Optional[TradingBot]],
    invalidate_status_cache: Callable[[], None],
) -> APIRouter:
    router = APIRouter()
    autocomplete_cache: Dict[str, Dict[str, Any]] = {}

    @router.get("/api/strategy-mode")
    async def get_strategy_mode(username: str = Depends(auth_dependency)) -> Dict[str, Any]:
        bot = get_bot()
        if not bot:
            return {"error": "봇이 초기화되지 않았습니다."}
        mode: str = bot.strategy_mode
        auto_active: str = bot.auto_active_mode
        active: str = auto_active if mode == "auto" else mode
        return {
            "mode": mode,
            "auto_active": auto_active,
            "active": active,
        }

    @router.post("/api/strategy-mode")
    async def set_strategy_mode(request: Request, username: str = Depends(auth_dependency)) -> Dict[str, Any]:
        bot = get_bot()
        if not bot:
            return {"error": "봇이 초기화되지 않았습니다."}
        body: Dict[str, Any] = await request.json()
        mode: str = body.get("mode", "")
        if bot.set_strategy_mode(mode):
            return {"status": "ok", "mode": mode, "auto_active": bot.auto_active_mode}
        return {"error": f"유효하지 않은 모드: {mode}"}

    @router.get("/api/slots")
    async def get_slots(username: str = Depends(auth_dependency)) -> Dict[str, Any]:
        bot = get_bot()
        if not bot:
            return {"slots": [], "max_slots": 6}
        return {
            "slots": bot.slot_manager.get_active_slots(),
            "max_slots": bot.slot_manager.max_slots,
            "current_count": len(bot.symbols),
        }

    @router.post("/api/slots/add")
    async def add_slot(request: Request, username: str = Depends(auth_dependency)) -> Dict[str, Any]:
        bot = get_bot()
        if not bot:
            return {"success": False, "message": "봇이 초기화되지 않았습니다."}
        try:
            body: Dict[str, Any] = await request.json()
            symbol: str = body.get("symbol", "").strip().upper()
        except Exception:
            return {"success": False, "message": "잘못된 요청입니다."}
        if not symbol:
            return {"success": False, "message": "종목 코드를 입력해주세요."}
        buy_percent: float = float(body.get("buy_percent", 0))
        result: Dict[str, Any] = bot.add_symbol(symbol, buy_percent=buy_percent)
        if result.get("success"):
            invalidate_status_cache()
        return result

    @router.post("/api/slots/remove")
    async def remove_slot(request: Request, username: str = Depends(auth_dependency)) -> Dict[str, Any]:
        bot = get_bot()
        if not bot:
            return {"success": False, "message": "봇이 초기화되지 않았습니다."}
        try:
            body: Dict[str, Any] = await request.json()
            symbol: str = body.get("symbol", "").strip().upper()
            sell_all: bool = bool(body.get("sell_all", True))
        except Exception:
            return {"success": False, "message": "잘못된 요청입니다."}
        if not symbol:
            return {"success": False, "message": "종목 코드를 입력해주세요."}
        result: Dict[str, Any] = bot.remove_symbol(symbol, sell_all=sell_all)
        if result.get("success"):
            invalidate_status_cache()
        return result

    @router.get("/api/search-ticker")
    async def search_ticker(symbol: str = "", username: str = Depends(auth_dependency)) -> Dict[str, Any]:
        bot = get_bot()
        if not bot:
            return {"found": False, "message": "봇이 초기화되지 않았습니다."}
        if not symbol.strip():
            return {"found": False, "message": "종목 코드를 입력해주세요."}
        return bot.search_ticker(symbol)

    @router.get("/api/autocomplete")
    async def autocomplete_ticker(q: str = "", username: str = Depends(auth_dependency)) -> Dict[str, Any]:
        import yfinance as yf

        query: str = q.strip().upper()
        if len(query) < 1:
            return {"results": []}

        now: float = time.time()
        cached = autocomplete_cache.get(query)
        if cached and (now - cached["ts"]) < 600:
            return cached["data"]

        try:
            search = yf.Search(query, max_results=8)
            results: list = []
            for item in search.quotes:
                exchange: str = item.get("exchange", "")
                quote_type: str = item.get("quoteType", "")
                if quote_type not in ("EQUITY", "ETF"):
                    continue
                if any(x in exchange for x in ("PNK", "OTC")):
                    continue
                results.append({
                    "symbol": item.get("symbol", ""),
                    "name": item.get("shortname", item.get("longname", "")),
                    "exchange": exchange,
                    "type": quote_type,
                })
            data: Dict[str, Any] = {"results": results[:8]}
            autocomplete_cache[query] = {"data": data, "ts": now}
            return data
        except Exception:
            return {"results": []}

    return router
