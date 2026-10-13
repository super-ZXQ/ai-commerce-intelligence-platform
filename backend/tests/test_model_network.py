"""出站网络策略单元测试；HTTP传输用Mock，不代表真实模型能力。"""
import socket

import httpx
import pytest

from agent_core.model_network import ModelTransport, _pin_request, resolve_model_target


def _dns(*addresses):
    return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, 443)) for address in addresses]


@pytest.mark.parametrize("url", [
    "http://api.deepseek.com", "https://127.0.0.1", "https://localhost",
    "https://169.254.169.254/latest/meta-data", "https://[::1]",
    "https://api.deepseek.com.attacker.com", "https://api.deepseek.com:8443",
    "https://key@api.deepseek.com", "https://api.deepseek.com/#x",
])
def test_reject_untrusted_urls(url):
    with pytest.raises(ValueError):
        resolve_model_target(httpx.URL(url))


@pytest.mark.parametrize("address", ["127.0.0.1", "10.0.0.1", "192.168.1.1", "169.254.169.254", "::1", "::ffff:127.0.0.1"])
def test_reject_any_private_dns_answer(monkeypatch, address):
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **k: _dns("8.8.8.8", address))
    with pytest.raises(ValueError):
        resolve_model_target(httpx.URL("https://api.deepseek.com"))


def test_pin_preserves_host_and_tls_identity(monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **k: _dns("8.8.8.8"))
    request = _pin_request(httpx.Request("POST", "https://api.deepseek.com/v1/chat/completions", content=b"test"))
    assert request.url.host == "8.8.8.8"
    assert request.headers["Host"] == "api.deepseek.com"
    assert request.extensions["sni_hostname"] == "api.deepseek.com"
    assert request.read() == b"test"
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **k: _dns("127.0.0.1"))
    with pytest.raises(ValueError):
        _pin_request(httpx.Request("GET", "https://api.deepseek.com"))


def test_redirect_is_rejected(monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **k: _dns("8.8.8.8"))
    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", lambda *a: httpx.Response(302, headers={"location": "http://localhost"}))
    with ModelTransport() as transport, pytest.raises(ValueError, match="重定向"):
        transport.handle_request(httpx.Request("GET", "https://api.deepseek.com"))
