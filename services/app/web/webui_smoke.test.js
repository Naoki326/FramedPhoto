/** 管理台浏览器冒烟（node vm，零 npm 依赖）——B5（#23）验收的自动化形态。
 *
 * 在 vm 里以「无 CommonJS 上下文」加载真实 web/api.js（浏览器分支 → fpApi）
 * 与真实 index.html 内联脚本，只 stub fetch 与最小 DOM，验证：
 * - 页面载入：内容清单只发一次请求（两个面板共享），各面板照常渲染；
 * - 刷新语义：并发调用共享同一 in-flight 请求，各自独立刷新照常发请求；
 * - 失败场景：非 2xx 统一错误格式（JSON detail 优先 / 非 JSON 回退状态文本）到达 UI；
 * - 校准/拍照 blob 场景：marked / pick 成功透传 Blob，错误同样走统一归一。
 *
 * 运行：node --test "services/app/web/*.test.js"（自仓库根目录）
 */
'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const WEB = __dirname;

/* ---------- stub 设施 ---------- */

/** 记录赋值历史与事件 handler 的元素 stub（Proxy 兜底任意属性）。 */
function makeElement(id) {
  const target = {
    id, style: {}, dataset: {}, files: [], value: '', checked: false, disabled: false,
    offsetWidth: 0, offsetHeight: 0, className: '', src: '', title: '', textContent: '', innerHTML: '',
    classList: { add() {}, remove() {}, contains: () => false, toggle() {} },
  };
  const history = { textContent: [], innerHTML: [], src: [], value: [] };
  const handlers = {};
  return new Proxy(target, {
    get(t, p) {
      switch (p) {
        case 'addEventListener': return (ev, fn) => { (handlers[ev] = handlers[ev] || []).push(fn); };
        case 'removeEventListener': return () => {};
        case 'appendChild': case 'removeChild': return c => c;
        case 'remove': return () => {};
        case 'querySelectorAll': return () => [];
        case 'querySelector': return () => null;
        case 'getBoundingClientRect': return () => ({ left: 0, top: 0, width: 100, height: 50 });
        case 'click': return () => {};
        case 'contains': return () => false;
        case '_history': return history;
        case '_handlers': return handlers;
        default: return t[p];
      }
    },
    set(t, p, v) {
      if (p in history) history[p].push(v);
      t[p] = v;
      return true;
    },
  });
}

function makeDocument() {
  const els = new Map();
  const doc = {
    getElementById(id) { if (!els.has(id)) els.set(id, makeElement(id)); return els.get(id); },
    querySelectorAll: () => [],
    createElement: tag => makeElement('<' + tag + '>'),
    addEventListener: () => {},
  };
  return { doc, els };
}

/** Response 形状的最小 stub（api.js 只用 ok/json/blob）。 */
const jsonResponse = (body, status = 200) => ({
  ok: status < 300, status, statusText: status < 300 ? 'OK' : 'Error',
  json: async () => body,
});
/** 非 2xx：body 指定 JSON（含 detail）时走 detail 分支；省略 body 则模拟非 JSON。 */
const errorResponse = (status, statusText, body) => ({
  ok: false, status, statusText,
  json: body === undefined ? async () => { throw new Error('body 非 JSON'); } : async () => body,
});
const blobResponse = blob => ({ ok: true, status: 200, statusText: 'OK', blob: async () => blob });

function createFetchStub(routes) {
  const calls = [];
  const fetchImpl = async (url, init) => {
    calls.push({ url, init });
    const r = routes.find(x => x.url === url);
    if (!r) throw new Error('smoke: 未路由的请求 ' + url);
    if (r.error) return errorResponse(r.error.status, r.error.statusText, r.error.body);
    if (r.blobBody !== undefined) return blobResponse(r.blobBody);
    return jsonResponse(r.body ?? {});
  };
  return { fetchImpl, calls };
}

/** FileReader stub：readAsDataURL 经微任务回调 onload（时序与浏览器一致）。 */
class StubFileReader {
  readAsDataURL(blob) {
    Promise.resolve().then(async () => {
      const text = await blob.text();
      this.result = 'data:image/png;base64,' + Buffer.from(text, 'utf8').toString('base64');
      if (this.onload) this.onload();
    });
  }
}
class StubFormData { append() {} }

let blobSeq = 0;

/** 载入真实 index.html 内联脚本 + 真实 api.js（浏览器分支）。 */
function loadPage(routes) {
  const html = fs.readFileSync(path.join(WEB, 'index.html'), 'utf8');
  const apiSrc = fs.readFileSync(path.join(WEB, 'api.js'), 'utf8');
  const inline = html.split('<script>')[1].split('</script>')[0];
  const { fetchImpl, calls } = createFetchStub(routes);
  const { doc } = makeDocument();
  const context = {
    fetch: fetchImpl, document: doc, console,
    setTimeout: () => 0, setInterval: () => 0, clearTimeout: () => {}, clearInterval: () => {},
    confirm: () => true, alert: () => {},
    URL: { createObjectURL: () => 'blob:fake-' + (++blobSeq) },
    FileReader: StubFileReader, FormData: StubFormData,
  };
  context.window = context;
  vm.createContext(context);
  vm.runInContext(apiSrc, context, { filename: 'api.js' });
  if (!context.fpApi) throw new Error('api.js 未在浏览器分支暴露 fpApi');
  vm.runInContext(inline, context, { filename: 'index-inline.js' });
  return { context, calls, el: id => doc.getElementById(id) };
}

/** 排干页面异步链（boot 的 fire-and-forget 调用无法直接 await）。 */
async function flush(rounds = 12) {
  for (let i = 0; i < rounds; i++) await new Promise(r => setImmediate(r));
}

/* ---------- 页面载入的标准应答 ---------- */

const SETTINGS = {
  config: {
    slot_segments: [{ start: '00:00', type: 'weather' }],
    free_rotate: false, weather_style: 'auto',
    qweather_city: '', qweather_location: null, free_modules: [],
  },
  status: { weather_style_today: '', weather_ready: true, ip_city: '', free_today: '', free_llm_ready: true },
};

const CALIBRATION = {
  calibrated: false, captured_at: '',
  device: [[0, 0, 0], [255, 255, 255], [255, 255, 0], [255, 0, 0], [0, 0, 255], [0, 255, 0]],
  defaults: [[0, 0, 0], [255, 255, 255], [255, 255, 0], [255, 0, 0], [0, 0, 255], [0, 255, 0]],
  names: ['黑', '白', '黄', '红', '蓝', '绿'],
};

function bootRoutes(overrides = []) {
  const base = [
    { url: '/health', body: { epd: 'ok' } },
    { url: '/api/images/display/current', body: { displaying: 'daily' } },
    { url: '/api/images/content', body: {
      images: [{ id: 'd1', filename: '回忆.png', preview_url: '/api/images/daily/preview',
                 caption: '测试回忆', date: '2026-08-17', memory_score: 9 }],
      source: 'daily' } },
    { url: '/api/sync/status', body: { status: 'idle', percent: 0 } },
    { url: '/api/analysis/scores?limit=500', body: { scores: [] } },
    { url: '/api/images/daily/selected', body: { path: null } },
    { url: '/api/images', body: { images: [] } },
    { url: '/api/images/content/pushed', body: {} },
    { url: '/api/devices', body: { devices: [] } },
    { url: '/api/ota/manifest', body: { detail: null } },
    { url: '/api/settings', body: SETTINGS },
    { url: '/api/calibration', body: CALIBRATION },
  ];
  return overrides.concat(base);
}

/** assert.include 的等价物（node:assert 无该 API）。 */
const include = (haystack, needle, msg) =>
  assert.ok(String(haystack).includes(needle), (msg || '') + `（期望包含 “${needle}”，实际：“${String(haystack).slice(0, 120)}”）`);

const contentCalls = calls => calls.filter(c => c.url === '/api/images/content');
const lastText = (el, id) => {
  const h = el(id)._history.textContent;
  return h.length ? String(h[h.length - 1]) : '';
};
const allText = (el, id) => el(id)._history.textContent.map(String).join('\n');

/* ---------- 冒烟断言 ---------- */

test('页面载入：内容清单单次请求共享，两个面板照常渲染', async () => {
  const { calls, el } = loadPage(bootRoutes());
  await flush();
  // 「当前显示」与「每日精选」两面板同时起跑 → 只发一次内容清单请求
  assert.equal(contentCalls(calls).length, 1, '内容清单应只请求一次（两面板共享）');
  // 两个面板都消费了同一份清单数据
  include(el('displayMode')._history.innerHTML.join(''), '时段自动内容');
  include(el('daily')._history.innerHTML.join(''), '回忆度');
  // 健康检查（绝对路径）/ 分析分数 / 同步状态迁移后照常请求
  for (const url of ['/health', '/api/analysis/scores?limit=500', '/api/sync/status']) {
    assert.ok(calls.some(c => c.url === url), '缺少请求 ' + url);
  }
  include(lastText(el, 'health'), 'EPD ok');
});

test('刷新语义：并发共享一次请求，各自独立刷新照常发请求', async () => {
  const { context, calls } = loadPage(bootRoutes());
  await flush();
  assert.equal(contentCalls(calls).length, 1);            // 页面载入：1 次
  await context.loadDisplay();
  assert.equal(contentCalls(calls).length, 2);            // 单面板刷新：照常 1 次
  await Promise.all([context.loadDisplay(), context.loadDaily()]);
  assert.equal(contentCalls(calls).length, 3);            // 并发：共享同一次
  await context.loadDisplay();
  await context.loadDaily();
  assert.equal(contentCalls(calls).length, 5);            // 顺序各自刷新：各 1 次
});

test('失败场景：非 2xx JSON detail 优先，统一格式错误提示到达 UI', async () => {
  const { context, el } = loadPage(bootRoutes([
    { url: '/api/calibration/photo',
      error: { status: 422, statusText: 'Unprocessable Entity', body: { detail: '采样点不足，请重拍' } } },
  ]));
  await flush();
  el('calPhoto').files = [{ name: 'p.jpg' }];
  await context.calUpload(false);                          // 「应用校准」分支（JSON）
  include(allText(el, 'toast'), '失败: 采样点不足，请重拍');
});

test('失败场景：body 非 JSON 时回退状态文本', async () => {
  const { context, el } = loadPage(bootRoutes([
    { url: '/api/calibration/photo', error: { status: 500, statusText: 'Internal Server Error' } },
  ]));
  await flush();
  el('calPhoto').files = [{ name: 'p.jpg' }];
  await context.calUpload(false);
  include(allText(el, 'toast'), '失败: Internal Server Error');
});

test('校准拍照 blob 场景：marked 成功透传 Blob 并上屏', async () => {
  const { context, el } = loadPage(bootRoutes([
    { url: '/api/calibration/photo/marked', blobBody: { type: 'image/png', text: async () => 'fake-marked-png' } },
  ]));
  await flush();
  el('calPhoto').files = [{ name: 'p.jpg' }];
  await context.calUpload(true);                           // 「核对采样位置」分支（blob）
  assert.ok(el('calMarked')._history.src.some(s => String(s).startsWith('blob:fake')),
    'marked 图应经 URL.createObjectURL 上屏');
  assert.equal(el('calMarked').style.display, '');
  include(lastText(el, 'calMarkedTip'), '红色采样框');
});

test('校准拍照 blob 场景：marked 失败同样走统一错误路径', async () => {
  const { context, el } = loadPage(bootRoutes([
    { url: '/api/calibration/photo/marked',
      error: { status: 422, statusText: 'Unprocessable Entity', body: { detail: '未检测到六条色带' } } },
  ]));
  await flush();
  el('calPhoto').files = [{ name: 'p.jpg' }];
  await context.calUpload(true);
  include(allText(el, 'toast'), '失败: 未检测到六条色带');
});

test('照片点选：pick blob 上传与取色 JSON 链路照常', async () => {
  const { calls, el } = loadPage(bootRoutes([
    { url: '/api/calibration/photo?mode=pick', blobBody: { type: 'image/png', text: async () => 'fake-pick-png' } },
    { url: '/api/calibration/pick/color', body: { color: [10, 20, 30] } },
  ]));
  await flush();
  // 触发「照片点选取色」的 change handler（选文件 → blob 上传 → FileReader 转 base64）
  const onChange = el('calPickPhoto')._handlers.change[0];
  assert.ok(onChange, 'calPickPhoto 应注册 change handler');
  await onChange({ target: { files: [{ name: 'pick.jpg' }] } });
  await flush();
  assert.ok(el('calPickImg')._history.src.some(s => String(s).startsWith('blob:fake')),
    '点选照片应经 URL.createObjectURL 上屏');
  // 触发照片点击取色（归一化坐标 0.5/0.5 → POST pick/color）
  const onClick = el('calPickImg')._handlers.click[0];
  assert.ok(onClick, 'calPickImg 应注册 click handler');
  await onClick({ target: el('calPickImg'), clientX: 50, clientY: 25 });
  await flush();
  const pick = calls.find(c => c.url === '/api/calibration/pick/color');
  assert.ok(pick, '应发起取色请求');
  const body = JSON.parse(pick.init.body);
  assert.equal(body.photo_b64, Buffer.from('fake-pick-png', 'utf8').toString('base64'));
  assert.equal(body.x, 0.5);
  assert.equal(body.y, 0.5);
  include(allText(el, 'toast'), '已取色');
});

test('人眼匹配保存与天气城市查询迁移照常', async () => {
  const unknown = '/api/weather/lookup?location=' + encodeURIComponent('不存在城市');
  const known = '/api/weather/lookup?location=' + encodeURIComponent('测试市');
  const { context, el } = loadPage(bootRoutes([
    { url: '/api/calibration/device', body: { status: CALIBRATION } },
    { url: unknown, body: { location: [] } },
    { url: known, body: { location: [{ name: '测试市', id: '999999', adm1: '测试省' }] } },
  ]));
  await flush();
  await context.calManualSave();                           // POST calibration/device（JSON）
  include(allText(el, 'toast'), '人眼匹配校准已应用');
  // 天气城市查询：查无此城 → 200 + 空列表的领域提示保留
  await assert.rejects(context.resolveCityToLocation('不存在城市'), { message: '城市“不存在城市”未找到，可改用城市 ID（如 101020300）' });
  // 查到 → 解析出 location ID 与城市标签（vm realm 对象，逐字段断言）
  const hit = await context.resolveCityToLocation('测试市');
  assert.equal(hit.location, '999999');
  assert.equal(hit.city, '测试省·测试市');
});
