import io
import json
import os
import time
import zipfile
import re
from typing import Any, Callable, Dict, List, Optional, Tuple

import requests

from fastapi import APIRouter, Depends, Request

from bot import LEVERAGED_ETF_MAP, TradingBot

SYMBOL_PATTERN = re.compile(r"^[A-Z0-9][A-Z0-9.\-]{0,14}$")


def _is_valid_symbol(symbol: str) -> bool:
    return bool(SYMBOL_PATTERN.fullmatch(str(symbol or "").strip().upper()))


def create_slots_strategy_router(
    auth_dependency: Callable[..., str],
    get_bot: Callable[[], Optional[TradingBot]],
    invalidate_status_cache: Callable[[], None],
) -> APIRouter:
    router = APIRouter()
    autocomplete_cache: Dict[str, Dict[str, Any]] = {}
    master_index_cache: Dict[str, Any] = {"items": [], "by_symbol": {}, "ts": 0.0, "last_attempt_ts": 0.0}
    master_cache_ttl_sec: float = 6 * 3600.0
    master_file_ttl_sec: float = 24 * 3600.0
    master_fetch_backoff_sec: float = 120.0
    master_file_path: str = "us_symbol_master.json"
    master_urls: Dict[str, str] = {
        "NASDAQ": "https://new.real.download.dws.co.kr/common/master/nasmst.cod.zip",
        "NYSE": "https://new.real.download.dws.co.kr/common/master/nysmst.cod.zip",
        "AMEX": "https://new.real.download.dws.co.kr/common/master/amsmst.cod.zip",
    }

    def _load_master_from_file(now_ts: float, allow_stale: bool = False) -> Tuple[List[Dict[str, str]], Dict[str, Dict[str, str]]]:
        if not os.path.exists(master_file_path):
            return [], {}
        try:
            with open(master_file_path, "r", encoding="utf-8") as f:
                payload = json.load(f)
            saved_ts = float(payload.get("updated_ts", 0.0))
            if (now_ts - saved_ts) > master_file_ttl_sec and not allow_stale:
                return [], {}
            items = payload.get("items", [])
            by_symbol = {str(item.get("symbol", "")).upper(): item for item in items if item.get("symbol")}
            return items, by_symbol
        except Exception:
            return [], {}

    def _save_master_to_file(items: List[Dict[str, str]], now_ts: float) -> None:
        try:
            payload = {"updated_ts": now_ts, "items": items}
            with open(master_file_path, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False)
        except Exception:
            pass

    def _parse_master_bytes(raw_bytes: bytes, exchange_name: str) -> List[Dict[str, str]]:
        text = raw_bytes.decode("cp949", errors="ignore")
        parsed: List[Dict[str, str]] = []
        for line in text.splitlines():
            if not line.strip():
                continue
            cols = line.split("\t")
            if len(cols) < 8:
                continue
            symbol = cols[4].strip().upper()
            if not symbol:
                continue
            name_kr = cols[6].strip() if len(cols) > 6 else ""
            name_en = cols[7].strip() if len(cols) > 7 else ""
            sec_type_code = cols[8].strip() if len(cols) > 8 else ""
            quote_type = "ETF" if (sec_type_code == "3" or "ETF" in name_en.upper()) else "EQUITY"
            parsed.append(
                {
                    "symbol": symbol,
                    "name": name_en or name_kr or symbol,
                    "exchange": exchange_name,
                    "type": quote_type,
                }
            )
        return parsed

    def _download_master_items() -> List[Dict[str, str]]:
        all_items: List[Dict[str, str]] = []
        for exchange_name, url in master_urls.items():
            response = requests.get(url, timeout=(2.0, 8.0))
            response.raise_for_status()
            with zipfile.ZipFile(io.BytesIO(response.content)) as zf:
                names = zf.namelist()
                if not names:
                    continue
                with zf.open(names[0]) as cod_file:
                    raw = cod_file.read()
                all_items.extend(_parse_master_bytes(raw, exchange_name))
        return all_items

    def _normalize_master_items(items: List[Dict[str, str]]) -> Tuple[List[Dict[str, str]], Dict[str, Dict[str, str]]]:
        order = {"NASDAQ": 0, "NYSE": 1, "AMEX": 2}
        dedup: Dict[str, Dict[str, str]] = {}
        for item in items:
            symbol = str(item.get("symbol", "")).upper()
            if not symbol:
                continue
            prev = dedup.get(symbol)
            if not prev:
                dedup[symbol] = item
                continue
            prev_order = order.get(prev.get("exchange", ""), 99)
            cur_order = order.get(item.get("exchange", ""), 99)
            if cur_order < prev_order:
                dedup[symbol] = item
        normalized = sorted(dedup.values(), key=lambda x: x.get("symbol", ""))
        by_symbol = {item["symbol"]: item for item in normalized}
        return normalized, by_symbol

    def _get_master_index(force_refresh: bool = False) -> Tuple[List[Dict[str, str]], Dict[str, Dict[str, str]]]:
        now_ts = time.time()
        if (
            not force_refresh
            and master_index_cache["items"]
            and (now_ts - float(master_index_cache.get("ts", 0.0))) < master_cache_ttl_sec
        ):
            return master_index_cache["items"], master_index_cache["by_symbol"]

        if not force_refresh:
            file_items, file_by_symbol = _load_master_from_file(now_ts)
            if file_items:
                master_index_cache["items"] = file_items
                master_index_cache["by_symbol"] = file_by_symbol
                master_index_cache["ts"] = now_ts
                return file_items, file_by_symbol

        if (now_ts - float(master_index_cache.get("last_attempt_ts", 0.0))) < master_fetch_backoff_sec:
            return master_index_cache["items"], master_index_cache["by_symbol"]

        try:
            master_index_cache["last_attempt_ts"] = now_ts
            downloaded = _download_master_items()
            normalized, by_symbol = _normalize_master_items(downloaded)
            if normalized:
                master_index_cache["items"] = normalized
                master_index_cache["by_symbol"] = by_symbol
                master_index_cache["ts"] = now_ts
                _save_master_to_file(normalized, now_ts)
                return normalized, by_symbol
        except Exception:
            pass

        file_items, file_by_symbol = _load_master_from_file(now_ts, allow_stale=True)
        master_index_cache["items"] = file_items
        master_index_cache["by_symbol"] = file_by_symbol
        master_index_cache["ts"] = now_ts
        return file_items, file_by_symbol

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
        if not _is_valid_symbol(symbol):
            return {"success": False, "message": "종목 코드는 영문/숫자/.- 만 허용됩니다. (최대 15자)"}
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
        if not _is_valid_symbol(symbol):
            return {"success": False, "message": "종목 코드 형식이 올바르지 않습니다."}
        result: Dict[str, Any] = bot.remove_symbol(symbol, sell_all=sell_all)
        if result.get("success"):
            invalidate_status_cache()
        return result

    @router.post("/api/slots/reorder")
    async def reorder_slots(request: Request, username: str = Depends(auth_dependency)) -> Dict[str, Any]:
        bot = get_bot()
        if not bot:
            return {"success": False, "message": "봇이 초기화되지 않았습니다."}
        try:
            body: Dict[str, Any] = await request.json()
            symbols: List[str] = body.get("symbols", []) or []
            if not isinstance(symbols, list):
                return {"success": False, "message": "잘못된 요청입니다. (symbols 배열 필요)"}
        except Exception:
            return {"success": False, "message": "잘못된 요청입니다."}

        current_symbols = set(bot.symbols)
        request_symbols = {str(s).upper() for s in symbols if str(s).strip()}
        if current_symbols != request_symbols:
            return {"success": False, "message": "요청 순서가 현재 슬롯 구성과 일치하지 않습니다. 새로고침 후 다시 시도해주세요."}

        ok: bool = bot.slot_manager.reorder_slots([str(s).upper() for s in symbols])
        if not ok:
            return {"success": False, "message": "슬롯 순서 저장에 실패했습니다."}

        invalidate_status_cache()
        return {
            "success": True,
            "message": "슬롯 순서가 저장되었습니다.",
            "slots": bot.slot_manager.get_active_slots(),
        }

    @router.get("/api/search-ticker")
    async def search_ticker(symbol: str = "", username: str = Depends(auth_dependency)) -> Dict[str, Any]:
        bot = get_bot()
        if not bot:
            return {"found": False, "message": "봇이 초기화되지 않았습니다."}
        query = symbol.strip().upper()
        if not query:
            return {"found": False, "message": "종목 코드를 입력해주세요."}
        if not _is_valid_symbol(query):
            return {"found": False, "message": "종목 코드 형식이 올바르지 않습니다."}
        _, by_symbol = _get_master_index()
        item = by_symbol.get(query)
        if not item:
            return {"found": False, "message": f"{query} 종목을 찾을 수 없습니다."}

        is_leveraged: bool = query in LEVERAGED_ETF_MAP
        base_asset: str = LEVERAGED_ETF_MAP.get(query, query)
        already_added: bool = bot.slot_manager.has_symbol(query)
        price: float = 0.0
        tradeable: bool = False
        try:
            now_kst = bot.get_korean_time()
            price = float(bot.api.get_current_price(query, prefer_daytime=bot.is_daytime_market_open(now_kst)) or 0.0)
            tradeable = price > 0
        except Exception:
            tradeable = False

        return {
            "found": True,
            "symbol": query,
            "name": item.get("name", query),
            "price": price,
            "is_leveraged": is_leveraged,
            "base_asset": base_asset,
            "tradeable": tradeable,
            "already_added": already_added,
            "currency": "USD",
            "exchange": item.get("exchange", ""),
        }

    @router.get("/api/autocomplete")
    async def autocomplete_ticker(q: str = "", username: str = Depends(auth_dependency)) -> Dict[str, Any]:
        query: str = q.strip().upper()
        if len(query) < 1:
            return {"results": []}

        now: float = time.time()
        cached = autocomplete_cache.get(query)
        if cached and (now - cached["ts"]) < 600:
            return cached["data"]

        items, _ = _get_master_index()
        if not items:
            return {"results": []}

        matched: List[Tuple[int, Dict[str, str]]] = []
        for item in items:
            symbol = str(item.get("symbol", "")).upper()
            name = str(item.get("name", ""))
            if symbol.startswith(query):
                matched.append((0, item))
                continue
            if query in name.upper():
                matched.append((1, item))

        matched.sort(key=lambda pair: (pair[0], len(pair[1].get("symbol", "")), pair[1].get("symbol", "")))
        results = [
            {
                "symbol": item.get("symbol", ""),
                "name": item.get("name", ""),
                "exchange": item.get("exchange", ""),
                "type": item.get("type", "EQUITY"),
            }
            for _, item in matched[:8]
        ]
        data: Dict[str, Any] = {"results": results}
        autocomplete_cache[query] = {"data": data, "ts": now}
        return data

    return router
