import json
import os
from typing import Any, Callable, Dict

from fastapi import APIRouter, Depends


def create_ai_router(
    auth_dependency: Callable[..., str],
    generate_ai_report: Callable[[], Dict[str, Any]],
    ai_report_file: str,
) -> APIRouter:
    router = APIRouter()

    @router.get("/api/ai-report")
    async def get_ai_report(username: str = Depends(auth_dependency)) -> Dict[str, Any]:
        try:
            if os.path.exists(ai_report_file):
                with open(ai_report_file, "r", encoding="utf-8") as file:
                    return json.load(file)
            return {"report": None, "message": "아직 생성된 리포트가 없습니다."}
        except Exception as error:
            return {"error": str(error)}

    @router.post("/api/ai-report/refresh")
    async def refresh_ai_report(username: str = Depends(auth_dependency)) -> Dict[str, Any]:
        try:
            result: Dict[str, Any] = generate_ai_report()
            if result.get("error"):
                return {"success": False, "message": result["error"]}
            return {
                "success": True,
                "report": result.get("report", ""),
                "generated_at": result.get("generated_at", ""),
            }
        except Exception as error:
            return {"success": False, "message": f"리포트 생성 실패: {error}"}

    return router
