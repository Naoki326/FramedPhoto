# 墨水屏颜色校准与调色板 v2

## 背景：为什么有「两套颜色」

Spectra 6 只有黑/白/黄/红/蓝/绿六种墨水。系统里存在两个不同的颜色概念：

| 概念 | 含义 | 用途 |
| --- | --- | --- |
| **量化目标色**（targets） | 每种墨水在「输入图片色空间」里的代表色 | 决定输入像素归到哪个墨水 |
| **设备观感色**（device） | 该墨水在真机屏幕上实际呈现的颜色 | 预览/模拟真机观感，不参与量化 |

v1 把两者混为一谈（都用理想 sRGB 色），导致：
- 预览图比真机鲜艳得多，无法预判真机效果；
- RGB 欧氏距离不符合人眼感知（对绿色明度、蓝紫混淆不敏感）。

v2（默认）做了三件事：
1. **OKLab 感知距离**量化（`services/app/epd_image.py::_nearest_index`）；
2. **双色模型**：`Spectra6Profile(targets, device)`，预览用 device 色渲染，贴近真机；
3. **校准通道**：真机拍照采样，把 device 色替换成这块屏的真实观感。

## 调色板结构

```python
Spectra6Profile(name, targets, device, distance)
```

- `v1`：targets = device = 理想 sRGB 色，distance = `rgb`（历史行为，A/B 用）
- `v2`：targets = 理想 sRGB 色（量化目标），distance = `oklab`；
  device 初值为手机实拍采样（黑白为占位），校准后由
  `services/app/profiles/calibrated.json` 覆盖（`epd_image.py` 启动时自动加载）

profile 只在「量化/预览」层生效，FPS6 数据区仍是设备 nibble，**固件无需改动、
已上线设备无需升级**。`PALETTE_VERSION` 字段不变。

## 校准流程（一次性，约 10 分钟）

```bash
# 1. 生成校准图（1600x1200 横放视角，nibble 直写六色）
python3 tools/generate_calibration_chart.py -o cal.fps6 --preview cal.png

# 2. 直推给设备（绕过服务端量化，原样写入 display 通道）
curl -X POST http://chenMac-mini.local:8010/api/images/display/raw-fps6 \
     -F "file=@cal.fps6"

# 3. 按设备刷新键（或等下次轮询），屏幕显示校准图；横放设备观看
#    「1 黑 / 2 白 / 3 黄 / 4 红 / 5 蓝 / 6 绿」

# 4. 手机正面垂直拍摄：占满画面、避免反光、固定曝光与白平衡
#    （建议同一光线多拍 2-3 张，选最正的一张）

# 5. 采样出六色真实观感（--preview 输出红框标注图，核对采样区）
python3 tools/calibrate_profile.py photo.jpg --preview marked.png
#    如果照片里校准图是竖着拍的，加 --rotate 90/180/270

# 6. 检查标注图红框是否落在每条色带中央，确认后生成的 calibrated.json
#    已写入 services/app/profiles/，v2 自动生效（无需重启？见下）
```

> `calibrated.json` 在 `epd_image.py` **模块导入时**加载。写入后需重启服务端
> （`launchctl kickstart -k gui/$(id -u)/com.framedphoto.service`）或重启 uvicorn。

拍摄要点：
- 正面垂直，手机与屏幕平行；侧视会让上下色带亮度不均；
- 关掉屏幕补光/避开顶灯反光；可在屏幕右上角垫暗色纸防反光；
- 拍摄时锁定曝光/白平衡（轻点屏幕长按锁定 AE/AF），多张取最正的。

## A/B 对比

```bash
python3 tools/compare_palettes.py photo.jpg -o ab/ --name rainbow
```

输出：`ab/rainbow-v1.fps6`、`ab/rainbow-v2.fps6`、各自预览、以及
`ab/rainbow-grid.png`（左原图 | 中 v1 理想预览 | 右 v2 真机观感预览），
并打印两种方案的墨水占比。可用同一张图对比 `--no-dither` 效果。

## 后续可选优化（暂未实现）

- 抖动算法对比：Atkinson / 蛇形 F-S / 蓝噪声；
- 量化目标色按面板色相微调（如黄偏橄榄后，targets 中黄从 (255,150,0) 前移）；
- 灰度优先级：大面积暗部优先黑墨水、避免彩色噪点；
- 端到端 A/B：同一图 v1/v2 各刷一次真机拍照对比。
