/** 管理台 api() helper 契约测试（node 内置 test runner，零 npm 依赖）。
 *
 * 运行：node --test "services/app/web/*.test.js"（自仓库根目录）
 * seam：对注入 fetch 的输入输出，不测实现细节。
 */
'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const { createApi } = require('./api.js');

/** 造一个记录调用的 fetch stub：返回固定 Response。 */
function stubFetch(resp) {
  const calls = [];
  const fetchImpl = async (url, init) => {
    calls.push({ url, init });
    return resp;
  };
  fetchImpl.calls = calls;
  return fetchImpl;
}

// ---------- 错误归一三分支 ----------

test('非 2xx：body 是 JSON 且带 detail 时，错误消息用 detail', async () => {
  const api = createApi(stubFetch(
    Response.json({ detail: '该照片未找到' }, { status: 404, statusText: 'Not Found' }),
  ));
  await assert.rejects(api('/images/1'), { message: '该照片未找到' });
});

test('非 2xx：body 非 JSON 时，错误消息回退状态文本', async () => {
  const api = createApi(stubFetch(
    new Response('Internal Server Error', { status: 500, statusText: 'Internal Server Error' }),
  ));
  await assert.rejects(api('/settings'), { message: 'Internal Server Error' });
});

test('非 2xx：JSON body 无 detail 时同样回退状态文本', async () => {
  const api = createApi(stubFetch(
    Response.json({ other: 1 }, { status: 502, statusText: 'Bad Gateway' }),
  ));
  await assert.rejects(api('/devices'), { message: 'Bad Gateway' });
});

test('非 2xx：状态文本为空时回退 HTTP <status>', async () => {
  const api = createApi(stubFetch(
    new Response('oops', { status: 503, statusText: '' }),
  ));
  await assert.rejects(api('/ota/manifest'), { message: 'HTTP 503' });
});

// ---------- 路径拼接两种形态 ----------

test('默认形态：路径拼 /api 前缀', async () => {
  const f = stubFetch(Response.json({ ok: true }));
  await createApi(f)('/images/content');
  assert.equal(f.calls[0].url, '/api/images/content');
});

test('absolute 形态：页面根路径拼部署前缀（无前缀时透传）', async () => {
  const f = stubFetch(Response.json({ status: 'ok' }));
  await createApi(f)('/health', { absolute: true });
  assert.equal(f.calls[0].url, '/health');
});

// ---------- 部署前缀感知（前缀代理部署的回归锁） ----------
// 场景：管理台经 nginx 前缀代理（/apps/frame/）部署时，api() 必须把
// 调用打到前缀下，而不是裸根路径（否则请求会落到代理层根站点）。

test('前缀部署：默认形态拼 <base>/api<path>', async () => {
  const f = stubFetch(Response.json({ ok: true }));
  await createApi(f, '/apps/frame')('/images/content');
  assert.equal(f.calls[0].url, '/apps/frame/api/images/content');
});

test('前缀部署：absolute 形态拼 <base><path>', async () => {
  const f = stubFetch(Response.json({ status: 'ok' }));
  await createApi(f, '/apps/frame')('/health', { absolute: true });
  assert.equal(f.calls[0].url, '/apps/frame/health');
});

test('前缀部署：空字符串 base 与未注入 base 行为一致', async () => {
  const f = stubFetch(Response.json({ ok: true }));
  await createApi(f, '')('/images');
  assert.equal(f.calls[0].url, '/api/images');
});

// ---------- detectBase：从 currentScript 推导前缀 ----------

const { detectBase } = require('./api.js');

test('detectBase：前缀部署的 script src 推导出 /apps/frame', () => {
  const document = { currentScript: { src: 'http://mini.local:8080/apps/frame/api.js' } };
  assert.equal(detectBase({ document }), '/apps/frame');
});

test('detectBase：根路径部署推导出空前缀', () => {
  const document = { currentScript: { src: 'http://127.0.0.1:8010/api.js' } };
  assert.equal(detectBase({ document }), '');
});

test('detectBase：src 带 query 串时仍取 pathname', () => {
  const document = { currentScript: { src: 'http://mini.local:8080/apps/frame/api.js?v=3' } };
  assert.equal(detectBase({ document }), '/apps/frame');
});

test('detectBase：无 document / 无 currentScript / 坏 src 一律空前缀', () => {
  assert.equal(detectBase({}), '');
  assert.equal(detectBase({ document: {} }), '');
  assert.equal(detectBase({ document: { currentScript: { src: '::bad::' } } }), '');
});

test('自定义键不外泄：absolute / blob 不进入传给 fetch 的 init', async () => {
  const f = stubFetch(Response.json({ ok: true }));
  await createApi(f)('/health', { absolute: true, method: 'POST' });
  assert.equal(f.calls[0].init.method, 'POST');
  assert.ok(!('absolute' in f.calls[0].init));
  assert.ok(!('blob' in f.calls[0].init));
});

// ---------- 成功路径 ----------

test('默认返回解析后的 JSON', async () => {
  const api = createApi(stubFetch(Response.json({ devices: [1, 2] })));
  assert.deepEqual(await api('/devices'), { devices: [1, 2] });
});

test('blob 形态：2xx 时透传响应 Blob（类型与内容不变）', async () => {
  const payload = 'fake-png-bytes';
  const api = createApi(stubFetch(
    new Response(new Blob([payload], { type: 'image/png' }), { status: 200 }),
  ));
  const blob = await api('/calibration/photo/marked', { method: 'POST', blob: true });
  assert.ok(blob instanceof Blob);
  assert.equal(blob.type, 'image/png');
  assert.equal(await blob.text(), payload);
});

test('blob 场景错误路径：非 2xx 同样读 JSON detail', async () => {
  const api = createApi(stubFetch(
    Response.json({ detail: '采样点不足' }, { status: 422, statusText: 'Unprocessable Entity' }),
  ));
  await assert.rejects(
    api('/calibration/photo', { method: 'POST', blob: true }),
    { message: '采样点不足' },
  );
});

test('blob 场景错误路径：body 非 JSON 回退状态文本', async () => {
  const api = createApi(stubFetch(
    new Response('gateway down', { status: 504, statusText: 'Gateway Timeout' }),
  ));
  await assert.rejects(
    api('/calibration/photo', { method: 'POST', blob: true }),
    { message: 'Gateway Timeout' },
  );
});
