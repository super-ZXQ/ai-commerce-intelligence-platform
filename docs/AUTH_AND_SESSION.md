# 统一认证与会话策略

## 身份来源

- 用户目录在 MySQL `auth_users`，密码只保存 bcrypt 哈希；角色与 scopes 也在数据库中。
- JWT 只承载 `username`、`role`、`scopes`、`token_version` 与过期时间。每次请求都会重新读取
  `auth_users`：账号停用、角色变更或 `token_version` 增加会立即让旧 token 失效。
- 机器客户端使用 `X-API-Key`，服务端只保存 SHA-256 哈希；scopes 不继承用户角色。
- 首次启动且 `auth_users` 为空时，用 `ADMIN_USERNAME` / `ADMIN_PASSWORD` 引导管理员；
  之后账号变更应使用 `python -m backend.scripts.manage_auth`。

## 登录链路

1. 浏览器访问 `POST /api/auth/login`，FastAPI 校验数据库账号并写入 HttpOnly Cookie
   `AUTH_COOKIE_NAME`（默认 `ea_session`）。页面脚本无法读取该 Cookie。
2. 直接访问 `/api/*` 时使用 Bearer token；浏览器页面统一使用 Cookie。
3. Nginx 对 `/ai/`、`/BI/`（包括 WebSocket 握手）执行 `auth_request /_auth`，
   子请求转到 `/api/auth/verify`。未登录返回 302 到 `/login?next=...`，无权限返回 403。
4. Streamlit 从 `st.context.cookies` 读取同一个 HttpOnly Cookie，并把它转发给后端；
   助手进程不签发 token、不保存平台密钥、不连接数据库。
5. 后端对每个业务操作重新校验账号状态、角色与 scope；Nginx 只负责页面入口，
   不能替代后端授权。

## 会话丢失策略

- Cookie 过期或被清除：页面请求得到 401，跳转登录页；已打开的 AI 对话正文仍留在浏览器
  页面状态中，但服务端会话历史可能已经丢失。
- Redis 会话不可用：会话存储降级到进程内内存（上限 1,000 个会话），进程重启后丢失。
- Redis 逻辑 DB 拆分（`REDIS_DB` 与 `REDIS_SESSION_DB`）只做键命名隔离，**不构成淘汰隔离**：
  同一 Redis 实例的 `allkeys-lru` 仍会跨逻辑 DB 淘汰。会话丢失属于可接受事件，不承诺持久化。
- 因此多轮对话只保证“最近 6 轮”（12 条消息）在会话存储可用时被保留；
  第 7 轮及更早内容会被裁剪。20 次连续写入全部成功，但只保留最近 6 轮，
  不承诺 20 轮全部留存。
- 重新生成答案时，只有末轮问题匹配才删除末轮；问题不匹配时宁可不删，避免误删新上下文。
- Redis 写入使用 `WATCH/MULTI` 原子读改写：并发追加不会互相覆盖，撤销末轮时在事务内校验末轮问题。观察到并发修改会重试 3 次，仍冲突则抛出异常并由降级存储接管。客户端设置连接/读写超时各 2 秒，Redis 抖动不会无限挂起请求。

## Cookie 与 HTTPS

| 配置 | 默认 | 说明 |
|------|------|------|
| `AUTH_COOKIE_NAME` | `ea_session` | 只允许字母、数字、下划线、连字符 |
| `AUTH_COOKIE_SECURE` | `false` | 生产启用 HTTPS 后必须设为 `true` |
| `AUTH_COOKIE_SAMESITE` | `lax` | `none` 时必须同时 `secure=true` |
| `JWT_EXPIRE_MINUTES` | `1440` | Cookie 与 Bearer token 共用有效期 |

直接以 HTTP 暴露服务时保持 `secure=false`；通过 TLS 反代上线后改为 `true`，
否则浏览器不会回传 Cookie，登录会表现为“登录成功后仍被跳回登录页”。

## 服务凭据

```bash
python -m backend.scripts.manage_auth create-service bi-exporter --scopes data:read,data:export
```

密钥只在创建时输出一次，之后只能看到前缀；吊销使用
`revoke-service <name>`。服务凭据不能用于 `/api/ai/config` 等需要 `ai:query` 的接口，
除非显式授予对应 scope。

## 已知限制

- 当前是单客户私有部署：角色为全局角色，没有租户隔离。
- 限流与并发计数在单 uvicorn worker 内准确；多 worker 会按 worker 放大。限流分两层：IP 桶（匿名滥用防护）与登录主体桶（AI 查询 10 次/分钟、导出 3 次/120 秒、事件写入 120 次/分钟）。主体桶按账号共享同一类操作额度，流式/非流式 AI 端点合计计数，切换端点不能绕过配额。
- 密码重置没有自助找回流程，由运维通过 `manage_auth set-password` 完成。
