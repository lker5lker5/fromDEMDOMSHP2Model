# -*- coding: utf-8 -*-
import platform
import pandas as pd
from pathlib import Path
import numpy as np
import matplotlib
import matplotlib.patheffects
import matplotlib.pyplot as plt
from matplotlib.ticker import MaxNLocator
from matplotlib import font_manager
from matplotlib.colors import LightSource
from matplotlib.ticker import FuncFormatter
from mpl_toolkits.mplot3d import Axes3D  # noqa: F401
import rasterio
from rasterio.warp import reproject, Resampling
import geopandas as gpd
from scipy.interpolate import RegularGridInterpolator

# ==================== 1. 中文字体（解决乱码） ====================
font_candidates = [
    "PingFang SC", "Arial Unicode MS", "Heiti TC", "Hiragino Sans GB",  # macOS
    "Microsoft YaHei", "SimHei", "SimSun",                               # Windows
    "Noto Sans CJK SC", "WenQuanYi Micro Hei",                           # Linux
]
available_fonts = {f.name for f in font_manager.fontManager.ttflist}
for f in font_candidates:
    if f in available_fonts:
        matplotlib.rcParams["font.sans-serif"] = [f]
        print(f"使用中文字体: {f}")
        break
else:
    print("警告：未找到中文字体，中文可能显示为方块")
matplotlib.rcParams["axes.unicode_minus"] = False

# ==================== 2. 路径 ====================
CANDIDATE_DIRS = [
    Path("/Users/kaner/BaiduSyncdisk/1-工作事物/9.样例数据/地质制图/地灾早期识别/单点制图/庆元新发现点"),
    Path(r"T:/BaiduSyncdisk/1-工作事物/9.样例数据/地质制图/地灾早期识别/单点制图/庆元新发现点"),
]
DATA_DIR = next((d for d in CANDIDATE_DIRS if d.exists()), None)
if DATA_DIR is None:
    raise FileNotFoundError("未找到数据目录，请检查候选路径")

DEM_PATH = DATA_DIR / "DEM_QY-D2.tif"
DOM_PATH = DATA_DIR / "IMG_QY-D2.tiff"
SHP_PATH = DATA_DIR / "QY_new_disaster_boundary.shp"
OUTPUT_PATH = DATA_DIR / "庆元新发现点_3D.png"

# ==================== 3. 参数 ====================
MAX_SIZE = 600        # 3D 网格最大边长，性能控制，太慢就降到 400
VERT_EXAG = 0.8       # 垂直夸张系数
AZDEG = 315           # 光照方位角
ALTDEG = 45           # 光照高度角
VIEW_ELEV = 40        # 视角俯仰
VIEW_AZIM = -55       # 视角方位
LINE_COLOR = "#FFD700"  # 边界线颜色
LINE_WIDTH = 2.2

# ==================== 4. 读取 DEM（降采样，不裁剪） ====================
with rasterio.open(DEM_PATH) as src:
    dem_crs = src.crs
    nodata = src.nodata
    H, W = src.height, src.width

    # 计算降采样比例
    scale = max(1, int(np.ceil(max(H, W) / MAX_SIZE)))
    out_h, out_w = H // scale, W // scale

    dem = src.read(
        1, out_shape=(out_h, out_w), resampling=Resampling.bilinear
    ).astype(np.float32)

    # 新的 transform（对应降采样后的网格）
    dem_transform = src.transform * src.transform.scale(
        src.width / out_w, src.height / out_h
    )

if nodata is not None:
    dem[dem == nodata] = np.nan

# 像元大小
dx = abs(dem_transform.a)
dy = abs(dem_transform.e)

# 生成 x, y 坐标（像元中心）
xs = np.arange(out_w) * dem_transform.a + dem_transform.c + dem_transform.a / 2
ys = np.arange(out_h) * dem_transform.e + dem_transform.f + dem_transform.e / 2
X, Y = np.meshgrid(xs, ys)

# ==================== 5. 读取 DOM 并重采样到 DEM 网格 ====================
with rasterio.open(DOM_PATH) as src_dom:
    n_bands = min(src_dom.count, 3)
    dst = np.zeros((3, out_h, out_w), dtype=np.uint8)

    for b in range(n_bands):
        reproject(
            source=rasterio.band(src_dom, b + 1),
            destination=dst[b],
            src_transform=src_dom.transform,
            src_crs=src_dom.crs,
            dst_transform=dem_transform,
            dst_crs=dem_crs,
            resampling=Resampling.bilinear,
        )
    # 单波段灰度补齐为 RGB
    if n_bands == 1:
        dst[1] = dst[0]
        dst[2] = dst[0]

# 归一化到 0-1
dom_rgb = np.transpose(dst, (1, 2, 0)).astype(np.float32) / 255.0

# ==================== 6. 山体阴影增强立体感（可选但推荐） ====================
ls = LightSource(azdeg=AZDEG, altdeg=ALTDEG)
hillshade = ls.hillshade(
    np.where(np.isnan(dem), np.nanmin(dem), dem),
    vert_exag=VERT_EXAG, dx=dx, dy=dy
)
# DOM 颜色 × 阴影亮度（0.55 底光 + 0.45 阴影），保持 DOM 本色同时增强立体
dom_shaded = dom_rgb * (0.55 + 0.45 * hillshade[..., None])
dom_shaded = np.clip(dom_shaded, 0, 1)

# NoData 区域改成灰色
mask_nan = np.isnan(dem)
dom_shaded[mask_nan] = [0.75, 0.75, 0.75]

# ==================== 7. 3D 绘制 ====================
Z = np.where(mask_nan, np.nanmin(dem), dem) * VERT_EXAG

fig = plt.figure(figsize=(14, 10), dpi=120)
ax = fig.add_subplot(111, projection="3d")

# plot_surface 用 stride 进一步降采样（性能）
stride = max(1, max(out_h, out_w) // 350)
Xs = X[::stride, ::stride]
Ys = Y[::stride, ::stride]
Zs = Z[::stride, ::stride]
Cs = dom_shaded[::stride, ::stride]

ax.plot_surface(
    Xs, Ys, Zs,
    facecolors=Cs,
    rstride=1, cstride=1,
    linewidth=0,
    antialiased=False,
    shade=False,       # 已用 DOM + hillshade 着色，不再额外着色
)

# ==================== 8. 叠加 SHP 边界 + 质心标注 ====================
gdf = gpd.read_file(SHP_PATH)
if gdf.crs is not None and gdf.crs != dem_crs:
    gdf = gdf.to_crs(dem_crs)
gdf = gdf[gdf.geometry.notna() & ~gdf.geometry.is_empty].reset_index(drop=True)

# 用 DEM 网格构建高程插值器，输入顺序：(y, x)
interp = RegularGridInterpolator(
    (ys, xs), Z, bounds_error=False, fill_value=np.nan
)
z_range = np.nanmax(Z) - np.nanmin(Z)
z_offset_line = z_range * 0.005   # 线条抬高一点点，防 z-fighting
z_offset_text = z_range * 0.03    # 文字再抬高一点，避免被山头挡

def get_coord_arrays(geom):
    """兼容 Polygon / MultiPolygon / LineString"""
    gt = geom.geom_type
    if gt == "Polygon":
        return [geom.exterior.coords] + [r.coords for r in geom.interiors]
    if gt == "MultiPolygon":
        rings = []
        for p in geom.geoms:
            rings.append(p.exterior.coords)
            rings += [r.coords for r in p.interiors]
        return rings
    if gt == "LineString":
        return [geom.coords]
    if gt == "MultiLineString":
        return [g.coords for g in geom.geoms]
    return []

# 自动选择属性字段
PREFERRED_NAME_FIELDS = ["name", "Name", "NAME", "名称", "区名", "灾害点", "隐患点"]
name_field = None
for f in PREFERRED_NAME_FIELDS:
    if f in gdf.columns:
        name_field = f
        break
if name_field is None:
    for col in gdf.columns:
        if col == gdf.geometry.name:
            continue
        if gdf[col].dtype == object:
            name_field = col
            break
print(f"用于标注的属性字段：{name_field}")

gdf["__seq__"] = gdf.index + 1

for _, row in gdf.iterrows():
    geom = row.geometry

    # --- 边界线 ---
    for coords in get_coord_arrays(geom):
        arr = np.asarray(coords)
        if len(arr) < 2:
            continue
        px, py = arr[:, 0], arr[:, 1]
        pts = np.column_stack([py, px])         # 注意顺序 (y, x)
        pz = interp(pts)
        pz = np.where(np.isnan(pz), np.nanmin(Z), pz) + z_offset_line
        ax.plot(px, py, pz,
                color=LINE_COLOR, linewidth=LINE_WIDTH, zorder=10)

    # --- 质心标注 ---
    pt = geom.representative_point()            # 保证落在多边形内部
    cx, cy = pt.x, pt.y
    cz = interp([[cy, cx]])[0]
    if np.isnan(cz):
        cz = np.nanmin(Z)
    cz += z_offset_text

    seq = row["__seq__"]
    if name_field is not None and pd.notna(row[name_field]):
        label = f"{seq}. {row[name_field]}"
    else:
        label = f"{seq}"

    ax.scatter([cx], [cy], [cz],
               s=30, c="#FFD700", edgecolors="black",
               linewidths=0.8, zorder=19, depthshade=False)

    ax.text(cx, cy, cz, label,
            fontsize=11, fontweight="bold", color="white",
            ha="center", va="center", zorder=20,
            path_effects=[
                matplotlib.patheffects.withStroke(linewidth=3, foreground="black")
            ])

# ==================== 9. 图面设置 ====================
ax.set_xlabel("经度", fontsize=11)
ax.set_ylabel("纬度", fontsize=11)
ax.set_zlabel("高程 (m)", fontsize=11)
ax.set_title("庆元新发现点 — DOM + DEM 三维地形与灾害边界叠加",
             fontsize=15, fontweight="bold", pad=15)
ax.xaxis.set_major_locator(MaxNLocator(5))
ax.view_init(elev=VIEW_ELEV, azim=VIEW_AZIM)
ax.set_proj_type("persp", focal_length=1.0)

# --- 去掉背景网格与面板 ---
ax.grid(False)
for axis in (ax.xaxis, ax.yaxis, ax.zaxis):
    axis.pane.fill = False
    axis.pane.set_edgecolor("white")
    axis.pane.set_alpha(0)

# --- X / Y 轴刻度：十进制经纬度 ---
if dem_crs.is_geographic:
    ax.xaxis.set_major_formatter(
        FuncFormatter(lambda v, pos: f"{v:.4f}°E")
    )
    ax.yaxis.set_major_formatter(
        FuncFormatter(lambda v, pos: f"{v:.4f}°N")
    )
    ax.tick_params(axis="x", labelsize=9, rotation=0)
    ax.tick_params(axis="y", labelsize=9, rotation=0)
else:
    # 投影坐标系（米）保持默认
    ax.tick_params(axis="x", labelsize=9)
    ax.tick_params(axis="y", labelsize=9)

# --- Z 轴范围：最高值上方略留白 ---
z_min_data = np.nanmin(Z)
z_max_data = np.nanmax(Z)
z_pad = (z_max_data - z_min_data) * 0.05
ax.set_zlim(z_min_data, z_max_data + z_pad)

# --- 视觉比例：按物理米数计算，避免经纬度/米混用导致拉伸 ---
if dem_crs.is_geographic:
    lat_c = np.radians((ys.min() + ys.max()) / 2)
    xr_m = (xs.max() - xs.min()) * 111320 * np.cos(lat_c)
    yr_m = (ys.max() - ys.min()) * 111320
else:
    xr_m = xs.max() - xs.min()
    yr_m = ys.max() - ys.min()

zr_m = Z.max() - Z.min()

# Z 方向视觉夸张系数，1.0 表示按真实比例
Z_EXAG_VISUAL = 1.2
ax.set_box_aspect((xr_m, yr_m, max(zr_m, 1e-6) * Z_EXAG_VISUAL))

ax.set_facecolor("white")
fig.patch.set_facecolor("white")
plt.tight_layout()
plt.savefig(OUTPUT_PATH, bbox_inches="tight", dpi=150, facecolor="white")
plt.show()