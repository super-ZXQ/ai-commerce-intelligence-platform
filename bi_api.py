"""BI 页面调用 FastAPI 的客户端。

页面不持有数据库凭据：浏览器 Cookie 原样转发给后端，由后端重新校验账号
状态、data:read 与 data:export。分页拉取与导出都走同一条认证链路。
"""
from __future__ import annotations

import math

import httpx


class BiApiError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        status_code: int = 0,
        error_code: str = "BI_BACKEND_ERROR",
    ):
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.error_code = error_code


class BiAuthError(BiApiError):
    """会话缺失或已失效。"""


class BiPermissionError(BiApiError):
    """已登录但缺少后端要求的业务权限。"""


def _error_from_response(response: httpx.Response) -> BiApiError:
    message = f"后端返回 {response.status_code}"
    code = "BI_BACKEND_ERROR"
    try:
        payload = response.json()
    except ValueError:
        payload = {}
    detail = payload.get("detail") if isinstance(payload, dict) else None
    if isinstance(detail, dict):
        message = str(detail.get("message") or message)
        code = str(detail.get("error_code") or code)
    elif isinstance(detail, str):
        message = detail
    error_type = BiApiError
    if response.status_code == 401:
        error_type = BiAuthError
    elif response.status_code == 403:
        error_type = BiPermissionError
    return error_type(message, status_code=response.status_code, error_code=code)


class BiApiClient:
    def __init__(
        self,
        base_url: str,
        cookie_name: str,
        session_token: str,
        *,
        timeout: float = 60.0,
        transport: httpx.BaseTransport | None = None,
    ):
        self.base_url = base_url.rstrip("/")
        self.cookie_name = cookie_name
        self.session_token = session_token.strip()
        self.timeout = timeout
        self.transport = transport

    def _headers(self) -> dict[str, str]:
        headers = {"Accept": "application/json"}
        if self.session_token:
            headers["Cookie"] = f"{self.cookie_name}={self.session_token}"
        return headers

    def _require_token(self) -> None:
        """无会话时在本地失败，避免匿名请求到达后端后才被拒绝。"""
        if not self.session_token:
            raise BiAuthError(
                "未登录或会话已失效",
                status_code=401,
                error_code="BI_AUTH_REQUIRED",
            )

    def _client(self) -> httpx.Client:
        return httpx.Client(
            timeout=self.timeout,
            transport=self.transport,
            follow_redirects=False,
        )

    def _request(
        self,
        client: httpx.Client,
        method: str,
        path: str,
        **kwargs,
    ) -> httpx.Response:
        try:
            response = client.request(
                method,
                f"{self.base_url}{path}",
                headers=self._headers(),
                **kwargs,
            )
        except httpx.HTTPError as exc:
            # 连接失败不能冒充登录失效；页面据此显示“后端不可用”而不是登录入口。
            raise BiApiError("无法连接后端，请稍后重试") from exc
        if response.status_code >= 400:
            raise _error_from_response(response)
        return response

    def verify_session(self) -> dict | None:
        """无 Cookie 或后端判定会话失效时返回 None；后端不可用则抛错。"""
        if not self.session_token:
            return None
        try:
            with self._client() as client:
                return self._request(client, "GET", "/api/auth/verify").json()
        except BiAuthError:
            return None

    def fetch_orders(self, page_size: int = 10000, max_retries: int = 3) -> list[dict]:
        """分页拉取完整非取消订单集，整次读取绑定同一数据版本。

        首屏返回 data_version，后续页携带该版本；服务端发现期间有新增/取消
        订单会返回 409，客户端放弃已拼接结果并整次重读，最多 max_retries 次。
        """
        self._require_token()
        last_conflict: BiApiError | None = None
        for _ in range(max(1, max_retries)):
            try:
                return self._fetch_orders_snapshot(page_size)
            except BiApiError as exc:
                if exc.error_code != "BI_DATA_VERSION_CHANGED":
                    raise
                last_conflict = exc
        raise BiApiError(
            "BI 数据持续变化，暂时无法取得一致快照，请稍后重试",
            status_code=last_conflict.status_code if last_conflict else 0,
            error_code="BI_DATA_VERSION_CHANGED",
        )

    def _fetch_orders_snapshot(self, page_size: int) -> list[dict]:
        with self._client() as client:
            first = self._request(
                client,
                "GET",
                "/api/bi/orders",
                params={"page": 1, "page_size": page_size},
            ).json()
            version = first.get("data_version")
            if not isinstance(version, int):
                raise BiApiError("后端未返回 BI 数据版本，无法保证分页一致性")
            total = int(first.get("total") or 0)
            rows = list(first.get("items") or [])
            pages = math.ceil(total / page_size) if total else 0
            for page in range(2, pages + 1):
                payload = self._request(
                    client,
                    "GET",
                    "/api/bi/orders",
                    params={
                        "page": page,
                        "page_size": page_size,
                        "data_version": version,
                    },
                ).json()
                # 任何一页的版本或总数漂移都说明快照已失效，不能拼接后交付。
                if (
                    payload.get("data_version") != version
                    or int(payload.get("total") or 0) != total
                ):
                    raise BiApiError(
                        "BI 数据在分页期间发生变化，正在重新读取",
                        error_code="BI_DATA_VERSION_CHANGED",
                    )
                rows.extend(payload.get("items") or [])
        if len(rows) != total:
            raise BiApiError(
                f"BI 数据分页不完整（{len(rows)}/{total} 行），请刷新重试"
            )
        return rows

    def fetch_rfm_export(
        self,
        *,
        segment: str,
        reference_date: str,
        n_bins: int,
        r_threshold: int,
        f_threshold: int,
        m_threshold: int,
        limit: int = 200,
    ) -> bytes:
        self._require_token()
        with self._client() as client:
            response = self._request(
                client,
                "GET",
                "/api/export/rfm-segment",
                params={
                    "segment": segment,
                    "reference_date": reference_date,
                    "n_bins": n_bins,
                    "r_threshold": r_threshold,
                    "f_threshold": f_threshold,
                    "m_threshold": m_threshold,
                    "limit": limit,
                },
            )
            return response.content
