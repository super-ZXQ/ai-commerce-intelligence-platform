export class ApiError extends Error {
  constructor(message, status = 0, code = '', payload = null) {
    super(message);
    this.name = 'ApiError';
    this.status = status;
    this.code = code;
    this.payload = payload;
  }
}

function errorMessage(payload, status) {
  const detail = payload?.detail;
  if (typeof detail === 'string') return detail;
  if (detail && typeof detail.message === 'string') return detail.message;
  if (typeof payload?.message === 'string') return payload.message;
  if (Array.isArray(detail)) return '请求参数不符合接口要求，请检查筛选条件';
  if (status === 401) return '登录已失效，请重新登录';
  if (status === 403) return '当前账号没有执行此操作的权限';
  if (status === 429) return '请求过于频繁或额度已用完，请稍后重试';
  if (status === 503) return '服务暂时不可用，请稍后重试';
  if (status === 504) return '查询超时，请缩小范围后重试';
  return `请求失败（HTTP ${status}）`;
}

function errorCode(payload) {
  return payload?.detail?.error_code || payload?.error_code || '';
}

export function redirectToLogin() {
  const next = `${window.location.pathname}${window.location.search}${window.location.hash}`;
  const safeNext = next.startsWith('/') && !next.startsWith('//') ? next : '/workspace';
  window.location.assign(`/login?next=${encodeURIComponent(safeNext)}`);
}

async function parseBody(response) {
  if (response.status === 204) return null;
  const type = response.headers.get('content-type') || '';
  if (type.includes('json') || type.includes('ndjson')) {
    return response.json().catch(() => null);
  }
  const text = await response.text();
  return text ? { message: text } : null;
}

async function responseError(response) {
  const payload = await parseBody(response);
  return new ApiError(errorMessage(payload, response.status), response.status, errorCode(payload), payload);
}

export async function apiFetch(path, options = {}) {
  const headers = new Headers(options.headers || {});
  headers.set('Accept', 'application/json');
  let body = options.body;
  if (body && typeof body === 'object' && !(body instanceof FormData)) {
    headers.set('Content-Type', 'application/json');
    body = JSON.stringify(body);
  }
  let response;
  try {
    response = await fetch(path, { ...options, body, headers, credentials: 'same-origin' });
  } catch (error) {
    if (error.name === 'AbortError') throw error;
    throw new ApiError('网络连接失败，请检查服务是否运行后重试');
  }
  if (!response.ok) {
    if (response.status === 401) redirectToLogin();
    throw await responseError(response);
  }
  return parseBody(response);
}

export async function streamAI({ query, threadId, onEvent, signal }) {
  let response;
  try {
    response = await fetch('/api/ai/stream', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', Accept: 'application/x-ndjson' },
      credentials: 'same-origin',
      body: JSON.stringify({ query, thread_id: threadId || undefined }),
      signal,
    });
  } catch (error) {
    if (error.name === 'AbortError') throw error;
    throw new ApiError('无法连接 AI 服务，请检查网络后重试');
  }
  if (!response.ok) {
    if (response.status === 401) redirectToLogin();
    throw await responseError(response);
  }
  if (!response.body) throw new ApiError('浏览器无法读取 AI 流式响应');

  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = '';
  let finalResponse = null;

  const processLine = (line) => {
    if (!line.trim()) return;
    let event;
    try {
      event = JSON.parse(line);
    } catch {
      throw new ApiError('AI 服务返回了无法解析的流式数据');
    }
    onEvent?.(event);
    if (event.type === 'final') finalResponse = event.response;
    if (event.type === 'error') {
      throw new ApiError(event.message || 'AI 查询失败', 200, event.error_code || 'AI_STREAM_FAILED', event);
    }
  };

  while (true) {
    const { done, value } = await reader.read();
    buffer += decoder.decode(value || new Uint8Array(), { stream: !done });
    const lines = buffer.split('\n');
    buffer = done ? '' : lines.pop() || '';
    for (const line of lines) processLine(line);
    if (done) {
      if (buffer.trim()) processLine(buffer);
      break;
    }
  }
  if (!finalResponse) throw new ApiError('AI 查询未返回完整结果，请重试');
  return finalResponse;
}
