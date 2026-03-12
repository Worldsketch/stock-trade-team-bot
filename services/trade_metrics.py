import json
import os
import time
from typing import Any, Dict, List


def migrate_trade_pnl(trade_file: str = "trade_log.json") -> None:
    """기존 매도 기록의 pnl 필드를 평균단가 기반으로 소급 계산합니다."""
    if not os.path.exists(trade_file):
        return
    try:
        with open(trade_file, "r", encoding="utf-8") as file:
            trades: List[Dict[str, Any]] = json.load(file)
    except Exception:
        return

    needs_update: bool = any(trade.get("side") == "매도" and "pnl" not in trade for trade in trades)
    if not needs_update:
        return

    holdings: Dict[str, Dict[str, float]] = {}
    for trade in trades:
        symbol: str = trade.get("symbol", "")
        side: str = trade.get("side", "")
        quantity: float = float(trade.get("qty", 0))
        price: float = float(trade.get("price", 0))
        if quantity <= 0 or price <= 0:
            continue

        if symbol not in holdings:
            holdings[symbol] = {"qty": 0.0, "avg_cost": 0.0}
        holding = holdings[symbol]

        if side == "매수":
            total_cost: float = holding["qty"] * holding["avg_cost"] + quantity * price
            holding["qty"] += quantity
            holding["avg_cost"] = total_cost / holding["qty"] if holding["qty"] > 0 else 0.0
        elif side == "매도" and "pnl" not in trade:
            if holding["qty"] > 0 and holding["avg_cost"] > 0:
                avg_cost: float = holding["avg_cost"]
                pnl: float = quantity * (price - avg_cost)
                pnl_pct: float = (price - avg_cost) / avg_cost * 100
                trade["avg_price"] = round(avg_cost, 2)
                trade["pnl"] = round(pnl, 2)
                trade["pnl_pct"] = round(pnl_pct, 2)
                holding["qty"] = max(0.0, holding["qty"] - quantity)

    try:
        with open(trade_file, "w", encoding="utf-8") as file:
            json.dump(trades, file, indent=2, ensure_ascii=False)
        print("[마이그레이션] 기존 매도 내역에 수익/손실 정보를 추가했습니다.")
    except Exception as error:
        print(f"[마이그레이션 오류] {error}")


class RealizedPnlCalculator:
    def __init__(self, cache_ttl_seconds: float = 60.0, trade_file: str = "trade_log.json") -> None:
        self.cache_ttl_seconds: float = cache_ttl_seconds
        self.trade_file: str = trade_file
        self._cache: Dict[str, Any] = {"data": None, "ts": 0.0}

    def calculate(self) -> Dict[str, Any]:
        now: float = time.time()
        if self._cache["data"] and (now - self._cache["ts"]) < self.cache_ttl_seconds:
            return self._cache["data"]

        if not os.path.exists(self.trade_file):
            return {"total": 0.0, "count": 0, "wins": 0, "losses": 0}

        try:
            with open(self.trade_file, "r", encoding="utf-8") as file:
                trades: List[Dict[str, Any]] = json.load(file)
        except Exception:
            return {"total": 0.0, "count": 0, "wins": 0, "losses": 0}

        holdings: Dict[str, Dict[str, float]] = {}
        total_pnl: float = 0.0
        sell_count: int = 0
        win_count: int = 0
        loss_count: int = 0

        for trade in trades:
            symbol: str = trade.get("symbol", "")
            side: str = trade.get("side", "")
            quantity: float = float(trade.get("qty", 0))
            price: float = float(trade.get("price", 0))
            if quantity <= 0 or price <= 0:
                continue

            if symbol not in holdings:
                holdings[symbol] = {"qty": 0.0, "avg_cost": 0.0}
            holding = holdings[symbol]

            if side == "매수":
                total_cost: float = holding["qty"] * holding["avg_cost"] + quantity * price
                holding["qty"] += quantity
                holding["avg_cost"] = total_cost / holding["qty"] if holding["qty"] > 0 else 0.0
            elif side == "매도":
                if holding["qty"] > 0 and holding["avg_cost"] > 0:
                    pnl: float = quantity * (price - holding["avg_cost"])
                    total_pnl += pnl
                    sell_count += 1
                    if pnl >= 0:
                        win_count += 1
                    else:
                        loss_count += 1
                    holding["qty"] = max(0.0, holding["qty"] - quantity)

        result: Dict[str, Any] = {
            "total": round(total_pnl, 2),
            "count": sell_count,
            "wins": win_count,
            "losses": loss_count,
        }
        self._cache = {"data": result, "ts": now}
        return result
