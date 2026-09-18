"""Locust 负载模型：持续事件写入与 BI/订单/RFM/AI 混合读取。

安装：pip install locust
运行：locust -f load/locustfile.py --host http://127.0.0.1:8000
可选：设置 LOAD_JWT 访问受认证接口；未设置时会记录 401，不伪造成功率。
"""

from __future__ import annotations

import os
from datetime import UTC, datetime
from uuid import uuid4

from locust import HttpUser, between, task


class CommerceUser(HttpUser):
    wait_time = between(0.1, 0.5)

    def on_start(self):
        token = os.getenv("LOAD_JWT", "")
        if token:
            self.client.headers.update({"Authorization": f"Bearer {token}"})

    @task(5)
    def ingest_synthetic_order(self):
        suffix = uuid4().hex[:16]
        self.client.post("/api/order-events", json={
            "event_id": str(uuid4()), "order_id": f"LOCUST-{suffix}",
            "event_type": "ORDER_CREATED", "occurred_at": datetime.now(UTC).isoformat(),
            "version": 1, "source": "locust-synthetic",
            "payload": {"user_name": f"synthetic_{suffix[:6]}", "product_id": "LOAD-PRODUCT",
                        "order_amount": "99.00", "payment_amount": "99.00", "platform_type": "APP"},
        }, name="POST /api/order-events")

    @task(3)
    def sales_overview(self):
        self.client.get("/api/analytics/sales-overview", name="GET /api/analytics/sales-overview")

    @task(2)
    def rfm_overview(self):
        self.client.get("/api/rfm/overview", name="GET /api/rfm/overview")

    @task(2)
    def orders(self):
        self.client.get("/api/orders?page_size=20", name="GET /api/orders")

    @task(1)
    def ai_query(self):
        self.client.post("/api/ai/query", json={"query": "最近销售额趋势"}, name="POST /api/ai/query")
