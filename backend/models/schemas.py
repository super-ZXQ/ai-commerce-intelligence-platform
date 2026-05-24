from datetime import date, datetime
from typing import Optional, Any
from pydantic import BaseModel, Field


class PaginationParams(BaseModel):
    page: int = Field(1, ge=1, description="页码")
    page_size: int = Field(20, ge=1, le=100, description="每页数量")


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
    channel_id: Optional[str] = Field(None, description="渠道编号")
    platform_type: str = Field(..., description="平台类型")
    order_time: datetime = Field(..., description="下单时间")
    payment_time: Optional[datetime] = Field(None, description="付款时间")
    is_refunded: str = Field("否", description="是否退款")
    discount_amount: Optional[float] = Field(0.0, description="优惠金额")
    payment_duration_sec: Optional[float] = Field(None, description="支付耗时(秒)")
    order_date: Optional[date] = Field(None, description="下单日期")
    order_hour: Optional[int] = Field(None, description="下单小时")
    weekday: Optional[str] = Field(None, description="星期几")

    model_config = {"from_attributes": True}


class OrderFilterParams(BaseModel):
    start_date: Optional[date] = Field(None, description="开始日期")
    end_date: Optional[date] = Field(None, description="结束日期")
    platform_type: Optional[str] = Field(None, description="平台类型")
    user_name: Optional[str] = Field(None, description="用户名")
    product_id: Optional[str] = Field(None, description="商品编号")
    is_refunded: Optional[str] = Field(None, description="是否退款")
    min_amount: Optional[float] = Field(None, ge=0, description="最小金额")
    max_amount: Optional[float] = Field(None, ge=0, description="最大金额")
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


class AIQueryResponse(BaseModel):
    sql: Optional[str] = Field(None, description="生成的SQL语句")
    result: list[dict[str, Any]] = Field(default_factory=list, description="查询结果")
    answer: str = Field(..., description="AI回答文本")
    visualization: Optional[dict[str, Any]] = Field(None, description="可视化配置")


class ErrorResponse(BaseModel):
    error_code: str = Field(..., description="错误码")
    message: str = Field(..., description="错误信息")
    detail: Optional[str] = Field(None, description="详细错误信息")
