# 接入群晖 NAS 作为照片数据源

照片库放在群晖 NAS 上。按 NAS 部署位置选择接入方式：

## 方案 A：远程 NAS（IPv6 公网）— rsync over SSH（推荐）

**不要用 SMB 直连**：公网 SMB 慢、易断、445 端口暴露不安全，且每次读图全量传文件。

```
群晖 NAS（IPv6 公网）
   │  rsync over SSH（增量、加密）
   ▼
本机同步目录 ./photos  ←─ PHOTO_LIB_DIR 指向这里
   │  分析/每日精选全在本地跑（NAS 掉线不影响）
   ▼
服务端 → 相框
```

### 群晖侧准备

1. 控制面板 → 终端机和 SNMP → 启用 **SSH**
2. 确认照片目录路径（默认 `/volume1/photo`，可在 File Station 里看属性）
3. （可选）为 FramedPhoto 建一个只读权限的专用用户，或用管理员账号

### 本机配置与同步

```bash
# .env 配置
NAS_SSH_HOST=ds923             # SSH config 别名 / IPv6 / 域名
NAS_SSH_USER=root
NAS_SSH_PORT=22
NAS_PHOTO_DIR=/volume1/photo/相片
NAS_RSYNC_PATH=/usr/local/bin/rsync   # 群晖必须：/usr/bin/rsync 被 hook 成
                                       # daemon 模式，synocli-net 套件的真 rsync
NAS_RSYNC_BWLIMIT=100          # 限速 KB/s（0 = 不限）
PHOTO_LIB_DIR=./photos

# 增量同步（首次全量，之后只传新增）
./scripts/sync_nas.sh
# 演练 / 删除本机已不存在的照片
./scripts/sync_nas.sh --dry-run
./scripts/sync_nas.sh --delete
```

### 同步实现细节（踩过的坑，勿回退）

- **macOS 自带 openrsync 与群晖 rsync 协议不兼容**，脚本会优先使用 brew 官方 rsync
  （`/opt/homebrew/bin/rsync`，当前 3.4.4）；两个候选都找不到才退回系统 rsync。
- **群晖相片目录 POSIX 权限为 000**（SMB 共享权限模式下），脚本用
  `--chmod=Du+rwx,Dg+rx,Fu+rw,Fgo+r` 修复，否则本地文件不可读。
- **默认排除**：`@eaDir` / `#recycle` / `.DS_Store` / `Thumbs.db` / `N8BookData/`，以及所有非图片文件——相框只需要静态图片。
  图片扩展名大小写不敏感（`.jpg` 与 `.JPG` 都会同步）；像 `婚礼视频/` 这样的目录不会整体排除，目录里的图片会保留，视频/音频仍会被白名单过滤。
- **`sync_nas.sh` 的 `set -e` 陷阱**：列文件清单阶段日志无 `%`，进度循环里 grep
  非零会误杀脚本导致 rsync 变孤儿——进度循环与 wait 期间必须关闭 errexit，
  rsync 退出码由变量显式捕获。此修复勿回退。
- **互斥锁**：脚本用 `${TMPDIR:-/tmp}/framedphoto-sync-nas.lock` 目录锁防止并发；
  手动按钮、launchd 定时任务同时触发时，后到者检测到锁会直接退出（exit 75），不会双跑 rsync。
- 进度写入 `services/sync_status.json`（约每 5 秒一次），只统计白名单中的图片，Web 管理台
  「NAS 照片同步」板块展示；日志见 `docs/development.md`「运行中的服务」。
  状态文件路径由服务端 `__file__` 上溯三层计算，**勿改动该路径约定**。
- IPv6 地址在脚本里自动用 `[...]` 包裹（rsync/ssh 要求）。
- 建议配置 SSH 免密（`ssh-copy-id`）。

### 多用户 Frame 文件夹（家庭共享进照片库）

想让多位家庭成员的手机照片自动进照片库、每人一个独立分类：在 NAS 的 photo
共享根目录（即 `NAS_PHOTO_DIR` 指向的目录）下，**每个用户建一个
`Frame_<用户名>` 文件夹**（如 `Frame_naoki`、`Frame_Chen`），把照片放进去即可。

```bash
# NAS 上（File Station 或命令行）
mkdir -p /volume1/photo/Frame_naoki    # 每个家庭成员一个
```

链路（无需额外配置）：

1. 用户把照片放进 `Frame_<用户名>/`（手机 App / 网页版手动上传，见下）
2. 现有每小时同步自动拉取 → 本机 `photos/Frame_<用户名>/`
3. 同步后**自动启发式分析**（默认开，`NAS_AUTO_ANALYZE=0` 关闭）——新照片直接进照片库
4. 照片分类 = 文件夹名（`Frame_naoki`），管理台照片库自动出现对应 tab，
   时段编排可给照片时段绑定该分类（不出圈选片）

**手机怎么把照片放进去（Synology Photos 实测/官方规格，DSM 7.2+）**：

- **手机 App 手动上传（推荐）**：选中照片 → 上传 → 目的地选「共享空间」→
  浏览文件夹选 `Frame_<用户名>`（文件夹需预先建好；官方规格 "Supports
  browsing photos in folder and timeline views" / "Albums must exist prior
  to upload"）。
- **网页版拖放（推荐）**：浏览器登录 DSM → Synology Photos → 切到共享空间
  → 文件夹视图进入 `Frame_<用户名>` → 拖放照片（官方 "Easy File Upload"，
  支持拖放上传照片/视频/整个文件夹）。
- **不要用「照片备份」直连 Frame 文件夹**：备份目的地是空间级选择
  （个人空间 / 共享空间，官方 "Supports customizing backup destinations"），
  不能指定到共享空间里的子文件夹；备份到共享空间会落在根/日期文件夹，
  污染照片库第一层分类。自动备份继续用个人空间即可，精选过的再手动传 Frame。

要点：
- 同步脚本只放行图片（过滤 `@eaDir`/缩略图/视频），用户文件夹里放照片即可
- VLM 深度评分仍手动跑 `tools/analyze_photos.py`（花钱）；自动分析走免费启发式
- 想关掉自动分析：`.env` 加 `NAS_AUTO_ANALYZE=0`；并发线程 `NAS_ANALYZE_JOBS=4`

### 定时任务（macOS launchd）

```bash
# 安装：登录时运行一次，之后每 3600 秒（1 小时）运行一次
./scripts/install_sync_schedule.sh install

# 其他命令
./scripts/install_sync_schedule.sh status     # 查看状态
./scripts/install_sync_schedule.sh uninstall  # 卸载

# 自定义间隔（秒）
SYNC_INTERVAL=7200 ./scripts/install_sync_schedule.sh install
```

launchd 任务 `com.framedphoto.nas-sync` 与 Web 服务 `com.framedphoto.service` 相互独立；
同步失败不会影响管理台。日志：`services/sync_nas_launchd.log`。

### Web 管理台手动同步

「同步」页有 **立即同步 NAS** 按钮：后台启动一次增量同步，正在同步时按钮禁用，
状态条实时显示进度/限速/均速。与定时任务共用同一把锁，不会并发。

### 内容库照片 → NAS

内容库每张图片提供 **⬆ 同步到 NAS**：把原图复制到 NAS 专用目录
（`NAS_UPLOAD_DIR`，默认 `<NAS_PHOTO_DIR>/FramedPhoto`），校验远端大小一致后标记
「✅ NAS 已保存」，并在本地照片库 `photos/FramedPhoto/` 建立镜像供照片库区显示。

- 同步成功后出现 **删除本地副本** 按钮：再次校验远端文件后删除内容库本地副本
  （删除后内容库不再保留该图，NAS 与照片库镜像保留）；
- 未同步成功前不会出现删除按钮，远端校验失败也不会删除；
- 删除会顺带清掉内容库的评分记录，镜像评分记录保留在照片库。

### 流量说明

| 阶段 | 流量 |
| --- | --- |
| 首次同步 | 整个照片库一次（如 100GB 照片 = 100GB） |
| 之后每次 | 仅新增/变化文件（几 MB ~ 几十 MB） |
| 每日精选渲染 | 读本地副本，**0 网络流量** |

## 方案 B：局域网 NAS — SMB 挂载

NAS 与运行服务端的机器在同一局域网时，直接 SMB 挂载（实时读取，无需同步）：

```bash
# .env 配置
NAS_HOST=192.168.1.5
NAS_SHARE=photo
NAS_USER=yourname
NAS_PASSWORD=               # 留空则交互输入
NAS_MOUNT_POINT=/Volumes/framedphoto-nas

./scripts/mount_nas.sh mount
# PHOTO_LIB_DIR=/Volumes/framedphoto-nas/photo
```

## 使用流程（两种方案通用）

```bash
# 1. 同步或挂载 NAS 照片到本机
# 2. 全量分析（AI 评分，断点续跑）
python tools/analyze_photos.py ./photos -j 4
# 3. 启动服务（每日精选自动选片）
cd services && uvicorn app.main:app
```

## 掉线容错

| 场景 | 行为 |
| --- | --- |
| 分析时目录不可达 | 明确报错并提示先同步/挂载 |
| 每日精选选中照片不可达 | 跳过，选下一张（不崩溃） |
| 照片删除/目录变化 | `python tools/analyze_photos.py <dir> --prune-stale` 清理失效记录 |
| rsync 同步中断 | 脚本带 `--partial --timeout`，重跑继续，不重复传已完成部分 |

## 注意

- `NAS_PASSWORD` 等敏感配置在 `services/.env`（已被 gitignore），建议 `chmod 600 services/.env`
- 挂载点 / 同步目录路径固定，避免已分析照片的绝对路径失效（失效可重分析恢复）
