"""模型出站边界：精确主机许可、公共IP、DNS绑定、禁重定向/环境代理。"""
from __future__ import annotations

import asyncio
import ipaddress
import os
import socket

import httpx


def resolve_model_target(url: httpx.URL) -> str:
    allowed = {host.strip().lower() for host in os.getenv(
        "MODEL_ALLOWED_HOSTS", "api.deepseek.com,api.openai.com",
    ).split(",") if host.strip()}
    if (url.scheme != "https" or url.port not in (None, 443) or url.userinfo
            or url.host.lower() not in allowed or url.fragment):
        raise ValueError("模型地址必须为许可主机的HTTPS地址（443端口，无凭据或片段）")
    try:
        addresses = {item[4][0] for item in socket.getaddrinfo(url.host, 443, type=socket.SOCK_STREAM)}
    except OSError as exc:
        raise ValueError("模型域名解析失败") from exc
    if not addresses or any(not ipaddress.ip_address(address).is_global for address in addresses):
        raise ValueError("模型域名解析到非公共IP，拒绝访问")
    return sorted(addresses)[0]


def _pin_request(request: httpx.Request) -> httpx.Request:
    """连接到已验证IP，同时保留原域名的Host、TLS SNI与证书验证。"""
    address = resolve_model_target(request.url)
    headers = request.headers.copy()
    headers["Host"] = request.url.host
    return httpx.Request(
        request.method, request.url.copy_with(host=address), headers=headers,
        stream=request.stream,
        extensions={**request.extensions, "sni_hostname": request.url.host},
    )


class ModelTransport(httpx.HTTPTransport):
    def handle_request(self, request):
        response = super().handle_request(_pin_request(request))
        if 300 <= response.status_code < 400:
            response.close()
            raise ValueError("模型服务重定向被拒绝")
        return response


class AsyncModelTransport(httpx.AsyncHTTPTransport):
    async def handle_async_request(self, request):
        pinned = await asyncio.to_thread(_pin_request, request)
        response = await super().handle_async_request(pinned)
        if 300 <= response.status_code < 400:
            await response.aclose()
            raise ValueError("模型服务重定向被拒绝")
        return response


def model_http_clients() -> tuple[httpx.Client, httpx.AsyncClient]:
    timeout = httpx.Timeout(connect=5, read=60, write=15, pool=5)
    return (
        httpx.Client(transport=ModelTransport(), timeout=timeout, follow_redirects=False, trust_env=False),
        httpx.AsyncClient(transport=AsyncModelTransport(), timeout=timeout, follow_redirects=False, trust_env=False),
    )
