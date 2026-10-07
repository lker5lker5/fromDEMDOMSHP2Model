# 地灾单点制图 Web 应用 (Cesium 版)

将 DEM/DOM 栅格与 SHP 灾害边界叠加，生成**交互式 3D 地形图**的 Web 应用。
**SHP-only 模式**使用 Cesium 3D 全球地形底图替代天地图，支持自由旋转/缩放/倾斜的 3D 地球浏览。

## 功能

- 📂 上传 zip 打包的 DEM + DOM + SHP 数据
- 🌄 交互式 3D 地形渲染（Plotly.js）
- 🎛 实时调整参数（垂直夸张、光照、视角、边界颜色等）
- ⬇ 下载高分辨率 PNG（最高 3840×2880，300 DPI）
- 🔄 全部重投影到 CGCS2000（EPSG:4490）坐标系
- 🌍 **SHP-only** 模式使用 Cesium 3D 全球底图（ArcGIS World Imagery + 全球地形）

## 与天地图版区别

| 特性 | 天地图版 (v2.0) | Cesium 版 (v2.0-cesium) |
| :--- | :--- | :--- |
| SHP-only 底图 | 天地图 2D 平面 | **Cesium 3D 全球球体** |
| 地形 | 无（平面底图） | ArcGIS 全球地形（3D 起伏） |
| 底图来源 | 天地图 API | ArcGIS World Imagery |
| 交互操作 | 平移/缩放 | **旋转/倾斜/缩放/平移** |
| 依赖 API Key | 是（天地图） | 否（完全 CDN 加载） |
| 端口 | 5603 | **5604** |

## 系统要求

- Python 3.9+
- pip

## 安装

```bash
# 1. 进入项目目录
cd geoMaps/demDomDisaster_cesium

# 2. 安装依赖
pip install -r webapp/requirements.txt
```

## 启动

**macOS / Linux：**
```bash
./run_webapp.sh
```

**Windows：**
```
run_webapp.bat
```

或者手动启动：

```bash
cd webapp
python -m uvicorn main:app --host 127.0.0.1 --port 5604 --reload
```

启动后浏览器打开 **http://127.0.0.1:5604**（也可通过环境变量 `PORT` 自定义端口）

## 使用说明

### 上传数据

1. 将 DEM（GeoTIFF）、DOM（GeoTIFF）、SHP（.shp/.shx/.dbf/.prj）打包为 **zip 文件**
2. 在页面中选择文件，点击「开始处理」
3. 等待数据处理完成后，自动展示：

   - **仅上传 SHP**：在 Cesium 3D 全球地形上展示区划边界（可鼠标拖拽旋转、滚轮缩放）
   - **上传 DEM+DOM+SHP**：使用 Plotly 生成 3D 地形叠加图

**zip 内容要求：**

| 文件 | 说明 | 必需 |
| :--- | :--- | :--- |
| `*DEM*.tif` / `*DEM*.tiff` | 数字高程模型 | ✅ |
| `*IMG*.tif*` / `*DOM*.tif*` | 数字正射影像 | ✅ |
| `*.shp` + `*.shx` + `*.dbf` | 灾害边界矢量文件 | ✅（三者缺一不可） |
| `*.prj` | 投影信息文件 | 推荐（缺失时继续处理） |

### 参数说明

| 参数 | 说明 | 默认值 |
| :--- | :--- | :--- |
| 标题 | 图面标题文字 | DOM + DEM 三维地形… |
| 垂直夸张 | Z 轴数据压缩比 | 0.8 |
| 光照方位角 | 山体阴影光源方向（0=北，90=东） | 315° |
| 光照高度角 | 光源仰角 | 45° |
| 视角俯仰 | 3D 相机俯仰角 | 40° |
| 视角方位 | 3D 相机水平旋转 | -55° |
| 边界线颜色 | 灾害边界线条颜色 | #FFD700 |
| 线宽 | 边界线宽度 | 2.2 |
| 阴影混合比 | hillshade 与 DOM 的混合程度 | 0.45 |
| Z 轴视觉夸张 | 显示时 Z 方向放大倍率 | 1.2 |
| 显示标注文字 | 是否显示质心标注 | 开 |
| 显示质心点 | 是否显示质心圆点 | 开 |

调整参数后点击 **「应用」** 或按 **Ctrl+Enter** 刷新 3D 视图。

### 下载 PNG

1. 在左侧下方选择分辨率预设或自定义
2. 点击「下载 PNG」
3. 浏览器自动下载带标题的 PNG 文件

## 项目结构

```
geoMaps/demDomDisaster_cesium/
├── demDomDisaster_plot.py     # 原始命令行脚本（不受影响）
├── webapp/
│   ├── main.py                # FastAPI 入口
│   ├── processing.py          # 数据处理（读栅格/矢量、重投影、降采样）
│   ├── rendering.py           # Plotly 渲染与 PNG 导出
│   ├── config.py              # 默认参数配置（端口 5604）
│   ├── static/
│   │   ├── index.html         # 前端页面（Cesium CDN）
│   │   ├── app.js             # 前端逻辑（Cesium 替代天地图）
│   │   └── style.css          # 样式
│   ├── sessions/              # 运行时自动创建的会话目录
│   └── requirements.txt
├── run_webapp.sh              # macOS/Linux 启动脚本（端口 5604）
├── run_webapp.bat             # Windows 启动脚本（端口 5604）
└── README.md                  # 本文件
```

## 常见问题

### Q: 上传后提示缺少 .shx / .dbf 文件
SHP 矢量格式需要配套的 `.shp`、`.shx`、`.dbf` 三个文件**一起打包**。请确认 zip 中包含全部三个文件。

### Q: Cesium 地图显示空白
- 检查浏览器网络连接是否可访问 `cdn.jsdelivr.net` 和 `server.arcgisonline.com`
- Cesium 通过 CDN 加载，需要网络连接

### Q: 页面显示空白或报错
检查终端中的 FastAPI 日志，常见原因：
- zip 内文件命名不规范（如 DEM 文件名不含 "DEM" 关键字可自动匹配）
- 数据本身问题（如坐标系无法识别）

### Q: 下载 PNG 失败
PNG 导出依赖 kaleido（已在 requirements.txt 中）。如果仍失败，请手动安装：
```bash
pip install kaleido
```

### Q: 3D 图颜色不如原始 DOM 丰富
这是已知限制：Plotly 的 `surface` 类型不支持 RGB 真彩贴图。本应用采用亮度+阴影调制方案，保留地形立体感和主色调，但无法展示 DOM 的精细纹理。如需真彩图，请使用原始命令行脚本 `demDomDisaster_plot.py`（基于 matplotlib）。

## 命令行脚本

原始的 `demDomDisaster_plot.py` 不受影响，可继续独立运行：
```bash
python demDomDisaster_plot.py
```

## 许可证

内部工具