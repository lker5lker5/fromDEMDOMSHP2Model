# -*- coding: utf-8 -*-
"""
数据处理：zip 校验与解压、DEM/DOM/SHP 读取、CGCS2000 重投影、降采样、JSON 打包。
"""

import sys
from pathlib import Path
_project_root = Path(__file__).parent.parent
if str(_project_root) not in sys.path:
    sys.path.insert(0, str(_project_root))

import time
import zipfile
import shutil
import re
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd
import rasterio
from rasterio.warp import reproject, Resampling, calculate_default_transform
import geopandas as gpd
from scipy.interpolate import RegularGridInterpolator
from matplotlib.colors import LightSource

from . import config


# ==================== 辅助函数 ====================

def hex_to_rgb(hex_str: str) -> tuple:
    """将十六进制颜色字符串 '#RRGGBB' 转换为 (r, g, b) 整数元组。"""
    hex_str = hex_str.lstrip("#")
    if len(hex_str) != 6:
        return (128, 128, 128)
    return (int(hex_str[0:2], 16), int(hex_str[2:4], 16), int(hex_str[4:6], 16))


# ==================== 1. Zip 校验与解压 ====================

def _find_file(extract_dir: Path, pattern: str) -> Optional[Path]:
    """在解压目录中不区分大小写地查找匹配 pattern 的文件（全匹配）。"""
    extract_dir = Path(extract_dir)
    pat = re.compile(pattern, re.IGNORECASE)
    for f in extract_dir.rglob("*"):
        if f.is_file() and pat.fullmatch(f.name):
            return f
    return None


def _find_extension(extract_dir: Path, ext: str) -> list[Path]:
    """在解压目录中不区分大小写地查找特定扩展名的文件。"""
    extract_dir = Path(extract_dir)
    ext_lower = ext.lower()
    results = []
    for f in extract_dir.rglob("*"):
        if f.is_file() and f.suffix.lower() == ext_lower:
            results.append(f)
    return results


def validate_and_extract_zip(zip_path: Path, extract_dir: Path) -> dict:
    """
    解压 zip，校验文件完整性。

    返回:
        {
            "success": True/False,
            "missing": [缺失文件列表],
            "warnings": [警告列表],
            "dem_path": Path or None,
            "dom_path": Path or None,
            "shp_stem": str or None,   # SHP 文件名主干（不含扩展名）
            "shp_dir": Path or None,
        }
    """
    extract_dir = Path(extract_dir)
    if extract_dir.exists():
        shutil.rmtree(extract_dir)
    extract_dir.mkdir(parents=True, exist_ok=True)

    with zipfile.ZipFile(zip_path, "r") as zf:
        zf.extractall(extract_dir)

    result = {
        "success": True,
        "missing": [],
        "warnings": [],
        "dem_path": None,
        "dom_path": None,
        "shp_stem": None,
        "shp_dir": None,
    }

    # --- 查找 DEM ---
    def _build_dem_pattern(kw, ext):
        ext_dot = ext.lstrip(".")
        return f".*{re.escape(kw)}.*\\.{re.escape(ext_dot)}$"

    for kw in config.DEM_KEYWORDS:
        for ext in config.DEM_EXTENSIONS:
            p = _find_file(extract_dir, _build_dem_pattern(kw, ext))
            if p is not None:
                result["dem_path"] = p
                break
        if result["dem_path"] is not None:
            break
    if result["dem_path"] is None:
        # 放宽：只要有一个 .tif/.tiff 就行，优先选文件较小的作为 DEM
        tifs = _find_extension(extract_dir, ".tif") + _find_extension(extract_dir, ".tiff")
        if tifs:
            # 选 size 较小的作为 DEM（通常 DEM < DOM）
            tifs.sort(key=lambda p: p.stat().st_size)
            result["dem_path"] = tifs[0]
            result["warnings"].append(f"未发现文件名含 DEM 关键字的文件，自动选择 {tifs[0].name} 作为 DEM")

    # --- 查找 DOM ---
    for kw in config.DOM_KEYWORDS:
        for ext in config.DOM_EXTENSIONS:
            p = _find_file(extract_dir, _build_dem_pattern(kw, ext))
            if p is not None:
                result["dom_path"] = p
                break
        if result["dom_path"] is not None:
            break
    if result["dom_path"] is None:
        tifs = _find_extension(extract_dir, ".tif") + _find_extension(extract_dir, ".tiff")
        # 排除已选为 DEM 的文件，选 size 较大的作为 DOM
        others = [p for p in tifs if p != result["dem_path"]]
        if others:
            others.sort(key=lambda p: p.stat().st_size, reverse=True)
            result["dom_path"] = others[0]
            result["warnings"].append(f"未发现文件名含 IMG/DOM 关键字的文件，自动选择 {others[0].name} 作为 DOM")

    # --- 查找 SHP ---
    shp_files = _find_extension(extract_dir, ".shp")
    if not shp_files:
        result["success"] = False
        result["missing"].append("*.shp 文件")
        return result

    shp_path = shp_files[0]
    stem = shp_path.stem
    shp_dir = shp_path.parent

    # 校验配套文件
    for ext in config.SHP_REQUIRED_EXTENSIONS:
        if ext == ".shp":
            continue
        candidate = shp_dir / f"{stem}{ext}"
        if not candidate.exists():
            result["missing"].append(f"{stem}{ext}")
            result["success"] = False

    # 可选文件
    for ext in config.SHP_OPTIONAL_EXTENSIONS:
        candidate = shp_dir / f"{stem}{ext}"
        if not candidate.exists():
            result["warnings"].append(f"缺少 {stem}{ext}（可选，继续处理）")

    result["shp_stem"] = stem
    result["shp_dir"] = str(shp_dir)
    return result


# ==================== 2. 栅格读取与重投影 ====================

def _downsample_shape(H: int, W: int, max_size: int):
    """计算降采样后的尺寸。"""
    scale = max(1, int(np.ceil(max(H, W) / max_size)))
    return H // scale, W // scale, scale


def read_dem_to_4490(dem_path: Path, max_size: int) -> dict:
    """
    读取 DEM，重投影到 EPSG:4490，降采样，返回数据字典。

    返回:
        {
            "dem": 2D ndarray (float32),
            "transform": affine transform,
            "crs": "EPSG:4490",
            "xs": 1D array,
            "ys": 1D array,
            "X": 2D meshgrid x,
            "Y": 2D meshgrid y,
            "dx": float,
            "dy": float,
            "nodata": float or None,
        }
    """
    with rasterio.open(dem_path) as src:
        src_crs = src.crs
        nodata = src.nodata if src.nodata is not None else -9999

        # 重投影到 EPSG:4490
        dst_crs = "EPSG:4490"
        transform, width, height = calculate_default_transform(
            src_crs, dst_crs, src.width, src.height, *src.bounds
        )

        # 先以原始分辨率重投影
        dem_4490 = np.zeros((height, width), dtype=np.float32)
        reproject(
            source=rasterio.band(src, 1),
            destination=dem_4490,
            src_transform=src.transform,
            src_crs=src_crs,
            dst_transform=transform,
            dst_crs=dst_crs,
            resampling=Resampling.bilinear,
        )

        # 降采样
        out_h, out_w, _ = _downsample_shape(height, width, max_size)
        dem_down = np.zeros((out_h, out_w), dtype=np.float32)
        dst_transform = transform * transform.scale(width / out_w, height / out_h)
        reproject(
            source=dem_4490,
            destination=dem_down,
            src_transform=transform,
            src_crs=dst_crs,
            dst_transform=dst_transform,
            dst_crs=dst_crs,
            resampling=Resampling.bilinear,
        )

    # 处理 NoData
    dem_down[dem_down == nodata] = np.nan
    dem_down[np.isclose(dem_down, nodata)] = np.nan

    # 坐标网格
    dx = abs(dst_transform.a)
    dy = abs(dst_transform.e)
    xs = np.arange(out_w) * dst_transform.a + dst_transform.c + dst_transform.a / 2
    ys = np.arange(out_h) * dst_transform.e + dst_transform.f + dst_transform.e / 2
    X, Y = np.meshgrid(xs, ys)

    return {
        "dem": dem_down,
        "transform": dst_transform,
        "crs": dst_crs,
        "xs": xs,
        "ys": ys,
        "X": X,
        "Y": Y,
        "dx": dx,
        "dy": dy,
        "nodata": nodata,
        "src_crs": str(src_crs),
        "dem_shape": list(dem_down.shape),
    }


def read_dom_to_4490(dom_path: Path, dem_info: dict) -> dict:
    """
    读取 DOM，重投影到 EPSG:4490 并重采样到 DEM 网格。
    返回字典，含 rgb 数组 (H, W, 3，值域 [0, 1]) 及元信息。
    """
    out_h = len(dem_info["ys"])
    out_w = len(dem_info["xs"])
    dst_transform = dem_info["transform"]
    dst_crs = dem_info["crs"]

    with rasterio.open(dom_path) as src_dom:
        n_bands = min(src_dom.count, 3)
        dst = np.zeros((3, out_h, out_w), dtype=np.uint8)

        for b in range(n_bands):
            reproject(
                source=rasterio.band(src_dom, b + 1),
                destination=dst[b],
                src_transform=src_dom.transform,
                src_crs=src_dom.crs,
                dst_transform=dst_transform,
                dst_crs=dst_crs,
                resampling=Resampling.bilinear,
            )

        if n_bands == 1:
            dst[1] = dst[0]
            dst[2] = dst[0]

    rgb = np.transpose(dst, (1, 2, 0)).astype(np.float32) / 255.0
    return {
        "rgb": rgb,
        "src_crs": str(src_dom.crs),
        "n_bands": int(src_dom.count),
        "dom_shape": list(rgb.shape[:2]),
    }


# ==================== 3. SHP 读取与边界提取 ====================

def _get_coord_arrays(geom):
    """兼容 Polygon / MultiPolygon / LineString，返回坐标列表。"""
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


def extract_field_metadata(gdf: gpd.GeoDataFrame) -> list:
    """
    提取 SHP 非几何字段的元数据。

    返回:
        [
            {
                "name": str,
                "dtype": str,
                "n_unique": int,
                "n_missing": int,
                "samples": list,
            },
            ...
        ]
    """
    fields = []
    geom_col = gdf.geometry.name
    for col in gdf.columns:
        if col == geom_col or col == "__seq__":
            continue
        series = gdf[col]
        meta = {
            "name": col,
            "dtype": str(series.dtype),
            "n_unique": int(series.nunique()),
            "n_missing": int(series.isna().sum()),
            "samples": [],
        }
        # 取最多 5 个非 NULL 的唯一值示例
        uniq = series.dropna().unique()
        if len(uniq) > 0:
            samples = []
            for v in uniq[:5]:
                s = str(v)
                if len(s) > 30:
                    s = s[:30] + "…"
                samples.append(s)
            meta["samples"] = samples
        fields.append(meta)
    return fields


def auto_detect_display_fields(gdf: gpd.GeoDataFrame, fields_meta: list) -> list:
    """
    返回 SHP 的第一个非几何字段作为默认展示字段。

    如果用户未手动勾选字段，则默认展示此字段。
    """
    if fields_meta:
        return [fields_meta[0]["name"]]
    return []


def extract_shp_fields_from_zip(zip_path: Path) -> dict:
    """
    从 SHP zip 文件提取字段元数据（轻量预处理，无需 DEM/DOM 地形数据）。
    用于前端在正式上传前展示 SHP 字段勾选项。

    返回:
        {
            "fields": [...],        # extract_field_metadata 格式
            "default_display_fields": [...],
            "n_zones": int,
        }
    """
    import tempfile

    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp_path = Path(tmp_dir)
        with zipfile.ZipFile(zip_path, "r") as zf:
            zf.extractall(tmp_path)

        shp_files = list(tmp_path.rglob("*.shp"))
        if not shp_files:
            raise ValueError("ZIP 文件中未找到 .shp 文件")

        gdf = gpd.read_file(str(shp_files[0]))

        fields_meta = extract_field_metadata(gdf)
        default_fields = auto_detect_display_fields(gdf, fields_meta)

        return {
            "fields": fields_meta,
            "default_display_fields": default_fields,
            "n_zones": len(gdf),
        }


def build_label_text(properties: dict, seq: int, display_fields: list) -> str:
    """
    根据 display_fields 生成质心标注文本。

    规则:
      - 0 个字段: "{seq}" (例如 "1")
      - 1-3 个字段: "{seq}. {val1} / {val2} / {val3}"
      - 4+ 个字段: "{seq}. {val1}\\n{val2}\\n{val3}…"(换行)
    """
    if not display_fields:
        return str(seq)

    vals = []
    for f in display_fields:
        v = properties.get(f, "")
        if v == "" or v is None:
            vals.append("—")
        else:
            vals.append(str(v))

    prefix = f"{seq}. "
    if len(vals) <= 3:
        return prefix + " / ".join(vals)
    else:
        return prefix + "\n".join(vals)


def build_hovertext(properties: dict, seq: int, elevation: float = None) -> str:
    """
    生成悬停提示文本，始终包含全部字段。

    格式:
        序号: 1
        高程: 307.2 m
        name: A区
        code: 1
        ...
    """
    parts = [f"序号: {seq}"]
    if elevation is not None:
        parts.append(f"高程: {elevation:.1f} m")
    for k, v in properties.items():
        if v is not None and v != "":
            parts.append(f"{k}: {v}")
        else:
            parts.append(f"{k}: —")
    return "<br>".join(parts)


def collect_diagnostics(dem_info: dict, dom_info: dict,
                        shp_info: dict, processing_time: float) -> dict:
    """收集 DEM/DOM/SHP 的处理诊断信息。"""
    dem_shape = dem_info.get("dem_shape", [0, 0])
    dom_shape = dom_info.get("dom_shape", dem_shape)
    return {
        "dem": {
            "shape": [int(dem_shape[0]), int(dem_shape[1])],
            "src_crs": str(dem_info.get("src_crs", "unknown")),
            "zmin": float(np.nanmin(dem_info["dem"])),
            "zmax": float(np.nanmax(dem_info["dem"])),
        },
        "dom": {
            "shape": [int(dom_shape[0]), int(dom_shape[1])],
            "n_bands": int(dom_info.get("n_bands", 3)),
            "src_crs": str(dom_info.get("src_crs", "unknown")),
        },
        "shp": {
            "n_zones": int(shp_info.get("n_zones", 0)),
            "src_crs": str(shp_info.get("src_crs", "unknown")),
            "n_fields": len(shp_info.get("fields_meta", [])),
        },
        "target_crs": "EPSG:4490",
        "processing_time_sec": round(processing_time, 1),
    }


def _detect_name_field(gdf: gpd.GeoDataFrame) -> Optional[str]:
    """自动检测用于标注的字段名。"""
    for f in config.PREFERRED_NAME_FIELDS:
        if f in gdf.columns:
            return f
    for col in gdf.columns:
        if col == gdf.geometry.name:
            continue
        if gdf[col].dtype == object:
            return col
    return None


def read_shp_to_4490(shp_dir: str, shp_stem: str, dem_info: dict) -> dict:
    """
    读取 SHP，重投影到 EPSG:4490，提取边界坐标和高程。

    返回:
        {
            "zones": [
                {
                    "seq": int,
                    "name": str or None,
                    "label": str,
                    "boundaries": [{"x": [...], "y": [...]}, ...],
                    "centroid": {"x": float, "y": float},
                    "centroid_z": float,
                    "properties": dict,
                },
                ...
            ],
            "name_field": str or None,
            "n_zones": int,
            "fields_meta": list,
            "default_display_fields": list,
            "src_crs": str,
        }
    """
    # 构建 SHP 路径
    shp_dir = Path(shp_dir) if isinstance(shp_dir, str) else shp_dir
    shp_path = shp_dir / f"{shp_stem}.shp"

    gdf = gpd.read_file(shp_path)
    if gdf.crs is not None and str(gdf.crs).upper() != "EPSG:4490":
        gdf = gdf.to_crs("EPSG:4490")
    gdf = gdf[gdf.geometry.notna() & ~gdf.geometry.is_empty].reset_index(drop=True)

    name_field = _detect_name_field(gdf)
    gdf["__seq__"] = gdf.index + 1

    # 构建高程插值器
    Z = np.where(np.isnan(dem_info["dem"]), np.nanmin(dem_info["dem"]), dem_info["dem"])
    interp = RegularGridInterpolator(
        (dem_info["ys"], dem_info["xs"]), Z, bounds_error=False, fill_value=np.nan
    )
    z_range = np.nanmax(Z) - np.nanmin(Z)
    z_offset_line = z_range * 0.005
    z_offset_text = z_range * 0.03

    zones = []
    for _, row in gdf.iterrows():
        geom = row.geometry
        boundaries = []

        for coords in _get_coord_arrays(geom):
            arr = np.asarray(coords)
            if len(arr) < 2:
                continue
            px, py = arr[:, 0], arr[:, 1]
            pts = np.column_stack([py, px])
            pz = interp(pts)
            pz = np.where(np.isnan(pz), np.nanmin(Z), pz) + z_offset_line
            boundaries.append({
                "x": px.tolist(),
                "y": py.tolist(),
                "z": pz.tolist(),
            })

        # 质心
        pt = geom.representative_point()
        cx, cy = pt.x, pt.y
        cz = interp([[cy, cx]])[0]
        if np.isnan(cz):
            cz = np.nanmin(Z)
        cz += z_offset_text

        seq = int(row["__seq__"])
        if name_field is not None and pd.notna(row.get(name_field)):
            name = str(row[name_field])
        else:
            name = None

        # 收集属性字段
        properties = {}
        for col in gdf.columns:
            if col in ("__seq__", gdf.geometry.name):
                continue
            properties[col] = str(row[col]) if pd.notna(row[col]) else ""

        zones.append({
            "seq": seq,
            "name": name,
            "label": f"{seq}. {name}" if name else f"{seq}",
            "boundaries": boundaries,
            "centroid": {"x": float(cx), "y": float(cy)},
            "centroid_z": float(cz),
            "properties": properties,
        })

    return {
        "zones": zones,
        "name_field": name_field,
        "n_zones": len(zones),
        "fields_meta": extract_field_metadata(gdf),
        "default_display_fields": auto_detect_display_fields(gdf, extract_field_metadata(gdf)),
        "src_crs": str(gdf.crs) if gdf.crs else "unknown",
    }


# ==================== 4. Hillshade 与颜色混合 ====================

def compute_hillshade(dem: np.ndarray, dx: float, dy: float,
                      azdeg: float = config.DEFAULT_AZDEG,
                      altdeg: float = config.DEFAULT_ALTDEG,
                      vert_exag: float = config.DEFAULT_VERT_EXAG) -> np.ndarray:
    """计算山体阴影。"""
    ls = LightSource(azdeg=azdeg, altdeg=altdeg)
    dem_filled = np.where(np.isnan(dem), np.nanmin(dem), dem)
    return ls.hillshade(dem_filled, vert_exag=vert_exag, dx=dx, dy=dy)


def blend_dom_hillshade(dom_rgb: np.ndarray, hillshade: np.ndarray,
                        mix: float = config.DEFAULT_HILLSHADE_MIX) -> np.ndarray:
    """DOM 颜色 × 阴影亮度。"""
    blended = dom_rgb * (0.55 + 0.45 * hillshade[..., None])
    blended = np.clip(blended, 0, 1)
    mask_nan = np.all(dom_rgb == 0, axis=-1)  # 近似 NoData
    blended[mask_nan] = [0.75, 0.75, 0.75]
    return blended


def enhance_dom_for_preview(dom_rgb: np.ndarray, brightness: float = 1.0) -> np.ndarray:
    """
    对 DOM 做对比度拉伸 + gamma 校正，用于 Cesium 真彩贴图导出。

    解决航片/DOM 数据值域集中（视觉上很暗、对比度低）的问题：
      1. 按 2%-98% 百分位拉伸到 [0,1] 范围
      2. 应用 gamma 校正提亮中间调

    参数:
        dom_rgb: (H, W, 3) float32, 值域 [0,1]
        brightness: gamma 校正参数（>1.0 提亮, <1.0 变暗）

    返回:
        (H, W, 3) float32, 值域 [0,1]
    """
    valid = ~np.all(dom_rgb == 0, axis=-1)
    if valid.sum() < 100:
        return dom_rgb

    enhanced = dom_rgb.copy()
    for i in range(3):
        band = dom_rgb[:, :, i]
        vals = band[valid]
        # 采样加速（如果像素太多，取 10000 个估算百分位）
        if len(vals) > 10000:
            idx = np.linspace(0, len(vals) - 1, 10000, dtype=int)
            vals = vals[idx]
        p2, p98 = np.percentile(vals, [2, 98])
        if p98 - p2 < 0.01:
            continue
        # 对比度拉伸
        stretched = np.clip((band.astype(float) - p2) / (p98 - p2), 0, 1)
        # gamma 校正
        if brightness != 1.0:
            gamma = 1.0 / max(brightness, 0.1)
            stretched = stretched ** gamma
        enhanced[:, :, i] = stretched

    # NoData 区域保持中性灰
    enhanced[~valid] = 0.75
    return enhanced


def compute_luminance_and_intensity(dom_rgb: np.ndarray, hillshade: np.ndarray,
                                    mix: float = config.DEFAULT_HILLSHADE_MIX,
                                    brightness: float = 1.0) -> np.ndarray:
    """
    计算亮度（用于 Plotly surfacecolor）。
    L = 0.299R + 0.587G + 0.114B，再用 hillshade 调制。

    brightness 使用 gamma 校正：gamma = 1/brightness
    - brightness=1.0 → 无变化
    - brightness>1.0 → 提亮（暗部提升明显，亮部不过曝）
    - brightness<1.0 → 变暗
    """
    luminance = 0.299 * dom_rgb[:, :, 0] + 0.587 * dom_rgb[:, :, 1] + 0.114 * dom_rgb[:, :, 2]
    intensity = luminance * (1 - mix + mix * hillshade)
    intensity = np.clip(intensity, 0, 1)
    # gamma 校正：比线性乘法更自然，亮部不易过曝
    if brightness != 1.0:
        gamma = 1.0 / max(brightness, 0.1)
        intensity = np.clip(intensity ** gamma, 0, 1)
    return intensity


def compute_dominant_color(dom_rgb: np.ndarray) -> tuple:
    """计算 DOM 的主色调（均值 RGB）。"""
    valid = ~np.all(dom_rgb == 0, axis=-1)
    if not valid.any():
        return (0.4, 0.6, 0.3)  # 默认绿色调
    r = dom_rgb[valid, 0].mean()
    g = dom_rgb[valid, 1].mean()
    b = dom_rgb[valid, 2].mean()
    return (float(r), float(g), float(b))


def build_terrain_colorscale(dom_rgb: np.ndarray, n_steps: int = 48,
                              color_mask: str = "",
                              color_mask_strength: float = 0.0) -> list:
    """
    基于 DOM 实际像素颜色（按亮度排序）生成自然的 colorscale。

    相比旧版单色渐变，此方法提取 DOM 中不同亮度区间的真实像素颜色
    （植被绿、土壤褐、建筑灰/白等），保留地物的自然色调变化。
    配合 hillshade 调制的 intensity，可显著提升地形视觉真实感。

    返回 Plotly 格式：[[0, 'rgb(r,g,b)'], ..., [1, 'rgb(r,g,b)']]

    可选参数 color_mask / color_mask_strength 用于叠加颜色遮罩（色调偏移）。
    """
    mask_rgb = None
    if color_mask:
        mask_rgb = hex_to_rgb(color_mask)
        ms = max(0.0, min(1.0, color_mask_strength))
    else:
        ms = 0.0

    # 提取有效像素（排除 NoData）
    valid = ~np.all(dom_rgb == 0, axis=-1)
    n_valid = valid.sum()
    if n_valid < 100:
        # 像素太少，回退到旧版单色渐变
        r, g, b = compute_dominant_color(dom_rgb)
        return _build_single_color_ramp(r, g, b, n_steps, mask_rgb, ms)

    pixel_rgb = dom_rgb[valid]  # (N, 3) float [0,1]

    # 计算亮度（与 compute_luminance_and_intensity 保持一致）
    luminance = 0.299 * pixel_rgb[:, 0] + 0.587 * pixel_rgb[:, 1] + 0.114 * pixel_rgb[:, 2]

    # 按亮度排序
    order = np.argsort(luminance)
    r_sorted = pixel_rgb[order, 0]
    g_sorted = pixel_rgb[order, 1]
    b_sorted = pixel_rgb[order, 2]

    # 均匀采样 n_steps 个颜色（相邻 3 像素平均平滑离群点）
    colorscale = []
    for i in range(n_steps):
        t = i / (n_steps - 1)
        center = t * (n_valid - 1)
        lo = max(0, int(center) - 1)
        hi = min(n_valid - 1, int(center) + 1)
        cr = int(np.mean(r_sorted[lo:hi + 1]) * 255)
        cg = int(np.mean(g_sorted[lo:hi + 1]) * 255)
        cb = int(np.mean(b_sorted[lo:hi + 1]) * 255)

        # 颜色遮罩：向遮罩色插值
        if mask_rgb and ms > 0:
            cr = int(cr * (1 - ms) + mask_rgb[0] * ms)
            cg = int(cg * (1 - ms) + mask_rgb[1] * ms)
            cb = int(cb * (1 - ms) + mask_rgb[2] * ms)

        colorscale.append([t, f"rgb({cr},{cg},{cb})"])

    return colorscale


def _build_single_color_ramp(r: float, g: float, b: float, n_steps: int = 256,
                              mask_rgb: tuple = None, ms: float = 0.0) -> list:
    """回退方案：单色从暗到亮渐变（旧版逻辑）。"""
    colorscale = []
    for i in range(n_steps):
        t = i / (n_steps - 1)
        dark = 0.15
        bright = 0.85
        factor = dark + t * (bright - dark)
        whiten = t * 0.3
        cr = min(255, int((r * factor + whiten) * 255))
        cg = min(255, int((g * factor + whiten) * 255))
        cb = min(255, int((b * factor + whiten) * 255))
        if mask_rgb and ms > 0:
            cr = int(cr * (1 - ms) + mask_rgb[0] * ms)
            cg = int(cg * (1 - ms) + mask_rgb[1] * ms)
            cb = int(cb * (1 - ms) + mask_rgb[2] * ms)
        colorscale.append([t, f"rgb({cr},{cg},{cb})"])
    return colorscale


# ==================== 4a. 坡度与分区统计 ====================


def compute_slope(dem: np.ndarray, dx: float, dy: float,
                  ys: np.ndarray) -> np.ndarray:
    """
    从 DEM 计算物理坡度（度）。

    dx/dy 为地理度单位（EPSG:4490），通过纬度平均计算米/度转换系数。
    返回与 dem 同形状的 2D 坡度数组（度）。
    """
    lat_mean = np.radians(np.mean(ys))
    dx_m = dx * 111320.0 * np.cos(lat_mean)
    dy_m = dy * 111320.0
    dzdy, dzdx = np.gradient(dem, dy_m, dx_m)
    slope = np.degrees(np.arctan(np.sqrt(dzdx ** 2 + dzdy ** 2)))
    return slope


def compute_zone_statistics(dem: np.ndarray, slope: np.ndarray,
                             X: np.ndarray, Y: np.ndarray,
                             zones: list) -> list:
    """
    对每个 SHP zone 计算海拔和坡度的分区统计量。

    使用 matplotlib.path.Path.contains_points 做多边形遮罩，
    提取每个 zone 内的 DEM 和 slope 栅格值，返回统计量。
    """
    from matplotlib.path import Path

    H, W = dem.shape
    points = np.column_stack([X.ravel(), Y.ravel()])
    results = []

    for zone in zones:
        boundaries = zone.get("boundaries", [])
        if not boundaries:
            continue
        # 取第一个 ring（外边界）
        b = boundaries[0]
        poly_xy = np.column_stack([b["x"], b["y"]])
        path = Path(poly_xy)
        mask = path.contains_points(points).reshape(H, W)

        dem_vals = dem[mask]
        slope_vals = slope[mask]
        dem_vals = dem_vals[~np.isnan(dem_vals)]
        slope_vals = slope_vals[~np.isnan(slope_vals)]

        has_dem = len(dem_vals) > 0
        has_slope = len(slope_vals) > 0

        results.append({
            "seq": zone["seq"],
            "label": zone.get("label", str(zone["seq"])),
            "name": zone.get("name"),
            "pixel_count": int(np.sum(mask)),
            "elevation_mean": float(np.mean(dem_vals)) if has_dem else None,
            "elevation_min": float(np.min(dem_vals)) if has_dem else None,
            "elevation_max": float(np.max(dem_vals)) if has_dem else None,
            "elevation_std": float(np.std(dem_vals)) if has_dem else None,
            "slope_mean": float(np.mean(slope_vals)) if has_slope else None,
            "slope_min": float(np.min(slope_vals)) if has_slope else None,
            "slope_max": float(np.max(slope_vals)) if has_slope else None,
            "slope_std": float(np.std(slope_vals)) if has_slope else None,
        })

    return results


def compute_zone_pivot(zone_stats: list, field: str, bin_width: float) -> dict:
    """
    按 zone 均值的字段分组，统计各区间的 zone 分布（透视表）。

    参数:
        zone_stats: compute_zone_statistics 返回的列表
        field: "elevation" 或 "slope"
        bin_width: 分组间隔

    返回:
        {"bins": [...], "counts": [...], "table": [{range, count, zones}]}
    """
    mean_key = field + "_mean"
    values = []
    labels = []
    for z in zone_stats:
        v = z.get(mean_key)
        if v is not None:
            values.append(v)
            labels.append(z["label"])

    if not values:
        return {"bins": [], "counts": [], "table": []}

    values = np.array(values)
    labels = np.array(labels)

    bin_start = np.floor(values.min() / bin_width) * bin_width
    bin_end = np.ceil(values.max() / bin_width) * bin_width + bin_width
    bins = np.arange(bin_start, bin_end, bin_width)

    counts = []
    table = []
    for i in range(len(bins) - 1):
        lo, hi = bins[i], bins[i + 1]
        mask = (values >= lo) & (values < hi)
        # 最后一个区间包含右边界
        if i == len(bins) - 2:
            mask = (values >= lo) & (values <= hi)
        n = int(np.sum(mask))
        counts.append(n)
        table.append({
            "range": f"{lo:.1f} – {hi:.1f}",
            "count": n,
            "zones": labels[mask].tolist(),
        })

    return {
        "bins": bins.tolist(),
        "counts": counts,
        "table": table,
    }


# ==================== 4b. 属性着色 ====================

# 数值型渐变色带（perceptually uniform: viridis）
_COLORMAP_NUMERIC = [
    "#440154", "#482878", "#3e4989", "#31688e", "#26828e",
    "#1f9e89", "#35b779", "#6ece58", "#b5de2b", "#fde725",
]

# 分类型离散调色板（ColorBrewer Set2 + Set3）
_COLORMAP_CATEGORICAL = [
    "#66c2a5", "#fc8d62", "#8da0cb", "#e78ac3", "#a6d854",
    "#ffd92f", "#e5c494", "#b3b3b3", "#1b9e77", "#d95f02",
    "#7570b3", "#e7298a", "#66a61e", "#e6ab02", "#a6761d",
]


def detect_field_type(values: list) -> str:
    """自动判定字段类型：超过 80% 的值可转为数字 → numeric，否则 categorical。"""
    if not values:
        return "categorical"
    numeric = 0
    total = 0
    for v in values:
        if v is None or str(v).strip() == "":
            continue
        total += 1
        try:
            float(str(v))
            numeric += 1
        except ValueError:
            pass
    if total == 0:
        return "categorical"
    return "numeric" if numeric / total > 0.8 else "categorical"


def assign_zone_colors(zones: list, field_name: str,
                        n_bins: int = 5) -> dict:
    """
    按指定字段值为每个 zone 分配颜色。

    返回:
        {
            "field_type": "numeric" | "categorical",
            "field_name": str,
            "zone_colors": [{seq, label, value, color, bin_label?}, ...],
            "legend": [{color, label}, ...],
        }
    """
    # 提取字段值
    values = []
    labels = []
    for z in zones:
        props = z.get("properties", {})
        v = props.get(field_name, "")
        if v is None:
            v = ""
        values.append(str(v).strip() if v != "" else "")
        labels.append(z.get("label", str(z.get("seq", ""))))

    ftype = detect_field_type(values)

    if ftype == "numeric":
        return _assign_numeric_colors(zones, values, labels, field_name, n_bins)
    else:
        return _assign_categorical_colors(zones, values, labels, field_name)


def _assign_numeric_colors(zones, raw_values, labels, field_name, n_bins):
    """数值型：等距分段 + 渐变色带。"""
    import math

    nums = []
    for i, v in enumerate(raw_values):
        if v != "":
            try:
                nums.append((float(v), i))
            except ValueError:
                pass

    if not nums:
        return {"field_type": "numeric", "field_name": field_name,
                "zone_colors": [], "legend": []}

    vals = [n[0] for n in nums]
    vmin, vmax = min(vals), max(vals)
    if vmax - vmin < 1e-9:
        # 所有值相同，统一用中间色
        cmap = _COLORMAP_NUMERIC
        mid_color = cmap[len(cmap) // 2]
        zone_colors = []
        for i, z in enumerate(zones):
            zone_colors.append({
                "seq": z["seq"], "label": labels[i],
                "value": raw_values[i] if raw_values[i] != "" else "—",
                "color": mid_color, "bin_label": "全部",
            })
        return {
            "field_type": "numeric", "field_name": field_name,
            "zone_colors": zone_colors,
            "legend": [{"color": mid_color, "label": f"{vmin:.1f}"}],
        }

    # 等距分箱
    bin_width = (vmax - vmin) / n_bins
    bin_colors = _build_numeric_bin_colors(n_bins)

    zone_colors = []
    color_map = {}
    for i, z in enumerate(zones):
        raw = raw_values[i]
        if raw == "":
            zone_colors.append({
                "seq": z["seq"], "label": labels[i],
                "value": "—", "color": "#cccccc", "bin_label": "无数据",
            })
            continue
        try:
            v = float(raw)
        except ValueError:
            zone_colors.append({
                "seq": z["seq"], "label": labels[i],
                "value": raw, "color": "#cccccc", "bin_label": "无数据",
            })
            continue
        idx = min(int((v - vmin) / bin_width), n_bins - 1)
        lo = vmin + idx * bin_width
        hi = lo + bin_width
        color = bin_colors[idx]
        bin_label = f"{lo:.1f} – {hi:.1f}"
        zone_colors.append({
            "seq": z["seq"], "label": labels[i],
            "value": raw, "color": color, "bin_label": bin_label,
        })
        color_map[bin_label] = color

    # 有序 legend
    legend = []
    for i in range(n_bins):
        lo = vmin + i * bin_width
        hi = lo + bin_width
        bl = f"{lo:.1f} – {hi:.1f}"
        if bl in color_map:
            legend.append({"color": color_map[bl], "label": bl})
    return {
        "field_type": "numeric", "field_name": field_name,
        "zone_colors": zone_colors, "legend": legend,
    }


def _build_numeric_bin_colors(n_bins):
    """从渐变色带中均匀采样 n 个颜色。"""
    cmap = _COLORMAP_NUMERIC
    if n_bins >= len(cmap):
        return cmap[:n_bins]
    indices = [int(i * (len(cmap) - 1) / max(n_bins - 1, 1)) for i in range(n_bins)]
    return [cmap[idx] for idx in indices]


def _assign_categorical_colors(zones, raw_values, labels, field_name):
    """分类型：每个唯一值一个颜色。"""
    uniq = []
    seen = set()
    for v in raw_values:
        if v != "" and v not in seen:
            uniq.append(v)
            seen.add(v)
    # 空值统一为 "—"
    has_empty = any(v == "" for v in raw_values)

    cmap = _COLORMAP_CATEGORICAL
    color_map = {}
    for i, u in enumerate(uniq):
        color_map[u] = cmap[i % len(cmap)]
    if has_empty:
        color_map[""] = "#cccccc"

    zone_colors = []
    for i, z in enumerate(zones):
        raw = raw_values[i]
        color = color_map.get(raw, "#d0d0d0")
        zone_colors.append({
            "seq": z["seq"], "label": labels[i],
            "value": raw if raw != "" else "—",
            "color": color,
            "bin_label": raw if raw != "" else "无数据",
        })

    legend = []
    for u in uniq:
        legend.append({"color": color_map[u], "label": u})
    if has_empty:
        legend.append({"color": "#cccccc", "label": "无数据"})

    return {
        "field_type": "categorical", "field_name": field_name,
        "zone_colors": zone_colors, "legend": legend,
    }


# ==================== 5. 主处理流程 ====================


def _process_common(dem_path: Path, dom_path: Path, shp_dir: str, shp_stem: str,
                    session_dir: Path, max_size: int, params: dict = None,
                    processing_time: float = 0.0) -> dict:
    """
    核心处理流程（解压后阶段共享）：读取 → 重投影 → 降采样 → hillshade → 缓存。
    """
    t_start = time.time()
    # 1. 读取 DEM
    dem_info = read_dem_to_4490(dem_path, max_size)

    # 2. 读取 DOM（返回 dict，含 rgb + 元信息）
    dom_info = read_dom_to_4490(dom_path, dem_info)
    dom_rgb = dom_info["rgb"]

    # 3. 读取 SHP
    shp_info = read_shp_to_4490(shp_dir, shp_stem, dem_info)

    # 4. Hillshade
    azdeg = params.get("azdeg", config.DEFAULT_AZDEG) if params else config.DEFAULT_AZDEG
    altdeg = params.get("altdeg", config.DEFAULT_ALTDEG) if params else config.DEFAULT_ALTDEG
    vert_exag = params.get("vert_exag", config.DEFAULT_VERT_EXAG) if params else config.DEFAULT_VERT_EXAG
    hillshade_mix = params.get("hillshade_mix", config.DEFAULT_HILLSHADE_MIX) if params else config.DEFAULT_HILLSHADE_MIX
    texture_brightness = params.get("texture_brightness", 1.0) if params else 1.0
    color_mask = (params.get("color_mask") or "") if params else ""
    color_mask_strength = params.get("color_mask_strength", 0.0) if params else 0.0

    hillshade = compute_hillshade(dem_info["dem"], dem_info["dx"], dem_info["dy"],
                                  azdeg=azdeg, altdeg=altdeg, vert_exag=vert_exag)

    # 5. 计算亮度 + intensity（供 Plotly surfacecolor）— gamma 校正
    intensity = compute_luminance_and_intensity(dom_rgb, hillshade, mix=hillshade_mix,
                                                brightness=texture_brightness)

    # 6. 构建 colorscale（含颜色遮罩）
    colorscale = build_terrain_colorscale(dom_rgb, color_mask=color_mask,
                                          color_mask_strength=color_mask_strength)

    # 7. 构建 Z（抬升，用于 3D）
    Z = np.where(np.isnan(dem_info["dem"]), np.nanmin(dem_info["dem"]), dem_info["dem"])

    # === 缓存中间结果到 session 目录 ===
    cs_t = np.array([c[0] for c in colorscale])
    cs_rgb = np.array([
        [int(v) for v in c[1].replace("rgb(", "").replace(")", "").split(",")]
        for c in colorscale
    ])
    np.savez_compressed(
        session_dir / "cached_data.npz",
        dem=dem_info["dem"],
        X=dem_info["X"],
        Y=dem_info["Y"],
        Z=Z,
        xs=dem_info["xs"],
        ys=dem_info["ys"],
        dom_rgb=dom_rgb,
        intensity=intensity,
        colorscale_t=cs_t,
        colorscale_rgb=cs_rgb,
        dx=np.array([dem_info["dx"]]),
        dy=np.array([dem_info["dy"]]),
    )

    # 8. 构建返回数据
    z_min = float(np.nanmin(Z))
    z_max = float(np.nanmax(Z))
    bounds = {
        "xmin": float(dem_info["xs"].min()),
        "xmax": float(dem_info["xs"].max()),
        "ymin": float(dem_info["ys"].min()),
        "ymax": float(dem_info["ys"].max()),
    }

    defaults = shp_info.get("default_display_fields", config.DEFAULT_DISPLAY_FIELDS)

    return {
        "meta": {
            "bounds": bounds,
            "zmin": z_min,
            "zmax": z_max,
            "n_zones": shp_info["n_zones"],
            "crs": "EPSG:4490",
        },
        "zones": shp_info["zones"],
        "fields": shp_info.get("fields_meta", []),
        "default_display_fields": defaults,
        "diagnostics": collect_diagnostics(dem_info, dom_info, shp_info, time.time() - t_start),
        "cached": {
            "has_cache": True,
            "dem_shape": list(Z.shape),
            "xs": dem_info["xs"].tolist(),
            "ys": dem_info["ys"].tolist(),
        },
    }


def process_upload(zip_path: Path, session_dir: Path, max_size: int = None,
                   params: dict = None) -> dict:
    """
    完整处理流程（从 zip 解压）：解压 → 校验 → 读取 → 重投影 → 降采样 → 打包。

    返回可序列化为 JSON 的响应字典。
    """
    if max_size is None:
        max_size = config.MAX_SIZE

    vresult = validate_and_extract_zip(zip_path, session_dir / "extracted")
    if not vresult["success"]:
        missing_str = ", ".join(vresult["missing"])
        raise ValueError(
            f"缺少以下文件：{missing_str}\n"
            "请将 SHP 的 .shp/.shx/.dbf/.prj 一起打包上传。"
        )

    return _process_common(
        vresult["dem_path"], vresult["dom_path"],
        vresult["shp_dir"], vresult["shp_stem"],
        session_dir, max_size, params,
    )


def process_upload_direct(dem_path: Path, dom_path: Path, shp_zip_path: Path,
                          session_dir: Path, max_size: int = None,
                          params: dict = None) -> dict:
    """
    处理分别上传的三个文件：DEM(.tif) + DOM(.tif) + SHP(.zip)。

    返回可序列化为 JSON 的响应字典。
    """
    if max_size is None:
        max_size = config.MAX_SIZE

    # 解压 SHP zip
    shp_extract_dir = session_dir / "extracted_shp"
    vresult = validate_and_extract_zip(shp_zip_path, shp_extract_dir)
    if not vresult["success"]:
        missing_str = ", ".join(vresult["missing"])
        raise ValueError(
            f"SHP zip 中缺少以下文件：{missing_str}\n"
            "请将 SHP 的 .shp/.shx/.dbf/.prj 一起打包上传。"
        )

    result = _process_common(
        dem_path, dom_path,
        vresult["shp_dir"], vresult["shp_stem"],
        session_dir, max_size, params,
    )

    return result


def process_shp_only(shp_zip_path: Path, session_dir: Path) -> dict:
    """
    仅处理 SHP 数据（无 DEM/DOM），用于天地图底图展示。

    提取 SHP 区划的边界和质心坐标（EPSG:4490），不涉及高程插值。
    返回的 zones 中 centroid_z 统一设为 0（无 DEM 高程）。

    返回:
        {
            "meta": {"bounds": {...}, "n_zones": int, "crs": "EPSG:4490"},
            "zones": [...],
            "fields": [...],
            "default_display_fields": [...],
            "diagnostics": {...},
            "cached": {"has_cache": False},
        }
    """
    t_start = time.time()

    # 解压 SHP zip
    shp_extract_dir = session_dir / "extracted_shp"
    vresult = validate_and_extract_zip(shp_zip_path, shp_extract_dir)
    if not vresult["success"]:
        missing_str = ", ".join(vresult["missing"])
        raise ValueError(
            f"SHP zip 中缺少以下文件：{missing_str}\n"
            "请将 SHP 的 .shp/.shx/.dbf/.prj 一起打包上传。"
        )

    shp_dir = Path(vresult["shp_dir"])
    shp_stem = vresult["shp_stem"]
    shp_path = shp_dir / f"{shp_stem}.shp"

    gdf = gpd.read_file(shp_path)
    if gdf.crs is not None and str(gdf.crs).upper() != "EPSG:4490":
        gdf = gdf.to_crs("EPSG:4490")
    gdf = gdf[gdf.geometry.notna() & ~gdf.geometry.is_empty].reset_index(drop=True)
    gdf["__seq__"] = gdf.index + 1

    name_field = _detect_name_field(gdf)
    fields_meta = extract_field_metadata(gdf)
    defaults = auto_detect_display_fields(gdf, fields_meta) or config.DEFAULT_DISPLAY_FIELDS

    zones = []
    bounds = {"xmin": float("inf"), "xmax": float("-inf"),
              "ymin": float("inf"), "ymax": float("-inf")}

    for _, row in gdf.iterrows():
        geom = row.geometry
        boundaries = []

        for coords in _get_coord_arrays(geom):
            arr = np.asarray(coords)
            if len(arr) < 2:
                continue
            px, py = arr[:, 0], arr[:, 1]
            # 无 DEM，z 设为 0
            pz = np.zeros(len(px)).tolist()
            boundaries.append({
                "x": px.tolist(),
                "y": py.tolist(),
                "z": pz,
            })
            # 更新 bounds
            bounds["xmin"] = min(bounds["xmin"], float(px.min()))
            bounds["xmax"] = max(bounds["xmax"], float(px.max()))
            bounds["ymin"] = min(bounds["ymin"], float(py.min()))
            bounds["ymax"] = max(bounds["ymax"], float(py.max()))

        # 质心
        pt = geom.representative_point()
        cx, cy = float(pt.x), float(pt.y)

        seq = int(row["__seq__"])
        if name_field is not None and pd.notna(row.get(name_field)):
            name = str(row[name_field])
        else:
            name = None

        # 收集属性字段
        properties = {}
        for col in gdf.columns:
            if col in ("__seq__", gdf.geometry.name):
                continue
            properties[col] = str(row[col]) if pd.notna(row[col]) else ""

        zones.append({
            "seq": seq,
            "name": name,
            "label": f"{seq}. {name}" if name else f"{seq}",
            "boundaries": boundaries,
            "centroid": {"x": cx, "y": cy},
            "centroid_z": 0.0,
            "properties": properties,
        })

    proc_time = time.time() - t_start

    # 修复 bounds（无数据时
    if bounds["xmin"] == float("inf"):
        bounds = {"xmin": 0, "xmax": 0, "ymin": 0, "ymax": 0}

    return {
        "meta": {
            "bounds": bounds,
            "zmin": 0,
            "zmax": 0,
            "n_zones": len(zones),
            "crs": "EPSG:4490",
        },
        "zones": zones,
        "fields": fields_meta,
        "default_display_fields": defaults,
        "diagnostics": {
            "shp": {
                "n_zones": len(zones),
                "src_crs": str(gdf.crs) if gdf.crs else "unknown",
                "n_fields": len(fields_meta),
            },
            "target_crs": "EPSG:4490",
            "processing_time_sec": round(proc_time, 1),
        },
        "cached": {"has_cache": False},
    }


def load_cached_data(session_dir: Path) -> dict:
    """从 session 缓存加载中间数据。"""
    cache_path = session_dir / "cached_data.npz"
    if not cache_path.exists():
        raise FileNotFoundError("Session 缓存数据不存在")

    data = np.load(cache_path, allow_pickle=True)

    # 重建 colorscale（兼容新格式和旧格式）
    if "colorscale_rgb" in data:
        rgb_vals = data["colorscale_rgb"]
        t_vals = data.get("colorscale_t", None)
        n = len(rgb_vals)
        colorscale = []
        for i in range(n):
            t = float(t_vals[i]) if t_vals is not None else (i / (n - 1) if n > 1 else 0)
            r, g, b = int(rgb_vals[i][0]), int(rgb_vals[i][1]), int(rgb_vals[i][2])
            colorscale.append([t, f"rgb({r},{g},{b})"])
    else:
        # 旧格式兼容
        r_vals = data["colorscale_r"]
        g_vals = data["colorscale_g"]
        b_vals = data["colorscale_b"]
        n = len(r_vals)
        colorscale = []
        for i in range(n):
            colorscale.append([
                i / (n - 1) if n > 1 else 0,
                f"rgb({int(r_vals[i])},{int(g_vals[i])},{int(b_vals[i])})"
            ])

    dem = data["dem"]

    return {
        "dem": dem,
        "X": data["X"],
        "Y": data["Y"],
        "Z": data["Z"],
        "xs": data["xs"],
        "ys": data["ys"],
        "dom_rgb": data["dom_rgb"],
        "intensity": data["intensity"],
        "colorscale": colorscale,
        "dx": float(data["dx"][0]),
        "dy": float(data["dy"][0]),
        "dem_shape": list(dem.shape),
    }


def recompute_with_params(cached: dict, params: dict) -> dict:
    """
    用新参数重新计算 hillshade、intensity（含 gamma 校正纹理亮度 + 颜色遮罩）。
    返回更新后的数据字典（供 rendering 使用）。
    """
    dem = cached["dem"]
    dx = cached["dx"]
    dy = cached["dy"]
    dom_rgb = cached["dom_rgb"]

    azdeg = params.get("azdeg", config.DEFAULT_AZDEG)
    altdeg = params.get("altdeg", config.DEFAULT_ALTDEG)
    vert_exag = params.get("vert_exag", config.DEFAULT_VERT_EXAG)
    hillshade_mix = params.get("hillshade_mix", config.DEFAULT_HILLSHADE_MIX)
    texture_brightness = params.get("texture_brightness", 1.0)
    color_mask = params.get("color_mask") or ""
    color_mask_strength = params.get("color_mask_strength", 0.0)

    hillshade = compute_hillshade(dem, dx, dy, azdeg=azdeg, altdeg=altdeg, vert_exag=vert_exag)

    # 亮度 gamma 校正
    intensity = compute_luminance_and_intensity(dom_rgb, hillshade, mix=hillshade_mix,
                                                brightness=texture_brightness)

    # 颜色遮罩：重建 colorscale
    colorscale = build_terrain_colorscale(dom_rgb, color_mask=color_mask,
                                          color_mask_strength=color_mask_strength)

    # 计算增强后的 DOM（供 Mesh3d vertexcolor 使用）
    enhanced_dom = enhance_dom_for_preview(dom_rgb, texture_brightness)
    if color_mask and color_mask_strength > 0:
        mr, mg, mb = hex_to_rgb(color_mask)
        mask_rgb = np.array([mr, mg, mb], dtype=np.float32) / 255.0
        ms = max(0.0, min(1.0, color_mask_strength))
        enhanced_dom = enhanced_dom * (1 - ms) + mask_rgb * ms
        enhanced_dom = np.clip(enhanced_dom, 0, 1)

    result = dict(cached)
    result["intensity"] = intensity
    result["colorscale"] = colorscale
    result["hillshade"] = hillshade
    result["enhanced_dom"] = enhanced_dom
    return result