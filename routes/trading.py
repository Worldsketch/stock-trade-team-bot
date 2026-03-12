import threading
import time
from datetime import datetime
from typing import Any, Callable, Dict, Optional
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, Request

from bot import TradingBot


def create_trading_router(
    auth_dependency: Callable[..., str],
    get_bot: Callable[[], Optional[TradingBot]],
    invalidate_status_cache: Callable[[], None],
    monitor_sell_fill: Callable[[str, int, float, str], None],
) -> APIRouter:
    router = APIRouter()

    @router.post("/api/sell")
    async def manual_sell(request: Request, username: str = Depends(auth_dependency)) -> Dict[str, Any]:
        bot = get_bot()
        if not bot:
            return {"success": False, "message": "봇이 초기화되지 않았습니다."}

        try:
            body: Dict[str, Any] = await request.json()
            symbol: str = body.get("symbol", "")
            percent: int = int(body.get("percent", 0))
        except Exception:
            return {"success": False, "message": "잘못된 요청입니다."}

        if symbol not in bot.symbols:
            return {"success": False, "message": f"슬롯에 등록되지 않은 종목: {symbol}"}
        if percent not in (10, 25, 50, 100):
            return {"success": False, "message": f"잘못된 매도 비율: {percent}%"}

        try:
            data: Dict[str, Any] = bot.api.get_balance_and_positions(item_cd=symbol, symbols=bot.symbols)
            position = None
            for pos in data["positions"]:
                if pos["symbol"] == symbol and pos.get("quantity", 0) > 0:
                    position = pos
                    break

            if not position:
                return {"success": False, "message": f"{symbol} 보유 포지션이 없습니다."}

            total_qty: int = int(position["quantity"])
            sell_qty: int = max(1, int(total_qty * percent / 100))
            if percent == 100:
                sell_qty = total_qty

            current_price: float = position.get("current_price", 0.0)
            if current_price <= 0:
                current_price = bot.api.get_current_price(symbol)
            if current_price <= 0:
                return {"success": False, "message": f"{symbol} 현재가를 가져올 수 없습니다."}

            now_et = datetime.now(ZoneInfo("America/New_York"))
            is_regular: bool = (9 <= now_et.hour < 16) and (now_et.weekday() < 5)
            if is_regular:
                sell_price: float = round(current_price * 0.98, 2)
                order_desc: str = f"시장가 (하한 ${sell_price:.2f})"
            else:
                sell_price = round(current_price * 0.995, 2)
                order_desc = f"지정가 ${sell_price:.2f} (현재가 -0.5%)"

            success: bool = bot.api.place_order(symbol, sell_qty, sell_price, is_buy=False)
            if success:
                bot.manual_sell_block[symbol] = time.time()
                invalidate_status_cache()
                est_amount: float = sell_qty * sell_price
                krw_est: float = est_amount * bot.exchange_rate
                label: str = f"{percent}%"
                msg: str = (
                    f"📤 [수동 매도 주문 접수]\n종목: {symbol}\n수량: {sell_qty}주 ({label})\n"
                    f"{order_desc}\n예상 금액: ${est_amount:,.2f} (약 {krw_est:,.0f}원)"
                )
                bot.send_telegram_message(msg)
                bot.log(f"📤 [수동매도 접수] {symbol} {sell_qty}주 {order_desc} ({label})")
                manual_avg: float = position.get("avg_price", 0.0)
                bot._log_trade(
                    symbol,
                    "매도",
                    sell_qty,
                    sell_price,
                    est_amount,
                    f"[{bot._get_mode_label()}] 수동 매도 ({label})",
                    avg_price=manual_avg,
                )
                thread = threading.Thread(
                    target=monitor_sell_fill,
                    args=(symbol, sell_qty, sell_price, label),
                    daemon=True,
                )
                thread.start()
                return {
                    "success": True,
                    "message": f"{symbol} {sell_qty}주 매도 주문 완료",
                    "qty": sell_qty,
                    "price": sell_price,
                }
            return {"success": False, "message": f"{symbol} 매도 주문 실패"}
        except Exception as error:
            return {"success": False, "message": f"매도 처리 오류: {str(error)}"}

    @router.get("/api/pending-orders")
    async def get_pending_orders(username: str = Depends(auth_dependency)) -> Dict[str, Any]:
        bot = get_bot()
        if not bot:
            return {"orders": []}
        try:
            orders = bot.api.get_pending_orders(symbols=bot.symbols)
            return {"orders": orders}
        except Exception as error:
            return {"orders": [], "error": str(error)}

    @router.post("/api/cancel-order")
    async def cancel_order(request: Request, username: str = Depends(auth_dependency)) -> Dict[str, Any]:
        bot = get_bot()
        if not bot:
            return {"success": False, "message": "봇이 초기화되지 않았습니다."}
        try:
            body: Dict[str, Any] = await request.json()
            order_no: str = body.get("order_no", "")
            symbol: str = body.get("symbol", "")
            remaining_qty: int = int(body.get("remaining_qty", 0))
        except Exception:
            return {"success": False, "message": "잘못된 요청입니다."}

        if not order_no or not symbol:
            return {"success": False, "message": "주문번호와 종목이 필요합니다."}

        try:
            success: bool = bot.api.cancel_order(order_no, symbol, remaining_qty)
            if success:
                bot.log(f"🚫 [주문취소] {symbol} 주문번호 {order_no}")
                return {"success": True, "message": f"{symbol} 주문 취소 완료"}
            return {"success": False, "message": f"{symbol} 주문 취소 실패"}
        except Exception as error:
            return {"success": False, "message": f"취소 처리 오류: {str(error)}"}

    return router
