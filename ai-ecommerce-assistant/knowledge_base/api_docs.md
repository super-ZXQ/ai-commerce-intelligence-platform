# 平台功能与 API 说明 (Platform Features & API)

> 当用户问"平台有什么功能"、"怎么用XX"、"XX 数据在哪里"时检索

## 一、平台概览

**AI 商业智能分析平台** —— 基于 10 万+ 真实电商订单的全栈 BI 平台
- **后端**：FastAPI + SQLAlchemy (async) + MySQL 8
- **前端**：Streamlit + Plotly
- **AI 引擎**：LangChain + DeepSeek / OpenAI 兼容 LLM
- **部署**：Docker Compose + Nginx 反向代理

## 二、访问地址

### 本地开发

| 服务 | 端口 | 用途 |
|------|------|------|
| FastAPI 后端 | 8000 | RESTful API |
| BI 看板 | 8501 | 销售总览 / RFM 分层 |
| AI 助手 | 8505 | 当前页（你在这里） |
| Swagger 文档 | 8000/docs | API 在线文档 |

### Docker 部署

| 路径 | 后端 | 说明 |
|------|------|------|
| `/` | 8000 | 统一导航页（默认入口） |
| `/BI/` | 8501 | BI 看板 |
| `/ai/` | 8505 | AI 助手 |
| `/api/*` | 8000 | RESTful API |
| `/docs` | 8000 | Swagger |
| `/health` | 8000 | 健康检查 |
| `/monitor` | 8000 | 系统监控 |

## 三、BI 看板功能

### 📊 销售总览（页面 1）
- **顶部指标卡**：总销售额、订单数、活跃用户、客单价
- **趋势图**：日/月销售趋势
- **平台对比**：各平台销售/订单/退款率
- **TOP 10**：商品、用户排行

### 👥 RFM 客户分层（页面 2）
- **Tab 1 高价值概览**：8 种分层客户数、占比、GMV 贡献
- **Tab 2 分层详情**：各分层 R/F/M 维度、行为画像
- **Tab 3 价值矩阵**：3D 散点图，标注核心分层

## 四、AI 助手功能（当前页）

### 主要能力
1. **Text-to-SQL**：自然语言 → SQL → 数据查询
2. **自动可视化**：根据数据特征智能选择图表（柱状/折线/饼图）
3. **异常预警**：检测到异常指标时主动提示
4. **业务知识问答**：回答业务术语、计算公式等（基于 RAG）

### 支持的问题类型

| 类型 | 示例 |
|------|------|
| 数据查询 | "销售额最高的前 3 个商品" |
| 平台对比 | "APP 和公众号哪个销售额高" |
| 退款分析 | "哪个平台退款率最高" |
| 趋势分析 | "最近 30 天销售趋势" |
| 用户画像 | "复购率是多少" |
| 业务问答 | "客单价怎么算" |
| 异常检测 | "最近有什么异常指标" |

### 不能做的事
- 修改数据库（DROP/DELETE/UPDATE 都会被拦截）
- 查询个人隐私数据（手机号、地址、身份证）
- 访问平台功能之外的数据（如外部网络）
- 跨用户身份查询

## 五、RESTful API 速查

### 公开端点（无需认证）
```
GET  /                          # 导航页
GET  /health                    # 基础健康检查
GET  /health/detailed           # 详细健康检查（DB/Redis/AI 状态）
GET  /api/monitor/services-status  # 服务状态
GET  /api/monitor/metrics       # 监控指标
GET  /docs                      # Swagger 文档
```

### 认证端点
```
POST /api/auth/login            # 登录获取 JWT
GET  /api/auth/me               # 获取当前用户信息
```

### 订单端点（需认证）
```
GET  /api/orders                # 订单列表（分页/筛选）
GET  /api/orders/{order_id}     # 订单详情
GET  /api/orders/filter         # 多条件筛选
GET  /api/orders/stats          # 订单统计
```

### 分析端点（需认证）
```
GET  /api/analytics/sales-overview    # 销售概览
GET  /api/analytics/sales-trend       # 销售趋势
GET  /api/analytics/top-products      # TOP 商品
GET  /api/analytics/user-behavior     # 用户行为
GET  /api/analytics/category-analysis # 品类分析
```

### RFM 端点（需认证）
```
GET  /api/rfm/segments          # RFM 分层概览
GET  /api/rfm/customers         # 分层用户列表
GET  /api/rfm/thresholds        # 当前阈值
POST /api/rfm/thresholds        # 更新阈值
```

### AI 端点
```
POST /api/ai/query              # AI 问答（与 AI 助手同步）
GET  /api/ai/history            # 历史问答
DELETE /api/ai/history          # 清除历史
```

### 导出端点
```
POST /api/export/orders         # 导出订单（CSV/Excel）
GET  /api/export/formats        # 支持的格式
```

## 六、认证方式

### 登录
```http
POST /api/auth/login
Content-Type: application/json

{
  "username": "demo",
  "password": "demo123"
}

# 响应
{
  "access_token": "eyJ0eXAi...",
  "token_type": "bearer",
  "user": {"id": 1, "username": "demo", "role": "user"}
}
```

### 使用 Token
```http
GET /api/orders
Authorization: Bearer eyJ0eXAi...
```

### 测试账号
- **用户名**：`demo`
- **密码**：`demo123`
- **角色**：普通用户

## 七、限流规则

| 端点 | 限制 |
|------|------|
| `/api/auth/login` | 5 次/分钟/IP |
| `/api/ai/query` | 10 次/分钟/用户 |
| `/api/export/orders` | 3 次/2分钟/用户 |
| 其他 API | 100 次/分钟/IP |

## 八、技术栈

- **Python**：3.12
- **Web 框架**：FastAPI 0.110+
- **ORM**：SQLAlchemy 2.0 (async)
- **数据库**：MySQL 8 / SQLite (本地)
- **缓存**：Redis 7 / 内存降级
- **AI**：LangChain + DeepSeek-V4-Flash / OpenAI 兼容
- **前端**：Streamlit 1.30+
- **可视化**：Plotly 5.20+
- **部署**：Docker + Docker Compose + Nginx

## 九、常见问题

### Q1: 为什么 AI 给的 SQL 报错？
- A: 可能列名写错，参考 [data_dictionary.md](data_dictionary.md)

### Q2: 为什么退款率与 BI 看板不一致？
- A: BI 看板按付款 > 0 过滤；AI 助手按 `is_refunded` 计算率时分母可能不同

### Q3: AI 能查商品/类目吗？
- A: 当前数据库只有 `orders` 表（无商品维度），只能基于订单金额、平台、时间、用户等分析

### Q4: 数据是实时的吗？
- A: 否，10万+ 历史数据（2025 年），用于 BI 演示，不连接生产环境

### Q5: 怎么更新数据？
- A: 替换 `data/cleaned_orders.csv`，重启服务
