# 最小题库轮换实现记录

## 范围

- 产品：`services/app/free_module.py`、`story_rotation.py`、`story_catalog.py`。
- 离线测试：`services/tests/test_story_rotation.py`。
- 用户说明：`docs/ai-features.md` 题库轮换小节。未修改研究结论，不提交 git。
- 每类 20 条；默认 prompt 精确匹配启用，名称不限，改 prompt 回开放生成。
- 稳定 ID 按故事族使用，已知译名／别名映射旧标题；不做 embedding/MMR、不加依赖。
- 最少使用次数优先、容量减一的成功生成次数冷却、可行时分类变化。
- POSIX flock + 线程 RLock 覆盖选择、渲染、保存及记账；槽位使用起点加模块配置摘要。
- 同槽位刷新锁定原 ID，不再消耗；先存完整帧，再原子替换历史。缓存须有对应历史记录，未记账孤儿帧不放行。

## Red / green（无真实 API）

先写最小测试，再实现：

```text
services/.venv/bin/python -m pytest services/tests/test_story_rotation.py -q
RED: ImportError: cannot import name 'story_rotation'; collection error (0.16s)
```

首轮实现：

```text
services/.venv/bin/python -m pytest services/tests/test_story_rotation.py services/tests/test_free_module.py -q
26 passed, 16 warnings in 79.65s
```

补充文件锁、损坏历史统一 fail-closed、孤儿帧检查及风格保留后：

```text
services/.venv/bin/python -m pytest services/tests/test_story_rotation.py -q
11 passed in 0.15s

services/.venv/bin/python -m pytest services/tests/test_free_module.py -k 'same_segment or segment_changes or saves_card_png' -q
3 passed, 16 deselected, 8 warnings in 44.73s

git diff --check
# 无输出
```

主代理最终复核：完整执行 `services/.venv/bin/python -m pytest services/tests/test_story_rotation.py services/tests/test_free_module.py -q`，**30 passed**（81.29 秒）。`git diff --check` 通过。

尝试全套 `services/.venv/bin/python -m pytest services/tests -q`，300 秒超时，未完成，不能报告全套通过。改动前已观察到 `test_ai_config.py::test_get_never_returns_plaintext_secret` 失败（期待密钥掩码含 `…`，实际为空串）；改动后单独重跑仍是该失败，未在本次修复范围内处理。

警告为原有 Pillow getdata 弃用提示。
新增测试覆盖：无放回、跨周期、分类、别名、重载、生成失败不消耗、损坏历史、并发槽位幂等、
错误 LLM 身份降级、落盘失败、自定义损坏历史、孤儿缓存、模块槽位区分、风格保留。

## 限制与待审核

- 文献／作品名称与章节为人工整理，未逐条在线核验或做专业史料审校；史书记述不等于每个细节均获独立证明。
- 只校验 ID／标题／字段，不能识别“伪装成正确 ID 的错误正文”；仍须抽样审核文案和画面。
- 单次文本请求 30 秒，不加重试；原有两次图片尝试和供应商异步轮询预算未重构。
- 历史与内容库不是共同事务；崩溃或记账失败可能留未被返回的归档卡，但不会把未记账缓存直接当已完成返回。
- 文件原子替换有 fsync 文件内容，未实现目录 fsync 的断电级保证；flock 需要 POSIX 及可靠共享文件锁，不是多机事务服务。
- 记录的是成功生成，不是设备展示；管理台刷新也属于生成，同槽位已有记录不再计数。
- 未知旧标题不伪装完成迁移；旧模块改名之前的非默认名称无法自动推断。
- 历史完整保留，尚无压缩；有限题库会循环，冷却按次数而非自然日。全套测试在本次 300 秒预算内未完成。
