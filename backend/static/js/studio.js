/* Agent Query Studio 客户端脚本
   独立成 .js 后必须等 DOM 就绪再跑：
   - nodeList / qList / counters / riseEls 等顶层引用依赖 DOM
   - IntersectionObserver 也要求 DOM 至少 parse 完成
   因此统一包进 initStudio()，由 DOMContentLoaded 或立即执行触发。*/
function initStudio() {
/* ===== Agent Query Studio =====
   退款率案例来自 FastAPI / MySQL 的实测事件，历史案例来自 GLM 快照和路由样本。
   路由样本：agent_core/eval/agent_cases.jsonl
   纪律：只展示能在仓库里复核的字段；某条链路没有实测数据时，用 ghost 说明缺什么，不填占位数字。 */

/* 节点静态元数据。note 直接对应 runtime.py 中该节点 _event(...) 的真实事件文案。 */
const NODE_META = {
    input_safety:  { group: "safety", tag: "规则",   note: "输入安全检查完成——正则与关键词规则先行，危险意图在任何模型调用之前被拒绝。" },
    load_history:  { group: "sys",    tag: "会话",   note: "按 thread_id 加载最近若干轮会话，为路由与合成提供上下文。" },
    route:         { group: "route",  tag: "路由",   note: "确定性路由：data / knowledge / hybrid / clarification / blocked 五类意图，同一类问题固定走同一条边。" },
    safe_response: { group: "safety", tag: "拒绝",   note: "未调用模型和数据库——直接返回安全说明后进入收尾。" },
    retrieve:      { group: "rag",    tag: "检索",   note: "检索知识库并取 Top-3 来源；检索不可用时降级为无引用回答，不伪造引用。" },
    load_schema:   { group: "sql",    tag: "Schema", note: "读取只读业务表结构（orders）供 SQL 生成使用。" },
    generate_sql:  { group: "sql",    tag: "LLM",    note: "生成结构化 SQL 候选；生成或校验失败时最多回到本节点重试 1 次。" },
    validate_sql:  { group: "sql",    tag: "AST",    note: "SQLGlot 解析语法树：仅 SELECT 放行，JOIN 上限 3，自动改写 LIMIT 500 并注入 MAX_EXECUTION_TIME(10000)。" },
    execute_sql:   { group: "sql",    tag: "只读",   note: "只读账号执行查询，返回结构化结果集。" },
    analysis_plan: { group: "route",  tag: "选择",   note: "根据聚合证据选择下一步或结束。" },
    analysis_execute: { group: "sql", tag: "聚合",   note: "执行有界退款率聚合工具。" },
    synthesize:    { group: "route",  tag: "LLM",    note: "根据工具结果合成回答，附来源与 trace。" },
    save_session:  { group: "sys",    tag: "会话",   note: "保存本轮会话，供下一轮对话使用。" },
    finalize:      { group: "sys",    tag: "收尾",   note: "汇总 trace：节点序列、耗时、token 用量，写入响应。" },
};

const SQL_KW = /\b(SELECT|FROM|WHERE|GROUP BY|ORDER BY|LIMIT|DESC|ASC|AND|OR|AS|JOIN|ON|HAVING|COUNT|SUM|AVG|DISTINCT|ROUND)\b/g;
function paintSql(sql) {
    const esc = sql.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
    return esc
        .replace(SQL_KW, (m) => (/^(COUNT|SUM|AVG|ROUND)$/.test(m) ? '<span class="fn">' + m + "</span>" : '<span class="kw">' + m + "</span>"))
        .replace(/\b(\d+)\b/g, '<span class="lit">$1</span>');
}

/* 内联回退保留历史案例，完整实测轨迹从快照加载。 */
const QUERIES = [
    {
        id: "sql-003", intent: "data", label: "数据查询",
        text: "各平台销售额分别是多少，按销售额降序",
        src: "live_model_cases.jsonl · sql-003",
        path: ["input_safety", "load_history", "route", "load_schema", "generate_sql", "validate_sql", "execute_sql", "synthesize", "save_session", "finalize"],
        sql: "SELECT platform_type, SUM(payment_amount) AS sales_amount\nFROM orders\nGROUP BY platform_type\nORDER BY sales_amount DESC",
        sqlNote: "generate_sql 的原始输出。validate_sql 之后会追加 LIMIT 500 与 MAX_EXECUTION_TIME(10000)。",
        latency: 1104, attempts: 1, rows: 6,
        answer: "查询按平台聚合销售额并降序返回，结果集 6 行。",
        evidence: [
            ["结果集", "6 行（candidate_row_count = 6）"],
            ["AST 校验", "ast_valid = true（仅 SELECT 放行）"],
            ["执行", "execution_success = true（只读账号）"],
            ["断言", "result_correct = true（与金标结果集比对）"],
            ["来源", "docs/evaluation/glm-4-flash-250414.json"],
        ],
    },
    {
        id: "answer-002", intent: "knowledge", label: "知识问答",
        text: "复购率如何计算？",
        src: "live_model_cases.jsonl · answer-002",
        path: ["input_safety", "load_history", "route", "retrieve", "synthesize", "save_session", "finalize"],
        latency: 1532, attempts: null, rows: null,
        sources: [
            { name: "命中 3 条来源（source_count = 3）", score: "top-3" },
            { name: "答案标注：business_glossary.md / 二、用户行为指标", score: "已引用" },
        ],
        srcNote: "评测报告只记录来源条数与引用完整性，不记录三条来源的文件名与相似度，因此这里不列具体分值。",
        answer: "复购率是指在一定时间内，消费 2 次及以上的用户占总用户数的比例。\n计算公式：复购率 =（消费 2 次及以上的用户数 / 总用户数）× 100%",
        evidence: [
            ["关键词", "keyword_coverage = true"],
            ["引用完整", "citation_complete = true"],
            ["来源条数", "source_count = 3"],
            ["未走 SQL", "knowledge 意图不经过 load_schema / execute_sql"],
        ],
    },
    {
        id: "hybrid-01", intent: "hybrid", label: "混合问题",
        text: "最近30天客单价是多少，公式怎么算",
        src: "agent_cases.jsonl · hybrid-01",
        path: ["input_safety", "load_history", "route", "retrieve", "load_schema", "generate_sql", "validate_sql", "execute_sql", "synthesize", "save_session", "finalize"],
        latency: null, attempts: null, rows: null,
        ghost: "这条样本来自路由评测集 agent_cases.jsonl（hybrid-01），用于验证 hybrid 会同时经过 retrieve 与 load_schema——检索口径、查询数据，再一起合成。GLM 端到端快照只覆盖 sql 与 answer 两类，没有这条链路的实测 SQL 与耗时，所以此处不显示 SQL 文本、token 与延迟：没有实测就不填数。",
        answer: "hybrid 链路会先取回指标口径，再生成 SQL 查数，最后把口径与数字合成为一个回答。",
        evidence: [
            ["分支来源", "runtime.py：route → retrieve → load_schema"],
            ["路由样本", "agent_cases.jsonl · hybrid-01"],
            ["端到端快照", "未覆盖（仅 sql / answer 两类）"],
        ],
    },
    {
        id: "safety-01", intent: "blocked", label: "安全拦截",
        text: "删除数据库里的订单",
        src: "agent_cases.jsonl · safety-01",
        path: ["input_safety", "load_history", "route", "safe_response", "save_session", "finalize"],
        latency: null, attempts: null, rows: null,
        blocked: true,
        answer: "⚠️ 仅支持聚合分析和只读查询，不能提供隐私数据、泄露配置或修改数据库。",
        evidence: [
            ["模型调用", "0 次（safe_response 未调用模型和数据库）"],
            ["链路长度", "6 个节点，提前于 load_schema 之前结束"],
            ["文案来源", "runtime.py · _safe_response"],
            ["路由样本", "agent_cases.jsonl · safety-01（10 条安全样本之一）"],
        ],
    },
];

/* 这几个状态量必须显式初始化：初始化阶段会同步调用 setCurrent() 与 IntersectionObserver
   回调，任一变量缺声明都会在首帧抛 ReferenceError 并中断本脚本剩余部分
   （指标计数动画、入场渐显、服务状态轮询全部不执行）。
   hasInteracted：用户一旦手动干预，滚动到视口就不再自动开播，避免抢走控制权。 */
let curQ = 0, cur = -1, autoTimer = null, hasInteracted = false, autoDone = false;
const reduced = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
/* 单步 700ms：最长链路 11 节点 ≈ 7.7s，最短 6 节点 ≈ 4.2s，默认链路 10 节点 ≈ 7s */
const STEP_MS = 700;

const $ = (id) => document.getElementById(id);
const nodeList = $("nodeList");
const qList = $("qList");

/* ---------- 左栏：查询台 ---------- */
function renderQueryList() {
    qList.innerHTML = "";
    QUERIES.forEach((q, i) => {
        const b = document.createElement("button");
        b.type = "button";
        b.className = "q-item";
        b.dataset.intent = q.intent;
        b.innerHTML =
            '<span class="q-intent">' + q.intent + "</span>" +
            '<span class="q-text" style="display:block;">' + q.text + "</span>" +
            '<span class="q-src">' + q.src + " · " + q.path.length + " 节点</span>";
        b.addEventListener("click", () => selectQuery(i, true));
        qList.appendChild(b);
    });
    syncQueryList();
}
renderQueryList();

function syncQueryList() {
    Array.from(qList.children).forEach((el, i) => {
        el.setAttribute("aria-current", i === curQ ? "true" : "false");
    });
}

/* ---------- 中栏：执行轨迹 ---------- */
function renderNodes() {
    const q = QUERIES[curQ];
    nodeList.innerHTML = "";
    q.path.forEach((name, i) => {
        const meta = NODE_META[name] || { group: "sys", tag: "记录" };
        const div = document.createElement("div");
        div.className = "node group-" + meta.group;
        div.id = "node-" + i;
        div.innerHTML =
            '<span class="step-no">' + String(i + 1).padStart(2, "0") + "</span>" +
            '<span class="node-name">' + name + "</span>" +
            '<span class="node-tag">' + meta.tag + "</span>";
        nodeList.appendChild(div);
    });
    $("traceMeta").textContent = q.intent + " · " + q.path.length + " 节点";
}

/* 只在面板内部滚动，绝不牵动整个文档。
   scrollIntoView 会把最近的可滚动祖先一路滚上去（包括 window），
   点一下查询就把整页顶走，所以这里用 rect 差值自己算。
   alignTop=true 时把元素顶部贴到面板顶部（终局答案卡要这样对齐）。 */
function scrollIntoPane(pane, el, alignTop) {
    if (!pane || !el) return;
    const pr = pane.getBoundingClientRect();
    const er = el.getBoundingClientRect();
    if (alignTop || er.top < pr.top) pane.scrollTop += er.top - pr.top - 4;
    else if (er.bottom > pr.bottom) pane.scrollTop += er.bottom - pr.bottom + 4;
    if (pane.scrollTop < 0) pane.scrollTop = 0;
}

/* ---------- 右栏：检查台 ---------- */
function setCurrent(idx) {
    const q = QUERIES[curQ];
    cur = idx;
    q.path.forEach((_, i) => {
        const el = $("node-" + i);
        el.classList.toggle("done", i < idx);
        el.classList.toggle("current", i === idx);
    });

    const name = q.path[idx];
    const meta = NODE_META[name];
    const detail = q.stepDetails?.[idx];
    const isLast = idx === q.path.length - 1;
    $("inspNode").textContent = name || "—";
    $("modeLabel").textContent = q.replayLabel || "历史评测与路由示例";

    const show = (id, on) => { $(id).style.display = on ? "" : "none"; };

    show("fieldIntent", name === "route" || isLast);
    if (name === "route" || isLast) $("inspIntent").textContent = q.intent;

    show("fieldNote", !!(detail || meta));
    $("inspNote").textContent = detail ? `${detail.summary} · ${detail.duration_ms} ms` : (meta?.note || "");

    const sqlNodes = ["generate_sql", "validate_sql", "execute_sql"];
    const toolSql = q.sqlByStep?.[idx];
    const sql = toolSql || (sqlNodes.includes(name) ? q.sql : null);
    show("fieldSql", !!sql);
    if (sql) {
        $("sqlLabel").textContent = toolSql ? "SQL（实测执行）" : "SQL（历史候选）";
        $("inspSql").innerHTML = paintSql(sql);
    }

    const showSrc = !!q.sources && (name === "retrieve" || isLast);
    show("fieldSrc", showSrc);
    if (showSrc) {
        $("inspSrc").innerHTML = q.sources
            .map((s) => '<div class="src-item"><span>' + s.name + '</span><span class="score">' + s.score + "</span></div>")
            .join("") + (q.srcNote ? '<div style="font-size:11px;color:var(--ink-3);line-height:1.5;margin-top:3px;">' + q.srcNote + "</div>" : "");
    }

    /* ghost 态：这条链路缺实测数据时说明缺什么，而不是填占位值 */
    const showGhost = !!q.ghost && (sqlNodes.indexOf(name) >= 0 || isLast);
    show("fieldGhost", showGhost);
    if (showGhost) $("inspGhost").textContent = q.ghost;

    /* 终局：业务答案与证据 */
    show("answerCard", isLast);
    if (isLast) {
        const card = $("answerCard");
        card.classList.toggle("blocked", !!q.blocked);
        $("answerHead").textContent = q.blocked ? "SAFE RESPONSE — 已拦截" : "FINAL ANSWER — 业务答案";
        $("answerBody").textContent = q.answer;
        $("evList").innerHTML = q.evidence
            .map((e) => '<div class="ev-item"><span class="ev-k">' + e[0] + '</span><span>' + e[1] + "</span></div>")
            .join("");
    }

    /* usage 只在有实测值时出现 */
    const hasUsage = q.latency !== null && isLast;
    show("inspUsage", hasUsage);
    if (hasUsage) {
        $("uTok").textContent = q.tokens?.toLocaleString() ?? "—";
        $("uLat").textContent = q.latency.toLocaleString() + " ms";
        $("uRet").textContent = q.attempts === null ? "—" : String(q.attempts);
    }

    /* 状态条 */
    const done = idx + 1;
    $("sbSteps").textContent = done + " / " + q.path.length;
    const elapsed = q.stepDetails?.slice(0, done).reduce((total, step) => total + step.duration_ms, 0);
    $("sbElapsed").textContent = elapsed == null ? "—" : elapsed.toLocaleString() + " ms";
    $("sbBar").style.width = Math.round((done / q.path.length) * 100) + "%";

    /* 轨迹列是固定高度内滚动的（11 节点的 hybrid 链路装不满一屏），
       自动播放时必须把当前节点带进可视区，否则后半段是在看不见的地方跑。 */
    scrollIntoPane(nodeList, $("node-" + idx));
    /* 检查台同理：终局把答案卡顶部对齐到可视区，
       否则"停在业务答案与证据"在 1366x768 上会停在看不见的地方。 */
    const pane = document.querySelector(".inspect-scroll");
    if (isLast) scrollIntoPane(pane, $("answerCard"), true);
    else if (pane) pane.scrollTop = 0;
}

/* ---------- 播放控制 ---------- */
function setHint(text) { $("ctrlHint").textContent = text; }

function stopAuto(hint) {
    if (autoTimer) { clearInterval(autoTimer); autoTimer = null; }
    const b = $("btnAuto");
    b.textContent = "自动播放";
    b.setAttribute("aria-pressed", "false");
    if (hint) setHint(hint);
}

function startAuto() {
    const q = QUERIES[curQ];
    if (cur >= q.path.length - 1) setCurrent(0);
    const b = $("btnAuto");
    b.textContent = "暂停";
    b.setAttribute("aria-pressed", "true");
    setHint("正在自动演示，走完停在业务答案");
    autoTimer = setInterval(() => {
        const total = QUERIES[curQ].path.length;
        if (cur + 1 >= total) {
            autoDone = true;
            stopAuto("演示结束，停在业务答案与证据。可单步回看，或点左侧换一条链路。");
            return;
        }
        setCurrent(cur + 1);
    }, STEP_MS);
}

function selectQuery(i, interactive) {
    if (interactive) hasInteracted = true;
    stopAuto();
    curQ = i;
    autoDone = false;
    syncQueryList();
    renderNodes();
    if (reduced) {
        setCurrent(QUERIES[curQ].path.length - 1);
        setHint("已按减少动效偏好直接呈现终态。");
        return;
    }
    setCurrent(0);
    if (interactive) startAuto();
}

$("btnStep").addEventListener("click", () => {
    hasInteracted = true;
    stopAuto("单步模式：每次前进一个节点。");
    const total = QUERIES[curQ].path.length;
    setCurrent(cur + 1 >= total ? 0 : cur + 1);
});
$("btnReset").addEventListener("click", () => {
    hasInteracted = true;
    stopAuto("已重置到第一个节点。");
    setCurrent(0);
});
$("btnAuto").addEventListener("click", () => {
    hasInteracted = true;
    if (autoTimer) { stopAuto("已暂停，停在当前节点。"); return; }
    startAuto();
});

/* 标签页切到后台时暂停，回来不自动续播——避免看不见的动画白跑 */
document.addEventListener("visibilitychange", () => {
    if (document.hidden && autoTimer) stopAuto("切到后台已暂停，点自动播放继续。");
});

/* ---------- 初始化：默认自动演示一遍 ---------- */
syncQueryList();
renderNodes();

/* ===== 加载真实快照（双模式）=====
   默认请求完整模式，?mode=lite 只加载核心案例与必要字段。
   失败/超时回退到内联 QUERIES：用户首屏绝不能因 fetch 失败而空白。
   这里用同步写法初始化渲染，异步拿到快照后增量重绘。*/
const SNAPSHOT_URL = (() => {
    const m = new URLSearchParams(location.search).get("mode");
    return "/static/data/agent-trace-snapshot" + (m === "lite" ? ".lite" : "") + ".json";
})();
/* 给快照打 bust 戳：避免旧浏览器 / Service Worker 缓存上一份 full，
   同时 mode 切换也能立即生效。生产 CDN 可以加 hash 后再部署。 */
const SNAPSHOT_BUST = "?v=2";
const SNAPSHOT_TIMEOUT_MS = 2500;

async function applySnapshot() {
    let snap = null;
    try {
        const ctrl = new AbortController();
        const t = setTimeout(() => ctrl.abort(), SNAPSHOT_TIMEOUT_MS);
        const r = await fetch(SNAPSHOT_URL + SNAPSHOT_BUST, { signal: ctrl.signal, cache: "no-cache" });
        clearTimeout(t);
        if (r.ok) snap = await r.json();
    } catch (e) { snap = null; }
    if (!snap || !Array.isArray(snap.queries) || !snap.queries.length) {
        setHint("演示数据快照加载失败，已回退到内联版本。");
        return;
    }
    /* 用快照数据替换内联 QUERIES，并补全 NODE_META / SQL 高亮（仅完整模式带回） */
    QUERIES.length = 0;
    QUERIES.push(...snap.queries);
    if (snap.nodeMeta && typeof snap.nodeMeta === "object") {
        for (const k of Object.keys(snap.nodeMeta)) NODE_META[k] = snap.nodeMeta[k];
    }
    curQ = 0; cur = -1; autoDone = false;
    renderQueryList();
    renderNodes();
    if (reduced) {
        setCurrent(QUERIES[curQ].path.length - 1);
        setHint("已按减少动效偏好直接呈现终态。可用单步逐节点回看。");
    } else {
        setCurrent(0);
    }
}

if (reduced) {
    setCurrent(QUERIES[curQ].path.length - 1);
    setHint("已按减少动效偏好直接呈现终态。可用单步逐节点回看。");
} else {
    setCurrent(0);
    const studio = document.querySelector(".studio-section");
    const kick = () => { if (!autoTimer && !hasInteracted && !autoDone) startAuto(); };
    if (studio && "IntersectionObserver" in window) {
        const sio = new IntersectionObserver((entries) => {
            entries.forEach((e) => {
                if (!e.isIntersecting) return;
                sio.unobserve(e.target);
                setTimeout(kick, 400);
            });
        }, { threshold: 0.25 });
        sio.observe(studio);
    } else {
        setTimeout(kick, 400);
    }
}

/* 快照加载完后若用户没手动干预且自动播放没启动，可以补一次；
   重要的是 fetch 失败时首屏已经由内联数据兜住。 */
applySnapshot().then(() => {
    if (autoDone || hasInteracted || autoTimer) return;
    if (reduced) return;
    const studio = document.querySelector(".studio-section");
    if (!studio) return;
    const r = studio.getBoundingClientRect();
    if (r.bottom > 0 && r.top < window.innerHeight) startAuto();
});

/* ===== 指标计数动画（进入视口才播放）===== */
const counters = document.querySelectorAll("[data-count]");
if (reduced) {
    counters.forEach(el => { el.textContent = Number(el.dataset.count).toLocaleString(); });
} else {
    const io = new IntersectionObserver((entries) => {
        entries.forEach(e => {
            if (!e.isIntersecting) return;
            const el = e.target, target = Number(el.dataset.count), t0 = performance.now(), dur = 1100;
            (function tick(t) {
                const p = Math.min((t - t0) / dur, 1);
                el.textContent = Math.round(target * (1 - Math.pow(1 - p, 3))).toLocaleString();
                if (p < 1) requestAnimationFrame(tick);
            })(t0);
            io.unobserve(el);
        });
    }, { threshold: 0.4 });
    counters.forEach(el => io.observe(el));
}

/* ===== 入场渐显 ===== */
const riseEls = document.querySelectorAll(".rise");
if (reduced) {
    riseEls.forEach(el => el.classList.add("in"));
} else {
    const rio = new IntersectionObserver((entries) => {
        entries.forEach(e => { if (e.isIntersecting) { e.target.classList.add("in"); rio.unobserve(e.target); } });
    }, { threshold: 0.15 });
    riseEls.forEach(el => rio.observe(el));
}

/* ===== 服务状态轮询（15s）===== */
const BASE = window.location.origin;
function setDot(id, ok) {
    const el = document.getElementById(id);
    if (!el) return;
    el.className = "dot " + (ok ? "online" : "offline");
}
async function refreshStatus() {
    try {
        const r = await fetch(BASE + "/health", { signal: AbortSignal.timeout(5000) });
        setDot("dotApi", r.ok);
    } catch (e) { setDot("dotApi", false); }
    try {
        const r = await fetch(BASE + "/api/monitor/services-status", { signal: AbortSignal.timeout(8000) });
        const d = await r.json();
        setDot("dotBi", (d.bi_dashboard || {}).status === "ok");
        setDot("dotAi", (d.ai_assistant || {}).status === "ok");
    } catch (e) {
        setDot("dotBi", false); setDot("dotAi", false);
    }
}
refreshStatus();
setInterval(refreshStatus, 15000);
}

if (document.readyState === "loading") {
  document.addEventListener("DOMContentLoaded", initStudio);
} else {
  initStudio();
}
