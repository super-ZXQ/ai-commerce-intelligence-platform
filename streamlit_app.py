import streamlit as st
import pandas as pd
import plotly.express as px
from datetime import datetime
import os

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
    fig_line = px.line(daily_sales, x='日期', y='销售额', markers=True, title="每日销售额变化趋势", template="plotly_white")
    fig_line.update_traces(line=dict(width=3, color='#1f77b4'))
    fig_line.update_layout(hovermode='x unified')
    st.plotly_chart(fig_line, use_container_width=True)

with col_chart2:
    platform_sales = filtered_df.groupby('平台类型')['付款金额'].sum().reset_index()
    fig_pie = px.pie(platform_sales, values='付款金额', names='平台类型', title="平台销售占比", hole=0.4, color_discrete_sequence=px.colors.qualitative.Set3)
    fig_pie.update_traces(textposition='inside', textinfo='percent+label')
    st.plotly_chart(fig_pie, use_container_width=True)

st.subheader("🏆 商品销售额 TOP10")
top_products = filtered_df.groupby('商品编号')['付款金额'].sum().nlargest(10).reset_index()
fig_bar = px.bar(top_products, x='商品编号', y='付款金额', title="销售额 TOP10 商品", color='付款金额', color_continuous_scale='Reds')
fig_bar.update_layout(xaxis_title="商品编号", yaxis_title="销售额 (元)")
fig_bar.update_traces(texttemplate='%{y:.0f}', textposition='outside')
st.plotly_chart(fig_bar, use_container_width=True)

st.subheader("👑 用户消费 TOP10")
top_users = filtered_df.groupby('用户名')['付款金额'].sum().nlargest(10).reset_index()
fig_user = px.bar(top_users, x='付款金额', y='用户名', orientation='h', title="消费金额 TOP10 用户", color='付款金额', color_continuous_scale='Viridis')
fig_user.update_layout(xaxis_title="消费金额 (元)", yaxis_title="用户")
fig_user.update_traces(texttemplate='%{x:.0f}', textposition='outside')
st.plotly_chart(fig_user, use_container_width=True)

st.markdown("---")
st.caption(f"数据更新时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')} | 数据来源：cleaned_orders | 电商数据分析系统 v1.2")
