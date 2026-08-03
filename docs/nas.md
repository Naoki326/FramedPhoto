# 接入群晖 NAS 作为照片数据源

照片库放在群晖 NAS 上，FramedPhoto 通过 **SMB 挂载**直接读取 NAS 照片，
分析（`analyze_photos.py`）与每日精选（`daily.py`）均以挂载点路径访问。

## 群晖侧准备

1. 控制面板 → 文件服务 → **SMB/AFP** → 启用 SMB 服务
2. 控制面板 → 共享文件夹 → 新建/确认一个共享（如 `photo`），
   把照片放进去（可按年份/事件分子目录）
3. 控制面板 → 用户账号 → 确认有可用的 SMB 用户名/密码

## 本机挂载（macOS / Linux）

```bash
# 1) 配置（不提交到 git，.env 已被忽略）
cd services && cp .env.example .env

# .env 里填：
NAS_HOST=192.168.1.5          # 群晖 IP 或主机名
NAS_SHARE=photo               # 共享名
NAS_USER=yourname             # SMB 用户名
NAS_PASSWORD=                 # 密码（留空则挂载时交互输入）
NAS_MOUNT_POINT=/Volumes/framedphoto-nas   # 挂载点（固定，勿改）

# 2) 挂载 / 查看 / 卸载
./scripts/mount_nas.sh mount
./scripts/mount_nas.sh status
./scripts/mount_nas.sh umount
```

> **挂载点必须固定**：分析结果在数据库里存的是照片的绝对路径，
> 挂载点改变会导致已分析照片失效（重新分析即可恢复）。

## 配置照片库路径

```bash
# .env 里把 PHOTO_LIB_DIR 指向挂载点下的照片目录
PHOTO_LIB_DIR=/Volumes/framedphoto-nas/photo
```

## 使用流程

```bash
# 1. 挂载 NAS
./scripts/mount_nas.sh mount

# 2. 全量分析 NAS 照片（AI 评分；断点续跑，只分析新增）
python tools/analyze_photos.py /Volumes/framedphoto-nas/photo -j 4

# 3. 启动服务（每日精选自动从 NAS 照片里选片）
cd services && uvicorn app.main:app
```

## NAS 掉线的容错

| 场景 | 行为 |
| --- | --- |
| 分析时 NAS 未挂载 | `analyze_photos.py` 明确报错并提示运行 `mount_nas.sh mount` |
| 每日精选选中的照片不可达 | 跳过该候选，选下一张（`daily.py` 已做存在性检查） |
| 照片被删除 / 挂载点变化 | 运行 `python tools/analyze_photos.py <dir> --prune-stale` 清理失效记录 |

## 其它方案（按需）

- **rsync 同步**：不依赖实时挂载，`rsync -av nas:/photo/ ./photos/` 定期同步到本地
- **Synology Photos API**：直接调群晖相册 API（开发量大，暂不支持）

## 注意

- `NAS_PASSWORD` 写入 `.env` 有泄露风险（.env 已被 gitignore，本机权限默认 644，
  建议 `chmod 600 services/.env`）；也可留空每次交互输入
- 挂载对 NAS 掉线敏感：相框轮询期间 NAS 不可达，每日精选会自动跳过
  不可达照片，不会崩溃
