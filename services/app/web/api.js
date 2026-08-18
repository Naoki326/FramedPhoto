/* 管理台 api() helper —— 独立 seam（可注入 fetch，零依赖，双环境加载）。
 *
 * 浏览器：管理台页面以 <script src="/api.js"> 引用，暴露 window.fpApi
 *   = { api, createApi }；与 index.html 内既有 api() 并存（expand 步，
 *   不迁移调用点，现页面行为不变）。
 * Node：module.exports = { createApi }，由 api.test.js（node --test）
 *   直接引用，零 npm 依赖、不引入构建链。
 *
 * 行为契约（与旧内联 api() 兼容并加深）：
 * - 部署前缀：默认从加载自身的 <script src> 推导部署前缀（detectBase）。
 *   前缀代理部署（如 nginx /apps/frame/）下 src 为 /apps/frame/api.js，
 *   推导出 base=/apps/frame；根路径部署（直连）base 为空串。
 *   api() 不再依赖代理层重写 JS 内容 —— URL 前缀由本模块自己拼。
 * - 路径：默认拼 <base>/api 前缀（'/images' → '<base>/api/images'）；
 *   opts.absolute 为真时拼 <base> + path（页面根路径，如 '/health'），
 *   不拼 /api 段。
 * - 响应：默认解析 JSON；opts.blob 为真时返回 Blob。
 * - 错误归一：非 2xx 时优先读 JSON detail；body 非 JSON（或无 detail）
 *   回退状态文本；状态文本为空再回退 'HTTP <status>'。blob 场景
 *   同样走这条错误路径。
 * - opts 其余键（method/headers/body…）原样传给 fetch；blob / absolute
 *   两个自定义键不外泄给 fetch。
 */
(function (root) {
  'use strict';

  /** 从加载自身的 script 标签推导部署前缀：去掉文件名段，保留目录。
   *
   * 浏览器经典脚本执行时 document.currentScript 指向自身；直连部署
   * （/api.js）推导出 ''，前缀代理部署（/apps/frame/api.js）推导出
   * '/apps/frame'。非浏览器或取不到时一律 ''（等价根路径部署）。
   * 导出供 node --test 直接验证推导表。
   */
  function detectBase(env) {
    env = env || root;
    const cur = env.document && env.document.currentScript;
    if (!cur || !cur.src) return '';
    try {
      const base = new URL(cur.src).pathname.split('/').slice(0, -1).join('/');
      return base === '/' ? '' : base;
    } catch (_) { return ''; }
  }

  /** 创建 api(path, opts)。fetchImpl 可注入；缺省在调用时取全局 fetch。
   *  base 显式注入供测试；缺省在创建时用 detectBase() 推导一次。 */
  function createApi(fetchImpl, base) {
    const prefix = base !== undefined ? base : detectBase();
    return async function api(path, opts) {
      opts = opts || {};
      const doFetch = fetchImpl || root.fetch;
      if (!doFetch) throw new Error('api(): 无可用 fetch 实现，可经 createApi(fetch) 注入');
      // 路径规则：默认拼 <base>/api 前缀；absolute 为真时拼 <base> + path
      const url = opts.absolute ? prefix + path : prefix + '/api' + path;
      // blob / absolute 是本 helper 的自定义键，不外泄给 fetch
      const init = Object.assign({}, opts);
      delete init.absolute;
      delete init.blob;
      const resp = await doFetch(url, init);
      if (!resp.ok) throw await normalizeError(resp);
      return opts.blob ? resp.blob() : resp.json();
    };
  }

  /** 非 2xx 的统一错误归一：JSON detail 优先，非 JSON 回退状态文本。 */
  async function normalizeError(resp) {
    let msg = '';
    try {
      const body = await resp.json();
      if (body && body.detail) msg = String(body.detail);
    } catch (_) { /* body 非 JSON → 状态文本回退 */ }
    if (!msg) msg = resp.statusText || ('HTTP ' + resp.status);
    return new Error(msg);
  }

  if (typeof module === 'object' && module.exports) {
    module.exports = { createApi: createApi, detectBase: detectBase };
  } else {
    root.fpApi = { createApi: createApi, detectBase: detectBase, api: createApi() };
  }
})(typeof globalThis !== 'undefined' ? globalThis : this);
