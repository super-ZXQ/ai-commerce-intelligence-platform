"""部署与运维契约：配置 fail-fast、容器加固、TLS、备份/恢复与授权验收脚本。"""
from __future__ import annotations

import json
import logging
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from backend.config import Settings
from backend.utils.logging_config import JsonFormatter

_ROOT = Path(__file__).resolve().parents[2]


def _settings(**overrides) -> Settings:
    values = dict(
        _env_file=None,
        debug=True,
        jwt_secret="test-secret-not-for-production-use-only",
        admin_password="admin123",
    )
    values.update(overrides)
    return Settings(**values)


class TestProductionConfigFailFast:
    def test_excel_limit_cannot_exceed_csv_limit(self):
        with pytest.raises(ValueError, match="EXPORT_EXCEL_MAX_ROWS"):
            _settings(export_max_rows=100, export_excel_max_rows=200)

    def test_export_limits_must_be_positive(self):
        with pytest.raises(ValueError, match="EXPORT_MAX_ROWS"):
            _settings(export_max_rows=0)
        with pytest.raises(ValueError, match="EXPORT_MAX_ROWS"):
            _settings(export_excel_max_rows=0)

    def test_page_size_must_be_consistent(self):
        with pytest.raises(ValueError, match="DEFAULT_PAGE_SIZE"):
            _settings(default_page_size=0)
        with pytest.raises(ValueError, match="MAX_PAGE_SIZE"):
            _settings(default_page_size=50, max_page_size=20)

    def test_cache_ttl_must_be_positive(self):
        with pytest.raises(ValueError, match="CACHE_TTL_SECONDS"):
            _settings(cache_ttl_seconds=0)

    def test_outbound_prompt_budget_limits_are_bounded(self):
        """出域预算配错必须启动即失败：过小会让结果集一行都发不出去。"""
        with pytest.raises(ValueError, match="AGENT_PROMPT_ROWS_MAX_CHARS"):
            _settings(agent_prompt_rows_max_chars=100)
        with pytest.raises(ValueError, match="AGENT_LLM_MAX_OUTPUT_TOKENS"):
            _settings(agent_llm_max_output_tokens=32)
        with pytest.raises(ValueError, match="AGENT_LLM_MAX_OUTPUT_TOKENS"):
            _settings(agent_llm_max_output_tokens=200_000)

    def test_managed_key_rejects_non_https_base_url(self):
        with pytest.raises(ValueError, match="https"):
            _settings(llm_api_key="sk-test", llm_base_url="http://api.deepseek.com")

    def test_managed_key_host_must_be_allowlisted(self):
        with pytest.raises(ValueError, match="LLM_PROVIDER_ALLOWLIST"):
            _settings(
                llm_api_key="sk-test",
                llm_base_url="https://api.example.com",
                llm_provider_allowlist="api.deepseek.com",
            )

    def test_managed_key_with_matching_allowlist_is_accepted(self):
        settings = _settings(
            llm_api_key="sk-test",
            llm_base_url="https://api.deepseek.com",
            llm_provider_allowlist="api.deepseek.com",
        )
        assert settings.llm_allowed_hosts == ("api.deepseek.com",)

    def test_byok_does_not_require_platform_key_base_url(self):
        settings = _settings(llm_mode="byok", llm_base_url="http://localhost:11434")
        assert settings.llm_mode == "byok"


class TestContainerHardening:
    @pytest.mark.parametrize(
        "dockerfile",
        ["Dockerfile", "Dockerfile.streamlit", "ai-ecommerce-assistant/Dockerfile"],
    )
    def test_images_run_as_non_root(self, dockerfile):
        text = (_ROOT / dockerfile).read_text(encoding="utf-8")
        assert "USER 10001:10001" in text
        assert "USER root" not in text
        assert "PYTHONDONTWRITEBYTECODE=1" in text

    def test_compose_wires_readiness_and_resource_limits(self):
        text = (_ROOT / "docker-compose.yml").read_text(encoding="utf-8")
        assert "http://localhost:8000/readyz" in text
        assert "no-new-privileges:true" in text
        for variable in ("BACKEND_MEMORY", "STREAMLIT_MEMORY", "AI_MEMORY", "MYSQL_MEMORY"):
            assert variable in text

    def test_production_overlay_requires_pinned_images(self):
        text = (_ROOT / "docker-compose.prod.yml").read_text(encoding="utf-8")
        for variable in (
            "BACKEND_IMAGE",
            "STREAMLIT_IMAGE",
            "AI_ASSISTANT_IMAGE",
            "MYSQL_IMAGE",
            "REDIS_IMAGE",
            "NGINX_IMAGE",
        ):
            assert f"${{{variable}:?" in text
        # 覆盖层必须移除构建指令，确保生产只运行固定镜像（db-migrate/data-import/backend/streamlit/ai）。
        assert text.count("build: !reset null") == 5


class TestObservability:
    def test_json_formatter_includes_request_id_and_message(self):
        formatter = JsonFormatter()
        record = logging.LogRecord(
            name="test",
            level=logging.INFO,
            pathname=__file__,
            lineno=1,
            msg="hello %s",
            args=("world",),
            exc_info=None,
        )
        record.request_id = "trace-123"
        payload = json.loads(formatter.format(record))
        assert payload["request_id"] == "trace-123"
        assert payload["message"] == "hello world"
        assert payload["level"] == "INFO"

    def test_log_format_is_validated(self):
        with pytest.raises(ValueError, match="LOG_FORMAT"):
            _settings(log_format="xml")

    def test_nginx_propagates_request_id(self):
        for name in ("nginx.conf", "nginx.tls.conf"):
            text = (_ROOT / "deploy" / name).read_text(encoding="utf-8")
            assert "log_format main" in text
            assert "request_id=$request_id" in text
            assert "proxy_set_header X-Request-ID $request_id;" in text
            # 响应头由 FastAPI 回写，Nginx 不再重复 add_header。
            assert "add_header X-Request-ID" not in text

    def test_nginx_uses_relative_login_redirect(self):
        # 默认 absolute_redirect on 会把 $scheme/$host 写进 Location：
        # 外部 TLS 终止时可能降级为 http，也增加 Host 伪造风险。
        for name in ("nginx.conf", "nginx.tls.conf"):
            text = (_ROOT / "deploy" / name).read_text(encoding="utf-8")
            assert "absolute_redirect off;" in text
            assert "return 302 /login?next=$uri;" in text


class TestTlsLayer:
    def test_tls_config_terminates_https_with_hsts(self):
        text = (_ROOT / "deploy" / "nginx.tls.conf").read_text(encoding="utf-8")
        assert "listen 443 ssl;" in text
        assert "ssl_certificate" in text and "/etc/nginx/certs/fullchain.pem;" in text
        assert "ssl_certificate_key" in text and "/etc/nginx/certs/privkey.pem;" in text
        assert "Strict-Transport-Security" in text
        assert "return 301 https://$host$request_uri;" in text

    def test_tls_keeps_auth_and_websocket_protection(self):
        text = (_ROOT / "deploy" / "nginx.tls.conf").read_text(encoding="utf-8")
        assert "location = /_auth" in text
        for block_name in ("location /ai/ {", "location /BI/ {"):
            block = text.split(block_name, 1)[1].split("\n        }", 1)[0]
            assert "auth_request /_auth;" in block
            assert "Upgrade $http_upgrade" in block
        stream_block = text.split("location = /api/ai/stream {", 1)[1].split("\n        }", 1)[0]
        assert "proxy_buffering off;" in stream_block

    def test_tls_overlay_forces_secure_cookie(self):
        text = (_ROOT / "docker-compose.tls.yml").read_text(encoding="utf-8")
        assert 'AUTH_COOKIE_SECURE: "true"' in text
        assert "deploy/certs:/etc/nginx/certs:ro" in text


class TestWorkerGuard:
    """多 worker 会让进程内限流/AI 并发按倍数放大，默认必须拒绝启动。"""

    @staticmethod
    def _clear_env(monkeypatch):
        for name in ("WEB_CONCURRENCY", "GUNICORN_WORKERS", "UVICORN_WORKERS"):
            monkeypatch.delenv(name, raising=False)

    def test_detects_worker_count_from_env_and_argv(self, monkeypatch):
        from backend import main

        self._clear_env(monkeypatch)
        monkeypatch.setattr(sys, "argv", ["uvicorn", "backend.main:app"])
        assert main._detect_worker_count() == 1

        monkeypatch.setenv("WEB_CONCURRENCY", "2")
        assert main._detect_worker_count() == 2

        monkeypatch.delenv("WEB_CONCURRENCY")
        monkeypatch.setattr(
            sys, "argv", ["uvicorn", "backend.main:app", "--workers", "3"]
        )
        assert main._detect_worker_count() == 3

        monkeypatch.setattr(
            sys, "argv", ["uvicorn", "backend.main:app", "--workers=4"]
        )
        assert main._detect_worker_count() == 4

    def test_multi_worker_is_rejected_without_override(self, monkeypatch):
        from backend import main

        self._clear_env(monkeypatch)
        monkeypatch.setenv("WEB_CONCURRENCY", "2")
        monkeypatch.setattr(main, "settings", SimpleNamespace(allow_multi_worker=False))
        with pytest.raises(RuntimeError, match="ALLOW_MULTI_WORKER"):
            main._enforce_single_worker()

    def test_multi_worker_can_be_explicitly_allowed(self, monkeypatch):
        from backend import main

        self._clear_env(monkeypatch)
        monkeypatch.setenv("WEB_CONCURRENCY", "2")
        monkeypatch.setattr(main, "settings", SimpleNamespace(allow_multi_worker=True))
        # 显式放行时只记录警告，不应再次抛出。
        main._enforce_single_worker()

    def test_ai_db_account_is_required_by_api_startup(self, monkeypatch):
        from backend import main

        monkeypatch.setattr(
            main, "settings", SimpleNamespace(debug=False, ai_db_user="")
        )
        with pytest.raises(RuntimeError, match="AI_DB_USER"):
            main._enforce_ai_db_account()

        monkeypatch.setattr(
            main, "settings", SimpleNamespace(debug=False, ai_db_user="ea_ai")
        )
        main._enforce_ai_db_account()


class TestHealthCheckRedisUrl:
    def test_builds_url_with_encoded_password(self, monkeypatch):
        from scripts.health_check import _default_redis_url

        monkeypatch.delenv("REDIS_URL", raising=False)
        monkeypatch.setenv("REDIS_PASSWORD", "p@ss+w/rd=1#")
        monkeypatch.setenv("REDIS_HOST", "redis")
        monkeypatch.setenv("REDIS_PORT", "6380")
        monkeypatch.setenv("REDIS_DB", "3")

        assert (
            _default_redis_url()
            == "redis://:p%40ss%2Bw%2Frd%3D1%23@redis:6380/3"
        )

    def test_explicit_redis_url_wins(self, monkeypatch):
        from scripts.health_check import _default_redis_url

        monkeypatch.setenv("REDIS_URL", "redis://:explicit@localhost:6379/0")
        monkeypatch.setenv("REDIS_PASSWORD", "ignored")
        assert _default_redis_url() == "redis://:explicit@localhost:6379/0"

    def test_no_password_means_redis_check_is_skipped(self, monkeypatch):
        from scripts.health_check import _default_redis_url

        monkeypatch.delenv("REDIS_URL", raising=False)
        monkeypatch.delenv("REDIS_PASSWORD", raising=False)
        assert _default_redis_url() is None


class TestBackupAndGrantScripts:
    @pytest.mark.parametrize(
        ("script", "shell"),
        [
            ("scripts/backup_mysql.sh", "bash"),
            ("scripts/restore_mysql.sh", "bash"),
            ("scripts/backup_restore_drill.sh", "bash"),
            ("deploy/mysql/verify-grants.sh", "sh"),
        ],
    )
    def test_shell_syntax(self, script, shell):
        result = subprocess.run(
            [shell, "-n", str(_ROOT / script)],
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode == 0, result.stderr

    def test_restore_defaults_to_scratch_database(self):
        text = (_ROOT / "scripts" / "restore_mysql.sh").read_text(encoding="utf-8")
        assert "_restore_drill" in text
        assert "--allow-overwrite" in text
        assert "拒绝覆盖生产库" in text

    def test_backup_does_not_expose_password_on_command_line(self):
        text = (_ROOT / "scripts" / "backup_mysql.sh").read_text(encoding="utf-8")
        # 密码通过容器内 MYSQL_ROOT_PASSWORD 展开，宿主机参数中不出现 -p 明文。
        assert 'MYSQL_PWD="$MYSQL_ROOT_PASSWORD"' in text
        assert "-p${" not in text
        assert "Dump completed" in text

    def test_grant_verifier_covers_all_runtime_accounts(self):
        text = (_ROOT / "deploy" / "mysql" / "verify-grants.sh").read_text(encoding="utf-8")
        for account in ("ea_app", "ea_ai", "ea_events", "ea_auth"):
            assert account in text
        assert "DELETE FROM data_state" in text
        assert "SELECT COUNT(*) FROM auth_users" in text

    def test_private_artifacts_are_gitignored(self):
        text = (_ROOT / ".gitignore").read_text(encoding="utf-8")
        assert "deploy/certs/" in text
        assert "backups/" in text
