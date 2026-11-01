"""部署与运维契约：配置 fail-fast、容器加固、TLS、备份/恢复与授权验收脚本。"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from backend.config import Settings

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
