# -*- coding: utf-8 -*-
"""
Plotly Figure 生成与 PNG 导出。
"""

import sys
from pathlib import Path
_project_root = Path(__file__).parent.parent
if str(_project_root) not in sys.path:
    sys.path.insert(0, str(_project_root))

import io
import json
import math
import numpy as np
import plotly.graph_objects as go
from plotly.io import to_image
from plotly.utils import PlotlyJSONEncoder

from webapp import config
from webapp.processing import build_label_text, build_hovertext


# ==================== 相机转换 ====================

def elev_azim_to_eye(elev_deg: float, azim_deg: float, distance: float = None):
    """
    将 matplotlib 的 view_init(elev, azim) 参数转换为 Plotly 的 scene.camera.eye。
    返回相对于原点的偏移量。
    """
    if distance is None:
        distance = config.CAMERA_DISTANCE
    elev = math.radians(elev_deg)
    azim = math.radians(azim_deg)
    x = distance * math.cos(elev) * math.cos(azim)
    y = distance * math.cos(elev) * math.sin(azim)
    z = distance * math.sin(elev)
    return {"x": x, "y": y, "z": z}


def elev_azim_to_up(elev_deg: float):
    """计算相机的 up 向量。"""
    return {"x": 0, "y": 0, "z": 1}


# ==================== 宽高比计算 ====================

def get_aspect_ratio(xs, ys, Z, z_exag_visual: float = None):
    """
    计算 Plotly scene.aspectratio。
    基于真实物理尺寸比例，与原始脚本的 box_aspect 逻辑一致。
    """
    if z_exag_visual is None:
        z_exag_visual = config.DEFAULT_Z_EXAG_VISUAL

    x_range = float(xs.max() - xs.min())
    y_range = float(ys.max() - ys.min())
    z_range = float(np.nanmax(Z) - np.nanmin(Z))

    # 如果是地理坐标，需要转换为米来算比例
    # 由于已经是 EPSG:4490 地理坐标，使用近似转换
    lat_c = math.radians(float((ys.min() + ys.max()) / 2))
    xr_m = x_range * 111320 * math.cos(lat_c)
    yr_m = y_range * 111320
    zr_m = max(z_range, 1.0)

    # 归一化比例
    max_dim = max(xr_m, yr_m, zr_m * z_exag_visual)
    return {
        "x": xr_m / max_dim,
        "y": yr_m / max_dim,
        "z": zr_m * z_exag_visual / max_dim,
    }


# ==================== Figure 生成 ====================

def _build_surface_trace(data: dict, params: dict) -> go.Surface:
    """构建地形 Surface trace（保留用于快速回退）。"""
    X = data["X"]
    Y = data["Y"]
    Z = data["Z"]
    intensity = data["intensity"]
    colorscale = data["colorscale"]

    # 降采样 stride（性能控制）
    max_size = params.get("max_size", config.MAX_SIZE)
    stride = max(1, max(X.shape[0], X.shape[1]) // 350)

    trace = go.Surface(
        x=X[::stride, ::stride],
        y=Y[::stride, ::stride],
        z=Z[::stride, ::stride],
        surfacecolor=intensity[::stride, ::stride],
        colorscale=colorscale,
        showscale=False,
        contours={
            "x": {"show": False},
            "y": {"show": False},
            "z": {"show": False},
        },
        lighting={
            "ambient": 0.4,
            "diffuse": 0.85,
            "specular": 0.05,
            "roughness": 0.7,
            "fresnel": 0.0,
        },
        lightposition={
            "x": float(X.mean() + (X.max() - X.min()) * 0.5),
            "y": float(Y.mean() + (Y.max() - Y.min()) * 0.5),
            "z": float(np.nanmax(Z) * 3) if not np.all(np.isnan(Z)) else 100,
        },
        opacity=1.0,
        name="terrain",
        hoverinfo="skip",
    )
    return trace


# Mesh3d 网格目标分辨率（每维度最多顶点数，平衡画质与 JSON 体积）
_MESH_TARGET = 250


def _build_mesh_data(data: dict, params: dict) -> dict:
    """
    构建地形网格原始数据（供 Mesh3d trace 和 /api/mesh-data 共用）。

    返回:
        {
            "x": list[float], "y": list[float], "z": list[float],  # 降采样后的顶点坐标
            "i": list[int], "j": list[int], "k": list[int],         # 三角形索引
            "vertexcolor": list[str],  # ['rgb(R,G,B)', ...] 供 Plotly 使用
            "colors": list[list[int]], # [[R,G,B], ...] 供 Three.js vertexColors 使用
            "H_sub": int, "W_sub": int,                             # 降采样后网格尺寸
        }
    """
    X = data["X"]
    Y = data["Y"]
    Z = data["Z"]
    enhanced_dom = data.get("enhanced_dom")   # (H, W, 3) float [0,1]
    hillshade = data.get("hillshade")          # (H, W) float [0,1]

    # --- 1. 降采样 ---
    H, W = X.shape
    stride = max(1, max(H, W) // _MESH_TARGET)
    xs_sub = X[::stride, ::stride].ravel()
    ys_sub = Y[::stride, ::stride].ravel()
    zs_sub = Z[::stride, ::stride].ravel()
    H_sub = (H + stride - 1) // stride
    W_sub = (W + stride - 1) // stride

    # --- 2. 构建 vertexcolor ---
    colors_raw = None
    if enhanced_dom is not None and enhanced_dom.size > 0:
        rgb_sub = enhanced_dom[::stride, ::stride].reshape(-1, 3)

        # 可选：烘焙 hillshade（通过 hillshade_mix 控制强度）
        mix = params.get("hillshade_mix", 0.0)
        if hillshade is not None and mix > 0:
            hs_sub = hillshade[::stride, ::stride].ravel()
            shade = 1.0 - mix + mix * hs_sub
            rgb_sub = np.clip(rgb_sub * shade[:, np.newaxis], 0, 1)

        # float [0,1] → int [0,255] → rgb 字符串
        rgb_int = (rgb_sub * 255).astype(np.uint8)
        vertexcolor = [f'rgb({r},{g},{b})' for r, g, b in rgb_int]
        colors_raw = rgb_int.tolist()  # [[R,G,B], ...] for Three.js
    else:
        vertexcolor = 'rgb(128,128,128)'

    # --- 3. 构建三角形索引（每个 quad → 2 个 triangle）---
    i_list, j_list, k_list = [], [], []
    for r in range(H_sub - 1):
        for c in range(W_sub - 1):
            a = r * W_sub + c
            b = a + 1
            d = a + W_sub
            e = d + 1
            # triangle 1: a-b-e, triangle 2: a-e-d
            i_list.extend([a, a])
            j_list.extend([b, e])
            k_list.extend([e, d])

    return {
        "x": xs_sub.tolist(),
        "y": ys_sub.tolist(),
        "z": zs_sub.tolist(),
        "i": i_list,
        "j": j_list,
        "k": k_list,
        "vertexcolor": vertexcolor,
        "colors": colors_raw,
        "H_sub": H_sub,
        "W_sub": W_sub,
    }


def _build_mesh_trace(data: dict, params: dict) -> go.Mesh3d:
    """
    构建地形 Mesh3d trace，使用 vertexcolor（逐顶点 DOM RGB）替代
    Surface 的单通道 surfacecolor + colorscale 近似。
    """
    X = data["X"]
    Y = data["Y"]
    Z = data["Z"]

    mesh = _build_mesh_data(data, params)

    return go.Mesh3d(
        x=mesh["x"],
        y=mesh["y"],
        z=mesh["z"],
        i=mesh["i"],
        j=mesh["j"],
        k=mesh["k"],
        vertexcolor=mesh["vertexcolor"],
        lighting={
            "ambient": 0.45,
            "diffuse": 0.85,
            "specular": 0.08,
            "roughness": 0.6,
            "fresnel": 0.1,
        },
        lightposition={
            "x": float(X.mean() + (X.max() - X.min()) * 0.5),
            "y": float(Y.mean() + (Y.max() - Y.min()) * 0.5),
            "z": float(np.nanmax(Z) * 3) if not np.all(np.isnan(Z)) else 100,
        },
        hoverinfo="skip",
        showscale=False,
        name="terrain",
    )


def _build_boundary_traces(zones: list, params: dict) -> list[go.Scatter3d]:
    """构建边界线 Scatter3d traces。"""
    color = params.get("line_color", config.DEFAULT_LINE_COLOR)
    width = params.get("line_width", config.DEFAULT_LINE_WIDTH)

    traces = []
    for zone in zones:
        for bidx, boundary in enumerate(zone["boundaries"]):
            trace = go.Scatter3d(
                x=boundary["x"],
                y=boundary["y"],
                z=boundary["z"],
                mode="lines",
                line={
                    "color": color,
                    "width": width,
                },
                showlegend=False,
                hoverinfo="skip",
                name=f"zone_{zone['seq']}_boundary_{bidx}",
            )
            traces.append(trace)
    return traces


def _build_centroid_traces(zones: list, params: dict, z_min: float,
                           display_fields: list = None) -> list[go.Scatter3d]:
    """构建质心标注 traces（标注文字 + 质心圆点），支持动态 display_fields。"""
    show_labels = params.get("show_labels", config.DEFAULT_SHOW_LABELS)
    show_centroids = params.get("show_centroids", config.DEFAULT_SHOW_CENTROIDS)
    label_fontsize = params.get("label_fontsize", config.DEFAULT_LABEL_FONTSIZE)
    label_color = params.get("label_color", config.DEFAULT_LABEL_COLOR)
    centroid_size = params.get("centroid_size", config.DEFAULT_CENTROID_SIZE)
    line_color = params.get("line_color", config.DEFAULT_LINE_COLOR)
    traces = []

    if not show_labels and not show_centroids:
        return traces

    if display_fields is None:
        display_fields = config.DEFAULT_DISPLAY_FIELDS

    cx_list, cy_list, cz_list = [], [], []
    text_list, hover_list = [], []

    for zone in zones:
        cx = zone["centroid"]["x"]
        cy = zone["centroid"]["y"]
        cz = zone["centroid_z"]
        props = zone.get("properties", {})
        seq = zone["seq"]

        # label text 根据 display_fields 动态生成
        label_text = build_label_text(props, seq, display_fields)

        # hovertext 始终包含全部字段 + 高程信息
        hover_text = build_hovertext(props, seq, cz)

        cx_list.append(cx)
        cy_list.append(cy)
        cz_list.append(cz)
        text_list.append(label_text if show_labels else "")
        hover_list.append(hover_text)

    if not cx_list:
        return traces

    if show_centroids:
        traces.append(go.Scatter3d(
            x=cx_list,
            y=cy_list,
            z=cz_list,
            mode="markers" if not show_labels else "markers+text",
            marker={
                "size": centroid_size,
                "color": line_color,
                "line": {"color": "white", "width": 1.5},
                "symbol": "circle",
            },
            text=text_list if show_labels else None,
            textposition="top center",
            textfont={
                "size": label_fontsize,
                "color": label_color,
            },
            hovertext=hover_list,
            hoverinfo="text",
            hoverlabel={
                "bgcolor": "white",
                "bordercolor": line_color,
                "font": {"color": "#333", "size": 12, "family": "Microsoft YaHei, sans-serif"},
                "align": "left",
            },
            showlegend=False,
            name="centroids",
        ))

    return traces


def create_plotly_figure(data: dict, params: dict, zones: list = None,
                         meta: dict = None, z_min: float = None,
                         display_fields: list = None) -> dict:
    """
    生成完整的 Plotly figure，返回 plotly Figure 对象。

    参数:
        data: 来自 processing.recompute_with_params 或 processing.load_cached_data
        params: 参数字典
        zones: 区域信息列表（含 boundaries/centroid）
        meta: 元信息（bounds, zmin, zmax）
        z_min: Z 最小值（用于图面设置）
        display_fields: 标注展示字段列表

    返回:
        go.Figure 对象
    """
    if zones is None:
        zones = []
    if meta is None:
        meta = {}
    if z_min is None:
        z_min = float(np.nanmin(data["Z"]))

    title = params.get("title", config.DEFAULT_TITLE)
    title_fontsize = params.get("title_fontsize", config.DEFAULT_TITLE_FONTSIZE)
    title_font = params.get("title_font", config.DEFAULT_TITLE_FONT)
    display_fields = display_fields or params.get("display_fields", config.DEFAULT_DISPLAY_FIELDS)

    # --- 构建 traces ---
    traces = []
    traces.append(_build_mesh_trace(data, params))
    traces.extend(_build_boundary_traces(zones, params))
    traces.extend(_build_centroid_traces(zones, params, z_min, display_fields))

    # --- 相机：保持 view_init 语义，不依赖数据坐标 ---
    elev = params.get("view_elev", config.DEFAULT_VIEW_ELEV)
    azim = params.get("view_azim", config.DEFAULT_VIEW_AZIM)
    view_dist = params.get("view_distance", config.DEFAULT_VIEW_DISTANCE)
    eye_offset = elev_azim_to_eye(elev, azim, view_dist)
    camera = {
        "eye": eye_offset,
        "center": {"x": 0, "y": 0, "z": 0},
        "up": {"x": 0, "y": 0, "z": 1},
    }

    # --- 显式设置场景坐标轴范围（确保数据居中，不偏角落）---
    xs_arr = data.get("xs", np.array([]))
    ys_arr = data.get("ys", np.array([]))
    Z_arr = data["Z"]
    if len(xs_arr) > 0 and len(ys_arr) > 0:
        x_pad = max((xs_arr.max() - xs_arr.min()) * 0.15, 0.01)
        y_pad = max((ys_arr.max() - ys_arr.min()) * 0.15, 0.01)
        z_pad = max((np.nanmax(Z_arr) - np.nanmin(Z_arr)) * 0.1, 5)
        x_range_val = [float(xs_arr.min() - x_pad), float(xs_arr.max() + x_pad)]
        y_range_val = [float(ys_arr.min() - y_pad), float(ys_arr.max() + y_pad)]
        z_range_val = [float(np.nanmin(Z_arr) - z_pad), float(np.nanmax(Z_arr) + z_pad)]
    else:
        x_range_val = None
        y_range_val = None
        z_range_val = None

    # --- 宽高比 ---
    z_exag = params.get("z_exag_visual", config.DEFAULT_Z_EXAG_VISUAL)
    xs_arr = data.get("xs", np.array([]))
    ys_arr = data.get("ys", np.array([]))
    Z_arr = data["Z"]
    aspect_ratio = get_aspect_ratio(xs_arr, ys_arr, Z_arr, z_exag)

    # --- 坐标轴显示控制 ---
    show_axes = params.get("show_axes", config.DEFAULT_SHOW_AXES)
    show_grid = params.get("show_grid", config.DEFAULT_SHOW_GRID)

    # --- 建立 layout ---
    layout = go.Layout(
        title={
            "text": title,
            "x": 0.5,
            "xanchor": "center",
            "font": {"size": title_fontsize, "family": title_font},
        },
        scene={
            "xaxis": {
                "title": "经度" if show_axes else "",
                "tickformat": ".2f" if show_axes else "",
                "showticklabels": show_axes,
                "range": x_range_val,
                "showgrid": show_grid,
                "gridcolor": "rgba(100,100,100,0.5)" if show_grid else "rgba(0,0,0,0)",
                "gridwidth": 1,
                "showbackground": show_grid,
                "backgroundcolor": "rgba(220,220,220,0.4)" if show_grid else "rgba(0,0,0,0)",
                "showspikes": False,
                "zeroline": False,
            },
            "yaxis": {
                "title": "纬度" if show_axes else "",
                "tickformat": ".2f" if show_axes else "",
                "showticklabels": show_axes,
                "range": y_range_val,
                "showgrid": show_grid,
                "gridcolor": "rgba(100,100,100,0.5)" if show_grid else "rgba(0,0,0,0)",
                "gridwidth": 1,
                "showbackground": show_grid,
                "backgroundcolor": "rgba(220,220,220,0.4)" if show_grid else "rgba(0,0,0,0)",
                "showspikes": False,
                "zeroline": False,
            },
            "zaxis": {
                "title": "高程 (m)" if show_axes else "",
                "tickformat": "" if show_axes else "",
                "showticklabels": show_axes,
                "range": z_range_val,
                "showgrid": show_grid,
                "gridcolor": "rgba(100,100,100,0.5)" if show_grid else "rgba(0,0,0,0)",
                "gridwidth": 1,
                "showbackground": show_grid,
                "backgroundcolor": "rgba(220,220,220,0.4)" if show_grid else "rgba(0,0,0,0)",
                "showspikes": False,
                "zeroline": False,
            },
            "camera": camera,
            "aspectmode": "manual",
            "aspectratio": aspect_ratio,
            "bgcolor": "rgba(0,0,0,0)",
        },
        paper_bgcolor="rgba(0,0,0,0)",
        plot_bgcolor="rgba(0,0,0,0)",
        margin={"l": 0, "r": 0, "t": 40, "b": 0},
        hovermode="closest",
        dragmode="orbit",
    )

    fig = go.Figure(data=traces, layout=layout)
    return fig


def fig_to_dict(fig: go.Figure) -> dict:
    """
    将 Plotly figure 转换为 JSON 安全（无 numpy 类型）的字典。
    用 PlotlyJSONEncoder 处理 numpy 类型后再还原为 dict。
    """
    return json.loads(json.dumps(fig.to_dict(), cls=PlotlyJSONEncoder))


# ==================== PNG 导出 ====================

def export_png(fig: go.Figure, width: int, height: int, dpi: int = None) -> bytes:
    """
    尝试用 kaleido 导出 PNG。如果失败，返回错误信息 bytes。

    返回 PNG 字节流。
    """
    if dpi is None:
        dpi = config.DOWNLOAD_DPI

    try:
        scale = dpi / 96.0  # Plotly 默认 96 DPI
        img_bytes = to_image(fig, format="png", width=width, height=height, scale=scale)
        return img_bytes
    except Exception as e:
        # Fallback：返回 None 让调用方处理
        raise RuntimeError(
            f"PNG 导出失败（{e}）。请确保已安装 kaleido：pip install kaleido"
        )


def fig_to_json_safe(fig: go.Figure) -> dict:
    """
    将 Plotly figure 转换为可 JSON 序列化的 dict（处理 numpy 类型）。
    """
    return json.loads(json.dumps(fig.to_dict(), cls=PlotlyJSONEncoder))