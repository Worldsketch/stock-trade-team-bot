import time
from typing import Any, Callable, Dict, Optional

from fastapi import APIRouter, Depends

from bot import TradingBot
from services.price_cache import BasePriceCache
from services.trade_metrics import RealizedPnlCalculator


def create_status_router(
    auth_dependency: Callable[..., str],
    get_bot: Callable[[], Optional[TradingBot]],
    status_cache: Dict[str, Any],
    base_price_cache: BasePriceCache,
    realized_pnl: RealizedPnlCalculator,
) -> APIRouter:
    router = APIRouter()

    def fetch_base_price(base_symbol: str) -> float:
        bot = get_bot()
        if not bot:
            return 0.0
        return bot.api.get_current_price(base_symbol)

    @router.get("/api/status")
    async def get_status(username: str = Depends(auth_dependency)) -> Dict[str, Any]:
        bot = get_bot()
        if not bot:
            return {"error": "Bot is not initialized."}

        now: float = time.time()
        if status_cache["data"] and (now - status_cache["ts"]) < 5:
            return status_cache["data"]

        try:
            item_code: str = bot.symbols[0] if bot.symbols else "AAPL"
            data: Dict[str, Any] = bot.api.get_balance_and_positions(item_cd=item_code, symbols=bot.symbols)
            is_daytime_session: bool = bot.is_daytime_market_open(bot.get_korean_time())
            current_symbols: list = bot.symbols
            positions_list: list = []
            held_symbols: set = set()

            active_slots = bot.slot_manager.get_active_slots()
            sorted_positions = sorted(
                data.get("positions", []),
                key=lambda p: (
                    float(p.get("quantity", 0.0)),
                    float(p.get("evlu_amt", 0.0)),
                    float(p.get("current_price", 0.0)),
                ),
                reverse=True,
            )
            for pos in sorted_positions:
                symbol: str = pos["symbol"]
                if symbol not in current_symbols:
                    continue
                if symbol in held_symbols:
                    continue
                held_symbols.add(symbol)
                current_price: float = pos.get("current_price", 0.0)
                if is_daytime_session:
                    try:
                        current_price = bot.api.get_current_price(symbol, prefer_daytime=True)
                    except Exception:
                        pass
                elif current_price <= 0:
                    try:
                        current_price = bot.api.get_current_price(symbol)
                    except Exception:
                        pass
                slot_info = next((slot for slot in active_slots if slot["symbol"] == symbol), {})
                base_symbol: str = slot_info.get("base_asset", symbol)
                base_price: float = (
                    base_price_cache.get_price(base_symbol, fetch_base_price)
                    if base_symbol != symbol
                    else 0.0
                )
                positions_list.append(
                    {
                        "symbol": symbol,
                        "quantity": pos.get("quantity", 0.0),
                        "avg_price": pos.get("avg_price", 0.0),
                        "current_price": current_price,
                        "evlu_amt": pos.get("evlu_amt", 0.0),
                        "evlu_pfls": pos.get("evlu_pfls", 0.0),
                        "return_rate": pos.get("return_rate", 0.0),
                        "pchs_amt": pos.get("pchs_amt", 0.0),
                        "is_leveraged": slot_info.get("is_leveraged", False),
                        "base_asset": base_symbol,
                        "base_price": base_price,
                    }
                )

            for symbol in current_symbols:
                if symbol in held_symbols:
                    continue
                slot_info = next((slot for slot in active_slots if slot["symbol"] == symbol), {})
                fallback_price: float = 0.0
                try:
                    fallback_price = bot.api.get_current_price(symbol, prefer_daytime=is_daytime_session)
                except Exception:
                    pass
                base_symbol = slot_info.get("base_asset", symbol)
                fallback_base_price: float = (
                    base_price_cache.get_price(base_symbol, fetch_base_price)
                    if base_symbol != symbol
                    else 0.0
                )
                positions_list.append(
                    {
                        "symbol": symbol,
                        "quantity": 0.0,
                        "avg_price": 0.0,
                        "current_price": fallback_price,
                        "return_rate": 0.0,
                        "is_leveraged": slot_info.get("is_leveraged", False),
                        "base_asset": base_symbol,
                        "base_price": fallback_base_price,
                    }
                )

            slot_order: Dict[str, int] = {slot["symbol"]: idx for idx, slot in enumerate(active_slots)}
            positions_list.sort(key=lambda position: slot_order.get(position["symbol"], 999))

            api_exchange_rate: float = data.get("exchange_rate", 0.0)
            if api_exchange_rate > 0:
                bot.exchange_rate = api_exchange_rate
            elif not bot.is_running:
                bot.update_exchange_rate()

            daily_pnl_usd: float = 0.0
            for position in positions_list:
                symbol = position["symbol"]
                quantity = position.get("quantity", 0.0)
                current_price = position.get("current_price", 0.0)
                previous_close = bot.prev_close.get(symbol, 0.0)
                if quantity > 0 and previous_close > 0 and current_price > 0:
                    daily_pnl_usd += (current_price - previous_close) * quantity

            result: Dict[str, Any] = {
                "is_running": bot.is_running,
                "usd_balance": data["usd_balance"],
                "krw_balance": data.get("krw_balance", 0.0),
                "krw_cash": data.get("krw_cash", 0.0),
                "exchange_rate": bot.exchange_rate,
                "positions": positions_list,
                "logs": bot.logs,
                "tot_evlu_pfls": data.get("tot_evlu_pfls", 0.0),
                "tot_pchs_amt": data.get("tot_pchs_amt", 0.0),
                "tot_stck_evlu": data.get("tot_stck_evlu", 0.0),
                "total_eval": data["usd_balance"] + data.get("tot_stck_evlu", 0.0),
                "daily_pnl_usd": round(daily_pnl_usd, 2),
                "strategy_mode": bot.strategy_mode,
                "auto_active": bot.auto_active_mode,
                "realized_pnl": realized_pnl.calculate(),
                "slots": active_slots,
                "max_slots": bot.slot_manager.max_slots,
                "market_open": bot.is_active_trading_time(bot.get_eastern_time()),
                "daytime_open": bot.is_daytime_market_open(bot.get_korean_time()),
                "is_dst": bool(bot.get_eastern_time().dst()),
                "et_time": bot.get_eastern_time().strftime("%H:%M"),
                "kst_time": bot.get_korean_time().strftime("%H:%M"),
            }
        except Exception as error:
            try:
                result = bot.get_status()
            except Exception:
                result = {"error": f"상태 조회 실패: {error}", "is_running": False, "positions": []}

        status_cache["data"] = result
        status_cache["ts"] = now
        return result

    return router
