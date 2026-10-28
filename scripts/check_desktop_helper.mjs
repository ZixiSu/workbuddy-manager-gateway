// Simulated checks for the renderer-facing function in buddygate/desktop_console.mjs.
//
// The function that runs inside the renderer is extracted from the real file and
// executed against a fake __wbInvoke, so no browser, desktop app, account or
// network is involved. This proves the operation contracts, not a live run.
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import vm from 'node:vm';

const root = join(dirname(fileURLToPath(import.meta.url)), '..');
const source = readFileSync(join(root, 'buddygate', 'desktop_console.mjs'), 'utf8');
const beforeEndpoint = source.split('\nfunction endpoint(value) {')[0];
const start = beforeEndpoint.indexOf('async function run(');
if (start < 0) throw new Error('Renderer function was not found in desktop_console.mjs');
const extracted = beforeEndpoint.slice(start);
if (extracted.includes('import ')) throw new Error('Renderer function must stay self-contained');

const calls = [];
const conversationId = 'conversation-fixture';
const requestId = '01890000-0000-7000-8000-000000000000';

globalThis.workbuddyDesktop = { appVersion: '5.9.3', platform: 'win32' };
let pendingValue = null;
let requestsValue = [];
let sendCalls = 0;
let conversationInfo = {
  id: conversationId, title: '【派工】标题', state: 'active',
  hasActiveChildAgents: false, hasRunningBackgroundTasks: false,
};
globalThis.__wbInvoke = async (method, _context, ...args) => {
  calls.push({ method, args });
  if (method === 'wb:conversations:create') {
    const config = args[0];
    return { info: { id: config.conversationId, title: config.title, state: 'idle' }, config: { model: config.model ?? null } };
  }
  if (method === 'wb:conversations:sendPrompt') {
    sendCalls += 1;
    return new Promise(() => {}); // A long run never settles quickly.
  }
  if (method === 'wb:conversations:get') return { info: conversationInfo };
  if (method === 'wb:conversations:requests') return { items: requestsValue };
  if (method === 'wb:conversations:getPendingInfo') return pendingValue;
  if (method === 'wb:conversations:resolvePending') return { ok: true };
  if (method === 'wb:conversations:cancel') return { stopped: false };
  throw new Error('Unexpected method ' + method);
};

vm.runInThisContext(extracted, { filename: 'desktop_console.mjs' });
assert.equal(typeof globalThis.run, 'function', 'Renderer function must be callable');

const identity = await globalThis.run('identity', {});
assert.equal(identity.native, true);
assert.equal(identity.version, '5.9.3');
assert.equal(identity.platform, 'win32');

const created = await globalThis.run('create', { conversationId, title: '【派工】标题', cwd: 'C:/work', model: 'model-a' });
assert.equal(created.id, conversationId);
assert.equal(created.title, '【派工】标题');
const createCall = calls.find(item => item.method === 'wb:conversations:create');
assert.equal(createCall.args[0].transport, 'local');
assert.equal(createCall.args[0].model, 'model-a');
await globalThis.run('create', { conversationId: 'second', title: 't', cwd: 'C:/work', model: null });
const withoutModel = calls.filter(item => item.method === 'wb:conversations:create')[1];
assert.equal('model' in withoutModel.args[0], false, 'model must only be sent when requested');

// The reserved conversation is read, never created again.
const conversation = await globalThis.run('conversation', { conversationId });
assert.equal(conversation.exists, true);
assert.equal(conversation.state, 'active');
assert.equal(calls.filter(item => item.method === 'wb:conversations:create').length, 2,
  'a conversation check must not create anything');

const sent = await globalThis.run('send', { conversationId, text: '任务正文', requestId });
assert.deepEqual(sent, { dispatched: true, state: 'sending' });
assert.equal(sendCalls, 1);
const sendCall = calls.find(item => item.method === 'wb:conversations:sendPrompt');
assert.equal(sendCall.args[0], conversationId);
assert.equal(sendCall.args[1][0].type, 'text');
assert.equal(sendCall.args[1][0].text, '任务正文');
assert.equal(sendCall.args[2].clientRequestId, requestId);
assert.equal(sendCall.args[2]._meta['codebuddy.ai/conversationRequestId'], requestId);
await assert.rejects(() => globalThis.run('send', { conversationId, text: '再次发送', requestId }),
  /already dispatched/, 'the same request ID must never be dispatched twice');
assert.equal(sendCalls, 1);

// The observed details counters are carried; cost is named but never valued.
requestsValue = [
  { clientRequestId: 'other-request', state: 'running', assistantMessage: '别的请求' },
  { clientRequestId: requestId, state: 'completed',
    requestedModel: { id: 'deepseek-v4.1-flash', name: 'DeepSeek-V4.1-Flash' },
    responseModel: { id: 'deepseek-v4.1-flash' },
    usage: {
      inputTokens: 37200, totalTokens: 37200, contextWindow: 300000,
      details: {
        promptTokens: 34265, completionTokens: 289,
        promptCacheHitTokens: 34048, promptCacheMissTokens: 217, promptCacheWriteTokens: 0,
      },
      byCategory: { systemPrompt: 6460, conversation: 0, tools: 27524, mcp: 503, skills: 2719, version: 1 },
      cost: 1.23, estimatedCost: { amount: 1.23, currency: 'CNY' }, label: 'not-a-number',
    },
    assistantMessage: { content: [{ type: 'text', text: '第一段' }, { type: 'text', text: '第二段' }] } },
];
const status = await globalThis.run('status', { conversationId, requestId });
assert.equal(status.requestSeen, true);
assert.equal(status.state, 'completed');
assert.equal(status.conversationState, 'active');
assert.equal(status.conversationId, conversationId);
assert.equal(status.requestCount, 2);
assert.deepEqual(status.usage.top, { inputTokens: 37200, totalTokens: 37200, contextWindow: 300000 });
assert.deepEqual(status.usage.details, {
  promptTokens: 34265, completionTokens: 289,
  promptCacheHitTokens: 34048, promptCacheMissTokens: 217, promptCacheWriteTokens: 0,
});
assert.deepEqual(status.usage.observed_detail_fields,
  ['promptTokens', 'completionTokens', 'promptCacheHitTokens', 'promptCacheMissTokens', 'promptCacheWriteTokens']);
assert.deepEqual(status.usage.byCategory,
  { systemPrompt: 6460, conversation: 0, tools: 27524, mcp: 503, skills: 2719 },
  'the context breakdown is carried without its version metadata');
assert.deepEqual(status.usage.observed_context_fields,
  ['systemPrompt', 'conversation', 'tools', 'mcp', 'skills']);
assert.deepEqual(status.usage.invalid_fields, [], 'only real counters are forwarded');
assert.ok(status.usage.ignored_fields.includes('cost'), 'a cost field is named, not converted');
assert.ok(status.usage.ignored_fields.includes('estimatedCost'));
assert.equal(JSON.stringify(status).includes('CNY'), false, 'cost values must not leave the page');
assert.deepEqual(status.usageFields, ['inputTokens', 'totalTokens', 'contextWindow']);
assert.equal(status.hasActiveChildAgents, false);
assert.equal(status.hasRunningBackgroundTasks, false);
assert.equal(status.assistant, '第一段第二段');
assert.deepEqual(status.requestedModel, { id: 'deepseek-v4.1-flash', name: 'DeepSeek-V4.1-Flash' });
assert.equal(status.dispatch.state, 'sending');

// Activity flags are tri-state: only a real boolean may claim "not running".
conversationInfo = { id: conversationId, state: 'active', hasActiveChildAgents: true };
const activeChildren = await globalThis.run('status', { conversationId, requestId });
assert.equal(activeChildren.hasActiveChildAgents, true);
assert.equal(activeChildren.hasRunningBackgroundTasks, null, 'a missing flag stays unknown, not false');
delete conversationInfo.hasActiveChildAgents;
const unknownChildren = await globalThis.run('status', { conversationId, requestId });
assert.equal(unknownChildren.hasActiveChildAgents, null);
conversationInfo = { id: conversationId, state: 'active', hasActiveChildAgents: false, hasRunningBackgroundTasks: false };

// Non-counters (bool, string, NaN, Infinity, negative, fractional) are not forwarded as data.
requestsValue = [{ clientRequestId: requestId, state: 'completed', usage: {
  inputTokens: true, totalTokens: '37200', contextWindow: Number.NaN,
  details: { promptTokens: Number.POSITIVE_INFINITY, completionTokens: -1, promptCacheHitTokens: 1.5 },
} }];
const invalid = await globalThis.run('status', { conversationId, requestId });
assert.deepEqual(invalid.usage.top, {});
assert.deepEqual(invalid.usage.details, {});
assert.deepEqual(invalid.usage.invalid_fields.slice().sort(),
  ['contextWindow', 'details.completionTokens', 'details.promptCacheHitTokens',
    'details.promptTokens', 'inputTokens', 'totalTokens'].sort());

const unmatched = await globalThis.run('status', { conversationId, requestId: 'unknown-request' });
assert.equal(unmatched.requestSeen, false);
assert.equal(unmatched.state, null);
assert.equal(unmatched.assistant, '');
assert.equal(unmatched.usage, null);
assert.equal(unmatched.matchBy, null);

// An application restart drops clientRequestId; the canonical id is the only
// surviving evidence, so it is matched only when it was observed earlier.
requestsValue = [
  { id: 'canonical-request-1', conversationRequestId: 'canonical-request-1', state: 'completed',
    assistantMessage: '重启后的请求', usage: { inputTokens: 10, outputTokens: 2, totalTokens: 12 } },
  { id: 'canonical-request-2', conversationRequestId: 'canonical-request-2', state: 'running' },
];
const anonymous = await globalThis.run('status', { conversationId, requestId });
assert.equal(anonymous.requestSeen, false, 'a client id that is gone must not be guessed');
assert.equal(anonymous.requestsWithoutClientRequestId, 2);
const byNative = await globalThis.run('status', {
  conversationId, requestId, nativeRequestId: 'canonical-request-2',
});
assert.equal(byNative.requestSeen, true);
assert.equal(byNative.matchBy, 'native_request_id');
assert.equal(byNative.nativeRequestId, 'canonical-request-2');
assert.equal(byNative.nativeRequestIdSource, 'conversationRequestId');
assert.equal(byNative.assistant, '', 'only the matched request is read');
// The client UUID is not the canonical id: an id field that equals it proves nothing here.
const clientUuidOnly = await globalThis.run('status', {
  conversationId, requestId, nativeRequestId: requestId,
});
assert.equal(clientUuidOnly.requestSeen, false);
assert.equal(clientUuidOnly.nativeRequestId, null);
// Two requests answering to the same identity are ambiguous, not merged.
requestsValue = [
  { id: 'canonical-request-3', conversationRequestId: 'canonical-request-3', state: 'completed',
    usage: { inputTokens: 10, outputTokens: 2, totalTokens: 12 } },
  { id: 'canonical-request-3-copy', conversationRequestId: 'canonical-request-3', state: 'running' },
];
const ambiguous = await globalThis.run('status', {
  conversationId, requestId, nativeRequestId: 'canonical-request-3',
});
assert.equal(ambiguous.matchAmbiguous, true);
assert.equal(ambiguous.requestSeen, false);
assert.equal(ambiguous.usage, null, 'an ambiguous identity exposes no usage');

pendingValue = { id: 'pending-1', options: [{ optionId: 'once', kind: 'allow_once' }] };
const pending = await globalThis.run('pending', { conversationId });
assert.equal(pending.pending.id, 'pending-1');
pendingValue = { id: 'pending-huge', padding: 'x'.repeat(9000) };
const truncated = await globalThis.run('pending', { conversationId });
assert.deepEqual(truncated.pending, { truncated: true }, 'oversized pending payloads are not forwarded');
pendingValue = null;

await globalThis.run('resolve', { conversationId, pendingId: 'pending-1', response: { optionId: 'once' } });
const resolved = calls.filter(item => item.method === 'wb:conversations:resolvePending').pop();
assert.deepEqual(resolved.args, [conversationId, 'pending-1', { optionId: 'once' }]);

await globalThis.run('cancel', { conversationId });
const cancelled = calls.filter(item => item.method === 'wb:conversations:cancel').pop();
assert.deepEqual(cancelled.args, [conversationId]);

await assert.rejects(() => globalThis.run('unknown-operation', {}), /Unknown operation/);
for (const call of calls) {
  assert.ok(call.method.startsWith('wb:conversations:'), 'only conversation methods are called');
}
assert.equal(calls.filter(item => item.method === 'wb:conversations:create').length, 2,
  'no operation may create a conversation implicitly');

console.log('desktop helper checks passed');
