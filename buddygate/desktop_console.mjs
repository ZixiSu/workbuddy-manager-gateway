// Local WorkBuddy renderer console for the manager gateway.
//
// One operation per line. Every operation first proves the target is a single
// local WorkBuddy page that exposes the native desktop bridge, then calls only
// the documented wb:conversations:* methods. No other page is touched, no
// ticket, cookie or chat of another conversation is read, and nothing is
// printed except one JSON response line.
import readline from 'node:readline';

const LIMIT = 4 * 1024 * 1024;
const VERSION = /^\d+\.\d+\.\d+/;

// This function is serialised and evaluated inside the renderer, so it must be
// self-contained: it may only use its own locals, its arguments and globals.
async function run(op, args) {
  const allowed = new Set(['create', 'sendPrompt', 'get', 'requests', 'getPendingInfo', 'resolvePending', 'cancel']);
  const textLimit = 20000;
  // Only counters that were actually observed are carried out of the page.
  const topTokens = ['inputTokens', 'totalTokens', 'contextWindow'];
  const detailTokens = ['promptTokens', 'completionTokens', 'promptCacheHitTokens',
    'promptCacheMissTokens', 'promptCacheWriteTokens'];
  // A context-composition breakdown observed next to the totals on some requests.
  const contextParts = ['systemPrompt', 'conversation', 'tools', 'mcp', 'skills'];
  const isCount = value => typeof value === 'number' && Number.isFinite(value)
    && Number.isInteger(value) && value >= 0;
  const isObject = value => Boolean(value) && typeof value === 'object' && !Array.isArray(value);
  // A non-token field (for example cost or estimatedCost) is named, never valued.
  const normalizeUsage = raw => {
    if (!isObject(raw)) return null;
    const top = {};
    const details = {};
    const invalid = [];
    for (const key of topTokens) {
      if (!(key in raw)) continue;
      if (isCount(raw[key])) top[key] = raw[key]; else invalid.push(key);
    }
    const nested = isObject(raw.details) ? raw.details : null;
    if (nested) {
      for (const key of detailTokens) {
        if (!(key in nested)) continue;
        if (isCount(nested[key])) details[key] = nested[key]; else invalid.push('details.' + key);
      }
    }
    const ignored = Object.keys(raw)
      .filter(key => !topTokens.includes(key) && key !== 'details' && key !== 'byCategory')
      .map(key => String(key).slice(0, 60));
    if (nested) {
      for (const key of Object.keys(nested)) {
        if (!detailTokens.includes(key)) ignored.push('details.' + String(key).slice(0, 50));
      }
    }
    let context = null;
    if (isObject(raw.byCategory)) {
      const parts = {};
      for (const key of contextParts) {
        if (!(key in raw.byCategory)) continue;
        if (isCount(raw.byCategory[key])) parts[key] = raw.byCategory[key]; else invalid.push('byCategory.' + key);
      }
      if (Object.keys(parts).length) context = parts;
    }
    return {
      top,
      details,
      byCategory: context,
      observed_fields: topTokens.filter(key => key in raw),
      observed_detail_fields: nested ? detailTokens.filter(key => key in nested) : [],
      observed_context_fields: context ? Object.keys(context) : [],
      invalid_fields: invalid.slice(0, 24),
      ignored_fields: ignored.slice(0, 24),
    };
  };
  const toText = value => {
    if (typeof value === 'string') return value;
    if (Array.isArray(value)) return value.map(toText).filter(Boolean).join('');
    if (value && typeof value === 'object') {
      if (typeof value.text === 'string') return value.text;
      if (typeof value.content === 'string') return value.content;
      if (Array.isArray(value.content)) return toText(value.content);
    }
    return '';
  };
  const invoke = globalThis.__wbInvoke;
  const call = (method, ...rest) => {
    if (!allowed.has(method)) throw new Error('Unsupported conversation method');
    return invoke('wb:conversations:' + method, {}, ...rest);
  };
  const flag = value => (typeof value === 'boolean' ? value : null);
  // A bounded id string; anything else is not carried out of the page.
  const identifier = value => (typeof value === 'string' && /^[0-9a-z_-]{8,64}$/i.test(value.trim())
    ? value.trim() : null);
  if (op === 'identity') {
    return { native: typeof invoke === 'function', version: globalThis.workbuddyDesktop?.appVersion ?? null,
             platform: globalThis.workbuddyDesktop?.platform ?? null };
  }
  if (op === 'create') {
    const config = { conversationId: args.conversationId, title: args.title, cwd: args.cwd, transport: 'local' };
    if (typeof args.model === 'string' && args.model) config.model = args.model;
    const created = await call('create', config);
    const info = created?.info ?? {};
    return { id: info.id ?? null, title: info.title ?? null, state: info.state ?? null, model: created?.config?.model ?? null };
  }
  if (op === 'conversation') {
    // Read-only existence check for a reserved conversation ID; never creates.
    const detail = await call('get', args.conversationId);
    const info = isObject(detail?.info) ? detail.info : null;
    return { exists: Boolean(info), id: info?.id ?? null, state: info?.state ?? null, title: info?.title ?? null };
  }
  if (op === 'send') {
    const current = globalThis.__buddyGatewayDispatch;
    // A second dispatch of the same request ID is a duplicate side effect.
    if (current && current.requestId === args.requestId) throw new Error('This request ID was already dispatched');
    globalThis.__buddyGatewayDispatch = { requestId: args.requestId, state: 'sending' };
    const payload = [{ type: 'text', text: String(args.text ?? '') }];
    const options = { clientRequestId: args.requestId, _meta: { 'codebuddy.ai/conversationRequestId': args.requestId } };
    Promise.resolve(call('sendPrompt', args.conversationId, payload, options)).then(
      () => { const s = globalThis.__buddyGatewayDispatch; if (s && s.requestId === args.requestId) s.state = 'returned'; },
      () => { const s = globalThis.__buddyGatewayDispatch; if (s && s.requestId === args.requestId) s.state = 'failed'; },
    );
    // Do not await the full run: the caller observes the request instead.
    return { dispatched: true, state: 'sending' };
  }
  if (op === 'status') {
    const detail = await call('get', args.conversationId);
    const listed = await call('requests', args.conversationId);
    const items = Array.isArray(listed) ? listed : (Array.isArray(listed?.items) ? listed.items : []);
    const clientId = identifier(args.requestId);
    const nativeId = identifier(args.nativeRequestId);
    // The client id is only trusted while the desktop still reports it. After an
    // application restart that field disappears, and the canonical request id is
    // the remaining evidence; it is matched only when it was observed earlier.
    const pick = predicate => items.filter(item => item && predicate(item));
    let basis = null;
    let candidates = [];
    if (clientId) {
      candidates = pick(item => item.clientRequestId === clientId || item.requestId === clientId
        || item?._meta?.['codebuddy.ai/conversationRequestId'] === clientId);
      if (candidates.length) basis = 'client_request_id';
    }
    if (!candidates.length && nativeId) {
      candidates = pick(item => identifier(item.conversationRequestId) === nativeId
        || identifier(item.id) === nativeId);
      if (candidates.length) basis = 'native_request_id';
    }
    // Two matches mean the identity is ambiguous; guessing could read a stranger's run.
    const ambiguous = candidates.length > 1;
    const match = ambiguous ? null : (candidates[0] ?? null);
    const usage = normalizeUsage(match ? match.usage : null);
    const info = isObject(detail?.info) ? detail.info : null;
    return {
      conversationState: info?.state ?? null,
      conversationExists: Boolean(info),
      conversationId: info?.id ?? null,
      conversationTitle: info?.title ?? null,
      // A finished request does not prove every side effect stopped.
      hasActiveChildAgents: flag(info?.hasActiveChildAgents),
      hasRunningBackgroundTasks: flag(info?.hasRunningBackgroundTasks),
      requestCount: items.length,
      requestSeen: Boolean(match),
      // Which identifier proved the match, and whether the evidence is still intact.
      matchBy: match ? basis : null,
      matchAmbiguous: ambiguous,
      requestsWithoutClientRequestId: items.filter(item => !identifier(item?.clientRequestId)).length,
      // The server-side request identity: what the transcript also records.
      nativeRequestId: identifier(match?.conversationRequestId) ?? identifier(match?.id),
      nativeRequestIdSource: (identifier(match?.conversationRequestId) ? 'conversationRequestId'
        : (identifier(match?.id) ? 'id' : null)),
      state: match?.state ?? match?.status ?? null,
      requestedModel: isObject(match?.requestedModel) ? match.requestedModel : null,
      responseModel: isObject(match?.responseModel) ? match.responseModel : null,
      usage,
      usageFields: usage ? usage.observed_fields : [],
      assistant: toText(match?.assistantMessage).slice(-textLimit),
      dispatch: globalThis.__buddyGatewayDispatch ?? null,
    };
  }
  if (op === 'pending') {
    const value = await call('getPendingInfo', args.conversationId);
    const raw = JSON.stringify(value ?? null);
    return { pending: raw.length > 8000 ? { truncated: true } : JSON.parse(raw) };
  }
  if (op === 'resolve') {
    const value = await call('resolvePending', args.conversationId, args.pendingId, args.response);
    return { resolved: true, value: value === undefined ? null : JSON.parse(JSON.stringify(value)) };
  }
  if (op === 'cancel') {
    const value = await call('cancel', args.conversationId);
    return { cancelRequested: true, value: value === undefined ? null : JSON.parse(JSON.stringify(value)) };
  }
  throw new Error('Unknown operation');
}

function endpoint(value) {
  const url = new URL(value);
  if (url.protocol !== 'ws:' || url.hostname !== '127.0.0.1' || !url.port) throw new Error('Renderer endpoint is not a local websocket');
  if (url.username || url.password || url.search || url.hash) throw new Error('Renderer endpoint carries credentials or a query');
  if (!/^\/devtools\/page\/[A-Za-z0-9-]+$/.test(url.pathname)) throw new Error('Renderer endpoint is not a single page target');
  return url;
}

async function connect(wsURL) {
  const ws = new WebSocket(endpoint(wsURL));
  let next = 0;
  const pending = new Map();
  const fail = () => {
    for (const item of pending.values()) { clearTimeout(item.timer); item.reject(new Error('Renderer disconnected')); }
    pending.clear();
  };
  ws.addEventListener('message', event => {
    if (typeof event.data !== 'string' || event.data.length > LIMIT) { fail(); ws.close(); return; }
    let message;
    try { message = JSON.parse(event.data); } catch { fail(); ws.close(); return; }
    const item = pending.get(message.id);
    if (!item) return;
    pending.delete(message.id);
    clearTimeout(item.timer);
    message.error ? item.reject(new Error('Renderer protocol error')) : item.resolve(message.result);
  });
  ws.addEventListener('close', fail);
  ws.addEventListener('error', fail);
  await new Promise((resolve, reject) => {
    const timer = setTimeout(() => { ws.close(); reject(new Error('Connection timeout')); }, 10000);
    ws.addEventListener('open', () => { clearTimeout(timer); resolve(); }, { once: true });
    ws.addEventListener('error', () => { clearTimeout(timer); reject(new Error('Connection failed')); }, { once: true });
  });
  const run = expression => new Promise((resolve, reject) => {
    const id = ++next;
    const timer = setTimeout(() => { pending.delete(id); reject(new Error('Evaluation outcome unknown; never replay a mutation')); }, 20000);
    pending.set(id, { resolve, reject, timer });
    ws.send(JSON.stringify({ id, method: 'Runtime.evaluate', params: { expression, awaitPromise: true, returnByValue: true } }));
  });
  return { ws, run };
}

const input = readline.createInterface({ input: process.stdin, crlfDelay: Infinity });
for await (const line of input) {
  let id = null;
  try {
    if (line.length > LIMIT) throw new Error('Input too large');
    const message = JSON.parse(line);
    id = message.id ?? null;
    if (!message.ws || typeof message.ws !== 'string') throw new Error('Missing renderer endpoint');
    if (typeof message.op !== 'string') throw new Error('Missing operation');
    const { ws, run: evaluate } = await connect(message.ws);
    try {
      const probe = await evaluate("({native:typeof globalThis.__wbInvoke==='function',version:globalThis.workbuddyDesktop?.appVersion,platform:globalThis.workbuddyDesktop?.platform})");
      const identity = probe?.result?.value;
      if (probe?.exceptionDetails || !identity?.native || !VERSION.test(identity.version || '') || identity.platform !== 'win32') {
        throw new Error('Not a supported native WorkBuddy renderer');
      }
      const expression = `(${run.toString()})(${JSON.stringify(message.op)}, ${JSON.stringify(message.args ?? {})})`;
      const result = await evaluate(expression);
      if (result?.exceptionDetails) throw new Error('WorkBuddy operation returned an exception');
      process.stdout.write(JSON.stringify({ id, identity, value: result?.result?.value ?? null }) + '\n');
    } finally {
      ws.close();
    }
  } catch (error) {
    process.stdout.write(JSON.stringify({ id, error: String(error && error.message ? error.message : error).slice(0, 2000) }) + '\n');
  }
}
