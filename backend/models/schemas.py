from datetime import date, datetime
from decimal import Decimal
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator


class PaginatedResponse(BaseModel):
    total: int = Field(..., description="总记录数")
    page: int = Field(..., description="当前页码")
    page_size: int = Field(..., description="每页数量")
    total_pages: int = Field(..., description="总页数")
    items: list[Any] = Field(..., description="数据列表")


class OrderResponse(BaseModel):
    id: int = Field(..., description="订单顺序编号")
    order_no: str = Field(..., description="订单号")
    user_name: str = Field(..., description="用户名")
    product_id: str = Field(..., description="商品编号")
    order_amount: float = Field(..., description="订单金额")
    payment_amount: float = Field(..., description="付款金额")
    channel_id: str | None = Field(None, description="渠道编号")
    platform_type: str = Field(..., description="平台类型")
    order_time: datetime = Field(..., description="下单时间")
    payment_time: datetime | None = Field(None, description="付款时间")
    is_refunded: str = Field("否", description="是否退款")
    discount_amount: float | None = Field(0.0, description="优惠金额")
    payment_duration_sec: float | None = Field(None, description="支付耗时(秒)")
    order_date: date | None = Field(None, description="下单日期")
    order_hour: int | None = Field(None, description="下单小时")
    weekday: str | None = Field(None, description="星期几")
    order_status: str = Field("ACTIVE", description="事件驱动订单状态")
    event_version: int = Field(0, ge=0, description="最后成功应用的事件版本")
    updated_at: datetime | None = Field(None, description="最后一次状态事件处理时间")

    model_config = {"from_attributes": True}


class OrderFilterParams(BaseModel):
    start_date: date | None = Field(None, description="开始日期")
    end_date: date | None = Field(None, description="结束日期")
    platform_type: str | None = Field(None, description="平台类型")
    user_name: str | None = Field(None, description="用户名")
    product_id: str | None = Field(None, description="商品编号")
    is_refunded: str | None = Field(None, description="是否退款")
    min_amount: float | None = Field(None, ge=0, description="最小金额")
    max_amount: float | None = Field(None, ge=0, description="最大金额")
    sort_by: str = Field("order_time", description="排序字段")
    sort_order: str = Field("desc", description="排序方向 asc/desc")


class ProductSalesResponse(BaseModel):
    product_id: str = Field(..., description="商品编号")
    order_count: int = Field(..., description="被下单次数")
    total_sales: float = Field(..., description="总销售额")


class UserSpendingResponse(BaseModel):
    user_name: str = Field(..., description="用户名")
    order_count: int = Field(..., description="累计订单数")
    total_spent: float = Field(..., description="累计消费额")
    avg_order_value: float = Field(..., description="客单价")


class SalesOverviewResponse(BaseModel):
    total_sales: float = Field(..., description="总销售额")
    total_orders: int = Field(..., description="总订单数")
    avg_order_value: float = Field(..., description="平均客单价")
    total_users: int = Field(..., description="总用户数")
    refund_rate: float = Field(..., description="退款率")
    total_refund_amount: float = Field(..., description="退款总额")


class SalesTrendItem(BaseModel):
    period: str = Field(..., description="时间段")
    sales: float = Field(..., description="销售额")
    order_count: int = Field(..., description="订单数")


class SalesTrendResponse(BaseModel):
    granularity: str = Field(..., description="聚合粒度: day/week/month")
    data: list[SalesTrendItem] = Field(..., description="趋势数据")


class TopProductResponse(BaseModel):
    rank: int = Field(..., description="排名")
    product_id: str = Field(..., description="商品编号")
    order_count: int = Field(..., description="被下单次数")
    total_sales: float = Field(..., description="总销售额")


class UserBehaviorResponse(BaseModel):
    total_users: int = Field(..., description="总用户数")
    repeat_purchase_rate: float = Field(..., description="复购率")
    avg_order_frequency: float = Field(..., description="平均消费频次")
    avg_customer_value: float = Field(..., description="平均客单价")
    active_users_7d: int = Field(..., description="7日内活跃用户数")
    active_users_30d: int = Field(..., description="30日内活跃用户数")


class CategoryAnalysisItem(BaseModel):
    platform_type: str = Field(..., description="平台类型")
    order_count: int = Field(..., description="订单量")
    total_sales: float = Field(..., description="总销售额")
    sales_ratio: float = Field(..., description="销售额占比(%)")
    avg_order_value: float = Field(..., description="客单价")
    refund_rate: float = Field(..., description="退款率")


class CategoryAnalysisResponse(BaseModel):
    categories: list[CategoryAnalysisItem] = Field(..., description="各平台分析数据")


class AIQueryRequest(BaseModel):
    query: str = Field(..., min_length=1, max_length=500, description="自然语言查询")
    thread_id: str | None = Field(None, min_length=1, max_length=128, description="多轮会话标识")


# ─────────────────── 订单事件摄入契约 ───────────────────

class _StrictPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")


class OrderCreatedPayload(_StrictPayload):
    """新订单事件必须给出创建订单读模型所需的完整字段。"""

    user_name: str = Field(..., min_length=1, max_length=100)
    product_id: str = Field(..., min_length=1, max_length=50)
    order_amount: Decimal = Field(..., gt=0, max_digits=18, decimal_places=2)
    payment_amount: Decimal = Field(..., gt=0, max_digits=18, decimal_places=2)
    platform_type: str = Field(..., min_length=1, max_length=50)
    channel_id: str | None = Field(None, max_length=50)
    discount_amount: Decimal = Field(Decimal("0"), ge=0, max_digits=18, decimal_places=2)
    order_time: datetime | None = None

    @model_validator(mode="after")
    def payment_cannot_exceed_order_amount(self):
        if self.payment_amount > self.order_amount:
            raise ValueError("payment_amount 不能大于 order_amount")
        return self


class OrderStatePayload(_StrictPayload):
    """退款/取消只允许携带可审计原因，不允许通过 payload 偷改金额或身份字段。"""

    reason: str | None = Field(None, max_length=500)


class OrderEventInput(BaseModel):
    """单条事件的传输协议。

    version 是事件源内同一 order_id 的单调序号。服务端拒绝小于等于当前订单
    watermark 的非重复事件，防止延迟投递使状态倒退。
    """

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    event_id: UUID
    order_id: str = Field(..., min_length=1, max_length=50)
    event_type: Literal["ORDER_CREATED", "ORDER_REFUNDED", "ORDER_CANCELLED"]
    occurred_at: datetime
    version: int = Field(..., ge=1, le=2_147_483_647)
    payload: dict[str, Any]
    source: str = Field(..., min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_.:-]+$")

    @model_validator(mode="after")
    def validate_payload_for_event_type(self):
        if self.event_type == "ORDER_CREATED":
            payload = OrderCreatedPayload.model_validate(self.payload)
        else:
            payload = OrderStatePayload.model_validate(self.payload)
        self.payload = payload.model_dump(mode="json")
        return self


class OrderEventBatchRequest(BaseModel):
    """小批量写入。批内重复 event_id 先在 Pydantic 层拒绝，避免无意义 DB 往返。"""

    model_config = ConfigDict(extra="forbid")

    events: list[OrderEventInput] = Field(..., min_length=1, max_length=100)

    @model_validator(mode="after")
    def event_ids_must_be_unique(self):
        ids = [event.event_id for event in self.events]
        if len(set(ids)) != len(ids):
            raise ValueError("同一批次 event_id 必须唯一")
        return self


class OrderEventResult(BaseModel):
    event_id: UUID
    order_id: str
    status: Literal["processed", "duplicate", "rejected"]
    event_type: str
    reason: str | None = None
    data_updated_at: datetime | None = None


class OrderEventBatchResponse(BaseModel):
    results: list[OrderEventResult]
    processed: int
    duplicates: int
    rejected: int


class AgentStep(BaseModel):
    name: str = Field(..., description="工作流节点名称")
    status: Literal["success", "error"] = Field("success", description="节点状态")
    duration_ms: int = Field(0, ge=0, description="节点耗时（毫秒）")
    summary: str = Field(..., description="可公开的动作摘要，不包含模型思维链")


class AgentUsage(BaseModel):
    input_tokens: int | None = Field(None, ge=0)
    output_tokens: int | None = Field(None, ge=0)
    total_tokens: int | None = Field(None, ge=0)
    latency_ms: int = Field(0, ge=0)


class AgentSource(BaseModel):
    filename: str
    section: str = ""
    doc_type: str = ""
    score: float = Field(0, ge=0)
    snippet: str = ""


class AIQueryResponse(BaseModel):
    sql: str | None = Field(None, description="生成的SQL语句")
    result: list[dict[str, Any]] = Field(default_factory=list, description="查询结果")
    answer: str = Field(..., description="AI回答文本")
    visualization: dict[str, Any] | None = Field(None, description="可视化配置")
    sql_error: str | None = Field(None, description="SQL执行失败时的错误说明；为空表示执行成功或未执行")
    request_id: str | None = Field(None, description="本次 Agent 请求标识")
    thread_id: str | None = Field(None, description="多轮会话标识")
    intent: Literal["data", "knowledge", "hybrid", "clarification", "blocked"] | None = None
    sources: list[AgentSource] = Field(default_factory=list, description="RAG 引用来源")
    steps: list[AgentStep] = Field(default_factory=list, description="可公开的执行轨迹")
    usage: AgentUsage | None = Field(None, description="模型 Token 与端到端耗时")


class ErrorResponse(BaseModel):
    error_code: str = Field(..., description="错误码")
    message: str = Field(..., description="错误信息")
    detail: str | None = Field(None, description="详细错误信息")
