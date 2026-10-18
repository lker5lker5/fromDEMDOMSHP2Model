# -*- coding: utf-8 -*-
"""
Web 应用常量与默认参数配置。
"""

import os
from pathlib import Path

# ==================== 服务端口 ====================
# 可通过环境变量 PORT 覆盖
PORT = int(os.getenv("PORT", "5604"))

# ==================== 路径 ====================
BASE_DIR = Path(__file__).parent
STATIC_DIR = BASE_DIR / "static"
SESSION_DIR = BASE_DIR / "sessions"

# ==================== 数据处理 ====================
# 3D 网格最大边长（性能控制）
MAX_SIZE = 600

# 允许的文件扩展名（小写）
DEM_EXTENSIONS = {".tif", ".tiff"}
DOM_EXTENSIONS = {".tif", ".tiff"}
SHP_REQUIRED_EXTENSIONS = {".shp", ".shx", ".dbf"}
SHP_OPTIONAL_EXTENSIONS = {".prj", ".sbn", ".sbx", ".xml"}

# 文件名包含的关键字（大小写不敏感）
DEM_KEYWORDS = ["dem"]
DOM_KEYWORDS = ["img", "dom", "正射", "影像"]

# ==================== 新参数默认值 (v2) ====================
DEFAULT_TITLE_FONTSIZE = 16       # 标题字号
DEFAULT_TITLE_FONT = "Microsoft YaHei, Times New Roman, sans-serif"  # 标题字体
DEFAULT_VIEW_DISTANCE = 2.5       # 视角距离
DEFAULT_LABEL_FONTSIZE = 11       # 标注字号
DEFAULT_NODATA_COLOR = "#BFBFBF"  # NoData 填充色
DEFAULT_DISPLAY_FIELDS = [] # 默认展示字段（由前端传第一个 field）
DEFAULT_LABEL_COLOR = "#E8E8E8"   # 质心标注文字颜色（浅灰）
DEFAULT_CENTROID_SIZE = 7         # 质心圆点大小

# 颜色遮罩
DEFAULT_COLOR_MASK = ""           # 空字符串 = 无遮罩
DEFAULT_COLOR_MASK_STRENGTH = 0.0 # 遮罩强度 0–1

# 坐标轴
DEFAULT_SHOW_AXES = True          # 显示坐标轴刻度与标注
DEFAULT_SHOW_GRID = False         # 显示网格线

# ==================== 默认渲染参数 ====================
DEFAULT_VERT_EXAG = 0.8          # 垂直夸张系数
DEFAULT_AZDEG = 315              # 光照方位角
DEFAULT_ALTDEG = 45              # 光照高度角
DEFAULT_VIEW_ELEV = 40           # 视角俯仰
DEFAULT_VIEW_AZIM = -55          # 视角方位
DEFAULT_LINE_COLOR = "rgba(255,255,255,0)"   # 边界线颜色（默认透明，无边框）
DEFAULT_LINE_WIDTH = 0           # 边界线宽度（默认 0，无边框）
DEFAULT_HILLSHADE_MIX = 0.45     # 阴影混合比
DEFAULT_Z_EXAG_VISUAL = 1.2      # Z 轴视觉夸张
DEFAULT_SHOW_LABELS = True       # 显示标注文字
DEFAULT_SHOW_CENTROIDS = True    # 显示质心点
DEFAULT_TITLE = "DOM + DEM 三维地形与灾害边界叠加"

# ==================== 会话管理 ====================
SESSION_TTL = 7200       # 2 小时无活动自动清理
SESSION_CLEANUP_AGE = 86400  # 启动时清理超过 24 小时的残留
CAMERA_DISTANCE = 2.5    # Plotly 相机默认距离

# ==================== 分辨率预设（下载）====================
RESOLUTION_PRESETS = {
    "screen": (1280, 960),
    "report": (1920, 1440),
    "hd": (2560, 1920),
    "publish": (3840, 2880),
}
DOWNLOAD_DPI = 300

# ==================== 常用字段名（自动检测标注字段） ====================
PREFERRED_NAME_FIELDS = ["name", "Name", "NAME", "名称", "区名", "灾害点", "隐患点"]

# 常用 ID 字段名（自动检测 SHP 图斑 ID）
PREFERRED_ID_FIELDS = [
    "FID", "fid", "ID", "id", "OBJECTID", "objectid", "OBJECTID_1",
    "BSM", "bsm", "TBYBH", "tbybh", "YSDM", "ysdm",
    "编号", "序号", "代码", "标识码", "图斑编号",
]

# ==================== 可用参数列表（用于 /api/render 校验） ====================
RENDER_PARAMS = {
    "title": str,
    "vert_exag": float,
    "azdeg": float,
    "altdeg": float,
    "view_elev": float,
    "view_azim": float,
    "line_color": str,
    "line_width": float,
    "hillshade_mix": float,
    "max_size": int,
    "z_exag_visual": float,
    "show_labels": bool,
    "show_centroids": bool,
}