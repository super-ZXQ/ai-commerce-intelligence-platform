"""模型提供方地址策略：托管/BYOK 分离与 SSRF 防护。

平台托管密钥只允许发往服务端配置的固定主机；用户自带密钥也必须落在
部署方允许的提供方列表内。所有地址在真正建立连接前都要通过协议、
凭据、主机和 DNS 解析结果检查，并禁止跟随重定向。
"""
from __future__ import annotations

import ipaddress
import re
import socket
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from urllib.parse import urlsplit

import httpx


class ProviderPolicyError(ValueError):
    """地址或模型配置不符合部署策略。"""


_MODEL_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,99}$")
_RESERVED_HOSTS = {"localhost", "localhost.localdomain"}


def _normalize_host(host: str) -> str:
    return host.strip().lower().rstrip(".")


def _is_forbidden_ip(value: str) -> bool:
    ip = ipaddress.ip_address(value)
    return bool(
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
    )


def validate_provider_url(
    url: str,
    *,
    allowed_hosts: Sequence[str] = (),
    resolve_dns: bool = True,
    resolver: Callable[..., list] = socket.getaddrinfo,
) -> str:
    """校验模型 Base URL，返回去除尾部斜杠的规范地址。

    - 只允许 https；
    - 不允许 URL 内嵌用户名/密码、query 或 fragment；
    - 配置了 allowlist 时主机必须精确匹配；
    - 拒绝回环、内网、链路本地、保留地址以及解析到这些地址的域名。
    """
    raw = (url or "").strip()
    if not raw:
        raise ProviderPolicyError("模型 Base URL 不能为空")

    parts = urlsplit(raw)
    if parts.scheme != "https":
        raise ProviderPolicyError("模型 Base URL 必须使用 https")
    if parts.username or parts.password:
        raise ProviderPolicyError("模型 Base URL 不能包含用户名或密码")
    if parts.query or parts.fragment:
        raise ProviderPolicyError("模型 Base URL 不能包含 query 或 fragment")

    host = _normalize_host(parts.hostname or "")
    if not host:
        raise ProviderPolicyError("模型 Base URL 缺少主机名")
    if host in _RESERVED_HOSTS:
        raise ProviderPolicyError("模型 Base URL 不允许指向本机")

    if allowed_hosts:
        allowed = {_normalize_host(item) for item in allowed_hosts if item.strip()}
        if host not in allowed:
            raise ProviderPolicyError(f"模型提供方主机不在允许列表内: {host}")

    port = parts.port or 443
    try:
        literal_ip = ipaddress.ip_address(host)
    except ValueError:
        if resolve_dns:
            try:
                infos = resolver(host, port, type=socket.SOCK_STREAM)
            except OSError as exc:
                raise ProviderPolicyError(f"模型提供方域名无法解析: {host}") from exc
            if not infos:
                raise ProviderPolicyError(f"模型提供方域名无解析结果: {host}")
            for info in infos:
                address = info[4][0]
                try:
                    if _is_forbidden_ip(address):
                        raise ProviderPolicyError("模型提供方域名解析到受限地址")
                except ValueError as exc:
                    raise ProviderPolicyError("模型提供方域名解析结果非法") from exc
    else:
        if _is_forbidden_ip(str(literal_ip)):
            raise ProviderPolicyError("模型提供方地址属于受限网段")

    return raw.rstrip("/")


def validate_model_name(model: str) -> str:
    value = (model or "").strip()
    if not _MODEL_PATTERN.fullmatch(value):
        raise ProviderPolicyError("模型名称格式非法")
    return value


def build_http_client(timeout: float = 120.0) -> httpx.Client:
    """同步客户端（仅测试连接等单次调用），同样禁止跟随重定向。"""
    limits = httpx.Limits(max_connections=10, max_keepalive_connections=5)
    return httpx.Client(timeout=timeout, follow_redirects=False, limits=limits)


def build_http_clients(timeout: float = 120.0) -> tuple[httpx.Client, httpx.AsyncClient]:
    """禁止跟随重定向的 HTTP 客户端；重定向本身会被当作错误返回。"""
    limits = httpx.Limits(max_connections=10, max_keepalive_connections=5)
    return (
        httpx.Client(timeout=timeout, follow_redirects=False, limits=limits),
        httpx.AsyncClient(timeout=timeout, follow_redirects=False, limits=limits),
    )


@dataclass(frozen=True)
class ModelConfig:
    mode: str
    base_url: str
    model: str
    api_key: str = field(repr=False)

    def __post_init__(self) -> None:
        if self.mode not in {"managed", "byok"}:
            raise ProviderPolicyError("未知模型模式")
        if not self.api_key:
            raise ProviderPolicyError("模型密钥未配置")


def build_managed_config(
    *,
    api_key: str,
    base_url: str,
    model: str,
    allowed_hosts: Sequence[str] = (),
    resolver: Callable[..., list] = socket.getaddrinfo,
    resolve_dns: bool = True,
) -> ModelConfig:
    """平台托管配置：地址和模型完全来自服务端，用户不能覆盖。"""
    if not api_key:
        raise ProviderPolicyError("平台托管模型未配置")
    safe_url = validate_provider_url(
        base_url,
        allowed_hosts=allowed_hosts,
        resolve_dns=resolve_dns,
        resolver=resolver,
    )
    return ModelConfig(
        mode="managed",
        base_url=safe_url,
        model=validate_model_name(model),
        api_key=api_key,
    )


def build_byok_config(
    *,
    api_key: str,
    base_url: str,
    model: str,
    allowed_hosts: Sequence[str],
    resolver: Callable[..., list] = socket.getaddrinfo,
    resolve_dns: bool = True,
) -> ModelConfig:
    """用户自带密钥：只允许部署方预置的提供方地址，密钥不进入托管路径。"""
    if not api_key:
        raise ProviderPolicyError("BYOK 模式需要用户密钥")
    if not allowed_hosts:
        raise ProviderPolicyError("BYOK 未配置允许的模型提供方")
    safe_url = validate_provider_url(
        base_url,
        allowed_hosts=allowed_hosts,
        resolve_dns=resolve_dns,
        resolver=resolver,
    )
    return ModelConfig(
        mode="byok",
        base_url=safe_url,
        model=validate_model_name(model),
        api_key=api_key,
    )
