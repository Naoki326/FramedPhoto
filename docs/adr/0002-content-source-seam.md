# ContentSource seam:渲染命名空间与 /{id}/raw 契约转正

六种「内容」的知识在 images.py 四处手工展开(/content 分支、/raw 前缀分发、固定
raw+preview 端点、manifest url 手拼),每种类型四份拷贝。决定把「内容源」定义为
**渲染命名空间**——id 前缀标识归属、独占该 id 到 FPS6 帧与原图的渲染知识,共五种:
内容库、每日精选、置顶显示、天气卡片、自由模块;接口为 `id_prefix` / `meta()` /
`render(id)` / `original(id)`,以有序注册表分发(`content_sources.py`,无前缀兜底
到内容库)。同时把 `GET /api/images/{id}/raw` 从旧固件兼容层**转正为唯一设备下载
契约**:manifest 的 url 字段全部指向它,10 条固定 raw/preview 路由
(daily/news/free/weather/display × 2)删除,preview 泛化为五源共用、统一原图缩放
~800px JPEG(承 ADR-0001)。固件零改动,WebUI 仅两处迁移。

## Considered Options

- **评审候选 1 原方案**(2026-08-16 架构评审):`/{source}/raw` 专用端点 +
  源方法 `manifest_item()` +「退役固件 id 拼 URL fallback」。被否决:新固件
  (2cf5454 起)只依赖 manifest 的 url 字段,`/{id}/raw` 已是 docs/api.md 文档化
  契约且旧固件兜底仍在用;专用端点不省任何东西,反而要改文档契约与固件兜底
  语义。前缀分发从「兼容层」转正为正式路由,id 拼 URL 兜底保留。
- **查找型 `render(id)`**(按指纹存档帧、id 即寻址键):被否决——四种生成源的
  id 是渲染字节的内容指纹,仅用于设备 NVS 变更检测,服务端一律服务「当前内容」;
  查找型为不存在的强一致需求引入按指纹寻址的存储。
- **「内容源 = 能进清单者」定义**(推送/置顶归为源):被否决——推送复用内容库
  id、无自有命名空间,是注入机制;置顶有 `display-` 命名空间与渲染分支,是源。
  渲染与「谁上屏」(置顶 > 推送 > 时段的注入优先级,留在 /content)是正交两轴,
  评审的 6×4 矩阵把两轴压扁正是 images.py 长成这样的原因。

## Consequences

- 新增内容类型 = 领域模块里一个新源适配器 + 注册表一行;不再碰 /content 分支、
  路由与 url 拼接。
- url/preview_url 组装是一个模块级 helper 函数(两处调用:/content 各分支、
  /display/current),**不是**源方法——源不负责 url。
- `news-` 保留为 free 源的第二前缀(旧固件兜底 `/news-xxx/raw` 仍命中),
  `/news/*` 固定别名路由删除;free 重生成移至 `POST /api/images/free/regenerate`。
- free 的 `original()` 收紧为只认自由模块自己落库的卡片,修掉「全 uploads 目录
  按 mtime 找最新 .orig」导致的误显。
- 测试以参数化契约测试为主:每源四断言(前缀分发命中、raw 可过 `parse_fps6`、
  preview 由原图而来且缺原图 404、`meta()` 字段齐全);固定路由用例随路由删除。
- 迁移四步 strangler:① seam 纯新增(行为零变化)→ ② 路由改道(双轨,旧路由
  保留但无内部调用者)→ ③ 退役(删 10 条路由 + WebUI 两处 + 测试同生死 +
  `daily_manual_pick()` 注解修正)→ ④ docs/api.md 契约转正描述。
