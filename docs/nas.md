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

> IPv6 地址在脚本里自动用 `[...]` 包裹（rsync/ssh 要求）。
> 建议配置 SSH 免密（`ssh-copy-id`）后，把同步加入定时任务：
> `crontab -e` → `0 3 * * * cd <repo> && ./scripts/sync_nas.sh >> /tmp/sync_nas.log 2>&1`

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
