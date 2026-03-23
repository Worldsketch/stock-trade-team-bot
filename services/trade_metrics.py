import json
import os
import time
from typing import Any, Dict, List, Optional, Tuple


def _parse_env_rate(name: str, default: float = 0.0) -> float:
    try:
        value = float(str(os.getenv(name, default)).strip())
        return max(0.0, value)
    except Exception:
        return max(0.0, default)


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
            buy_status: str = str(trade.get("status", "filled")).lower()
            if buy_status in ("pending", "cancelled", "partially_cancelled", "unfilled"):
                continue
            total_cost: float = holding["qty"] * holding["avg_cost"] + quantity * price
            holding["qty"] += quantity
            holding["avg_cost"] = total_cost / holding["qty"] if holding["qty"] > 0 else 0.0
        elif side == "매도" and "pnl" not in trade:
            status: str = str(trade.get("status", "filled")).lower()
            if status in ("pending", "cancelled", "unfilled"):
                continue
            avg_price_from_trade: float = float(trade.get("avg_price", 0.0) or 0.0)
            avg_cost: float = 0.0
            if avg_price_from_trade > 0:
                avg_cost = avg_price_from_trade
            elif holding["qty"] > 0 and holding["avg_cost"] > 0:
                avg_cost = holding["avg_cost"]

            if avg_cost > 0:
                sell_cost_rate: float = float(trade.get("sell_cost_rate", _parse_env_rate("SELL_FEE_RATE", 0.0025) + _parse_env_rate("SELL_TAX_RATE", 0.0)) or 0.0)
                sell_cost: float = float(trade.get("sell_cost", quantity * price * sell_cost_rate) or 0.0)
                pnl: float = quantity * (price - avg_cost) - sell_cost
                pnl_pct: float = (pnl / (avg_cost * quantity) * 100) if avg_cost > 0 and quantity > 0 else 0.0
                trade["avg_price"] = round(avg_cost, 2)
                trade["sell_cost"] = round(sell_cost, 2)
                trade["pnl"] = round(pnl, 2)
                trade["pnl_pct"] = round(pnl_pct, 2)

            if holding["qty"] > 0:
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
        self._cache: Dict[str, Any] = {"data": None, "ts": 0.0, "file_token": None}

    def _get_file_token(self) -> Optional[Tuple[int, int]]:
        if not os.path.exists(self.trade_file):
            return None
        try:
            stat = os.stat(self.trade_file)
            return (int(stat.st_mtime_ns), int(stat.st_size))
        except Exception:
            return None

    def calculate(self) -> Dict[str, Any]:
        now: float = time.time()
        file_token: Optional[Tuple[int, int]] = self._get_file_token()
        if (
            self._cache["data"]
            and (now - self._cache["ts"]) < self.cache_ttl_seconds
            and self._cache.get("file_token") == file_token
        ):
            return self._cache["data"]

        if file_token is None:
            result = {"total": 0.0, "count": 0, "wins": 0, "losses": 0}
            self._cache = {"data": result, "ts": now, "file_token": file_token}
            return result

        try:
            with open(self.trade_file, "r", encoding="utf-8") as file:
                trades: List[Dict[str, Any]] = json.load(file)
        except Exception:
            result = {"total": 0.0, "count": 0, "wins": 0, "losses": 0}
            self._cache = {"data": result, "ts": now, "file_token": file_token}
            return result

        holdings: Dict[str, Dict[str, float]] = {}
        total_pnl: float = 0.0
        sell_count: int = 0
        win_count: int = 0
        loss_count: int = 0
        default_sell_cost_rate: float = _parse_env_rate("SELL_FEE_RATE", 0.0025) + _parse_env_rate("SELL_TAX_RATE", 0.0)

        for trade in trades:
            symbol: str = trade.get("symbol", "")
            side: str = trade.get("side", "")
            status: str = str(trade.get("status", "filled")).lower()
            quantity: float = float(trade.get("qty", 0) or 0)
            price: float = float(trade.get("price", 0) or 0)
            if quantity <= 0 or price <= 0:
                continue

            if symbol not in holdings:
                holdings[symbol] = {"qty": 0.0, "avg_cost": 0.0}
            holding = holdings[symbol]

            if side == "매수":
                if status in ("pending", "cancelled", "partially_cancelled", "unfilled"):
                    continue
                total_cost: float = holding["qty"] * holding["avg_cost"] + quantity * price
                holding["qty"] += quantity
                holding["avg_cost"] = total_cost / holding["qty"] if holding["qty"] > 0 else 0.0
            elif side == "매도":
                if status in ("pending", "cancelled", "unfilled"):
                    continue
                sell_cost_rate: float = float(trade.get("sell_cost_rate", default_sell_cost_rate) or default_sell_cost_rate)
                sell_cost: float = float(trade.get("sell_cost", quantity * price * sell_cost_rate) or 0.0)

                pnl: Optional[float] = None
                raw_pnl = trade.get("pnl", None)
                if raw_pnl not in (None, ""):
                    try:
                        pnl = float(raw_pnl)
                    except Exception:
                        pnl = None

                avg_price_from_trade: float = float(trade.get("avg_price", 0.0) or 0.0)
                avg_cost: float = 0.0
                if avg_price_from_trade > 0:
                    avg_cost = avg_price_from_trade
                elif holding["qty"] > 0 and holding["avg_cost"] > 0:
                    avg_cost = holding["avg_cost"]

                if pnl is None and avg_cost > 0:
                    pnl = quantity * (price - avg_cost) - sell_cost
                if pnl is None:
                    continue

                total_pnl += pnl
                sell_count += 1
                if pnl >= 0:
                    win_count += 1
                else:
                    loss_count += 1

                if holding["qty"] > 0:
                    holding["qty"] = max(0.0, holding["qty"] - quantity)

        result: Dict[str, Any] = {
            "total": round(total_pnl, 2),
            "count": sell_count,
            "wins": win_count,
            "losses": loss_count,
        }
        self._cache = {"data": result, "ts": now, "file_token": file_token}
        return result
