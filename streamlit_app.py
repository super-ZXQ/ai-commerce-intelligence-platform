import streamlit as st
import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
from datetime import datetime
import os
import numpy as np

_PLOTLY_LAYOUT_DEFAULTS = dict(
    template="plotly_white",
    font=dict(family="Microsoft YaHei, PingFang SC, sans-serif", size=13),
    paper_bgcolor="rgba(0,0,0,0)",
    plot_bgcolor="rgba(0,0,0,0)",
)

st.set_page_config(
    page_title="电商数据 BI 看板",
    page_icon="🛒",
    layout="wide",
    initial_sidebar_state="expanded"
)

_REPO_ROOT = os.path.dirname(os.path.abspath(__file__))
if not os.path.exists(os.path.join(_REPO_ROOT, 'data')):
    _REPO_ROOT = os.path.dirname(_REPO_ROOT)

@st.cache_data(show_spinner="正在加载数据，请稍候...")
def load_data():
    base = os.path.join(_REPO_ROOT, 'data')
    for ext in ('.parquet', '.csv'):
        f = os.path.join(base, 'cleaned_orders' + ext)
        if os.path.exists(f):
            if ext == '.parquet':
                return pd.read_parquet(f)
            return pd.read_csv(f, parse_dates=['下单时间', '付款时间'])
    raise FileNotFoundError("未找到数据文件 data/cleaned_orders.parquet 或 .csv")

with st.spinner("⏳ 正在加载数据..."):
    try:
        df = load_data()
        st.success(f"✅ 成功加载 {len(df):,} 条订单数据")
    except Exception as e:
        st.error(f"❌ 数据加载失败：{e}")
        st.stop()

page = st.sidebar.selectbox("📋 选择看板页面", ["📊 销售总览", "👥 RFM 客户分层"])

if page == "📊 销售总览":
    st.title("🛒 电商订单数据分析看板")
    st.markdown("---")

    st.sidebar.header("🔍 筛选条件")

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

    st.subheader("📊 核心指标")
    col1, col2, col3, col4 = st.columns(4)

    total_sales = filtered_df['付款金额'].sum()
    total_orders = filtered_df['订单号'].nunique()
    total_users = filtered_df['用户名'].nunique()
    avg_order_value = total_sales / total_orders if total_orders > 0 else 0

    col1.metric(label="💰 总销售额", value=f"¥{total_sales:,.2f}", delta=f"¥{total_sales/len(filtered_df['下单时间'].dt.date.unique()):,.0f}/天")
    col2.metric(label="📦 总订单数", value=f"{total_orders:,}", delta=f"平均每用户 {total_orders/total_users:.1f} 单" if total_users > 0 else "0")
    col3.metric(label="👥 活跃用户", value=f"{total_users:,}", delta=f"占比 {total_users/df['用户名'].nunique()*100:.1f}%" if df['用户名'].nunique() > 0 else "0%")
    col4.metric(label="💵 客单价", value=f"¥{avg_order_value:.2f}", delta="较总体" + ("↑" if avg_order_value > df['付款金额'].mean() else "↓"))

    st.markdown("---")

    st.subheader("📈 每日销售趋势")
    col_chart1, col_chart2 = st.columns([2, 1])

    with col_chart1:
        daily_sales = filtered_df.groupby(filtered_df['下单时间'].dt.date)['付款金额'].sum().reset_index()
        daily_sales.columns = ['日期', '销售额']
        fig_line = px.line(daily_sales, x='日期', y='销售额', markers=True, title="每日销售额变化趋势")
        fig_line.update_traces(line=dict(width=3, color='#1f77b4'), marker=dict(size=6))
        fig_line.update_layout(**_PLOTLY_LAYOUT_DEFAULTS, hovermode='x unified')
        st.plotly_chart(fig_line, width='stretch')

    with col_chart2:
        platform_sales = filtered_df.groupby('平台类型')['付款金额'].sum().reset_index()
        fig_pie = px.pie(platform_sales, values='付款金额', names='平台类型', title="平台销售占比", hole=0.4, color_discrete_sequence=px.colors.qualitative.Set3)
        fig_pie.update_traces(textposition='inside', textinfo='percent+label')
        fig_pie.update_layout(**_PLOTLY_LAYOUT_DEFAULTS)
        st.plotly_chart(fig_pie, width='stretch')

    st.subheader("🏆 商品销售额 TOP10")
    top_products = filtered_df.groupby('商品编号')['付款金额'].sum().nlargest(10).reset_index()
    fig_bar = px.bar(top_products, x='商品编号', y='付款金额', title="销售额 TOP10 商品", color='付款金额', color_continuous_scale='Reds')
    fig_bar.update_layout(xaxis_title="商品编号", yaxis_title="销售额 (元)")
    fig_bar.update_traces(texttemplate='%{y:.0f}', textposition='outside')
    st.plotly_chart(fig_bar, width='stretch')

    st.subheader("👑 用户消费 TOP10")
    top_users = filtered_df.groupby('用户名')['付款金额'].sum().nlargest(10).reset_index()
    fig_user = px.bar(top_users, x='付款金额', y='用户名', orientation='h', title="消费金额 TOP10 用户", color='付款金额', color_continuous_scale='Viridis')
    fig_user.update_layout(xaxis_title="消费金额 (元)", yaxis_title="用户")
    fig_user.update_traces(texttemplate='%{x:.0f}', textposition='outside')
    st.plotly_chart(fig_user, width='stretch')

    st.markdown("---")
    st.caption(f"数据更新时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')} | 数据来源：cleaned_orders | 电商数据分析系统 v1.2")

elif page == "👥 RFM 客户分层":
    st.title("👥 RFM 客户分层分析")
    st.markdown("基于 **Recency（最近消费时间）**、**Frequency（消费频率）**、**Monetary（消费金额）** 三个维度，对客户进行价值分层，精准识别高价值客户与流失风险客户。")
    st.markdown("---")

    st.sidebar.header("⚙️ RFM 参数设置")

    n_bins = st.sidebar.slider("分位数分组数", min_value=3, max_value=10, value=5, step=1, help="将R/F/M各维度分为N个分位组，值越细分层越精细")

    ref_date_input = st.sidebar.date_input(
        "参考日期",
        value=df['下单时间'].max().date(),
        min_value=df['下单时间'].min().date(),
        max_value=df['下单时间'].max().date(),
        help="以该日期为基准计算Recency（距今天数）"
    )

    r_threshold = st.sidebar.slider("R评分阈值（≥此值为高）", min_value=2, max_value=n_bins, value=4, step=1, help="Recency评分≥此值视为'最近消费'")
    f_threshold = st.sidebar.slider("F评分阈值（≥此值为高）", min_value=2, max_value=n_bins, value=4, step=1, help="Frequency评分≥此值视为'高频消费'")
    m_threshold = st.sidebar.slider("M评分阈值（≥此值为高）", min_value=2, max_value=n_bins, value=4, step=1, help="Monetary评分≥此值视为'高消费金额'")

    paid_df = df[df['付款金额'] > 0].copy()

    if len(paid_df) == 0:
        st.warning("⚠️ 当前筛选条件下无有效付款数据，无法进行 RFM 分析。")
        st.stop()

    @st.cache_data(show_spinner="正在计算RFM指标...")
    def compute_rfm_data(_data_hash, ref_date, n_bins_val):
        rfm = paid_df.groupby('用户名').agg(
            last_order_date=('下单时间', 'max'),
            frequency=('订单号', 'nunique'),
            monetary=('付款金额', 'sum')
        ).reset_index()

        rfm['recency_days'] = (pd.Timestamp(ref_date) - rfm['last_order_date']).dt.days

        actual_r_bins = min(rfm['recency_days'].nunique(), n_bins_val)
        if actual_r_bins < 2:
            rfm['r_score'] = 1
        else:
            rfm['r_score'] = pd.qcut(rfm['recency_days'], q=actual_r_bins, labels=False, duplicates='drop') + 1
            rfm['r_score'] = (actual_r_bins + 1 - rfm['r_score']).astype(int)
            if actual_r_bins < n_bins_val:
                rfm['r_score'] = np.ceil(rfm['r_score'] * n_bins_val / actual_r_bins).astype(int).clip(1, n_bins_val)

        freq_for_cut = rfm['frequency'].copy()
        actual_f_bins = min(freq_for_cut.nunique(), n_bins_val)
        if actual_f_bins < 2:
            rfm['f_score'] = 1
        else:
            if freq_for_cut.nunique() < n_bins_val:
                rfm['f_score'] = pd.cut(freq_for_cut, bins=actual_f_bins, labels=False, include_lowest=True) + 1
            else:
                rfm['f_score'] = pd.qcut(freq_for_cut, q=n_bins_val, labels=False, duplicates='drop') + 1
            if actual_f_bins < n_bins_val:
                rfm['f_score'] = np.ceil(rfm['f_score'] * n_bins_val / actual_f_bins).astype(int).clip(1, n_bins_val)

        monetary_for_cut = rfm['monetary'].copy()
        actual_m_bins = min(monetary_for_cut.nunique(), n_bins_val)
        if actual_m_bins < 2:
            rfm['m_score'] = 1
        else:
            if monetary_for_cut.nunique() < n_bins_val:
                rfm['m_score'] = pd.cut(monetary_for_cut, bins=actual_m_bins, labels=False, include_lowest=True) + 1
            else:
                rfm['m_score'] = pd.qcut(monetary_for_cut, q=n_bins_val, labels=False, duplicates='drop') + 1
            if actual_m_bins < n_bins_val:
                rfm['m_score'] = np.ceil(rfm['m_score'] * n_bins_val / actual_m_bins).astype(int).clip(1, n_bins_val)

        return rfm

    # 缓存键包含用户名、订单数、总金额的哈希，确保订单变更时缓存失效
    _user_stats = paid_df.groupby('用户名').agg(
        order_count=('订单号', 'nunique'),
        total_amount=('付款金额', 'sum')
    ).reset_index()
    _cache_key = hash((
        tuple(paid_df['用户名'].values),
        tuple(_user_stats['order_count'].values),
        tuple(_user_stats['total_amount'].round(2).values),
    ))
    rfm = compute_rfm_data(_cache_key, ref_date_input, n_bins)

    def assign_segment(row, r_th, f_th, m_th):
        r_high = row['r_score'] >= r_th
        f_high = row['f_score'] >= f_th
        m_high = row['m_score'] >= m_th
        if r_high and f_high and m_high:
            return "重要价值客户"
        if r_high and f_high and not m_high:
            return "重要发展客户"
        if r_high and not f_high and m_high:
            return "重要保持客户"
        if r_high and not f_high and not m_high:
            return "重要挽留客户"
        if not r_high and f_high and m_high:
            return "一般价值客户"
        if not r_high and f_high and not m_high:
            return "一般发展客户"
        if not r_high and not f_high and m_high:
            return "一般保持客户"
        return "一般挽留客户"

    rfm['客户分层'] = rfm.apply(lambda row: assign_segment(row, r_threshold, f_threshold, m_threshold), axis=1)

    SEGMENT_COLORS = {
        "重要价值客户": "#E74C3C",
        "重要发展客户": "#E67E22",
        "重要保持客户": "#F1C40F",
        "重要挽留客户": "#3498DB",
        "一般价值客户": "#2ECC71",
        "一般发展客户": "#1ABC9C",
        "一般保持客户": "#9B59B6",
        "一般挽留客户": "#95A5A6",
    }

    SEGMENT_ICONS = {
        "重要价值客户": "💎",
        "重要发展客户": "📈",
        "重要保持客户": "🔄",
        "重要挽留客户": "⚠️",
        "一般价值客户": "🌟",
        "一般发展客户": "🌱",
        "一般保持客户": "🔵",
        "一般挽留客户": "⚪",
    }

    st.subheader("📊 RFM 核心指标概览")
    total_rfm_users = len(rfm)
    avg_recency = rfm['recency_days'].mean()
    avg_frequency = rfm['frequency'].mean()
    avg_monetary = rfm['monetary'].mean()

    col1, col2, col3, col4 = st.columns(4)
    col1.metric(label="👥 分析用户数", value=f"{total_rfm_users:,}")
    col2.metric(label="📅 平均最近消费天数", value=f"{avg_recency:.1f} 天")
    col3.metric(label="🔄 平均消费频次", value=f"{avg_frequency:.2f} 次")
    col4.metric(label="💰 平均消费金额", value=f"¥{avg_monetary:,.2f}")

    high_value = rfm[rfm['客户分层'].str.contains('重要')]
    at_risk = rfm[rfm['客户分层'].str.contains('挽留')]

    col5, col6, col7 = st.columns(3)
    col5.metric(
        label="💎 重要客户数",
        value=f"{len(high_value):,}",
        delta=f"占比 {len(high_value)/total_rfm_users*100:.1f}%"
    )
    col6.metric(
        label="⚠️ 流失风险客户数",
        value=f"{len(at_risk):,}",
        delta=f"占比 {len(at_risk)/total_rfm_users*100:.1f}%"
    )
    col7.metric(
        label="💎 重要客户贡献金额",
        value=f"¥{high_value['monetary'].sum():,.0f}",
        delta=f"占比 {high_value['monetary'].sum()/rfm['monetary'].sum()*100:.1f}%" if rfm['monetary'].sum() > 0 else "占比 0%"
    )

    st.markdown("---")

    st.subheader("🎯 客户分层分布")
    seg_counts = rfm['客户分层'].value_counts().reset_index()
    seg_counts.columns = ['客户分层', '人数']
    seg_order = ["重要价值客户", "重要发展客户", "重要保持客户", "重要挽留客户",
                 "一般价值客户", "一般发展客户", "一般保持客户", "一般挽留客户"]
    _sort_map = {name: i for i, name in enumerate(seg_order)}
    seg_counts['_sort_key'] = seg_counts['客户分层'].map(_sort_map)
    seg_counts = seg_counts.sort_values('_sort_key').drop(columns=['_sort_key'])
    seg_counts['颜色'] = seg_counts['客户分层'].astype(str).map(SEGMENT_COLORS)
    seg_counts['图标'] = seg_counts['客户分层'].astype(str).map(SEGMENT_ICONS)
    seg_counts['标签'] = seg_counts['图标'] + ' ' + seg_counts['客户分层'].astype(str)

    col_pie, col_bar = st.columns([1, 1])

    with col_pie:
        fig_pie = px.pie(
            seg_counts, values='人数', names='标签',
            title="客户分层占比",
            hole=0.45,
            color='标签',
            color_discrete_map={row['标签']: row['颜色'] for _, row in seg_counts.iterrows()}
        )
        fig_pie.update_traces(
            textposition='inside',
            textinfo='percent+label',
            textfont=dict(size=12),
            hovertemplate='<b>%{label}</b><br>人数: %{value:,}<br>占比: %{percent}<extra></extra>'
        )
        fig_pie.update_layout(
            **_PLOTLY_LAYOUT_DEFAULTS,
            showlegend=True,
            legend=dict(orientation="h", yanchor="bottom", y=-0.2, xanchor="center", x=0.5, font=dict(size=11)),
            margin=dict(t=50, b=80, l=20, r=20)
        )
        st.plotly_chart(fig_pie, width='stretch')

    with col_bar:
        fig_bar = px.bar(
            seg_counts, x='客户分层', y='人数',
            title="各分层客户人数",
            color='客户分层',
            color_discrete_map=SEGMENT_COLORS,
        )
        fig_bar.update_traces(
            texttemplate='%{y:,}',
            textposition='outside',
            hovertemplate='<b>%{x}</b><br>人数: %{y:,}<extra></extra>'
        )
        fig_bar.update_layout(
            **_PLOTLY_LAYOUT_DEFAULTS,
            xaxis_title="客户分层",
            yaxis_title="人数",
            showlegend=False,
            xaxis=dict(tickangle=30, tickfont=dict(size=11)),
            margin=dict(t=50, b=100, l=50, r=20)
        )
        st.plotly_chart(fig_bar, width='stretch')

    st.markdown("---")

    st.subheader("🔥 RFM 评分热力图")
    st.markdown("展示 R、F、M 各评分组合下的客户数量分布，颜色越深代表该组合的客户越多。")

    heat_data = rfm.groupby(['r_score', 'f_score']).size().reset_index(name='客户数')
    heat_pivot = heat_data.pivot(index='r_score', columns='f_score', values='客户数').fillna(0)
    for c in range(1, n_bins + 1):
        if c not in heat_pivot.columns:
            heat_pivot[c] = 0
    for r in range(1, n_bins + 1):
        if r not in heat_pivot.index:
            heat_pivot.loc[r] = 0
    heat_pivot = heat_pivot.sort_index(axis=0).sort_index(axis=1)

    fig_heat = go.Figure(data=go.Heatmap(
        z=heat_pivot.values,
        x=[f'F={c}' for c in heat_pivot.columns],
        y=[f'R={r}' for r in heat_pivot.index],
        text=heat_pivot.values.astype(int),
        texttemplate='%{text}',
        textfont=dict(size=12),
        colorscale='YlOrRd',
        hovertemplate='R=%{y}, F=%{x}<br>客户数: %{z:.0f}<extra></extra>',
    ))
    fig_heat.update_layout(
        **_PLOTLY_LAYOUT_DEFAULTS,
        title="R × F 评分交叉热力图（数字为客户数量）",
        xaxis_title="Frequency 评分",
        yaxis_title="Recency 评分",
        margin=dict(t=60, b=40, l=60, r=30)
    )
    st.plotly_chart(fig_heat, width='stretch')

    st.markdown("---")

    st.subheader("📐 RFM 三维散点图")
    st.markdown("以三维视角展示客户在 Recency、Frequency、Monetary 三个维度上的分布，颜色标识客户分层。")

    sample_n = min(2000, len(rfm))
    if len(rfm) > sample_n:
        important = rfm[rfm['客户分层'].str.contains('重要')]
        other = rfm[~rfm['客户分层'].str.contains('重要')]
        other_sample = other.sample(n=min(sample_n - len(important), len(other)), random_state=42)
        rfm_sample = pd.concat([important, other_sample])
    else:
        rfm_sample = rfm

    fig_3d = px.scatter_3d(
        rfm_sample, x='recency_days', y='frequency', z='monetary',
        color='客户分层', color_discrete_map=SEGMENT_COLORS,
        opacity=0.6, size_max=8,
        title="RFM 三维客户分布",
        hover_data={'用户名': True, 'recency_days': True, 'frequency': True, 'monetary': True, '客户分层': True}
    )
    fig_3d.update_layout(
        **_PLOTLY_LAYOUT_DEFAULTS,
        scene=dict(
            xaxis_title='Recency (天)',
            yaxis_title='Frequency (次)',
            zaxis_title='Monetary (元)',
        ),
        margin=dict(t=50, b=20, l=20, r=20),
        legend=dict(orientation="h", yanchor="bottom", y=-0.05, xanchor="center", x=0.5, font=dict(size=10))
    )
    st.plotly_chart(fig_3d, width='stretch')

    st.markdown("---")

    st.subheader("📊 R / F / M 评分分布")
    col_r, col_f, col_m = st.columns(3)

    with col_r:
        fig_r = px.histogram(rfm, x='r_score', nbins=n_bins, title="R 评分分布（最近消费）", color_discrete_sequence=['#E74C3C'])
        fig_r.update_layout(**_PLOTLY_LAYOUT_DEFAULTS, xaxis_title="R 评分", yaxis_title="人数", showlegend=False, margin=dict(t=50, b=30, l=40, r=20))
        fig_r.add_vline(x=r_threshold, line_dash="dash", line_color="blue", annotation_text=f"阈值={r_threshold}")
        st.plotly_chart(fig_r, width='stretch')

    with col_f:
        fig_f = px.histogram(rfm, x='f_score', nbins=n_bins, title="F 评分分布（消费频率）", color_discrete_sequence=['#3498DB'])
        fig_f.update_layout(**_PLOTLY_LAYOUT_DEFAULTS, xaxis_title="F 评分", yaxis_title="人数", showlegend=False, margin=dict(t=50, b=30, l=40, r=20))
        fig_f.add_vline(x=f_threshold, line_dash="dash", line_color="red", annotation_text=f"阈值={f_threshold}")
        st.plotly_chart(fig_f, width='stretch')

    with col_m:
        fig_m = px.histogram(rfm, x='m_score', nbins=n_bins, title="M 评分分布（消费金额）", color_discrete_sequence=['#2ECC71'])
        fig_m.update_layout(**_PLOTLY_LAYOUT_DEFAULTS, xaxis_title="M 评分", yaxis_title="人数", showlegend=False, margin=dict(t=50, b=30, l=40, r=20))
        fig_m.add_vline(x=m_threshold, line_dash="dash", line_color="red", annotation_text=f"阈值={m_threshold}")
        st.plotly_chart(fig_m, width='stretch')

    st.markdown("---")

    st.subheader("💰 各分层消费金额对比")
    seg_monetary = rfm.groupby('客户分层').agg(
        总消费金额=('monetary', 'sum'),
        平均消费金额=('monetary', 'mean'),
        人数=('用户名', 'count')
    ).reset_index()
    _sort_map3 = {name: i for i, name in enumerate(seg_order)}
    seg_monetary['_sort_key'] = seg_monetary['客户分层'].map(_sort_map3)
    seg_monetary = seg_monetary.sort_values('_sort_key').drop(columns=['_sort_key'])

    col_mon1, col_mon2 = st.columns([1, 1])

    with col_mon1:
        fig_mon_bar = px.bar(
            seg_monetary, x='客户分层', y='总消费金额',
            title="各分层总消费金额",
            color='客户分层',
            color_discrete_map=SEGMENT_COLORS,
        )
        fig_mon_bar.update_traces(
            texttemplate='¥%{y:,.0f}',
            textposition='outside',
            hovertemplate='<b>%{x}</b><br>总消费: ¥%{y:,.2f}<extra></extra>'
        )
        fig_mon_bar.update_layout(
            **_PLOTLY_LAYOUT_DEFAULTS,
            xaxis_title="客户分层", yaxis_title="总消费金额 (元)",
            showlegend=False, xaxis=dict(tickangle=30, tickfont=dict(size=11)),
            margin=dict(t=50, b=100, l=60, r=20)
        )
        st.plotly_chart(fig_mon_bar, width='stretch')

    with col_mon2:
        fig_mon_avg = px.bar(
            seg_monetary, x='客户分层', y='平均消费金额',
            title="各分层人均消费金额",
            color='客户分层',
            color_discrete_map=SEGMENT_COLORS,
        )
        fig_mon_avg.update_traces(
            texttemplate='¥%{y:,.0f}',
            textposition='outside',
            hovertemplate='<b>%{x}</b><br>人均消费: ¥%{y:,.2f}<extra></extra>'
        )
        fig_mon_avg.update_layout(
            **_PLOTLY_LAYOUT_DEFAULTS,
            xaxis_title="客户分层", yaxis_title="人均消费金额 (元)",
            showlegend=False, xaxis=dict(tickangle=30, tickfont=dict(size=11)),
            margin=dict(t=50, b=100, l=60, r=20)
        )
        st.plotly_chart(fig_mon_avg, width='stretch')

    st.markdown("---")

    st.subheader("📋 分层客户明细")
    selected_segment = st.selectbox(
        "选择查看的客户分层",
        options=seg_order,
        index=0
    )

    segment_users = rfm[rfm['客户分层'] == selected_segment].sort_values('monetary', ascending=False)
    st.markdown(f"**{SEGMENT_ICONS.get(selected_segment, '')} {selected_segment}** — 共 **{len(segment_users):,}** 位客户")

    display_cols = ['用户名', 'recency_days', 'frequency', 'monetary', 'r_score', 'f_score', 'm_score', '客户分层']
    display_labels = {
        '用户名': '用户名', 'recency_days': '最近消费天数', 'frequency': '消费频次',
        'monetary': '消费金额(元)', 'r_score': 'R评分', 'f_score': 'F评分',
        'm_score': 'M评分', '客户分层': '客户分层'
    }

    page_size = 20
    total_pages = (len(segment_users) + page_size - 1) // page_size
    page_num = st.number_input("页码", min_value=1, max_value=max(total_pages, 1), value=1, step=1)
    start_idx = (page_num - 1) * page_size
    end_idx = start_idx + page_size

    st.dataframe(
        segment_users[display_cols].iloc[start_idx:end_idx].rename(columns=display_labels),
        width='stretch',
        hide_index=True,
    )
    st.caption(f"第 {page_num}/{total_pages} 页 | 每页 {page_size} 条")

    st.markdown("---")

    st.subheader("📖 RFM 分层说明")
    with st.expander("点击查看 RFM 分层逻辑详解", expanded=False):
        st.markdown("""
        | 分层名称 | R评分 | F评分 | M评分 | 含义 | 运营建议 |
        |---------|-------|-------|-------|------|---------|
        | 💎 重要价值客户 | 高 | 高 | 高 | 最近消费、高频次、高金额 | VIP专属服务、优先体验新品 |
        | 📈 重要发展客户 | 高 | 高 | 低 | 最近消费、高频次、低金额 | 提升客单价、组合推荐 |
        | 🔄 重要保持客户 | 高 | 低 | 高 | 最近消费、低频次、高金额 | 提高复购率、定期推送 |
        | ⚠️ 重要挽留客户 | 高 | 低 | 低 | 最近消费、低频次、低金额 | 促活转化、限时优惠 |
        | 🌟 一般价值客户 | 低 | 高 | 高 | 较久未消费、高频次、高金额 | 召回激活、专属回归礼 |
        | 🌱 一般发展客户 | 低 | 高 | 低 | 较久未消费、高频次、低金额 | 唤醒提醒、小额优惠券 |
        | 🔵 一般保持客户 | 低 | 低 | 高 | 较久未消费、低频次、高金额 | 重点召回、大额优惠 |
        | ⚪ 一般挽留客户 | 低 | 低 | 低 | 较久未消费、低频次、低金额 | 自动化触达、成本控制 |
        """)

    st.markdown("---")
    st.caption(f"数据更新时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')} | 参考日期：{ref_date_input} | 分组数：{n_bins} | 电商数据分析系统 v1.2")
