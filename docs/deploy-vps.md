# VPS 公网部署指南

> 本文面向把本项目部署到公网 VPS 的场景。本地 Docker 部署见 README「快速开始」，
> 本文不重复。所有命令假设 Ubuntu 22.04/24.04 + root 或 sudo 用户。

## 0. 部署前检查清单

- [ ] VPS：2 核 4G 起（AI 助手镜像含 PyTorch CPU，内存紧张时先只上后端+MySQL+Redis+BI）
- [ ] 域名已备案（国内 VPS 必需；境外 VPS + 国内访问可免备案但延迟高）
- [ ] 决定是否启用 Langfuse 观测栈（额外约 4G 内存，见 `docs/observability.md`）
- [ ] `.env` 中四类 MySQL 密码、`JWT_SECRET`、`LLM_API_KEY` 已填（**不要提交 .env**）
- [ ] 生产环境建议开启 `MONITOR_AUTH_REQUIRED=true`（监控端点加 JWT，见第 5 节）

## 1. 域名解析

在域名服务商控制台添加 A 记录：

| 记录 | 类型 | 值 | 说明 |
|------|------|-----|------|
| `@` | A | VPS 公网 IP | 主域名，如 `example.com` |
| `www` | A | VPS 公网 IP | 可选 |

生效检查：`dig +short example.com` 返回 VPS IP 即可继续。DNS 未生效时
certbot 签证书会失败——**先确保解析再签证书**。

## 2. 防火墙（部署第一件事）

```bash
sudo ufw default deny incoming
sudo ufw default allow outgoing
sudo ufw allow 22/tcp      # SSH（建议改端口 + 密钥登录）
sudo ufw allow 80/tcp      # HTTP（certbot 验证 + 跳转 HTTPS）
sudo ufw allow 443/tcp     # HTTPS
sudo ufw enable
```

**不要**对公网开放 3306（MySQL）、6379（Redis）、3000（Langfuse）、
8501/8502/8000（业务端口）——Nginx 是唯一入口，业务服务只监听 Docker 内网。
云厂商安全组做同样限制（双保险）。

## 3. HTTPS（certbot + Nginx）

现有 `deploy/nginx.conf` 只监听 80。公网需要 443。两种方式：

### 方式 A：Docker 内 certbot（推荐，与 compose 体系一致）

```bash
# 1. 修改 deploy/nginx.conf：把 server_name _; 改为你的域名
#    server_name example.com;

# 2. 首次签证书（http-01 验证，80 端口必须可达）
docker run -it --rm --name certbot \
  -v /etc/letsencrypt:/etc/letsencrypt \
  -v /var/lib/letsencrypt:/var/lib/letsencrypt \
  -v /var/www/html:/var/www/html \
  certbot/certbot certonly --standalone -d example.com

# 3. 续期（crontab -e，每月 1 号凌晨）
# 0 3 1 * * docker run --rm -v /etc/letsencrypt:/etc/letsencrypt -v /var/lib/letsencrypt:/var/lib/letsencrypt -v /var/www/html:/var/www/html certbot/certbot renew --quiet && docker exec nginx nginx -s reload
```

然后把证书目录挂载进 nginx 容器（compose 的 nginx service 加）：

```yaml
    volumes:
      - ./deploy/nginx.conf:/etc/nginx/conf.d/default.conf:ro
      - /etc/letsencrypt:/etc/letsencrypt:ro
```

`nginx.conf` 增加 443 server（在现有 80 server 之外新增）：

```nginx
server {
    listen 443 ssl;
    server_name example.com;
    ssl_certificate     /etc/letsencrypt/live/example.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/example.com/privkey.pem;
    ssl_protocols TLSv1.2 TLSv1.3;
    # 其余 location 与 80 server 完全相同
}
server {
    listen 80;
    server_name example.com;
    return 301 https://$host$request_uri;   # 全站跳转 HTTPS
}
```

WebSocket 注意事项：现有 nginx 已配 `proxy_http_version 1.1` + `Upgrade`/`Connection`
头，443 server 的 `location /ai/` 等必须保留同样的配置，否则 Streamlit 白屏。

### 方式 B：宿主机 Nginx 反代 Docker

适合想用宿主机统一管理证书的场景：宿主机装 nginx + certbot，
`proxy_pass http://127.0.0.1:80`。证书自动续期由宿主机 systemd timer 处理。

## 4. 启动与验证

```bash
# 拉代码（或 scp 上传）
git clone <repo> && cd ai-commerce-intelligence-platform

# 配置 .env 后启动（顺序由 compose depends_on 保证：mysql/redis → db-bootstrap → migrate → backend）
docker compose up -d

# 冒烟验证
curl -s http://localhost/health                 # 基础健康
curl -s http://localhost/health/detailed | head # 组件明细（生产建议开 JWT 后带 token 访问）
curl -s https://example.com/ | head -5          # 公网 HTTPS 可达
```

首次启动 `db-bootstrap` 会创建四类最小权限账号；`db-migrate`（Alembic）建 schema。
**schema 的唯一权威路径是 Alembic**，不要手动执行 `sql/01_schema.sql` 到生产库。

## 5. 生产环境变量建议

在 `.env` 追加/确认：

```
DEBUG=false                                   # 关闭调试（JWT 弱密钥在生产会直接报错）
MONITOR_AUTH_REQUIRED=true                    # 监控端点加 JWT（默认关闭仅兼容内部调用）
# 如启用 Langfuse：
LANGFUSE_ENABLED=true
LANGFUSE_PUBLIC_KEY=...                       # 自托管实例项目 Key
LANGFUSE_SECRET_KEY=...
LANGFUSE_HOST=https://langfuse.example.com    # 建议单独子域名 + HTTPS
```

启用 `MONITOR_AUTH_REQUIRED=true` 后，导航页/监控面板/健康面板的调用方
需要带登录 JWT（前端已是登录态，无感知）；直接 curl `/metrics` 会返回 401。
这是预期行为，不是故障。

## 6. 数据备份

```bash
# 备份目录
mkdir -p /data/backup

# 每日 MySQL 全量（crontab -e）：凌晨 3 点，保留 14 天
0 3 * * * docker exec mysql sh -c 'exec mysqldump -uroot -p"$MYSQL_ROOT_PASSWORD" --single-transaction --routines ai_commerce_intelligence_platform' | gzip > /data/backup/db_$(date +\%F).sql.gz && find /data/backup -name 'db_*.sql.gz' -mtime +14 -delete

# 每周 data/ 快照（CSV 初始化数据 + 任何本地产出）
0 4 * * 0 tar -czf /data/backup/data_$(date +\%F).tar.gz -C /opt/ai-commerce-intelligence-platform data
```

备份三要素：**MySQL 逻辑备份**（可读、可跨版本恢复）、**data/ 快照**
（重灌入口）、**`.env` 副本**（放密码管理器，不要放服务器明文）。

恢复演练（至少做一次）：找一台干净机器 `docker compose up -d mysql`，
`gunzip < db_2026-10-01.sql.gz | docker exec -i mysql mysql -uroot -p ai_commerce_intelligence_platform`，
跑通再删。**没演练过的备份等于没有备份**。

## 7. 版本更新流程

```bash
cd /opt/ai-commerce-intelligence-platform

# 1. 更新前备份（见第 6 节，更新前必做）
# 2. 拉代码并记录当前版本（回滚锚点）
git log --oneline -1 > /tmp/version-before.txt
git pull --ff-only

# 3. 构建新镜像（本地构建，tag 带日期）
docker compose build
docker tag <image> <image>:rollback-$(date +%F)   # 保留上一版镜像便于秒级回滚

# 4. 数据库迁移（Alembic upgrade；有 downgrade 脚本才可回退）
docker compose run --rm db-migrate

# 5. 滚动重启业务（先 backend，再 ai，最后 nginx 不动）
docker compose up -d backend ai-assistant

# 6. 健康检查 + 观察 10 分钟
curl -s http://localhost/health/detailed
docker compose logs --tail=100 backend
```

## 8. 回滚

| 场景 | 回滚方式 | 耗时 |
|------|----------|------|
| 应用代码问题 | `docker compose up -d` 回到 rollback tag 镜像 | 分钟级 |
| 数据库迁移问题 | Alembic `downgrade -1`（迁移脚本必须写 downgrade） | 分钟级 |
| 数据损坏 | 停服 → 恢复第 6 节备份 → 重放事件 API 补偿 | 小时级 |
| 整机故障 | 新 VPS 重跑第 4 节 + 恢复备份 | 小时级 |

回滚决策点：**先恢复服务，再定位原因**。应用层回滚不影响数据库 schema 时
优先动应用层；schema 变更未向下兼容时不要只回滚应用（会出现新旧代码字段不一致）。

## 9. 常见问题

| 现象 | 排查 |
|------|------|
| 签证书失败 | `dig +short 域名` 是否已生效；80 端口是否被 ufw/安全组挡 |
| `/ai/` 白屏 | 443 server 是否漏配 WebSocket 头（见第 3 节） |
| 502 Bad Gateway | `docker compose ps` 看 backend/ai 是否健康；`logs` 看启动报错 |
| 内存不足 OOM | AI 助手 torch 镜像约 2G+；小机器先不上 ai-assistant |
| JWT 报错 | 生产 `DEBUG=false` 下 `JWT_SECRET` 未设置会拒绝启动（设计行为，防弱密钥上线） |
