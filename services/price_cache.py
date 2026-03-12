import time
from typing import Callable, Dict, Optional


class BasePriceCache:
    def __init__(self, ttl_seconds: float = 10.0) -> None:
        self.ttl_seconds: float = ttl_seconds
        self._cache: Dict[str, Dict[str, float]] = {}

    def get_price(self, symbol: str, price_fetcher: Callable[[str], float]) -> float:
        now: float = time.time()
        cached: Optional[Dict[str, float]] = self._cache.get(symbol)
        if cached and (now - cached["ts"]) < self.ttl_seconds:
            return cached["price"]

        price: float = 0.0
        try:
            price = price_fetcher(symbol)
        except Exception:
            price = 0.0
        self._cache[symbol] = {"price": price, "ts": now}
        return price
