import time
import threading
from typing import Any, Dict, List, Optional


class LiveDataCache:
    """KIS 조회 결과를 짧게 공유해 중복 호출을 줄이기 위한 런타임 캐시."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._portfolio: Dict[str, Any] = {"ts": 0.0, "data": None}
        self._pending: Dict[str, Any] = {"ts": 0.0, "data": None}

    def get_portfolio(self, ttl_sec: float) -> Optional[Dict[str, Any]]:
        now = time.time()
        with self._lock:
            ts = float(self._portfolio.get("ts", 0.0))
            data = self._portfolio.get("data")
            if data is None:
                return None
            if (now - ts) > ttl_sec:
                return None
            return data

    def set_portfolio(self, data: Dict[str, Any]) -> None:
        with self._lock:
            self._portfolio = {"ts": time.time(), "data": data}

    def invalidate_portfolio(self) -> None:
        with self._lock:
            self._portfolio = {"ts": 0.0, "data": None}

    def get_pending(self, ttl_sec: float) -> Optional[List[Dict[str, Any]]]:
        now = time.time()
        with self._lock:
            ts = float(self._pending.get("ts", 0.0))
            data = self._pending.get("data")
            if data is None:
                return None
            if (now - ts) > ttl_sec:
                return None
            return data

    def set_pending(self, orders: List[Dict[str, Any]]) -> None:
        with self._lock:
            self._pending = {"ts": time.time(), "data": orders}

    def invalidate_pending(self) -> None:
        with self._lock:
            self._pending = {"ts": 0.0, "data": None}

    def get_price_from_portfolio(self, symbol: str, ttl_sec: float = 3.0) -> float:
        snapshot = self.get_portfolio(ttl_sec=ttl_sec)
        if not snapshot:
            return 0.0
        for pos in snapshot.get("positions", []) or []:
            if str(pos.get("symbol", "")).upper() != symbol.upper():
                continue
            try:
                price = float(pos.get("current_price", 0.0) or 0.0)
            except Exception:
                price = 0.0
            if price > 0:
                return price
        return 0.0

