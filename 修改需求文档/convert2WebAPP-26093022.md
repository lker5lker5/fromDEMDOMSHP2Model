# 地灾单点制图 Web 应用 — 开发规格说明书

> 本文档为可直接交给 Claude CLI 执行的开发指令。请严格按本规格实现，不要自行更改技术选型或架构。

---

## 1. 项目目标

将现有命令行脚本 `demDomDisaster_3d.py`（DOM + DEM 三维地形 + SHP 灾害边界叠加制图）改造为一个**本地可运行的 Web 应用**，同时保留命令行脚本。

用户通过浏览器：
1. 上传打包好的 zip（内含 DEM、DOM、SHP 及其附属文件）
2. 后端处理后返回 3D 交互视图
3. 在前端动态调整参数（拉伸、角度、光照、颜色、标题等），点“应用”后刷新 3D 视图
4. 下载当前参数下的静态 PNG（可选分辨率）

---

## 2. 技术选型（已确认，不可更改）

| 层 | 技术 | 说明 |
| :--- | :--- | :--- |
| 后端 | **FastAPI + Uvicorn** | 异步、自带 `/docs`、单命令启动 |
| 前端 | **纯 HTML + vanilla JS + Plotly.js (CDN)** | 无需 Node/构建工具 |
| 3D 渲染 | **Plotly.js `Surface` + `Scatter3d`** | 后端返回 JSON 数据，前端本地渲染与交互 |
| 图像处理 | rasterio / geopandas / numpy / scipy / matplotlib | 与现有脚本一致 |
| 会话管理 | **服务端 session 目录 + 临时 token** | 刷新/关闭浏览器即失效，无需登录 |
| 数据打包 | **zip 上传** | 强制要求，缺项校验 |

**不做的事**：不做登录、不做数据库、不做任务队列、不做用户管理、不做前端框架（React/Vue）。

---

## 3. 项目目录结构

```
geoMaps/
├── demDomDisaster_3d.py          # 保留：原命令行脚本（不改动逻辑）
├── webapp/
│   ├── main.py                    # FastAPI 入口（路由、会话、上传校验）
│   ├── processing.py              # 核心处理：读栅格/矢量、CGCS2000转换、降采样、打包JSON
│   ├── rendering.py               # Plotly figure 生成 + PNG 导出
│   ├── config.py                  # 默认参数、常量、允许的文件扩展名
│   ├── static/
│   │   ├── index.html             # 单页应用
│   │   ├── app.js                 # 前端逻辑（上传、参数绑定、Plotly渲染、下载）
│   │   └── style.css              # 页面样式
│   ├── sessions/                  # 运行时自动创建，存放用户上传和生成结果
│   └── requirements.txt
├── run_webapp.sh                  # Linux/macOS 启动脚本
├── run_webapp.bat                 # Windows 启动脚本
└── README.md                      # 使用说明
```

**说明**：`processing.py` 与 `rendering.py` 职责明确但不要过度拆分。`config.py` 只放常量。前端只有 3 个文件，不要拆组件。

---

## 4. 后端设计

### 4.1 API 接口

| 方法 | 路径 | 作用 |
| :--- | :--- | :--- |
| `GET` | `/` | 返回 `index.html` |
| `POST` | `/api/upload` | 上传 zip，校验内容，处理数据，创建 session，返回 `session_id` + 初始 3D 数据 |
| `POST` | `/api/render` | 传入 `session_id` + 参数字典，返回新的 Plotly figure JSON |
| `POST` | `/api/download` | 传入 `session_id` + 参数 + 分辨率，返回 PNG 文件流 |
| `DELETE` | `/api/session/{session_id}` | 主动清理会话（可选，页面卸载时调用） |
| `GET` | `/api/health` | 健康检查 |

### 4.2 上传校验规则

上传必须是 `.zip`，解压后**必须**包含以下文件（大小写不敏感）：

- `*.tif` 或 `*.tiff`：DEM（文件名含 `DEM`）
- `*.tif` 或 `*.tiff`：DOM（文件名含 `IMG` 或 `DOM`）
- `*.shp` + `*.shx` + `*.dbf`（**必须有**，缺一不可）
- `*.prj`（推荐，缺失时打印警告但继续）

**缺项时必须返回明确错误信息**，例如：
```
缺少以下文件：QY_new_disaster_boundary.shx, QY_new_disaster_boundary.prj
请将 SHP 的 .shp/.shx/.dbf/.prj 一起打包上传。
```

### 4.3 坐标系统一

无论上传数据的原始 CRS 是什么，**全部转换成 CGCS2000 地理坐标系（EPSG:4490，十进制经纬度）**后再做后续处理：

- DEM：用 `rasterio.warp.reproject` 重投影到 EPSG:4490
- DOM：同样重投影到 EPSG:4490，并重采样到 DEM 的网格
- SHP：`gdf.to_crs("EPSG:4490")`

**注意**：不要转成 Web Mercator（EPSG:3857），因为你要的是十进制经纬度显示。

### 4.4 会话管理

- 每次上传生成 `session_id = uuid4().hex`
- 数据存到 `webapp/sessions/{session_id}/`
- 保存内容：原始 zip、解压后的文件、重投影后的中间结果（可复用，避免每次 render 都重算）、参数默认值
- **过期策略**：默认 2 小时无活动自动清理；页面关闭时前端调用 `DELETE /api/session/{id}`；启动时清理超过 24 小时的残留目录
- **不持久化**：不写数据库

### 4.5 数据处理流程（`/api/upload`）

1. 解压 zip 到临时目录
2. 校验文件完整性，缺项则返回 400 + 缺项清单
3. 读取 DEM/DOM/SHP
4. **全部重投影到 EPSG:4490**
5. **不裁剪**（保持原脚本行为，叠加完整范围）
6. DOM 重采样到 DEM 网格（`Resampling.bilinear`）
7. 生成山体阴影（`matplotlib.colors.LightSource`）
8. **降采样到 `MAX_SIZE=600`**（可通过参数调整）—— 3D 渲染需要的网格
9. 提取 SHP 每个区的边界线坐标 + 质心坐标（用 `representative_point()`）
10. 用 DEM 网格插值边界线和质心的高程
11. **打包成 JSON**（下面 4.6 节定义格式），返回前端

### 4.6 前端所需的数据格式（重要）

`/api/upload` 和 `/api/render` 返回的 JSON 结构：

```json
{
  "session_id": "abc123...",
  "figure": {
    "data": [ /* Plotly traces，见下 */ ],
    "layout": { /* Plotly layout，见下 */ }
  },
  "meta": {
    "bounds": {"xmin": 118.2, "xmax": 118.3, "ymin": 27.5, "ymax": 27.6},
    "zmin": 800, "zmax": 1500,
    "n_zones": 3,
    "crs": "EPSG:4490"
  }
}
```

**`figure.data` 中必须包含：**

1. **1 个 `surface` trace**（地形）：`x`, `y`, `z` 为二维数组（或一维 + 行列信息），`surfacecolor` 为 DOM 的 RGB 展平后的值，`colorscale` 自定义或使用 `[[0,'#...'],[1,'#...']]` 映射。**更推荐**：把 DOM 颜色预先量化为 RGB 并通过 `surfacecolor` + 自定义 colorscale 实现贴图效果；如果 Plotly 原生不支持真彩贴图，则退而求其次用 `surfacecolor` 表达亮度 × DOM 主色调。
2. **每个区 1 个 `scatter3d` trace**（边界线）：`mode='lines'`，颜色由参数控制，`line.width` 由参数控制
3. **每个区 1 个 `scatter3d` trace**（质心标注）：`mode='markers+text'`，`text` 为 `"{序号}. {名称}"`，`hovertext` 为该区所有属性字段的拼接（用于 Q7 的点击显示属性）
4. **可选：1 个 `scatter3d`**（质心圆点标记）

**`figure.layout` 要求：**

- `scene.xaxis.title = "经度"`, `scene.yaxis.title = "纬度"`, `scene.zaxis.title = "高程 (m)"`
- `scene.xaxis.tickformat = ".4f"`, `scene.yaxis.tickformat = ".4f"`
- **无背景面板**：`scene.xaxis.showbackground = false`、`scene.yaxis.showbackground = false`、`scene.zaxis.showbackground = false`
- **无网格线**：`scene.xaxis.showgrid = false` 等
- `scene.aspectmode = 'manual'` + `scene.aspectratio` 由参数 `Z_EXAG_VISUAL` 控制
- `title.text` 由前端参数控制
- **不设 `paper_bgcolor` / `plot_bgcolor`**，由前端 CSS 控制白底

### 4.7 参数接口（`/api/render`）

请求体：

```json
{
  "session_id": "abc123...",
  "params": {
    "title": "庆元新发现点 — DOM + DEM 三维地形与灾害边界叠加",
    "vert_exag": 0.8,
    "azdeg": 315,
    "altdeg": 45,
    "view_elev": 58,
    "view_azim": -55,
    "line_color": "#FFD700",
    "line_width": 2.2,
    "hillshade_mix": 0.45,
    "max_size": 600,
    "z_exag_visual": 1.2,
    "show_labels": true,
    "show_centroids": true
  }
}
```

后端用新参数重新计算 hillshade、重新打包 figure，返回同 4.6 格式。

**性能优化**：DEM/DOM/SHP 的读取和重投影结果**缓存在 session 目录**，`/api/render` 只重新做 hillshade、颜色混合、figure 打包，不重新读原文件。

### 4.8 PNG 下载（`/api/download`）

请求体：

```json
{
  "session_id": "abc123...",
  "params": { /* 同 4.7 */ },
  "resolution": {"width": 1920, "height": 1440},
  "dpi": 300
}
```

流程：
1. 用最新参数生成 Plotly figure
2. 用 `kaleido` 导出静态 PNG（如果 kaleido 安装有问题，退而求其次用 matplotlib 重新绘制同视角图）
3. 返回 `image/png` 流，文件名 `{title}_{timestamp}.png`

**推荐分辨率预设**（前端下拉框）：
- 1280×960（屏幕）
- 1920×1440（报告）
- 2560×1920（高清）
- 3840×2880（出版）
- 自定义（宽 × 高输入框）

**依赖**：`kaleido` 必须在 `requirements.txt` 里。如果用户环境无法安装 kaleido，提供 fallback：用 matplotlib 重新画一张同视角图（用 `plot_surface` + `view_init` 与 Plotly 视角对齐）。

---

## 5. 前端设计

### 5.1 页面结构（单页，`index.html`）

```
┌─────────────────────────────────────────────────┐
│  顶部：标题 + 上传区 + 状态提示                  │
├──────────────────┬──────────────────────────────┤
│                  │                              │
│   左侧参数面板    │     右侧 3D 视图             │
│   （可折叠）      │     （Plotly div，占满）      │
│                  │                              │
│   - 标题输入      │                              │
│   - 垂直夸张滑块  │                              │
│   - 光照方位角    │                              │
│   - 光照高度角    │                              │
│   - 视角俯仰      │                              │
│   - 视角方位      │                              │
│   - 线颜色        │                              │
│   - 线宽          │                              │
│   - 阴影混合比    │                              │
│   - 网格精度      │                              │
│   - Z 轴夸张      │                              │
│   - 显示标注开关  │                              │
│   - 显示质心开关  │                              │
│                  │                              │
│   [应用] [重置]   │                              │
│                  │                              │
│   下载区：        │                              │
│   - 分辨率下拉    │                              │
│   - [下载 PNG]    │                              │
└──────────────────┴──────────────────────────────┘
```

### 5.2 交互流程

1. 页面加载 → 显示上传区，隐藏参数面板和 3D 视图
2. 用户选 zip → 点上传 → loading 提示 → 成功后显示 3D 视图，参数面板填入后端返回的默认值
3. 用户拖动滑块 → **不立即刷新**（避免抖动）
4. 用户点“应用” → 调 `/api/render` → 替换 Plotly figure
5. 用户点“下载 PNG” → 调 `/api/download` → 触发浏览器下载
6. 页面卸载时（`beforeunload`）→ 调 `DELETE /api/session/{id}`

### 5.3 滑块实时显示数值

每个滑块右侧显示当前数值（如 `垂直夸张: 0.8`），拖动时实时更新数字，但**不触发后端**。

### 5.4 颜色选择器

用原生 `<input type="color">`，默认值 `#FFD700`。

### 5.5 错误提示

上传失败、参数非法、渲染失败时，在顶部用红色横幅显示后端返回的 `detail` 字段。

---

## 6. 命令行脚本保留

`demDomDisaster_3d.py` **完全不动**，继续支持命令行运行。

如果它和 `webapp/processing.py` 有公共逻辑，**不要抽公共模块**——允许少量重复，保持命令行脚本自包含、可独立运行。

---

## 7. 依赖与启动

### 7.1 `requirements.txt`

```
fastapi>=0.110
uvicorn[standard]>=0.27
python-multipart>=0.0.9
rasterio>=1.3
geopandas>=0.14
numpy>=1.26
scipy>=1.11
matplotlib>=3.8
plotly>=5.20
kaleido>=0.2.1
pandas>=2.2
```

### 7.2 启动脚本

**`run_webapp.sh`（Linux/macOS）**：
```bash
#!/usr/bin/env bash
set -e
cd "$(dirname "$0")/webapp"
python -m uvicorn main:app --host 127.0.0.1 --port 8000 --reload
```

**`run_webapp.bat`（Windows）**：
```bat
@echo off
cd /d "%~dp0webapp"
python -m uvicorn main:app --host 127.0.0.1 --port 8000 --reload
```

启动后浏览器打开 `http://127.0.0.1:8000`。

### 7.3 `README.md` 内容

- 项目简介
- 安装步骤（`pip install -r requirements.txt`）
- 启动命令
- 上传 zip 的内容要求
- 参数说明
- 常见问题（缺文件、CRS 不匹配、kaleido 安装失败）

---

## 8. 关键实现要点（易错处）

### 8.1 Plotly 真彩贴图

Plotly 的 `surface` 不直接支持 RGB 贴图。**推荐方案**：

- 把 DOM 的 RGB 转成亮度 `L = 0.299R + 0.587G + 0.114B`
- 用 hillshade 调制：`intensity = L * (1 - mix + mix * hillshade)`
- `surfacecolor = intensity`（0–1 之间的二维数组）
- `colorscale` 用 DOM 的主色系（可从 DOM 均值颜色生成一个从暗到亮的渐变）
- 这样虽然丢了 DOM 的真实色彩细节，但保留了亮度和地形立体感

**如果坚持要真彩**：把 surface 拆成多个 trace 不现实。替代方案是**前端用 Three.js 或 deck.gl**，但那超出本次需求，暂不做。在 README 里说明这个限制。

### 8.2 SHP 多区标注

- 用 `representative_point()` 而不是 `centroid()`，避免凹多边形质心落到界外
- 每个区遍历所有几何部件（Polygon / MultiPolygon）
- 标注格式：`{序号}. {名称}`，名称字段自动探测（`name`/`名称`/`隐患点`等），找不到就只显示序号
- 序号从 1 开始
- 文字抬高 `z_range * 0.03`，线抬高 `z_range * 0.005`，避免遮挡

### 8.3 CGCS2000 转换

- 用 `EPSG:4490`（CGCS2000 地理坐标系）
- DEM 用 `rasterio.warp.reproject`，DOM 用 `rasterio.warp.reproject` + `Resampling.bilinear`
- SHP 用 `gdf.to_crs("EPSG:4490")`
- **转换后一定要重新计算 `transform` 和 `xs/ys`**

### 8.4 视角对齐（Plotly 与 matplotlib）

Plotly 的 `scene.camera.eye` 与 matplotlib 的 `view_init(elev, azim)` 参数不同。**统一用 Plotly 的相机模型**：

```python
import numpy as np
def elev_azim_to_eye(elev_deg, azim_deg, distance=2.5):
    elev = np.radians(elev_deg)
    azim = np.radians(azim_deg)
    x = distance * np.cos(elev) * np.cos(azim)
    y = distance * np.cos(elev) * np.sin(azim)
    z = distance * np.sin(elev)
    return {"x": x, "y": y, "z": z}
```

### 8.5 PNG 导出

优先用 `plotly.io.to_image(fig, format='png', width=..., height=..., scale=...)`。如果 kaleido 不可用：

- 捕获异常，回退到 matplotlib 重画
- 或直接提示用户“PNG 导出需要 kaleido，请运行 `pip install kaleido`”

### 8.6 会话清理

- FastAPI 启动时 `@app.on_event("startup")` 清理超过 24 小时的 session 目录
- 每个 session 记录最后活动时间戳，`/api/render` 和 `/api/download` 更新它

---

## 9. 验收标准

Claude CLI 完成后，必须满足：

1. `run_webapp.sh` / `run_webapp.bat` 一键启动，浏览器打开 `http://127.0.0.1:8000` 可用
2. 上传样例 zip 后，3D 地形图正常显示，边界线和质心标注可见
3. 拖动任意参数滑块 + 点“应用”，3D 视图在 3 秒内刷新
4. 点“下载 PNG”，得到带标题、坐标轴、刻度的白底 PNG
5. X/Y 轴刻度显示为十进制经纬度（如 `118.2345`），Z 轴显示高程
6. 3D 视图无背景面板、无网格线
7. SHP 多区时，每个区都有独立边界线和质心标注，序号连续
8. 刷新页面后 session 失效，需要重新上传
9. 上传缺少 `.shx` 或 `.dbf` 的 zip 时，返回明确缺项提示
10. 原始 `demDomDisaster_3d.py` 命令行脚本不受影响，仍可独立运行

---

## 10. 不做的事（明确排除）

- ❌ 不做用户登录、注册、权限
- ❌ 不做数据库
- ❌ 不做任务队列（Celery/RQ）
- ❌ 不做前端框架（React/Vue/Svelte）
- ❌ 不做 Docker（本次不要求，未来可加）
- ❌ 不做 Three.js 自定义渲染（用 Plotly 即可）
- ❌ 不做多文件分开上传（强制 zip）
- ❌ 不做 DEM/DOM 裁剪（保持完整范围）
- ❌ 不做真彩 DOM 贴图（Plotly 限制，用亮度 + 阴影调制替代）

---

## 11. 实现顺序建议

1. **第一步**：搭 FastAPI 骨架 + 上传接口 + 文件校验（不做渲染，先打通上传流程）
2. **第二步**：实现 `processing.py`（读取、重投影到 CGCS2000、降采样、打包 JSON）
3. **第三步**：实现 `rendering.py`（Plotly figure 生成）
4. **第四步**：前端 `index.html` + `app.js`（上传 + 显示 3D）
5. **第五步**：参数面板 + `/api/render` 联调
6. **第六步**：PNG 下载（`/api/download` + kaleido）
7. **第七步**：会话清理 + README + 启动脚本

---

**以上为完整开发规格。请按此实现，不要自行增加或删减功能。如有技术障碍无法实现某条（如 Plotly 真彩贴图），在代码注释和 README 中说明，不要静默跳过。**
