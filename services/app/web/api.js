/* 管理台 api() helper —— 独立 seam（可注入 fetch，零依赖，双环境加载）。
 *
 * 浏览器：管理台页面以 <script src="/api.js"> 引用，暴露 window.fpApi
 *   = { api, createApi }；与 index.html 内既有 api() 并存（expand 步，
 *   不迁移调用点，现页面行为不变）。
 * Node：module.exports = { createApi }，由 api.test.js（node --test）
 *   直接引用，零 npm 依赖、不引入构建链。
 *
 * 行为契约（与旧内联 api() 兼容并加深半步）：
 * - 路径：默认拼 /api 前缀（'/images' → '/api/images'）；
 *   opts.absolute 为真时绝对路径透传（如 '/health'），不拼接。
 * - 响应：默认解析 JSON；opts.blob 为真时返回 Blob。
 * - 错误归一：非 2xx 时优先读 JSON detail；body 非 JSON（或无 detail）
 *   回退状态文本；状态文本为空再回退 'HTTP <status>'。blob 场景
 *   同样走这条错误路径。
 * - opts 其余键（method/headers/body…）原样传给 fetch；blob / absolute
 *   两个自定义键不外泄给 fetch。
 */
(function (root) {
  'use strict';

  /** 创建 api(path, opts)。fetchImpl 可注入；缺省在调用时取全局 fetch。 */
  function createApi(fetchImpl) {
    return async function api(path, opts) {
      opts = opts || {};
      const doFetch = fetchImpl || root.fetch;
      if (!doFetch) throw new Error('api(): 无可用 fetch 实现，可经 createApi(fetch) 注入');
      // 路径规则：默认拼 /api 前缀；absolute 为真时绝对路径透传不拼接
      const url = opts.absolute ? path : '/api' + path;
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
    module.exports = { createApi: createApi };
  } else {
    root.fpApi = { createApi: createApi, api: createApi() };
  }
})(typeof globalThis !== 'undefined' ? globalThis : this);
