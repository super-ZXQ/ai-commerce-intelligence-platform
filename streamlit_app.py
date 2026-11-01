# RFM 分层页面 v3.0 | 符合 CDP 数据产品规范
import os
from datetime import UTC, datetime

import numpy as np
import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st

from backend.utils.rfm_scoring import assign_segment, quantile_scores
from bi_api import BiApiClient, BiApiError, BiAuthError, BiPermissionError
from bi_style import BI_DASHBOARD_CSS

# ══════════════════════════════════════════════════════════════
# 全局样式
# ══════════════════════════════════════════════════════════════
_PLOTLY_LAYOUT_DEFAULTS = {
    "template": "plotly_white",
    "font": {
        "family": "Manrope, Noto Sans SC, Microsoft YaHei, sans-serif",
        "size": 13,
        "color": "#334155",
    },
    "paper_bgcolor": "rgba(0,0,0,0)",
    "plot_bgcolor": "#FFFFFF",
    "colorway": ["#1565C0", "#14B8A6", "#F97316", "#8B5CF6", "#64748B"],
    "hoverlabel": {
        "bgcolor": "#0F172A",
        "bordercolor": "#0F172A",
        "font": {
            "family": "Manrope, Noto Sans SC, Microsoft YaHei, sans-serif",
            "size": 12,
            "color": "#F8FAFC",
        },
    },
    "xaxis": {
        "gridcolor": "#E2E8F0",
        "zerolinecolor": "#CBD5E1",
        "linecolor": "#CBD5E1",
        "ticks": "outside",
        "tickfont": {"size": 11, "color": "#64748B"},
    },
    "yaxis": {
        "gridcolor": "#E2E8F0",
        "zerolinecolor": "#CBD5E1",
        "linecolor": "#CBD5E1",
        "ticks": "outside",
        "tickfont": {"size": 11, "color": "#64748B"},
    },
}

# 非 RFM 图表的受控色板；RFM 图表继续使用下方既有八分层色。
_CHART_SEQUENCE = ["#1565C0", "#14B8A6", "#F97316", "#8B5CF6", "#64748B"]
_BLUE_SCALE = [[0, "#EFF6FF"], [0.45, "#93C5FD"], [1, "#1565C0"]]
_TEAL_SCALE = [[0, "#F0FDFA"], [0.45, "#5EEAD4"], [1, "#0F766E"]]

# 企业标准色
ENTERPRISE_COLORS = {
    "重要保持客户": "#1E88E5",
    "重要挽留客户": "#FF5722",
    "一般保持客户": "#43A047",
    "一般挽留客户": "#7E57C2",
    "其他": "#BDBDBD",
}
ENTERPRISE_COLORS_FULL = {
    "重要价值客户": "#1565C0",
    "重要发展客户": "#1E88E5",
    "重要保持客户": "#1E88E5",
    "重要挽留客户": "#FF5722",
    "一般价值客户": "#43A047",
    "一般发展客户": "#43A047",
    "一般保持客户": "#43A047",
    "一般挽留客户": "#7E57C2",
}

def _hex_to_rgba(hex_color: str, alpha: float = 0.8) -> str:
    h = hex_color.lstrip('#')
    r, g, b = int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
    return f"rgba({r},{g},{b},{alpha})"


def _format_currency_cn(amount: float) -> str:
    """将大额金额转换为中文业务读法，避免指标卡溢出。"""
    if abs(amount) >= 100_000_000:
        return f"{amount / 100_000_000:.2f} 亿元"
    if abs(amount) >= 10_000:
        return f"{amount / 10_000:.2f} 万元"
    return f"{amount:,.2f} 元"


st.set_page_config(
    page_title="AI Commerce Intelligence Platform",
    layout="wide",
    initial_sidebar_state="expanded"
)

st.markdown(BI_DASHBOARD_CSS, unsafe_allow_html=True)

# ══════════════════════════════════════════════════════════════
# 后端会话与数据加载
# ══════════════════════════════════════════════════════════════
BACKEND_API_URL = os.environ.get('BACKEND_API_URL', 'http://localhost:8000').rstrip('/')
AUTH_COOKIE_NAME = os.environ.get('AUTH_COOKIE_NAME', 'ea_session')
LOGIN_URL = os.environ.get('LOGIN_URL', 'http://localhost:8000/login')

_BI_COLUMNS = {
    'order_no': '订单号',
    'order_time': '下单时间',
    'payment_time': '付款时间',
    'payment_amount': '付款金额',
    'user_name': '用户名',
    'product_id': '商品编号',
    'platform_type': '平台类型',
}


def _session_token() -> str:
    """从浏览器请求读取 HttpOnly 会话 Cookie；本进程不签发、不保存身份。"""
    try:
        return (st.context.cookies.get(AUTH_COOKIE_NAME) or '').strip()
    except Exception:
        return ''


def _bi_client() -> BiApiClient:
    return BiApiClient(BACKEND_API_URL, AUTH_COOKIE_NAME, _session_token())


def _render_login_gate() -> None:
    st.warning('需要登录后才能访问 BI 看板。')
    st.link_button('前往登录', LOGIN_URL)


def _require_session() -> dict:
    """每次脚本运行都向后端确认账号状态与权限，不依赖 Nginx 入口认证。"""
    if not _session_token():
        _render_login_gate()
        st.stop()
    try:
        session = _bi_client().verify_session()
    except BiApiError as exc:
        st.error(f'后端不可用：{exc.message}')
        st.stop()
    if session is None:
        _render_login_gate()
        st.stop()
    if 'data:read' not in set(session.get('scopes') or []):
        st.error('当前账号缺少 data:read 权限，无法查看经营数据。')
        st.stop()
    return session


bi_session = _require_session()
_bi_scopes = set(bi_session.get('scopes') or [])
export_allowed = 'data:export' in _bi_scopes


@st.cache_data(ttl=30, show_spinner="正在加载数据，请稍候...")
def load_data(refresh_token: int = 0) -> tuple[pd.DataFrame, str]:
    """经后端读取非取消订单；refresh_token 只用于手动刷新时失效页面缓存。"""
    rows = _bi_client().fetch_orders()
    if not rows:
        raise RuntimeError('业务库暂无订单数据')
    frame = pd.DataFrame(rows).rename(columns=_BI_COLUMNS)
    frame['下单时间'] = pd.to_datetime(frame['下单时间'])
    frame['付款时间'] = pd.to_datetime(frame['付款时间'])
    frame['付款金额'] = frame['付款金额'].astype(float)
    return frame, '后端 API（非取消订单）'


@st.cache_data(ttl=30, show_spinner=False)
def load_rfm_export(
    segment: str,
    reference_date: str,
    n_bins: int,
    r_threshold: int,
    f_threshold: int,
    m_threshold: int,
    refresh_token: int = 0,
) -> bytes:
    """导出由后端重新分层并生成；页面只负责触发与展示下载。"""
    return _bi_client().fetch_rfm_export(
        segment=segment,
        reference_date=reference_date,
        n_bins=n_bins,
        r_threshold=r_threshold,
        f_threshold=f_threshold,
        m_threshold=m_threshold,
        limit=200,
    )


page = st.sidebar.selectbox("选择看板页面", ["销售总览", "RFM 客户分层"])
st.sidebar.caption(f"登录账号：{bi_session.get('username', '')}")
if st.sidebar.button("刷新数据", help="重新经后端读取数据；页面缓存最长 30 秒"):
    st.session_state['data_refresh_token'] = st.session_state.get('data_refresh_token', 0) + 1
    load_data.clear()
    st.rerun()

with st.spinner("正在加载数据..."):
    try:
        df, data_source = load_data(st.session_state.get('data_refresh_token', 0))
        st.success(f"成功加载 {len(df):,} 条订单数据（来源：{data_source}）")
    except BiAuthError:
        _render_login_gate()
        st.stop()
    except BiPermissionError as exc:
        st.error(f"当前账号无权读取数据：{exc.message}")
        st.stop()
    except BiApiError as exc:
        st.error(f"后端数据加载失败：{exc.message}")
        st.stop()
    except Exception as e:
        st.error(f"数据加载失败：{e}")
        st.stop()

# ══════════════════════════════════════════════════════════════
# 销售总览
# ══════════════════════════════════════════════════════════════
if page == "销售总览":
    st.title("AI 智能商业分析平台")
    st.markdown("---")

    st.sidebar.header("筛选条件")
    platforms = st.sidebar.multiselect("选择平台", options=df['平台类型'].unique(), default=df['平台类型'].unique())
    min_date = df['下单时间'].min().date()
    max_date = df['下单时间'].max().date()
    date_range = st.sidebar.date_input("选择日期范围", value=(min_date, max_date), min_value=min_date, max_value=max_date)

    if len(date_range) == 2:
        start_date, end_date = date_range
        mask = (df['平台类型'].isin(platforms)) & (df['下单时间'].dt.date >= start_date) & (df['下单时间'].dt.date <= end_date)
        filtered_df = df[mask].copy()
    else:
        filtered_df = df[df['平台类型'].isin(platforms)].copy()

    st.subheader("核心指标")
    col1, col2, col3, col4 = st.columns(4)
    total_sales = filtered_df['付款金额'].sum()
    total_orders = filtered_df['订单号'].nunique()
    total_users = filtered_df['用户名'].nunique()
    avg_order_value = total_sales / total_orders if total_orders > 0 else 0

    # 提前计算各派生量，添加空数据保护
    unique_days = filtered_df['下单时间'].dt.date.unique()
    n_unique_days = len(unique_days)
    daily_avg_sales = total_sales / n_unique_days if n_unique_days > 0 else 0
    overall_unique_users = df['用户名'].nunique()
    overall_avg = df['付款金额'].mean() if len(df) > 0 else 0

    col1.metric(label="总销售额", value=_format_currency_cn(total_sales), delta=f"{daily_avg_sales:,.0f} 元/天")
    col2.metric(label="总订单数", value=f"{total_orders:,}", delta=f"平均每用户 {total_orders/total_users:.1f} 单" if total_users > 0 else "0")
    col3.metric(label="活跃用户", value=f"{total_users:,}", delta=f"占比 {total_users/overall_unique_users*100:.1f}%" if overall_unique_users > 0 else "0%")
    col4.metric(label="客单价", value=f"{avg_order_value:.2f} 元", delta="较总体" + ("↑" if avg_order_value > overall_avg else "↓"))

    st.markdown("---")
    st.subheader("每日销售趋势")
    col_chart1, col_chart2 = st.columns([2, 1])

    with col_chart1:
        daily_sales = filtered_df.groupby(filtered_df['下单时间'].dt.date)['付款金额'].sum().reset_index()
        daily_sales.columns = ['日期', '销售额']
        fig_line = px.line(daily_sales, x='日期', y='销售额', markers=True, title="每日销售额变化趋势")
        fig_line.update_traces(line={"width": 3, "color": "#1565C0"}, marker={"size": 6})
        fig_line.update_layout(**_PLOTLY_LAYOUT_DEFAULTS, hovermode='x unified')
        st.plotly_chart(fig_line, width='stretch')

    with col_chart2:
        platform_sales = filtered_df.groupby('平台类型')['付款金额'].sum().reset_index()
        fig_pie = px.pie(platform_sales, values='付款金额', names='平台类型', title="平台销售占比", hole=0.4, color_discrete_sequence=_CHART_SEQUENCE)
        fig_pie.update_traces(textposition='inside', textinfo='percent+label')
        fig_pie.update_layout(**_PLOTLY_LAYOUT_DEFAULTS)
        st.plotly_chart(fig_pie, width='stretch')

    st.subheader("商品销售额 TOP10")
    top_products = filtered_df.groupby('商品编号')['付款金额'].sum().nlargest(10).reset_index()
    fig_bar = px.bar(top_products, x='商品编号', y='付款金额', title="销售额 TOP10 商品", color='付款金额', color_continuous_scale=_BLUE_SCALE)
    fig_bar.update_layout(**_PLOTLY_LAYOUT_DEFAULTS)
    fig_bar.update_layout(xaxis_title="商品编号", yaxis_title="销售额 (元)")
    fig_bar.update_traces(texttemplate='%{y:.0f}', textposition='outside')
    st.plotly_chart(fig_bar, width='stretch')

    st.subheader("用户消费 TOP10")
    top_users = filtered_df.groupby('用户名')['付款金额'].sum().nlargest(10).reset_index()
    fig_user = px.bar(top_users, x='付款金额', y='用户名', orientation='h', title="消费金额 TOP10 用户", color='付款金额', color_continuous_scale=_TEAL_SCALE)
    fig_user.update_layout(**_PLOTLY_LAYOUT_DEFAULTS)
    fig_user.update_layout(xaxis_title="消费金额 (元)", yaxis_title="用户")
    fig_user.update_traces(texttemplate='%{x:.0f}', textposition='outside')
    st.plotly_chart(fig_user, width='stretch')

    st.markdown("---")
    st.caption(f"数据更新时间：{datetime.now(UTC).strftime('%Y-%m-%d %H:%M:%S')} | 数据来源：{data_source} | AI Commerce Intelligence Platform v1.0.3")

# ══════════════════════════════════════════════════════════════
# RFM 分层分析
# ══════════════════════════════════════════════════════════════
elif page == "RFM 客户分层":

    # ── 侧边栏参数 ──
    st.sidebar.header("RFM 参数设置")
    n_bins = st.sidebar.slider("分位数分组数", min_value=3, max_value=10, value=5, step=1)
    ref_date_input = st.sidebar.date_input(
        "参考日期",
        value=df['下单时间'].max().date(),
        min_value=df['下单时间'].min().date(),
        max_value=df['下单时间'].max().date(),
    )
    r_threshold = st.sidebar.slider("R 评分阈值（≤此值为高）", min_value=2, max_value=n_bins, value=3, step=1,
                                    help="R评分 ≤ 此值视为近期活跃")
    f_threshold = st.sidebar.slider("F 评分阈值（≥此值为高）", min_value=2, max_value=n_bins, value=3, step=1,
                                    help="F评分 ≥ 此值视为高频消费")
    m_threshold = st.sidebar.slider("M 评分阈值（≥此值为高）", min_value=2, max_value=n_bins, value=3, step=1,
                                    help="M评分 ≥ 此值视为高消费金额")

    # ── RFM计算 ──
    paid_df = df[
        (df['付款金额'] > 0)
        & (df['下单时间'].dt.date <= ref_date_input)
    ].copy()
    if len(paid_df) == 0:
        st.warning("当前筛选条件下无有效付款数据，无法进行 RFM 分析。")
        st.stop()

    @st.cache_data(show_spinner="正在计算RFM指标...")
    def _score_rfm_frame(_paid_df, ref_date, n_bins_val, data_version):
        """对已过滤的付款订单 DataFrame 计算 R/F/M 评分。

        _paid_df 加下划线前缀使 st.cache_data 跳过其哈希；data_version
        显式参与缓存键，保证订单数据变化后重新计算。
        """
        rfm = _paid_df.groupby('用户名').agg(
            last_order_date=('下单时间', 'max'),
            frequency=('订单号', 'nunique'),
            monetary=('付款金额', 'sum')
        ).reset_index()
        rfm['recency_days'] = (pd.Timestamp(ref_date) - rfm['last_order_date']).dt.days

        rfm['r_score'] = quantile_scores(
            rfm['recency_days'].tolist(),
            n_bins_val,
        )
        rfm['f_score'] = quantile_scores(
            rfm['frequency'].tolist(),
            n_bins_val,
        )
        rfm['m_score'] = quantile_scores(
            rfm['monetary'].tolist(),
            n_bins_val,
        )
        return rfm

    _user_stats = paid_df.groupby('用户名').agg(
        order_count=('订单号', 'nunique'),
        total_amount=('付款金额', 'sum'),
        latest_order_time=('下单时间', 'max'),
    ).reset_index()
    _cache_key = hash((
        tuple(paid_df['用户名'].values),
        tuple(_user_stats['order_count'].values),
        tuple(_user_stats['total_amount'].round(2).values),
        # R 值依赖最近下单时间，日期变化也必须触发重新计算。
        tuple(_user_stats['latest_order_time'].astype('int64').values),
    ))
    rfm = _score_rfm_frame(paid_df, ref_date_input, n_bins, _cache_key)

    if '平台类型' in paid_df.columns:
        user_platform = paid_df.groupby('用户名')['平台类型'].agg(
            lambda x: x.mode().iloc[0] if len(x.mode()) > 0 else x.iloc[0]
        ).reset_index().rename(columns={'平台类型': '平台'})
        rfm = rfm.merge(user_platform, on='用户名', how='left')

    # ── 分层逻辑（R≤阈值=高，F/M≥阈值=高）──
    rfm['客户分层'] = rfm.apply(
        lambda row: assign_segment(
            row['r_score'],
            row['f_score'],
            row['m_score'],
            r_threshold,
            f_threshold,
            m_threshold,
        ),
        axis=1,
    ).astype(str)

    seg_order = ["重要价值客户", "重要发展客户", "重要保持客户", "重要挽留客户",
                 "一般价值客户", "一般发展客户", "一般保持客户", "一般挽留客户"]

    # 四大核心分层（高管视角聚焦）
    core_segments = ["重要保持客户", "重要挽留客户", "一般保持客户", "一般挽留客户"]

    total_users = len(rfm)
    total_gmv = rfm['monetary'].sum() if total_users > 0 else 0

    # ── 页面标题 ──
    st.title("RFM 客户分层分析")
    st.caption(f"数据截止：{ref_date_input} | 分组数：{n_bins} | R阈值≤{r_threshold} F/M阈值≥{f_threshold} | 共 {total_users:,} 位客户 | 总GMV {total_gmv:,.0f} 元")
    st.markdown("---")

    # ══════════════════════════════════════════════════════
    # 三个Tab
    # ══════════════════════════════════════════════════════
    tab1, tab2, tab3 = st.tabs(["分层概览", "价值矩阵", "群体洞察"])

    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    # Tab1: 分层概览（高管视角）
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    with tab1:
        # ── KPI 指标卡 ──
        # 业务定义：高价值客户 = 全部"重要"前缀的 4 个分层（近期活跃 + 任一F/M高）
        # 与 RFM 标准术语对齐：包括 重要价值/发展/保持/挽留 客户
        high_value_segments = ["重要价值客户", "重要发展客户", "重要保持客户", "重要挽留客户"]
        high_value_mask = rfm['客户分层'].astype(str).isin(high_value_segments)
        high_value_count = int(high_value_mask.sum())
        high_value_pct = high_value_count / total_users * 100 if total_users > 0 else 0

        risk_mask = rfm['客户分层'].astype(str).isin(["一般挽留客户"])
        risk_count = int(risk_mask.sum())
        risk_pct = risk_count / total_users * 100 if total_users > 0 else 0

        high_value_gmv = rfm.loc[high_value_mask, 'monetary'].sum()
        high_value_gmv_pct = high_value_gmv / total_gmv * 100 if total_gmv > 0 else 0

        k1, k2, k3 = st.columns(3)
        with k1:
            st.metric(
                "高价值客户占比",
                f"{high_value_pct:.1f}%",
                delta=f"目标 ≥30%  {'达标' if high_value_pct >= 30 else '未达标'}",
            )
            st.caption("重要价值 + 重要发展 + 重要保持 + 重要挽留")
        with k2:
            st.metric(
                "流失风险客户",
                f"{risk_count:,}",
                delta=f"占比 {risk_pct:.1f}%  {'预警' if risk_pct > 50 else '可控'}",
            )
            st.caption("一般挽留客户（R低/F低/M低）")
        with k3:
            st.metric(
                "高价值GMV贡献率",
                f"{high_value_gmv_pct:.1f}%",
                delta=f"基准 ≥60%  {'达标' if high_value_gmv_pct >= 60 else '未达标'}",
            )
            st.caption(f"高价值客户贡献 {high_value_gmv:,.0f} 元")

        st.markdown("---")

        # ── 分群完整性检测 ──
        all_seg_counts = rfm['客户分层'].astype(str).value_counts()
        empty_segments = [s for s in seg_order if all_seg_counts.get(s, 0) == 0]
        if empty_segments:
            avg_freq = rfm['frequency'].mean()
            suggest_f = min(3, f_threshold - 1) if avg_freq < 2 else f_threshold
            st.warning(
                f"当前参数下 **{len(empty_segments)} 个分群为空**："
                f" {', '.join(empty_segments)}\n\n"
                f"建议调整：当前数据平均消费频次仅 **{avg_freq:.1f}次/人**，"
                f"F阈值建议降至 **{suggest_f}** 或更低，以激活更多分群。"
                f"可在左侧面板调整「F评分阈值」。"
            )

        # ── 环形图 + 分层明细表 ──
        st.subheader("客户资产结构")
        col_ring, col_table = st.columns([2, 3])

        with col_ring:
            seg_summary = rfm.groupby('客户分层').agg(
                人数=('用户名', 'count'),
                总GMV=('monetary', 'sum'),
            ).reset_index()

            # 核心分层 vs 其他
            seg_summary['分组'] = seg_summary['客户分层'].apply(
                lambda x: x if x in core_segments else "其他"
            )
            ring_data = seg_summary.groupby('分组').agg(
                人数=('人数', 'sum'),
                总GMV=('总GMV', 'sum'),
            ).reset_index()
            ring_data['占比'] = ring_data['人数'] / ring_data['人数'].sum() * 100

            ring_colors = [ENTERPRISE_COLORS.get(s, "#BDBDBD") for s in ring_data['分组']]

            fig_ring = go.Figure(data=[go.Pie(
                values=ring_data['人数'].tolist(),
                labels=ring_data['分组'].tolist(),
                hole=0.55,
                marker={"colors": ring_colors, "line": {"color": 'white', "width": 2}},
                textinfo='percent+label',
                textfont={"size": 13, "color": '#334155'},
                textposition='outside',
                pull=[0.05 if i == ring_data['人数'].idxmax() else 0 for i in range(len(ring_data))],
                hovertemplate='<b>%{label}</b><br>人数: %{value:,}<br>占比: %{percent}<extra></extra>',
                rotation=-90,
            )])
            fig_ring.add_annotation(
                text=f"<b>{total_users:,}</b><br>客户总数",
                x=0.5, y=0.5, font={"size": 18, "family": "Manrope, Noto Sans SC, Microsoft YaHei, sans-serif", "color": "#0F172A", "weight": "bold"},
                showarrow=False
            )
            fig_ring.update_layout(
                **_PLOTLY_LAYOUT_DEFAULTS,
                showlegend=False,
                uniformtext_minsize=10,
                uniformtext_mode='hide',
                margin={"t": 30, "b": 60, "l": 20, "r": 20},
                height=400,
            )
            st.plotly_chart(fig_ring, width='stretch')

        with col_table:
            table_data = rfm.groupby('客户分层').agg(
                人数=('用户名', 'count'),
                总GMV=('monetary', 'sum'),
                人均GMV=('monetary', 'mean'),
                平均R=('recency_days', 'mean'),
                平均F=('frequency', 'mean'),
            ).reset_index()
            table_data['占比'] = table_data['人数'] / total_users * 100
            table_data = table_data.sort_values('人数', ascending=False)

            table_data['人数'] = table_data['人数'].apply(lambda x: f"{x:,}")
            table_data['占比'] = table_data['占比'].apply(lambda x: f"{x:.1f}%")
            table_data['人均GMV'] = table_data['人均GMV'].apply(lambda x: f"{x:,.0f} 元")
            table_data['总GMV'] = table_data['总GMV'].apply(lambda x: f"{x:,.0f} 元")
            table_data['平均R'] = table_data['平均R'].apply(lambda x: f"{x:.0f}天")
            table_data['平均F'] = table_data['平均F'].apply(lambda x: f"{x:.1f}次")

            table_data = table_data.rename(columns={
                '客户分层': '客户分层', '人数': '人数', '占比': '占比',
                '人均GMV': '人均GMV', '平均R': '平均R(天)', '平均F': '平均F(次)', '总GMV': '总GMV'
            })

            st.dataframe(
                table_data[['客户分层', '人数', '占比', '人均GMV', '平均R(天)', '平均F(次)', '总GMV']],
                width='stretch', hide_index=True,
            )

        st.markdown("---")

        # ── RFM 分层规则说明 ──
        with st.expander("RFM 分层规则说明", expanded=True):
            st.markdown(f"""
| 客户分层 | R条件 | F条件 | M条件 | 业务定义 | 运营策略 |
|---------|-------|-------|-------|---------|---------|
| **重要价值客户** | R≤{r_threshold} | F≥{f_threshold} | M≥{m_threshold} | 近期活跃+高频+高额 | VIP专属权益、新品优先体验 |
| **重要发展客户** | R≤{r_threshold} | F≥{f_threshold} | M＜{m_threshold} | 近期活跃+高频+低额 | 提升客单价、组合推荐 |
| **重要保持客户** | R≤{r_threshold} | F＜{f_threshold} | M≥{m_threshold} | 近期活跃+低频+高额 | 提高复购率、定期推送 |
| **重要挽留客户** | R≤{r_threshold} | F＜{f_threshold} | M＜{m_threshold} | 近期活跃+低频+低额 | 促活转化、限时优惠 |
| **一般价值客户** | R＞{r_threshold} | F≥{f_threshold} | M≥{m_threshold} | 较久未购+高频+高额 | 召回激活、专属回归礼 |
| **一般发展客户** | R＞{r_threshold} | F≥{f_threshold} | M＜{m_threshold} | 较久未购+高频+低额 | 唤醒提醒、小额优惠券 |
| **一般保持客户** | R＞{r_threshold} | F＜{f_threshold} | M≥{m_threshold} | 较久未购+低频+高额 | 重点召回、大额优惠 |
| **一般挽留客户** | R＞{r_threshold} | F＜{f_threshold} | M＜{m_threshold} | 较久未购+低频+低额 | 自动化触达、成本控制 |
            """)
            st.info("R评分越小代表越近期消费（越优），F/M评分越大代表消费频次/金额越高（越优）。分层阈值可在左侧参数面板实时调整。")

    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    # Tab2: 价值矩阵（运营视角）
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    with tab2:
        col_matrix, col_ctrl = st.columns([4, 1])

        # ── 右侧控制面板 ──
        with col_ctrl:
            st.markdown("##### 控制面板")
            filter_platform = []
            if '平台' in rfm.columns:
                platform_options = sorted(rfm['平台'].dropna().unique().tolist())
                filter_platform = st.multiselect("平台筛选", options=platform_options, default=platform_options, key="matrix_platform")

            r_range = st.slider("R 评分范围", 1, n_bins, (1, n_bins), key="matrix_r")
            if st.button("重置筛选", key="reset_matrix"):
                # multiselect/slider 的值由 session_state 持久化，仅 rerun 不会复原；
                # 必须先移除对应的 state key，rerun 后控件才会回到默认值
                st.session_state.pop("matrix_platform", None)
                st.session_state.pop("matrix_r", None)
                st.rerun()

            st.markdown("---")
        with col_matrix:
            rfm_m = rfm.copy()
            if filter_platform and '平台' in rfm_m.columns:
                rfm_m = rfm_m[rfm_m['平台'].isin(filter_platform)]
            rfm_m = rfm_m[rfm_m['r_score'].between(r_range[0], r_range[1])]
            with col_ctrl:
                st.metric("当前客户数", f"{len(rfm_m):,}")

            # ── 预聚合 F×M ──
            all_f = list(range(1, n_bins + 1))
            all_m = list(range(1, n_bins + 1))

            fm_agg = rfm_m.groupby(['f_score', 'm_score']).agg(
                客户数=('用户名', 'count'),
                总GMV=('monetary', 'sum'),
                平均R=('recency_days', 'mean'),
                主要分层=('客户分层', lambda x: x.mode().iloc[0] if len(x.mode()) > 0 else x.iloc[0]),
            ).reset_index()

            full_idx = pd.MultiIndex.from_product([all_f, all_m], names=['f_score', 'm_score'])
            fm_agg = fm_agg.set_index(['f_score', 'm_score']).reindex(full_idx).reset_index()
            fm_agg['客户数'] = fm_agg['客户数'].fillna(0).astype(int)
            fm_agg['总GMV'] = fm_agg['总GMV'].fillna(0)
            fm_agg['平均R'] = fm_agg['平均R'].fillna(0)
            fm_agg['主要分层'] = fm_agg['主要分层'].fillna('其他')

            # ── 主图：F×M 价值矩阵 ──
            st.markdown("##### F×M 价值矩阵")

            # 热力底图
            fm_pivot = fm_agg.pivot(index='m_score', columns='f_score', values='客户数').fillna(0)
            for c in all_f:
                if c not in fm_pivot.columns:
                    fm_pivot[c] = 0
            for r in all_m:
                if r not in fm_pivot.index:
                    fm_pivot.loc[r] = 0
            fm_pivot = fm_pivot.sort_index(axis=0).sort_index(axis=1)

            fig_matrix = go.Figure()

            z_display = fm_pivot.values.copy()
            text_annotations = []
            for i, r_val in enumerate(fm_pivot.index):
                for j, c_val in enumerate(fm_pivot.columns):
                    if z_display[i, j] == 0:
                        text_annotations.append({
                            "x": f'F={c_val}', "y": f'M={r_val}',
                            "text": '—', "showarrow": False,
                            "font": {"size": 14, "color": '#475569'},
                        })

            fig_matrix.add_trace(go.Heatmap(
                z=z_display,
                x=[f'F={c}' for c in fm_pivot.columns],
                y=[f'M={r}' for r in fm_pivot.index],
                colorscale=_BLUE_SCALE,
                zmin=0, zmax=max(1, fm_pivot.values.max()),
                colorbar={"title": "客户规模", "len": 0.45, "y": 0.5, "thickness": 15},
                hovertemplate='F=%{x}, M=%{y}<br>客户数: %{z:,.0f}<extra></extra>',
                showscale=True,
                xgap=3, ygap=3,
            ))

            for ann in text_annotations:
                fig_matrix.add_annotation(**ann)

            # 气泡叠加：按segment着色
            bubble = fm_agg[fm_agg['客户数'] > 0].copy()
            if len(bubble) > 0:
                max_gmv = bubble['总GMV'].max() if bubble['总GMV'].max() > 0 else 1
                bubble['气泡大小'] = np.log1p(bubble['总GMV']) / np.log1p(max_gmv) * 45 + 5
                bubble['颜色'] = bubble['主要分层'].map(ENTERPRISE_COLORS_FULL).fillna("#BDBDBD")

                bubble['hover'] = bubble.apply(lambda row: (
                    f"F={int(row['f_score'])}, M={int(row['m_score'])}<br>"
                    f"客户数: {int(row['客户数']):,}<br>"
                    f"GMV: {row['总GMV']:,.0f} 元<br>"
                    f"平均R: {row['平均R']:.1f}天<br>"
                    f"分层: {row['主要分层']}"
                ), axis=1)

                fig_matrix.add_trace(go.Scatter(
                    x=['F=' + str(int(f)) for f in bubble['f_score']],
                    y=['M=' + str(int(m)) for m in bubble['m_score']],
                    mode='markers',
                    marker={
                        "size": bubble['气泡大小'].values,
                        "color": bubble['颜色'].values,
                        "opacity": 0.7,
                        "line": {"width": 1, "color": 'white'},
                    },
                    hovertext=bubble['hover'],
                    hoverinfo='text',
                    showlegend=False,
                ))

                # 标注分层标签（仅核心分层格子）
                for _, row in bubble.iterrows():
                    f_v, m_v = int(row['f_score']), int(row['m_score'])
                    seg = row['主要分层']
                    # 仅在符合分层条件的格子标注
                    should_label = False
                    if seg == "重要保持客户" and f_v < f_threshold and m_v >= m_threshold or seg == "重要挽留客户" and f_v < f_threshold and m_v < m_threshold or seg == "一般保持客户" and m_v >= m_threshold or seg == "一般挽留客户" and f_v < f_threshold and m_v < m_threshold:
                        should_label = True

                    if should_label and int(row['客户数']) > 0:
                        short_name = seg.replace('客户', '')
                        fig_matrix.add_annotation(
                            x=f'F={f_v}', y=f'M={m_v}',
                            text=f"<b>{short_name}</b>",
                            showarrow=False,
                            font={"size": 10, "color": "white", "family": "Manrope, Noto Sans SC, Microsoft YaHei, sans-serif"},
                            bgcolor=_hex_to_rgba(ENTERPRISE_COLORS_FULL.get(seg, "#999"), 0.8),
                            borderpad=2,
                        )

            fig_matrix.update_layout(
                **_PLOTLY_LAYOUT_DEFAULTS,
                xaxis_title="Frequency 评分（越高=消费越频繁）",
                yaxis_title="Monetary 评分（越高=消费金额越大）",
                margin={"t": 40, "b": 50, "l": 60, "r": 60},
                height=480,
            )
            st.plotly_chart(fig_matrix, width='stretch')

            st.markdown("---")

            # ── 辅助图：R评分分布堆叠柱状图 ──
            st.markdown("##### R 评分分布（按分层堆叠）")

            r_seg = rfm_m.groupby(['r_score', '客户分层']).size().reset_index(name='客户数')
            full_r_idx = pd.MultiIndex.from_product([all_f, seg_order], names=['r_score', '客户分层'])
            r_seg = r_seg.set_index(['r_score', '客户分层']).reindex(full_r_idx).reset_index()
            r_seg['客户数'] = r_seg['客户数'].fillna(0).astype(int)
            r_seg['r_label'] = 'R=' + r_seg['r_score'].astype(str)

            fig_r_stack = px.bar(
                r_seg, x='r_label', y='客户数', color='客户分层',
                barmode='stack',
                color_discrete_map=ENTERPRISE_COLORS_FULL,
                labels={'r_label': 'R 评分', '客户数': '客户数量', '客户分层': '分层'},
            )
            fig_r_stack.update_traces(
                hovertemplate='<b>%{data.name}</b><br>R=%{x}<br>客户数: %{y:,}<extra></extra>',
            )
            # 高价值分界线 & 流失预警线
            fig_r_stack.add_vline(
                x=r_threshold - 0.5, line_dash="dash", line_color="#1E88E5", line_width=2,
                annotation_text=f"← 高价值（R≤{r_threshold}）", annotation_position="top left",
                annotation={"font": {"size": 11, "color": "#1E88E5"}}
            )
            fig_r_stack.add_vline(
                x=r_threshold + 0.5, line_dash="dash", line_color="#FF5722", line_width=2,
                annotation_text=f"流失预警（R>{r_threshold}）→", annotation_position="top right",
                annotation={"font": {"size": 11, "color": "#FF5722"}}
            )
            fig_r_stack.update_layout(
                **_PLOTLY_LAYOUT_DEFAULTS,
                xaxis_title="R 评分（越小=越近期消费）",
                yaxis_title="客户数量",
                legend={
                    "orientation": "h", "yanchor": "bottom",
                    "y": -0.32, "xanchor": "center", "x": 0.5,
                    "font": {"size": 9},
                    "tracegroupgap": 5,
                },
                margin={"t": 40, "b": 100, "l": 50, "r": 20},
                bargap=0.15,
                height=380,
            )
            st.plotly_chart(fig_r_stack, width='stretch')

    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    # Tab3: 群体洞察（执行视角）
    # ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    with tab3:
        # ── 分层选择器（仅显示有数据的分群）──
        seg_counts = rfm['客户分层'].astype(str).value_counts()
        active_segments = [s for s in seg_order if seg_counts.get(s, 0) > 0]
        seg_options = [f"{s} ({seg_counts.get(s, 0):,}人)" for s in active_segments]
        sel_label = st.selectbox("选择查看的客户分层", options=seg_options, key="insight_seg")
        sel_segment = sel_label.split(" (")[0]

        seg_users = rfm[rfm['客户分层'].astype(str) == sel_segment].copy()
        seg_count = len(seg_users)

        if seg_count == 0:
            st.info("该分层暂无客户数据，请调整筛选条件。")
        else:
            # ── 画像指标卡 ──
            st.markdown(f"##### {sel_segment} 画像概览")

            c1, c2, c3, c4, c5 = st.columns(5)
            c1.metric("客户数", f"{seg_count:,}")
            c2.metric("占总客户", f"{seg_count/total_users*100:.1f}%")
            seg_gmv = seg_users['monetary'].sum()
            c3.metric("贡献GMV", f"{seg_gmv:,.0f} 元")
            c4.metric("人均消费", f"{seg_users['monetary'].mean():,.0f} 元")
            c5.metric("人均频次", f"{seg_users['frequency'].mean():.1f}次")

            c6, c7, c8, c9, c10 = st.columns(5)
            c6.metric("平均R评分", f"{seg_users['r_score'].mean():.1f}")
            c7.metric("平均F评分", f"{seg_users['f_score'].mean():.1f}")
            c8.metric("平均M评分", f"{seg_users['m_score'].mean():.1f}")
            c9.metric("平均消费间隔", f"{seg_users['recency_days'].mean():.0f}天")
            repurchase_rate = (seg_users['frequency'] >= 2).mean() * 100
            c10.metric("复购率", f"{repurchase_rate:.1f}%")

            st.markdown("---")

            # ── 行为趋势（按月聚合） ──
            st.markdown("##### 月度趋势")
            if '下单时间' in paid_df.columns:
                seg_orders = paid_df[paid_df['用户名'].isin(seg_users['用户名'])].copy()
                seg_orders['月份'] = seg_orders['下单时间'].dt.to_period('M').astype(str)
                monthly = seg_orders.groupby('月份').agg(
                    GMV=('付款金额', 'sum'),
                    订单数=('订单号', 'nunique'),
                    活跃人数=('用户名', 'nunique'),
                ).reset_index()

                fig_trend = go.Figure()
                fig_trend.add_trace(go.Bar(
                    x=monthly['月份'], y=monthly['GMV'],
                    name='GMV', marker_color="#1565C0", opacity=0.78,
                    yaxis='y',
                ))
                fig_trend.add_trace(go.Scatter(
                    x=monthly['月份'], y=monthly['活跃人数'],
                    name='活跃人数', mode='lines+markers',
                    line={"color": '#FF5722', "width": 2},
                    yaxis='y2',
                ))
                fig_trend.update_layout(
                    **_PLOTLY_LAYOUT_DEFAULTS,
                    title=f"{sel_segment} 月度趋势",
                    legend={"orientation": "h", "yanchor": "bottom", "y": 1.02, "xanchor": "center", "x": 0.5},
                    margin={"t": 60, "b": 40, "l": 60, "r": 60},
                    height=350,
                )
                # 默认布局已包含 yaxis；轴配置分次更新，避免关键字重复。
                fig_trend.update_layout(
                    yaxis={"title": "GMV (元)", "side": "left"},
                    yaxis2={"title": "活跃人数", "overlaying": "y", "side": "right", "showgrid": False},
                )
                st.plotly_chart(fig_trend, width='stretch')

            st.markdown("---")

            # ── 行动建议卡 ──
            # 注意：以下为通用运营打法示例，并非由本数据集统计得出；
            # 不标注虚构的"数据来源"，避免演示数据被误认为真实结论。
            st.markdown("##### 运营行动建议")

            ACTION_MAP = {
                "重要价值客户": [
                    ("VIP专属服务升级", "开放专属客服通道、新品优先试用权益"),
                    ("高价值品类推荐", "推送高毛利品类"),
                    ("会员积分加速", "双倍积分激励，提升粘性"),
                ],
                "重要发展客户": [
                    ("提升客单价组合推荐", "推送关联商品组合"),
                    ("满减门槛引导", "设置略高于当前客单价的满减门槛"),
                    ("品类交叉推荐", "根据已购品类推荐高关联品类"),
                ],
                "重要保持客户": [
                    ("复购提醒推送", "周期性推送复购提醒"),
                    ("专属客服外呼", "大客户经理主动触达，了解需求"),
                    ("定期专属优惠", "每月发放高面额专属券"),
                ],
                "重要挽留客户": [
                    ("推送高毛利品类券", "发放专属品类券"),
                    ("触发专属客服外呼", "人工外呼了解流失原因"),
                    ("排除低价引流广告", "该群体对价格敏感度低，避免低价引流"),
                ],
                "一般价值客户": [
                    ("召回激活短信", "发送专属回归礼召回"),
                    ("高价值商品推荐", "推送其历史偏好的高价值品类"),
                    ("限时回归优惠", "72小时限时大额优惠券"),
                ],
                "一般发展客户": [
                    ("唤醒提醒短信", "定期唤醒推送（频率≤2次/月）"),
                    ("小额优惠券投放", "发放小额无门槛券降低复购门槛"),
                    ("新品上新通知", "根据历史偏好推送新品"),
                ],
                "一般保持客户": [
                    ("加入会员成长体系", "引导进入积分/等级体系"),
                    ("复购提醒短信", "基于上次购买周期推送复购提醒"),
                    ("开放积分兑换权益", "积分兑换优惠券/实物"),
                ],
                "一般挽留客户": [
                    ("自动化触达", "低成本自动化营销触达"),
                    ("成本控制策略", "降低服务成本，聚焦高ROI渠道"),
                    ("沉默用户清理", "超过180天未购转入沉默库"),
                ],
            }

            actions = ACTION_MAP.get(sel_segment, [])
            if actions:
                st.caption("以下为运营打法示例，供参考；非本数据集的统计结论。")
                for title, desc in actions:
                    with st.container():
                        st.markdown(f"**{title}**")
                        st.markdown(f"    {desc}")

            st.markdown("---")

            # ── 客户明细表 ──
            st.markdown(f"##### {sel_segment} 客户明细（前200条）")

            display_cols = ['用户名', 'r_score', 'f_score', 'm_score', 'recency_days', 'frequency', 'monetary']
            display_labels = {
                '用户名': '客户ID', 'r_score': 'R评分', 'f_score': 'F评分', 'm_score': 'M评分',
                'recency_days': '最近消费(天)', 'frequency': '消费频次', 'monetary': '累计GMV(元)',
            }
            show_cols = [c for c in display_cols if c in seg_users.columns]

            detail_df = seg_users[show_cols].sort_values('monetary', ascending=False).head(200).rename(columns=display_labels)
            st.dataframe(detail_df, width='stretch', hide_index=True, height=400)

            # ── 导出CSV（由后端按 data:export 权限重新分层并生成）──
            if export_allowed:
                try:
                    csv_data = load_rfm_export(
                        sel_segment,
                        ref_date_input.isoformat(),
                        n_bins,
                        r_threshold,
                        f_threshold,
                        m_threshold,
                        st.session_state.get('data_refresh_token', 0),
                    )
                    st.download_button(
                        label="导出 CSV",
                        data=csv_data,
                        file_name=f"{sel_segment}_客户明细.csv",
                        mime="text/csv",
                    )
                    st.caption("导出由后端按当前账号权限执行，最多 200 条。")
                except BiAuthError:
                    _render_login_gate()
                except BiPermissionError:
                    st.caption("当前账号无导出权限（data:export）。")
                except BiApiError as exc:
                    st.caption(f"导出暂不可用：{exc.message}")
            else:
                st.caption("当前账号无导出权限（data:export），无法下载客户明细。")

    st.markdown("---")
    st.caption(f"数据更新时间：{datetime.now(UTC).strftime('%Y-%m-%d %H:%M:%S')} | RFM 分层页面 v3.0 | AI Commerce Intelligence Platform")

