# -*- coding: utf-8 -*-
"""
FastAPI 入口：路由、会话管理、启动清理。
"""

import time
import io
import uuid
import json
import shutil
import sys
import traceback
from contextlib import asynccontextmanager
from pathlib import Path
from datetime import datetime, timezone
from typing import Optional

# 确保项目根目录在 sys.path 中，以支持直接运行
_project_root = Path(__file__).parent.parent
if str(_project_root) not in sys.path:
    sys.path.insert(0, str(_project_root))

from fastapi import FastAPI, UploadFile, File, HTTPException, Depends, Request
from fastapi.responses import FileResponse, StreamingResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
import plotly.graph_objects as go

from webapp import config
from webapp import processing
from webapp import rendering


# ==================== Lifespan（替代废弃的 on_event） ====================

@asynccontextmanager
async def lifespan(app: FastAPI):
    """启动时清理超过 24 小时的残留 session 目录。"""
    sessions_dir = config.SESSION_DIR
    if not sessions_dir.exists():
        sessions_dir.mkdir(parents=True, exist_ok=True)
    else:
        now = time.time()
        for d in sessions_dir.iterdir():
            if d.is_dir():
                mtime = d.stat().st_mtime
                if now - mtime > config.SESSION_CLEANUP_AGE:
                    shutil.rmtree(d, ignore_errors=True)
                    print(f"清理过期 session 目录: {d.name}")
    yield  # 应用运行中
    # 关闭时可在此处添加清理逻辑


app = FastAPI(
    title="地灾单点制图 Web 应用",
    description="上传 DEM/DOM/SHP 打包 zip，生成交互式 3D 地形与灾害边界叠加图",
    version="1.0.0",
    lifespan=lifespan,
)

# 挂载静态文件
STATIC_DIR = config.STATIC_DIR
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

# ==================== 会话管理 ====================
# 内存存储：{session_id: {"last_active": timestamp, "dir": Path, "zones": list, "meta": dict, "cached": bool}}
sessions: dict[str, dict] = {}


def _cleanup_old_sessions():
    """清理过期 session。"""
    now = time.time()
    expired = []
    for sid, info in sessions.items():
        if now - info["last_active"] > config.SESSION_TTL:
            expired.append(sid)
    for sid in expired:
        _remove_session(sid)


def _remove_session(session_id: str):
    """删除 session 目录和内存记录。"""
    if session_id in sessions:
        info = sessions.pop(session_id)
        sdir = info.get("dir")
        if sdir and sdir.exists():
            shutil.rmtree(sdir, ignore_errors=True)


def _get_session(session_id: str) -> dict:
    """获取 session，同时更新活动时间。"""
    if session_id not in sessions:
        raise HTTPException(status_code=404, detail="Session 不存在或已过期")
    info = sessions[session_id]
    info["last_active"] = time.time()
    return info


# ==================== Pydantic 模型 ====================

class RenderParams(BaseModel):
    title: Optional[str] = config.DEFAULT_TITLE
    title_fontsize: Optional[int] = config.DEFAULT_TITLE_FONTSIZE
    title_font: Optional[str] = config.DEFAULT_TITLE_FONT
    vert_exag: Optional[float] = config.DEFAULT_VERT_EXAG
    azdeg: Optional[float] = config.DEFAULT_AZDEG
    altdeg: Optional[float] = config.DEFAULT_ALTDEG
    view_elev: Optional[float] = config.DEFAULT_VIEW_ELEV
    view_azim: Optional[float] = config.DEFAULT_VIEW_AZIM
    view_distance: Optional[float] = config.DEFAULT_VIEW_DISTANCE
    line_color: Optional[str] = config.DEFAULT_LINE_COLOR
    line_width: Optional[float] = config.DEFAULT_LINE_WIDTH
    hillshade_mix: Optional[float] = config.DEFAULT_HILLSHADE_MIX
    texture_brightness: Optional[float] = 1.0
    max_size: Optional[int] = config.MAX_SIZE
    z_exag_visual: Optional[float] = config.DEFAULT_Z_EXAG_VISUAL
    show_labels: Optional[bool] = config.DEFAULT_SHOW_LABELS
    show_centroids: Optional[bool] = config.DEFAULT_SHOW_CENTROIDS
    label_fontsize: Optional[int] = config.DEFAULT_LABEL_FONTSIZE
    label_color: Optional[str] = config.DEFAULT_LABEL_COLOR
    centroid_size: Optional[int] = config.DEFAULT_CENTROID_SIZE
    nodata_color: Optional[str] = config.DEFAULT_NODATA_COLOR
    color_mask: Optional[str] = ""
    color_mask_strength: Optional[float] = 0.0
    show_axes: Optional[bool] = config.DEFAULT_SHOW_AXES
    show_grid: Optional[bool] = config.DEFAULT_SHOW_GRID
    display_fields: Optional[list[str]] = None


class RenderRequest(BaseModel):
    session_id: str
    params: RenderParams


class DownloadRequest(BaseModel):
    session_id: str
    params: RenderParams
    width: int = 1920
    height: int = 1440
    dpi: int = 300


# ==================== 路由 ====================

@app.get("/")
async def root():
    """返回前端单页应用。"""
    return FileResponse(str(STATIC_DIR / "index.html"))


@app.get("/favicon.ico")
async def favicon():
    """屏蔽 favicon 404。"""
    return JSONResponse(status_code=204, content=None)


@app.get("/api/health")
async def health():
    """健康检查。"""
    return {"status": "ok", "timestamp": datetime.now(timezone.utc).isoformat()}


@app.post("/api/upload")
async def upload_files(
    dem: UploadFile = File(None),
    dom: UploadFile = File(None),
    shp: UploadFile = File(...),
):
    """
    分别上传 DEM(.tif/.tiff)、DOM(.tif/.tiff)、SHP(.zip)，处理数据，创建 session。
    DEM 和 DOM 为可选参数 — 仅上传 SHP 时使用天地图底图展示 SHP 区划。

    返回:
        {
            "session_id": str,
            "meta": {...},
            "zones": [...],
            "cached": {"has_cache": bool, ...},
            "shp_only": bool,
        }
    """
    # 校验文件类型
    if not shp.filename or not shp.filename.lower().endswith(".zip"):
        raise HTTPException(status_code=400, detail="SHP 文件须为 .zip 格式（内含 .shp/.shx/.dbf）")

    has_dem = dem is not None and dem.filename
    has_dom = dom is not None and dom.filename

    if has_dem:
        if not dem.filename.lower().endswith((".tif", ".tiff")):
            raise HTTPException(status_code=400, detail="DEM 文件须为 .tif 或 .tiff 格式")
    if has_dom:
        if not dom.filename.lower().endswith((".tif", ".tiff")):
            raise HTTPException(status_code=400, detail="DOM 文件须为 .tif 或 .tiff 格式")

    # 生成 session ID
    session_id = uuid.uuid4().hex
    session_dir = config.SESSION_DIR / session_id
    session_dir.mkdir(parents=True, exist_ok=True)

    # 保存文件
    shp_bytes = await shp.read()
    shp_path = session_dir / "shp_upload.zip"
    shp_path.write_bytes(shp_bytes)

    dem_path = None
    dom_path = None
    if has_dem:
        dem_bytes = await dem.read()
        dem_path = session_dir / ("dem" + Path(dem.filename).suffix)
        dem_path.write_bytes(dem_bytes)

    if has_dom:
        dom_bytes = await dom.read()
        dom_path = session_dir / ("dom" + Path(dom.filename).suffix)
        dom_path.write_bytes(dom_bytes)

    shp_only = not has_dem or not has_dom

    try:
        if shp_only:
            result = processing.process_shp_only(shp_path, session_dir)
        else:
            result = processing.process_upload_direct(dem_path, dom_path, shp_path, session_dir)
    except ValueError as e:
        shutil.rmtree(session_dir, ignore_errors=True)
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        shutil.rmtree(session_dir, ignore_errors=True)
        raise HTTPException(status_code=500, detail=f"处理失败：{str(e)}")

    # 存入 session
    sessions[session_id] = {
        "last_active": time.time(),
        "dir": session_dir,
        "zones": result.get("zones", []),
        "meta": result.get("meta", {}),
        "cached": result.get("cached", {}),
        "fields": result.get("fields", []),
        "default_display_fields": result.get("default_display_fields", config.DEFAULT_DISPLAY_FIELDS),
        "shp_only": shp_only,
    }

    return {
        "session_id": session_id,
        "meta": result.get("meta", {}),
        "zones": result.get("zones", []),
        "fields": result.get("fields", []),
        "default_display_fields": result.get("default_display_fields", config.DEFAULT_DISPLAY_FIELDS),
        "diagnostics": result.get("diagnostics", {}),
        "cached": result.get("cached", {}),
        "shp_only": shp_only,
    }


@app.post("/api/shp-fields")
async def extract_shp_fields(shp: UploadFile = File(...)):
    """
    接受 SHP zip，提取字段元数据（轻量预处理，无需 DEM/DOM）。
    用于前端在正式上传前展示 SHP 字段勾选项。

    返回:
        {
            "fields": [...],
            "default_display_fields": [...],
            "n_zones": int,
        }
    """
    if not shp.filename or not shp.filename.lower().endswith(".zip"):
        raise HTTPException(status_code=400, detail="SHP 文件须为 .zip 格式")

    # 保存到临时路径
    import tempfile
    with tempfile.TemporaryDirectory(dir=str(config.SESSION_DIR)) as tmp_dir:
        zip_path = Path(tmp_dir) / "shp_upload.zip"
        zip_bytes = await shp.read()
        zip_path.write_bytes(zip_bytes)

        try:
            result = processing.extract_shp_fields_from_zip(zip_path)
            return result
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"字段提取失败：{str(e)}")


@app.post("/api/render")
async def render(body: RenderRequest):
    """
    传入 session_id + 参数，返回 Plotly figure JSON。

    返回:
        {"figure": {...}, "meta": {...}}
    """
    session_info = _get_session(body.session_id)
    session_dir = session_info["dir"]

    # 加载缓存数据
    try:
        cached = processing.load_cached_data(session_dir)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Session 数据已丢失，请重新上传")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"数据加载失败：{str(e)}")

    # 用新参数重新计算
    params = body.params.model_dump()
    data = processing.recompute_with_params(cached, params)

    # 获取 display_fields（从前端传入，或从 session 默认值）
    display_fields = params.get("display_fields")
    if display_fields is None:
        display_fields = session_info.get("default_display_fields", config.DEFAULT_DISPLAY_FIELDS)

    # 生成 figure
    zones = session_info.get("zones", [])
    meta = session_info.get("meta", {})
    try:
        fig = rendering.create_plotly_figure(
            data, params, zones=zones, meta=meta,
            display_fields=display_fields,
        )
        fig_dict = rendering.fig_to_dict(fig)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"渲染失败：{str(e)}")

    return {
        "figure": fig_dict,
        "meta": meta,
    }


@app.post("/api/download")
async def download_png(body: DownloadRequest):
    """
    传入 session_id + 参数 + 分辨率，返回 PNG 文件流。
    """
    session_info = _get_session(body.session_id)
    session_dir = session_info["dir"]

    # 加载缓存数据
    try:
        cached = processing.load_cached_data(session_dir)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Session 数据已丢失，请重新上传")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"数据加载失败：{str(e)}")

    params = body.params.model_dump()
    data = processing.recompute_with_params(cached, params)

    display_fields = params.get("display_fields")
    if display_fields is None:
        display_fields = session_info.get("default_display_fields", config.DEFAULT_DISPLAY_FIELDS)

    zones = session_info.get("zones", [])
    meta = session_info.get("meta", {})
    try:
        fig = rendering.create_plotly_figure(
            data, params, zones=zones, meta=meta,
            display_fields=display_fields,
        )
        img_bytes = rendering.export_png(fig, body.width, body.height, body.dpi)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"PNG 导出失败：{str(e)}")

    # 生成文件名
    title = params.get("title", config.DEFAULT_TITLE)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = f"{title}_{timestamp}.png"

    return StreamingResponse(
        io.BytesIO(img_bytes),
        media_type="image/png",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.post("/api/download_html")
async def download_html(body: DownloadRequest):
    """
    传入 session_id + 参数 + 分辨率，返回离线可交互 HTML 文件。
    """
    session_info = _get_session(body.session_id)
    session_dir = session_info["dir"]

    try:
        cached = processing.load_cached_data(session_dir)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Session 数据已丢失，请重新上传")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"数据加载失败：{str(e)}")

    params = body.params.model_dump()
    data = processing.recompute_with_params(cached, params)

    display_fields = params.get("display_fields")
    if display_fields is None:
        display_fields = session_info.get("default_display_fields", config.DEFAULT_DISPLAY_FIELDS)

    zones = session_info.get("zones", [])
    meta = session_info.get("meta", {})
    try:
        fig = rendering.create_plotly_figure(
            data, params, zones=zones, meta=meta,
            display_fields=display_fields,
        )
        fig_json = json.loads(json.dumps(fig.to_dict(), cls=rendering.PlotlyJSONEncoder))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"HTML 生成失败：{str(e)}")

    title = params.get("title", config.DEFAULT_TITLE)

    html_content = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<title>{title}</title>
<script src="https://cdn.plot.ly/plotly-2.35.2.min.js"></script>
<style>body{{margin:0;overflow:hidden}}#fig{{width:100vw;height:100vh}}</style>
</head>
<body>
<div id="fig"></div>
<script>
var fig = {json.dumps(fig_json, ensure_ascii=False)};
Plotly.newPlot('fig', fig.data, fig.layout, {{responsive: true, displaylogo: false}});
</script>
</body>
</html>"""

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = f"{title}_{timestamp}.html"

    return StreamingResponse(
        io.BytesIO(html_content.encode("utf-8")),
        media_type="text/html",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.delete("/api/session/{session_id}")
async def delete_session(session_id: str):
    """主动清理会话。"""
    _remove_session(session_id)
    return {"status": "ok", "message": "Session 已清理"}


# 每次请求前清理过期 session（中间件方式）
@app.middleware("http")
async def cleanup_middleware(request, call_next):
    _cleanup_old_sessions()
    response = await call_next(request)
    return response


# 全局异常处理，打印详细错误
@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    tb = traceback.format_exc()
    print(f"=== SERVER ERROR ===\n{tb}")
    return JSONResponse(
        status_code=500,
        content={"detail": str(exc)},
    )