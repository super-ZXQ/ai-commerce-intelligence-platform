import logging

from fastapi import APIRouter, Depends

from backend.models.schemas import AIQueryRequest, AIQueryResponse
from backend.routes.auth import get_current_user
from backend.services import ai_service

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/ai", tags=["AI助手"])


@router.post("/query", response_model=AIQueryResponse, summary="自然语言查询（需认证）")
async def natural_language_query(
    request: AIQueryRequest,
    user: dict = Depends(get_current_user),
):
    """
    自然语言查询接口（Text-to-SQL）。

    接收自然语言问题，自动生成SQL并执行，返回结果和可视化建议。
    需要 JWT Bearer Token 认证。

    - **query**: 自然语言查询，如"查询最近7天的销售额"
    - **Authorization**: Bearer <token> (通过 /api/auth/login 获取)
    """
    logger.info(f"AI查询请求 [用户:{user['username']}]: {request.query}")
    return await ai_service.process_natural_language_query(request.query)
