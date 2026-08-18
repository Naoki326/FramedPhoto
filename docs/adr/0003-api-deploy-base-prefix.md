# 部署前缀感知:api() 自己拼 URL 前缀,代理层零 JS 内容重写

管理台经 nginx 前缀代理(`/apps/frame/` → `:8010/`)部署。此前依赖代理层
sub_filter 重写响应文本里的 `/api` 字面量补前缀——B3(#21)把 api() 从 index.html
内联抽成独立 `/api.js` 后,FastAPI 以 `text/javascript` serve 该文件,恰好落在
sub_filter_types 只列了 `application/javascript` 的盲区,重写失效,页面所有
api() 调用打到代理层根站点 404,「照片全丢、设置全空」(数据无损,纯前端断链)。
决定:URL 前缀归 api() 自己管——脚本执行时从 `document.currentScript.src`
推导部署前缀(`/apps/frame/api.js` → `/apps/frame`,根路径部署 → 空串),默认拼
`<base>/api<path>`,`absolute` 形态语义升级为拼 `<base><path>`(页面根路径);
代理层对 JS 内容零重写,只保留 HTML/CSS/JSON 中字面 URL 的重写(`src="/`、
`href="/`、`"/api/`,JSON 响应里的 preview_url 等字段仍需补前缀)。

## Considered Options

- **继续 sub_filter 内容重写,补齐 Content-Type**(text/javascript 加入
  sub_filter_types):被否决——内容重写对前端每种新写法(单引号/双引号/模板串/
  运行时拼接)都要追加规则,这次事故正是 B3 换了一种文件形态就断链;规则间还会
  互相叠加出双重前缀。
- **服务端注入部署前缀**(root_path / 模板变量):被否决——FastAPI 需感知代理
  形态,本地直连与代理部署两套行为,静态文件也要过模板引擎,复杂度不成比例。

## Consequences

- api() 的 URL 正确性收敛到一个模块(api.js),契约测试(node --test,注入
  fetch + 注入 base)锁住两种部署形态;代理配置只剩「字面 URL 前缀化」一种稳定
  期职责。
- `<script src="/api.js?v=N">` 引用需带版本参数:该响应无 ETag/Last-Modified,
  升级 api.js 后靠版本参数破浏览器启发式缓存(本次事故修复时踩过)。
- HTML 里不走 api() 的字面 URL(window.open、模板串 img src)仍靠代理层
  HTML 重写——新增此类调用点时,要么走 api(),要么确认字面量以 `"/` 起头可被
  重写。
