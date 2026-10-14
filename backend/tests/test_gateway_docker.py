"""真实Nginx容器+真实ASGI认证；页面上游为HTTP桩，不启动完整应用栈。"""
from __future__ import annotations

import json
import os
import socket
import subprocess
import threading
import time
from datetime import timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from uuid import uuid4

import httpx
import pytest
import uvicorn
from fastapi import FastAPI

from backend.routes.auth import router
from backend.utils.auth import create_access_token


@pytest.mark.skipif(os.environ.get("RUN_DOCKER_AUTH_TESTS") != "1", reason="显式启用真实Docker网关测试")
def test_real_nginx_auth_request_protects_pages_and_preserves_health(tmp_path):
    # 真实认证路由，其他页面为桩；临时测试服务不暴露业务数据接口。
    app = FastAPI()
    app.include_router(router)

    @app.get("/health")
    async def health():
        return {"status": "ok"}

    class PageStub(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"protected-page-upstream")

        def log_message(self, *args):
            pass

    listener = socket.socket()
    listener.bind(("0.0.0.0", 0))  # nosec B104 -- Docker集成测试需访问宿主临时端口，仅测试认证路由
    listener.listen(128)
    api_port = listener.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, log_level="error", lifespan="off"))
    api_thread = threading.Thread(target=server.run, kwargs={"sockets": [listener]}, daemon=True)
    stub = ThreadingHTTPServer(("0.0.0.0", 0), PageStub)  # nosec B104 -- 临时无数据页面桩，finally关闭
    stub_thread = threading.Thread(target=stub.serve_forever, daemon=True)
    name = "audit-nginx-" + uuid4().hex[:12]
    network = name + "-net"
    try:
        api_thread.start()
        stub_thread.start()
        for _ in range(100):
            if server.started:
                break
            time.sleep(.05)
        assert server.started
        config = (Path(__file__).resolve().parents[2] / "deploy" / "nginx.conf").read_text(encoding="utf-8")
        config = config.replace("backend:8000", f"host.docker.internal:{api_port}")
        config = config.replace("streamlit:8501", f"host.docker.internal:{stub.server_port}")
        config = config.replace("ai-assistant:8502", f"host.docker.internal:{stub.server_port}")
        target = tmp_path / "nginx.conf"
        target.write_text(config, encoding="utf-8")
        subprocess.run(["docker", "network", "create", network], check=True, capture_output=True)
        subprocess.run([
            "docker", "run", "-d", "--rm", "--name", name, "--network", network, "-p", "127.0.0.1::80",
            "--add-host", "host.docker.internal:host-gateway",
            "--mount", f"type=bind,source={target},target=/etc/nginx/nginx.conf,readonly",
            "nginx:alpine",
        ], check=True, capture_output=True)
        info = json.loads(subprocess.check_output(["docker", "inspect", name]))[0]
        port = info["NetworkSettings"]["Ports"]["80/tcp"][0]["HostPort"]
        with httpx.Client(base_url=f"http://127.0.0.1:{port}", timeout=10, trust_env=False) as client:
            for _ in range(100):
                try:
                    if client.get("/nginx-health").status_code == 200:
                        break
                except httpx.TransportError:
                    pass
                time.sleep(.05)
            for path in ("/BI/", "/ai/", "/BI/_stcore/stream", "/ai/static/test.js"):
                for token in ("", "forged", create_access_token("admin", timedelta(seconds=-1))):
                    response = client.get(path, headers={"Cookie": f"ea_session={token}", "X-User": "admin"})
                    if response.status_code != 401:
                        logs = subprocess.check_output(["docker", "logs", name], stderr=subprocess.STDOUT).decode(errors="replace")
                        pytest.fail(f"expected 401, got {response.status_code}; nginx logs: {logs}")
                response = client.get(path, headers={"Cookie": "ea_session=" + create_access_token("admin")})
                assert response.status_code == 200
                assert response.text == "protected-page-upstream"
            assert client.get("/health").status_code == 200
    finally:
        subprocess.run(["docker", "stop", name], capture_output=True)
        subprocess.run(["docker", "network", "rm", network], capture_output=True)
        server.should_exit = True
        api_thread.join(timeout=10)
        listener.close()
        stub.shutdown()
        stub.server_close()
