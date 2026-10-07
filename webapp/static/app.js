/**
 * 地灾单点制图 Web 应用 — v2 前端逻辑 (Cesium版)
 *
 * 功能：
 *   左右分栏布局 | 上传 → 数据处理面板 → 字段勾选 → 参数调整 → 渲染 → 下载
 *   底图：Cesium（ArcGIS World Imagery + 全球地形）
 */
(function () {
  "use strict";

  // ======================== 状态 ========================
  const state = {
    sessionId: null,
    figure: null,       // Plotly figure 对象
    currentParams: {},  // 当前参数（用户调整后的值）
    defaultParams: {},  // 上传时后端返回的默认参数
    meta: {},
    zones: [],
    fields: [],         // SHP 字段元数据
    defaultDisplayFields: [],
    isUploading: false,
    shpOnly: false,
    demFile: null,
    domFile: null,
    shpFile: null,
  };

  // ======================== DOM 引用 ========================
  const $ = (id) => document.getElementById(id);
  const demInput = $("dem-input");
  const domInput = $("dom-input");
  const shpInput = $("shp-input");
  const uploadStartBtn = $("upload-start-btn");
  const statusText = $("status-text");
  const errorBanner = $("error-banner");
  const mainContent = $("main-content");
  const plotlyDiv = $("plotly-div");
  const uploadPlaceholder = $("upload-placeholder");
  const loadingOverlay = $("loading-overlay");
  const loadingText = $("loading-text");
  const diagPanel = $("diag-panel");
  const fieldsContainer = $("fields-container");
  const fieldsList = $("fields-list");
  const fieldCheckCount = $("field-check-count");
  const fieldCheckWarn = $("field-check-warn");
  const downloadArea = $("download-area");

  // ======================== 工具函数 ========================

  function showError(msg) {
    errorBanner.textContent = msg;
    errorBanner.classList.remove("hidden");
    setTimeout(() => errorBanner.classList.add("hidden"), 8000);
  }

  function setStatus(text, className) {
    statusText.textContent = text || "";
    statusText.className = "status-" + (className || "idle");
  }

  function showLoading(text) {
    loadingText.textContent = text || "处理中...";
    loadingOverlay.classList.remove("hidden");
  }

  function hideLoading() {
    loadingOverlay.classList.add("hidden");
  }

  /**
   * 启动处理计时器 — 使用 requestAnimationFrame 更新加载覆盖层 (#loading-text)，
   * 避免 setInterval 在 Plotly.react 等同步操作阻塞时停止更新。
   * 停止时自动显示最终的已用时间。
   * @param {string} label - 提示文字，如"正在渲染 3D 视图"
   * @returns {Object} { stop, updateText }
   */
  function startProcessingTimer(label) {
    var container = loadingText;
    if (!container) return null;

    var startTime = Date.now();
    var timerId = null;
    var lastSeconds = -1;
    var stopped = false;

    function formatTime(seconds) {
      if (seconds < 60) return seconds + 's';
      var m = Math.floor(seconds / 60);
      var s = seconds % 60;
      return m + '分' + s + '秒';
    }

    function tick() {
      if (stopped) return;
      var elapsed = Math.floor((Date.now() - startTime) / 1000);
      if (elapsed !== lastSeconds) {
        lastSeconds = elapsed;
        container.textContent = label + '（⏱ 已用时 ' + formatTime(elapsed) + '）';
      }
      timerId = requestAnimationFrame(tick);
    }

    tick();

    return {
      stop: function () {
        stopped = true;
        if (timerId) {
          cancelAnimationFrame(timerId);
          timerId = null;
        }
        // 停止时显示最终用时
        var finalElapsed = Math.floor((Date.now() - startTime) / 1000);
        container.textContent = label + '（完成，总用时 ' + formatTime(finalElapsed) + '）';
      },
      updateText: function (newLabel) {
        label = newLabel;
        var elapsed = Math.floor((Date.now() - startTime) / 1000);
        container.textContent = label + '（⏱ 已用时 ' + formatTime(elapsed) + '）';
      }
    };
  }

  /** 记录日志到右侧日志面板 */
  function log(msg, type) {
    type = type || "info";
    var body = $("log-body");
    if (!body) return;
    var div = document.createElement("div");
    div.className = "log-entry log-" + type;
    var now = new Date();
    var t = now.getHours().toString().padStart(2, "0") + ":" +
            now.getMinutes().toString().padStart(2, "0") + ":" +
            now.getSeconds().toString().padStart(2, "0");
    div.innerHTML = '<span class="log-time">' + t + '</span>' + msg;
    body.appendChild(div);
    body.scrollTop = body.scrollHeight;
  }

  // ======================== 参数 <-> UI 同步 ========================

  const PARAM_MAP = {
    title:                  { el: "param-title",              type: "text" },
    title_fontsize:         { el: "param-title_fontsize",     type: "range", display: "val-title_fontsize" },
    title_font:             { el: "param-title_font",         type: "text" },
    vert_exag:              { el: "param-vert_exag",          type: "range", display: "val-vert_exag" },
    z_exag_visual:          { el: "param-z_exag_visual",      type: "range", display: "val-z_exag_visual" },
    max_size:               { el: "param-max_size",           type: "range", display: "val-max_size" },
    azdeg:                  { el: "param-azdeg",              type: "range", display: "val-azdeg" },
    altdeg:                 { el: "param-altdeg",             type: "range", display: "val-altdeg" },
    hillshade_mix:          { el: "param-hillshade_mix",      type: "range", display: "val-hillshade_mix" },
    texture_brightness:     { el: "param-texture_brightness", type: "range", display: "val-texture_brightness" },
    view_elev:              { el: "param-view_elev",          type: "range", display: "val-view_elev" },
    view_azim:              { el: "param-view_azim",          type: "range", display: "val-view_azim" },
    color_mask:             { el: "param-color_mask",         type: "text" },
    color_mask_strength:    { el: "param-color_mask_strength", type: "range", display: "val-color_mask_strength" },
    view_distance:          { el: "param-view_distance",      type: "range", display: "val-view_distance" },
    line_color:             { el: "param-line_color",         type: "color" },
    line_width:             { el: "param-line_width",         type: "range", display: "val-line_width" },
    nodata_color:           { el: "param-nodata_color",       type: "color" },
    label_color:            { el: "param-label_color",        type: "color" },
    centroid_size:          { el: "param-centroid_size",      type: "range", display: "val-centroid_size" },
    show_labels:            { el: "param-show_labels",        type: "checkbox" },
    show_centroids:         { el: "param-show_centroids",     type: "checkbox" },
    label_fontsize:         { el: "param-label_fontsize",     type: "range", display: "val-label_fontsize" },
    show_axes:              { el: "param-show_axes",          type: "checkbox" },
    show_grid:              { el: "param-show_grid",          type: "checkbox" },
  };

  function getParamsFromUI() {
    const params = {};
    for (const [key, cfg] of Object.entries(PARAM_MAP)) {
      const el = $(cfg.el);
      if (!el) continue;
      if (cfg.type === "checkbox") {
        params[key] = el.checked;
      } else if (cfg.type === "range" || cfg.type === "number") {
        params[key] = parseFloat(el.value);
      } else {
        params[key] = el.value;
      }
    }
    // 字段勾选
    params.display_fields = getSelectedFields();
    return params;
  }

  function setParamsToUI(params) {
    for (const [key, cfg] of Object.entries(PARAM_MAP)) {
      if (params[key] === undefined) continue;
      const el = $(cfg.el);
      if (!el) continue;
      if (cfg.type === "checkbox") {
        el.checked = params[key];
      } else if (key === "color_mask" && !params[key]) {
        // 空遮罩时保持颜色选择器为蓝色（但 strength=0 不生效）
        el.value = "#ffffff";
      } else {
        el.value = params[key];
      }
      // 更新显示值
      if (cfg.display) {
        const disp = $(cfg.display);
        if (disp) {
          const v = parseFloat(el.value);
          let dec = 0;
          if (key === "vert_exag" || key === "line_width" || key === "hillshade_mix" ||
              key === "z_exag_visual" || key === "view_distance") dec = 1;
          disp.textContent = v.toFixed(dec);
        }
      }
    }
    // 字段勾选
    if (params.display_fields) {
      setSelectedFields(params.display_fields);
    }
  }

  function getDefaultParams() {
    return {
      title: "庆元新发现点 — DOM + DEM 三维地形与灾害边界叠加",
      title_fontsize: 16,
      title_font: "Microsoft YaHei, Times New Roman, sans-serif",
      vert_exag: 0.8,
      z_exag_visual: 1.2,
      max_size: 600,
      azdeg: 315,
      altdeg: 45,
      hillshade_mix: 0.45,
      texture_brightness: 1.0,
      color_mask: "",
      color_mask_strength: 0.0,
      view_elev: 58,
      view_azim: -55,
      view_distance: 2.5,
      line_color: "#FFD700",
      line_width: 2.2,
      nodata_color: "#BFBFBF",
      show_labels: true,
      show_centroids: true,
      label_fontsize: 11,
      label_color: "#E8E8E8",
      centroid_size: 7,
      show_axes: true,
      show_grid: false,
      display_fields: [],
    };
  }

  // ======================== 实时渲染（防抖） ========================
  let _renderTimer = null;
  let _isRendering = false;
  let _renderPending = false;

  /** 静默渲染（不显示加载遮罩），防抖 250ms */
  async function doRenderSilent() {
    if (!state.sessionId || state.shpOnly) return;
    _isRendering = true;
    const params = getParamsFromUI();
    state.currentParams = Object.assign({}, params);
    try {
      const resp = await fetch("/api/render", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ session_id: state.sessionId, params }),
      });
      if (!resp.ok) {
        const err = await resp.json();
        throw new Error(err.detail || "渲染失败");
      }
      const data = await resp.json();
      const figData = data.figure;
      if (figData && figData.data && figData.layout) {
        await Plotly.react(plotlyDiv, figData.data, figData.layout, {
          responsive: true,
          displayModeBar: true,
          modeBarButtonsToRemove: ["sendDataToCloud", "lasso2d", "select2d"],
          displaylogo: false,
        });
        state.figure = { data: figData.data, layout: figData.layout };
        setupPlotlyClickHandler();
        log('✅ 实时渲染完成', 'success');
      }
    } catch (err) {
      showError(err.message);
    } finally {
      _isRendering = false;
      // 如果渲染期间又有新请求入队，接着渲染
      if (_renderPending) {
        _renderPending = false;
        scheduleRender();
      }
    }
  }

  function scheduleRender() {
    if (!state.sessionId || state.shpOnly) return;
    if (_isRendering) {
      _renderPending = true;
      return;
    }
    if (_renderTimer) clearTimeout(_renderTimer);
    _renderTimer = setTimeout(doRenderSilent, 250);
  }

  // ======================== 控件事件绑定（数值更新 + 实时渲染）====================
  (function initControls() {
    for (const [key, cfg] of Object.entries(PARAM_MAP)) {
      const el = $(cfg.el);
      if (!el) continue;

      // 所有滑块/数字：实时更新右侧显示值
      if (cfg.type === "range") {
        const disp = cfg.display ? $(cfg.display) : null;
        if (disp) {
          el.addEventListener("input", function () {
            const v = parseFloat(el.value);
            let dec = 0;
            if (key === "vert_exag" || key === "line_width" || key === "hillshade_mix" ||
                key === "z_exag_visual" || key === "view_distance" || key === "color_mask_strength") dec = 2;
            disp.textContent = v.toFixed(dec);
          });
        }
      }

      // 所有控件的变动都触发实时渲染（防抖）
      if (cfg.type === "range" || cfg.type === "text" || cfg.type === "color") {
        el.addEventListener("input", scheduleRender);
      } else if (cfg.type === "checkbox") {
        el.addEventListener("change", scheduleRender);
      }
    }
  })();

  function showView() {
    uploadPlaceholder.style.display = "none";
  }

  // ======================== Cesium 地图（Cesium.js）=======================

  let cesiumViewer = null;
  let cesiumEntities = [];      // Cesium 上的 SHP 覆盖物（多边形+标注）

  /** 初始化 Cesium Viewer，默认中心浙江省 */
  function initCesiumMap() {
    if (cesiumViewer) return;

    const mapEl = $("cesium-map");
    mapEl.classList.remove("hidden");

    // 确保容器有尺寸
    if (mapEl.offsetWidth === 0 || mapEl.offsetHeight === 0) {
      setTimeout(initCesiumMap, 200);
      return;
    }

    var viewer = new Cesium.Viewer("cesium-map", {
      baseLayer: false,
      animation: false,
      timeline: false,
      fullscreenButton: false,
      infoBox: false,
      geocoder: false,
      homeButton: false,
      sceneModePicker: false,
      baseLayerPicker: false,
      navigationHelpButton: false,
      selectionIndicator: true,
      shouldAnimate: true,
    });

    // 隐藏左下角 Cesium logo 与 credits（由 CSS 补充隐藏）
    viewer._cesiumWidget._creditContainer.style.display = "none";

    // 帧率显示（开发调试用）
    viewer.scene.debugShowFramesPerSecond = true;
    viewer.scene.globe.enableLighting = false;
    viewer.scene.fog.enabled = true;
    viewer.scene.fog.density = 0.00008;
    viewer.scene.skyAtmosphere.show = true;

    // 添加 ArcGIS World Imagery 卫星底图（同参考文件）
    viewer.imageryLayers.addImageryProvider(
      new Cesium.UrlTemplateImageryProvider({
        url: "https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}",
        maximumLevel: 18,
      })
    );

    // 加载全球地形
    Cesium.ArcGISTiledElevationTerrainProvider.fromUrl(
      "https://elevation3d.arcgis.com/arcgis/rest/services/WorldElevation3D/Terrain3D/ImageServer"
    )
      .then(function (provider) {
        viewer.terrainProvider = provider;
        log('🌍 Cesium 全球地形加载完成', 'success');
      })
      .catch(function (error) {
        console.warn("Cesium 地形加载失败", error);
        log('⚠️ Cesium 地形加载失败，使用椭球面', 'warn');
      });

    // 默认视角：浙江省范围
    viewer.camera.setView({
      destination: Cesium.Cartesian3.fromDegrees(120.2, 30.3, 300000),
      orientation: {
        heading: Cesium.Math.toRadians(0),
        pitch: Cesium.Math.toRadians(-35),
        roll: 0,
      },
    });

    cesiumViewer = viewer;

    // ---- 注册质心点击弹窗事件 ----
    setupCentroidClickHandler();

    // 隐藏 placeholder
    uploadPlaceholder.style.display = "none";

    log('🌍 Cesium 已初始化 — 3D 全球卫星影像 + 地形，浙江省范围', 'success');
  }

  /** 切换显示：Cesium 地图视图 / Plotly 3D */
  function switchToCesiumMapView() {
    const mapEl = $("cesium-map");
    if (!cesiumViewer) {
      initCesiumMap();
    } else {
      mapEl.classList.remove("hidden");
      // 触发 resize 以修正尺寸
      cesiumViewer.resize();
    }
    plotlyDiv.style.display = "none";
    uploadPlaceholder.style.display = "none";
    log('🗺 切换到 Cesium 地图视图', 'step');
  }

  function switchTo3DView() {
    $("cesium-map").classList.add("hidden");
    plotlyDiv.style.display = "block";
    uploadPlaceholder.style.display = "none";
    // 确保 Plotly 图表填满容器
    setTimeout(function () {
      Plotly.Plots.resize(plotlyDiv);
    }, 100);
    log('🏔 切换到 3D 地形视图', 'step');
  }

/** 构建区划信息弹窗 HTML（供 Cesium InfoBox 使用） */
  function buildZoneDescription(zone) {
    var html = '<div style="font-family:Microsoft YaHei,sans-serif;font-size:14px;line-height:1.8;padding:8px;">';
    html += '<table style="width:100%;border-collapse:collapse;">';
    if (zone.properties) {
      var keys = Object.keys(zone.properties);
      for (var ki = 0; ki < keys.length; ki++) {
        var k = keys[ki];
        var v = zone.properties[k];
        if (v === '' || v == null) v = '—';
        html += '<tr>';
        html += '<td style="padding:3px 8px;font-weight:600;border-bottom:1px solid #e0e0e0;white-space:nowrap;color:#555;">' + escapeHtml(k) + '</td>';
        html += '<td style="padding:3px 8px;border-bottom:1px solid #e0e0e0;">' + escapeHtml(v) + '</td>';
        html += '</tr>';
      }
    } else {
      html += '<tr><td style="padding:8px;color:#999;">无属性信息</td></tr>';
    }
    html += '</table></div>';
    return html;
  }

  /** HTML 转义 */
  function escapeHtml(str) {
    if (!str) return '';
    return String(str)
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;');
  }


  /** 在 Cesium 上绘制 SHP 区划（多边形边界 + 质心标注） */
  function drawShpOnCesium(zones) {
    if (!cesiumViewer || !zones || zones.length === 0) return;

    // 清除旧覆盖物
    cesiumEntities.forEach(function (e) {
      cesiumViewer.entities.remove(e);
    });
    cesiumEntities = [];

    // 追踪经纬度范围，用于自动定位
    var minLng = 180, maxLng = -180, minLat = 90, maxLat = -90;
    var hasCoords = false;

    zones.forEach(function (zone) {
      // 每个区划可能有多个边界环
      zone.boundaries.forEach(function (boundary) {
        if (boundary.x.length < 3) return;

        // 构建 Cesium Polygon 的经纬度数组
        var positions = [];
        for (var i = 0; i < boundary.x.length; i++) {
          var lng = boundary.x[i], lat = boundary.y[i];
          positions.push(lng, lat);
          if (lng < minLng) minLng = lng;
          if (lng > maxLng) maxLng = lng;
          if (lat < minLat) minLat = lat;
          if (lat > maxLat) maxLat = lat;
          hasCoords = true;
        }

        var polygon = cesiumViewer.entities.add({
          polygon: {
            hierarchy: Cesium.Cartesian3.fromDegreesArray(positions),
            material: Cesium.Color.GOLD.withAlpha(0.15),
            outline: true,
            outlineColor: Cesium.Color.GOLD,
            outlineWidth: 2,
          }
        });
        cesiumEntities.push(polygon);
      });

      // 质心标注（圆点 + 文字标签）
      var cx = zone.centroid.x;
      var cy = zone.centroid.y;
      if (cx != null && cy != null) {
        if (cx < minLng) minLng = cx;
        if (cx > maxLng) maxLng = cx;
        if (cy < minLat) minLat = cy;
        if (cy > maxLat) maxLat = cy;
        hasCoords = true;

        // 构建标注文字（只展示勾选的 SHP 字段）
        var displayFields = getSelectedFields();
        var vals = [];
        for (var fi = 0; fi < displayFields.length; fi++) {
          var fv = (zone.properties && zone.properties[displayFields[fi]]);
          if (fv && fv !== '') vals.push(fv);
        }
        var labelText = String(zone.seq);
        if (vals.length > 0) {
          if (vals.length <= 3) {
            labelText += '. ' + vals.join(' / ');
          } else {
            labelText += '.\n' + vals.join('\n');
          }
        }

        var entity = cesiumViewer.entities.add({
          position: Cesium.Cartesian3.fromDegrees(cx, cy),
          point: {
            pixelSize: 10,
            color: Cesium.Color.GOLD,
            outlineColor: Cesium.Color.WHITE,
            outlineWidth: 1,
            disableDepthTestDistance: Number.POSITIVE_INFINITY,
          },
          label: {
            text: labelText,
            font: '14px Microsoft YaHei',
            fillColor: Cesium.Color.WHITE,
            outlineColor: Cesium.Color.BLACK,
            outlineWidth: 2,
            style: Cesium.LabelStyle.FILL_AND_OUTLINE,
            pixelOffset: new Cesium.Cartesian2(0, -20),
            disableDepthTestDistance: Number.POSITIVE_INFINITY,
          },
        });
        // 存储 zone 引用用于点击弹窗（必须在 add 之后设置，Cesium Entity 构造器忽略未知属性）
        entity._zoneRef = zone;
        cesiumEntities.push(entity);
      }
    });

    log('📐 SHP 区划已绘制 — 共 ' + zones.length + ' 个区划, ' + cesiumEntities.length + ' 个覆盖物', 'success');

    // 自动定位到 SHP 区域（使用 Rectangle 让 Cesium 自动计算最佳视角）
    if (hasCoords && cesiumEntities.length > 0) {
      cesiumViewer.camera.flyTo({
        destination: Cesium.Rectangle.fromDegrees(minLng, minLat, maxLng, maxLat),
        orientation: {
          heading: Cesium.Math.toRadians(0),
          pitch: Cesium.Math.toRadians(-35),
          roll: 0,
        },
        duration: 1.5,
      });

      var centerLng = (minLng + maxLng) / 2;
      var centerLat = (minLat + maxLat) / 2;
      log('📍 已定位至 SHP 区域 [' + centerLng.toFixed(4) + ', ' + centerLat.toFixed(4) + ']', 'step');
    }
  }

  /** 注册 Cesium 质心点击弹窗事件（仅在 initCesiumMap 时调用一次） */
  function setupCentroidClickHandler() {
    if (!cesiumViewer) return;
    var handler = new Cesium.ScreenSpaceEventHandler(cesiumViewer.scene.canvas);
    handler.setInputAction(function (click) {
      var picked = cesiumViewer.scene.pick(click.position);
      if (Cesium.defined(picked) && Cesium.defined(picked.id) && picked.id._zoneRef) {
        showInfoPopup(picked.id._zoneRef);
      }
    }, Cesium.ScreenSpaceEventType.LEFT_CLICK);
  }

  /** 显示属性信息弹窗 */
  function showInfoPopup(zone) {
    var popup = $("info-popup");
    var body = $("info-popup-body");
    var title = $("info-popup-title");
    if (!popup || !body) return;

    // 设置标题
    var seq = zone.seq || '—';
    title.textContent = '📋 区划 ' + seq + ' — 属性信息';

    // 构建内容
    var html = '<div class="popup-grid">';
    if (zone.properties) {
      var keys = Object.keys(zone.properties);
      if (keys.length === 0) {
        html += '<div class="grid-val empty" style="grid-column:1/-1;text-align:center;">无属性信息</div>';
      } else {
        for (var ki = 0; ki < keys.length; ki++) {
          var k = keys[ki];
          var v = zone.properties[k];
          if (v === '' || v == null) v = '—';
          html += '<div class="grid-key">' + escapeHtml(k) + '</div>';
          html += '<div class="grid-val">' + escapeHtml(v) + '</div>';
        }
      }
    } else {
      html += '<div class="grid-val empty" style="grid-column:1/-1;text-align:center;">无属性信息</div>';
    }
    html += '</div>';
    html += '<div class="popup-tip">🖱 滚动查看全部字段 · 点击 ✕ 或按 <kbd>Esc</kbd> 关闭</div>';

    body.innerHTML = html;
    // 重置滚动到顶部
    body.scrollTop = 0;
    popup.classList.remove("hidden");
  }

  /** 隐藏属性信息弹窗 */
  function hideInfoPopup() {
    var popup = $("info-popup");
    if (popup) popup.classList.add("hidden");
  }

  /** 注册 Plotly 质心点击弹窗事件 */
  function setupPlotlyClickHandler() {
    // 清除旧事件避免重复
    if (plotlyDiv.removeAllListeners) {
      try { plotlyDiv.removeAllListeners('plotly_click'); } catch(e) {}
    }
    plotlyDiv.on('plotly_click', function (data) {
      if (!data || !data.points || data.points.length === 0) return;
      var pt = data.points[0];
      // 只响应 marker/centroid 图层的点击（排除表面/线图层）
      var mode = pt.data && pt.data.mode;
      if (!mode || mode.indexOf('markers') === -1) return;
      var zoneIdx = pt.pointNumber;
      if (zoneIdx !== undefined && state.zones && state.zones[zoneIdx]) {
        showInfoPopup(state.zones[zoneIdx]);
      }
    });
  }

  // ======================== SHP 字段提取（预处理） ========================

  let _fetchSeq = 0;

  /** 选中 SHP 后立即向服务器请求字段元数据，渲染勾选项 */
  async function fetchShpFields() {
    if (!state.shpFile) {
      // 清空字段区域
      fieldsContainer.classList.add("hidden");
      fieldsList.innerHTML = "";
      fieldCheckCount.classList.add("hidden");
      fieldCheckWarn.classList.add("hidden");
      state.fields = [];
      state.defaultDisplayFields = [];
      state.zones = [];
      return;
    }

    var seq = ++_fetchSeq;

    // 显示等待状态
    fieldsList.innerHTML = '<div class="fields-empty" style="padding:4px 0;font-size:12px;color:#999;">⏳ 正在解析 SHP 字段...</div>';
    fieldsContainer.classList.remove("hidden");
    fieldCheckCount.classList.add("hidden");
    fieldCheckWarn.classList.add("hidden");

    try {
      const formData = new FormData();
      formData.append("shp", state.shpFile);

      const resp = await fetch("/api/shp-fields", { method: "POST", body: formData });
      // 如果在此期间用户又换了 SHP，丢弃此响应
      if (seq !== _fetchSeq) return;

      if (!resp.ok) {
        const err = await resp.json();
        throw new Error(err.detail || "字段解析失败");
      }

      const data = await resp.json();
      // 再次检查是否已被后续请求取代
      if (seq !== _fetchSeq) return;

      state.fields = data.fields || [];
      // 默认使用第一个字段；如果字段列表为空则返回空数组
      var firstField = (state.fields.length > 0) ? [state.fields[0].name] : [];
      state.defaultDisplayFields = (data.default_display_fields && data.default_display_fields.length > 0)
        ? data.default_display_fields : firstField;

      renderFields(state.fields, state.defaultDisplayFields);
      log('📋 SHP 字段已加载 — 共 ' + state.fields.length + ' 个字段', 'info');
    } catch (err) {
      if (seq !== _fetchSeq) return;
      console.warn("SHP 字段提取失败:", err);
      fieldsList.innerHTML = '<div class="fields-empty" style="padding:4px 0;font-size:12px;color:#e74c3c;">⚠ 字段解析失败: ' + err.message + '</div>';
      state.fields = [];
      state.defaultDisplayFields = ["name"];
      // 失败后保留空白字段区域，不阻止后续操作
    }
  }

  /** 清空当前会话状态（当 SHP 文件变更时） */
  function clearSession() {
    // 通知服务器清理（fire-and-forget）
    if (state.sessionId) {
      navigator.sendBeacon("/api/session/" + state.sessionId);
    }
    state.sessionId = null;
    state.figure = null;
    state.currentParams = {};
    state.defaultParams = {};
    state.meta = {};
    state.zones = [];
    state.shpOnly = false;

    // 重置 UI
    downloadArea.classList.add("hidden");
    diagPanel.innerHTML = '<div class="diag-empty">请先上传数据</div>';
    $("apply-btn").disabled = false;
    $("reset-btn").disabled = false;

    // 清除 Cesium 覆盖物
    cesiumEntities.forEach(function (e) {
      if (cesiumViewer) cesiumViewer.entities.remove(e);
    });
    cesiumEntities = [];
  }

  // ======================== 文件选择 ========================

  function updateFileStatus() {
    const setFile = (id, file) => {
      const span = $(id);
      if (file) {
        span.textContent = "✓ " + file.name;
        span.className = "file-name loaded";
      } else {
        span.textContent = "未选择";
        span.className = "file-name";
      }
    };
    setFile("dem-filename", state.demFile);
    setFile("dom-filename", state.domFile);
    setFile("shp-filename", state.shpFile);

    const hasShp = !!state.shpFile;
    uploadStartBtn.style.display = hasShp ? "block" : "none";
    if (hasShp) {
      const extra = state.demFile && state.domFile ? "三个文件" : "SHP 文件（仅地图展示）";
      setStatus("已选择 " + extra + "，点击「开始处理」", "idle");
    } else {
      setStatus("请选择 SHP 文件（可选 DEM 和 DOM）", "idle");
    }
  }

  demInput.addEventListener("change", function (e) {
    if (e.target.files && e.target.files.length > 0) {
      state.demFile = e.target.files[0];
    }
    updateFileStatus();
  });
  domInput.addEventListener("change", function (e) {
    if (e.target.files && e.target.files.length > 0) {
      state.domFile = e.target.files[0];
    }
    updateFileStatus();
  });
  shpInput.addEventListener("change", function (e) {
    if (e.target.files && e.target.files.length > 0) {
      // 如果已有 session 且 SHP 文件变了，清空旧状态
      if (state.sessionId) {
        clearSession();
      }
      state.shpFile = e.target.files[0];
      // 选中 SHP 后立即提取字段元数据
      fetchShpFields();
    } else {
      state.shpFile = null;
      fetchShpFields(); // 清空字段区域
    }
    updateFileStatus();
  });

  // ======================== SHP 字段勾选 ========================

  function getSelectedFields() {
    const checked = [];
    document.querySelectorAll("#fields-list input[type='checkbox']").forEach(function (cb) {
      if (cb.checked) checked.push(cb.value);
    });
    return checked;
  }

  function setSelectedFields(fieldNames) {
    document.querySelectorAll("#fields-list input[type='checkbox']").forEach(function (cb) {
      cb.checked = fieldNames.indexOf(cb.value) !== -1;
    });
    updateFieldCount();
  }

  function updateFieldCount() {
    const checked = document.querySelectorAll("#fields-list input[type='checkbox']:checked");
    const total = checked.length;
    fieldCheckCount.textContent = "已选: " + total + " 个字段";
    fieldCheckCount.classList.remove("hidden");

    if (total > 5) {
      fieldCheckWarn.textContent = "⚠ 标注字段过多，建议不超过 3 个";
      fieldCheckWarn.classList.remove("hidden");
    } else if (total > 3) {
      fieldCheckWarn.textContent = "💡 标注字段超过 3 个，将换行显示";
      fieldCheckWarn.classList.remove("hidden");
    } else {
      fieldCheckWarn.classList.add("hidden");
    }
  }

  function renderFields(fields, defaultDisplayFields) {
    fieldsList.innerHTML = "";
    if (!fields || fields.length === 0) {
      fieldsList.innerHTML = '<div class="fields-empty">该 SHP 无可展示的属性字段，将只显示序号</div>';
      fieldsContainer.classList.remove("hidden");
      fieldCheckCount.classList.add("hidden");
      fieldCheckWarn.classList.add("hidden");
      return;
    }

    fields.forEach(function (f) {
      const div = document.createElement("div");
      div.className = "field-check-item";

      const cb = document.createElement("input");
      cb.type = "checkbox";
      cb.value = f.name;
      cb.checked = defaultDisplayFields.indexOf(f.name) !== -1;

      const nameSpan = document.createElement("span");
      nameSpan.className = "field-check-name";
      nameSpan.textContent = f.name;

      const samplesSpan = document.createElement("span");
      samplesSpan.className = "field-check-samples";
      samplesSpan.textContent = "示例: " + (f.samples && f.samples.length > 0 ? f.samples.slice(0, 3).join(", ") : "—");

      div.appendChild(cb);
      div.appendChild(nameSpan);
      div.appendChild(samplesSpan);
      fieldsList.appendChild(div);

      // 勾选变化时更新计数（不触发 Plotly 渲染，避免布局干扰）
      cb.addEventListener("change", function () {
        updateFieldCount();
      });
    });

    fieldsContainer.classList.remove("hidden");
    updateFieldCount();
  }

  // ======================== 数据处理面板 ========================

  function renderDiagnostics(diag) {
    if (!diag) {
      diagPanel.innerHTML = '<div class="diag-empty">诊断信息不可用</div>';
      return;
    }
    const d = diag.dem;
    const dom = diag.dom;
    const s = diag.shp || {};
    const isShpOnly = !d && !dom;

    let html = '<div class="diag-title">✓ 文件校验通过</div>';

    if (isShpOnly) {
      html += '<div class="diag-block"><div class="diag-block-label">SHP-only（无 DEM/DOM，Cesium 3D 底图展示）</div></div>';
    } else {
      html += '<div class="diag-block">' +
        '<div class="diag-block-label">DEM</div>' +
        '<div class="diag-item">尺寸: ' + (d.shape ? d.shape.join(" × ") : "—") + '</div>' +
        '<div class="diag-item">原始 CRS: ' + (d.src_crs || "—") + '</div>' +
        '<div class="diag-item">高程范围: ' + (d.zmin != null ? d.zmin.toFixed(2) : "—") + ' – ' + (d.zmax != null ? d.zmax.toFixed(2) : "—") + ' m</div>' +
      '</div>' +
      '<div class="diag-block">' +
        '<div class="diag-block-label">DOM</div>' +
        '<div class="diag-item">尺寸: ' + (dom.shape ? dom.shape.join(" × ") : "—") + '</div>' +
        '<div class="diag-item">波段: ' + (dom.n_bands || "—") + ' (RGB)</div>' +
        '<div class="diag-item">原始 CRS: ' + (dom.src_crs || "—") + '</div>' +
      '</div>';
    }

    html +=
      '<div class="diag-block">' +
        '<div class="diag-block-label">SHP</div>' +
        '<div class="diag-item">区数量: ' + (s.n_zones != null ? s.n_zones : "—") + '</div>' +
        '<div class="diag-item">原始 CRS: ' + (s.src_crs || "—") + '</div>' +
        '<div class="diag-item">字段数: ' + (s.n_fields != null ? s.n_fields : "—") + '</div>' +
      '</div>' +
      '<div class="diag-block">' +
        '<div class="diag-item">统一坐标系: ' + (diag.target_crs || "EPSG:4490") + '</div>' +
        '<div class="diag-item">处理耗时: ' + (diag.processing_time_sec != null ? diag.processing_time_sec + " 秒" : "—") + '</div>' +
      '</div>';

    diagPanel.innerHTML = html;
  }

  // ======================== 上传 ========================

  async function startUpload() {
    if (state.isUploading) return;
    if (!state.shpFile) {
      showError("请先选择 SHP 文件");
      return;
    }

    state.isUploading = true;

    const formData = new FormData();
    formData.append("shp", state.shpFile);
    if (state.demFile) formData.append("dem", state.demFile);
    if (state.domFile) formData.append("dom", state.domFile);

    var fileInfo = '📦 文件列表: ';
    if (state.demFile) fileInfo += 'DEM=' + state.demFile.name + ' (' + (state.demFile.size / 1024 / 1024).toFixed(1) + 'MB) ';
    if (state.domFile) fileInfo += 'DOM=' + state.domFile.name + ' (' + (state.domFile.size / 1024 / 1024).toFixed(1) + 'MB) ';
    fileInfo += 'SHP=' + state.shpFile.name + ' (' + (state.shpFile.size / 1024 / 1024).toFixed(1) + 'MB)';
    log(fileInfo, 'info');

    setStatus("上传中...", "loading");
    showLoading("正在上传并处理数据...");
    log('⬆ 开始上传...', 'step');

    try {
      const resp = await fetch("/api/upload", { method: "POST", body: formData });
      if (!resp.ok) {
        const err = await resp.json();
        throw new Error(err.detail || "上传失败");
      }

      const data = await resp.json();
      state.sessionId = data.session_id;
      state.meta = data.meta || {};
      state.zones = data.zones || [];
      state.fields = data.fields || [];
      state.defaultDisplayFields = data.default_display_fields || [];
      const shpOnly = data.shp_only === true;
      state.shpOnly = shpOnly;

      log('✅ 上传成功 — session=' + state.sessionId.slice(0, 8) + '...', 'success');

      // 输出处理诊断详情
      var diag = data.diagnostics;
      var timeStr = '';
      if (diag) {
        var pt = diag.processing_time_sec;
        timeStr = (pt != null ? '⏱ ' + pt + 's' : '');
        log('⏱ 处理耗时: ' + (pt != null ? pt + ' 秒' : '—'), 'info');
        if (diag.dem) log('📡 DEM: ' + (diag.dem.shape ? diag.dem.shape.join('×') : '—') + '  高程 ' + (diag.dem.zmin != null ? diag.dem.zmin : '—') + '–' + (diag.dem.zmax != null ? diag.dem.zmax : '—'), 'info');
        if (diag.dom) log('🛰 DOM: ' + (diag.dom.shape ? diag.dom.shape.join('×') : '—') + '  波段 ' + (diag.dom.n_bands || '—'), 'info');
        if (diag.shp) log('📐 SHP: ' + (diag.shp.n_zones || '—') + '个区划, ' + (diag.shp.n_fields || '—') + '个字段', 'info');
        if (shpOnly) log('ℹ SHP-only 模式: 在 Cesium 3D 全球地形上展示区划边界', 'warn');
      }

      // 保存用户当前已选的字段（不因上传后默认值覆盖）
      var userFields = getSelectedFields();

      // 保存后端返回的默认参数
      const defaults = getDefaultParams();
      if (data.default_display_fields) {
        defaults.display_fields = data.default_display_fields;
      }
      state.defaultParams = Object.assign({}, defaults);
      state.currentParams = Object.assign({}, defaults);

      // 设置 UI（保留用户已有的字段勾选）
      var preCheckedFields = (userFields.length > 0) ? userFields : defaults.display_fields;
      defaults.display_fields = preCheckedFields;
      setParamsToUI(defaults);

      // 渲染数据处理面板
      renderDiagnostics(data.diagnostics);

      // 显示下载区
      downloadArea.classList.remove("hidden");

      if (shpOnly) {
        // SHP-only: 在 Cesium 地图上展示 SHP 区划
        switchToCesiumMapView();
        log('🗺 开始绘制 SHP 区划在 Cesium 地图上...', 'step');
        drawShpOnCesium(state.zones);
        setStatus("✅ SHP 数据已加载，已展示在 Cesium 3D 底图上" + (timeStr ? '  ' + timeStr : ''), "success");
        // SHP-only 模式下不可使用 3D 渲染，禁用应用/重置按钮
        $("apply-btn").disabled = true;
        $("reset-btn").disabled = true;
      } else {
        // 全量数据: 3D 渲染
        log('🏔 开始 3D 渲染... (' + (data.zones || []).length + ' 个区划)', 'step');
        showLoading("正在渲染 3D 视图...");
        var renderTimer = startProcessingTimer('正在渲染 3D 视图');
        try {
          await doRender();
          if (renderTimer) renderTimer.updateText('✅ 已完成 3D 渲染');
          switchTo3DView();
          setStatus("✅ 数据已加载，可调整参数后点「应用」刷新" + (timeStr ? '  ' + timeStr : ''), "success");
        } catch (renderErr) {
          setStatus("❌ 渲染失败", "error");
        } finally {
          if (renderTimer) renderTimer.stop();
          hideLoading();
        }
        $("apply-btn").disabled = false;
        $("reset-btn").disabled = false;
      }
    } catch (err) {
      showError(err.message);
      log('❌ 上传/处理失败: ' + err.message, 'error');
      setStatus("❌ " + err.message, "error");
    } finally {
      state.isUploading = false;
      hideLoading();
    }
  }

  uploadStartBtn.addEventListener("click", startUpload);

  // ======================== 渲染 ========================

  async function doRender() {
    if (!state.sessionId || state.shpOnly) return;

    const params = getParamsFromUI();
    state.currentParams = Object.assign({}, params);

    log('🎨 正在渲染 3D 视图...', 'step');
    const renderStartMs = Date.now();
    try {
      const resp = await fetch("/api/render", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ session_id: state.sessionId, params: params }),
      });

      if (!resp.ok) {
        const err = await resp.json();
        throw new Error(err.detail || "渲染失败");
      }

      const data = await resp.json();
      const figData = data.figure;
      if (figData && figData.data && figData.layout) {
        // 调试日志
        console.log('[3D] plotlyDiv dims before render:', plotlyDiv.clientWidth, 'x', plotlyDiv.clientHeight);
        console.log('[3D] figure data length:', figData.data.length, 'traces');
        try {
          await Plotly.react(plotlyDiv, figData.data, figData.layout, {
            responsive: true,
            displayModeBar: true,
            modeBarButtonsToRemove: ["sendDataToCloud", "lasso2d", "select2d"],
            displaylogo: false,
          });
          console.log('[3D] Plotly.react() succeeded');
          console.log('[3D] plotlyDiv dims after render:', plotlyDiv.clientWidth, 'x', plotlyDiv.clientHeight);
        } catch (plotErr) {
          console.error('[3D] Plotly.react() FAILED:', plotErr);
          throw plotErr;  // 向上抛出，让外层 catch 处理
        }
        state.figure = { data: figData.data, layout: figData.layout };
        setupPlotlyClickHandler();
        var renderTime = ((Date.now() - renderStartMs) / 1000).toFixed(1);
        log('✅ 3D 渲染完成（用时 ' + renderTime + ' s）', 'success');
      } else {
        console.warn('[3D] figure data missing:', figData ? 'no layout/data' : 'null');
      }
    } catch (err) {
      showError(err.message);
      log('❌ 渲染失败: ' + err.message, 'error');
    }
  }

  // ======================== 应用 & 重置 ========================

  $("apply-btn").addEventListener("click", function () {
    var applyStart = Date.now();
    showLoading("正在更新渲染...");
    log('🔄 应用参数更新', 'step');
    doRender().then(function () {
      var t = ((Date.now() - applyStart) / 1000).toFixed(1);
      showLoading("正在更新渲染（用时 " + t + " s）");
    }).finally(hideLoading);
  });

  $("reset-btn").addEventListener("click", function () {
    const defaults = Object.assign({}, state.defaultParams);
    state.currentParams = defaults;
    setParamsToUI(defaults);
    showLoading("正在重置...");
    log('↺ 参数已重置为默认值', 'warn');
    doRender().finally(hideLoading);
  });

  // ======================== 快捷视角 ========================

  // 颜色遮罩清除按钮
  var clearMaskBtn = $("color-mask-clear-btn");
  if (clearMaskBtn) {
    clearMaskBtn.addEventListener("click", function () {
      var cp = $("param-color_mask");
      if (cp) cp.value = "#ffffff";
      var slider = $("param-color_mask_strength");
      if (slider) slider.value = "0";
      var disp = $("val-color_mask_strength");
      if (disp) disp.textContent = "0.0";
      scheduleRender();
    });
  }

  document.querySelectorAll(".view-quick-btn").forEach(function (btn) {
    btn.addEventListener("click", function () {
      const elev = parseFloat(this.dataset.elev);
      const azim = parseFloat(this.dataset.azim);
      $("param-view_elev").value = elev;
      $("param-view_azim").value = azim;
      // 更新显示值
      const vDisp = $("val-view_elev");
      if (vDisp) vDisp.textContent = elev;
      const aDisp = $("val-view_azim");
      if (aDisp) aDisp.textContent = azim;
      // 实时渲染
      scheduleRender();
    });
  });

  // ======================== 下载 ========================

  function getResolution() {
    const resSelect = $("resolution-select");
    if (resSelect.value === "custom") {
      return {
        width: parseInt($("custom-width").value) || 1920,
        height: parseInt($("custom-height").value) || 1440,
      };
    }
    const parts = resSelect.value.split(",");
    return { width: parseInt(parts[0]), height: parseInt(parts[1]) };
  }

  $("resolution-select").addEventListener("change", function () {
    const customDiv = $("custom-resolution");
    customDiv.classList.toggle("hidden", this.value !== "custom");
  });

  async function downloadPng() {
    if (!state.sessionId) { showError("请先上传数据"); return; }
    if (state.shpOnly) { showError("SHP-only 模式暂不支持 PNG 下载"); return; }
    const res = getResolution();
    const dpi = parseInt($("dpi-select").value) || 300;
    const params = getParamsFromUI();

    log('⬇ 开始下载 PNG (' + res.width + '×' + res.height + ' @' + dpi + 'dpi)', 'step');
    showLoading("正在生成 PNG...");
    try {
      const resp = await fetch("/api/download", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          session_id: state.sessionId,
          params: params,
          width: res.width,
          height: res.height,
          dpi: dpi,
        }),
      });
      if (!resp.ok) {
        const err = await resp.json();
        throw new Error(err.detail || "下载失败");
      }
      const blob = await resp.blob();
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = (params.title || "terrain").replace(/[<>:"/\\|?*]/g, "_") + "_" + res.width + "x" + res.height + ".png";
      document.body.appendChild(a);
      a.click();
      document.body.removeChild(a);
      URL.revokeObjectURL(url);
      setStatus("✅ PNG 下载成功", "success");
      log('✅ PNG 下载完成 (' + (blob.size / 1024).toFixed(0) + 'KB)', 'success');
    } catch (err) {
      showError(err.message);
      log('❌ PNG 下载失败: ' + err.message, 'error');
    } finally {
      hideLoading();
    }
  }

  async function downloadHtml() {
    if (!state.sessionId) { showError("请先上传数据"); return; }
    if (state.shpOnly) { showError("SHP-only 模式暂不支持 HTML 下载"); return; }
    const params = getParamsFromUI();

    log('⬇ 开始下载 HTML', 'step');
    showLoading("正在生成 HTML...");
    try {
      const resp = await fetch("/api/download_html", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          session_id: state.sessionId,
          params: params,
          width: 1920,
          height: 1440,
          dpi: 300,
        }),
      });
      if (!resp.ok) {
        const err = await resp.json();
        throw new Error(err.detail || "下载失败");
      }
      const blob = await resp.blob();
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = (params.title || "terrain").replace(/[<>:"/\\|?*]/g, "_") + ".html";
      document.body.appendChild(a);
      a.click();
      document.body.removeChild(a);
      URL.revokeObjectURL(url);
      setStatus("✅ HTML 下载成功", "success");
      log('✅ HTML 下载完成 (' + (blob.size / 1024).toFixed(0) + 'KB)', 'success');
    } catch (err) {
      showError(err.message);
      log('❌ HTML 下载失败: ' + err.message, 'error');
    } finally {
      hideLoading();
    }
  }

  $("download-png-btn").addEventListener("click", downloadPng);
  $("download-html-btn").addEventListener("click", downloadHtml);

  // ======================== 帮助弹窗 ========================

  $("help-btn").addEventListener("click", function () {
    $("help-overlay").classList.remove("hidden");
  });
  $("help-close-btn").addEventListener("click", function () {
    $("help-overlay").classList.add("hidden");
  });
  $("help-overlay").addEventListener("click", function (e) {
    if (e.target === this) this.classList.add("hidden");
  });

  // ======================== 属性信息弹窗 ========================

  var infoCloseBtn = $("info-popup-close");
  if (infoCloseBtn) {
    infoCloseBtn.addEventListener("click", function () {
      hideInfoPopup();
    });
  }
  $("info-popup").addEventListener("click", function (e) {
    // 点击遮罩背景（非弹窗内部）不关闭 — 用户需要手动关闭
  });

  // ======================== 会话清理 ========================

  window.addEventListener("beforeunload", function () {
    if (state.sessionId) {
      navigator.sendBeacon("/api/session/" + state.sessionId);
    }
  });

  // ======================== 键盘快捷键 ========================

  document.addEventListener("keydown", function (e) {
    if ((e.ctrlKey || e.metaKey) && e.key === "Enter") {
      e.preventDefault();
      if (!state.shpOnly) $("apply-btn").click();
    }
    if (e.key === "Escape") {
      if (!$("help-overlay").classList.contains("hidden")) {
        $("help-overlay").classList.add("hidden");
      }
      if (!$("info-popup").classList.contains("hidden")) {
        hideInfoPopup();
      }
      if (!$("loading-overlay").classList.contains("hidden")) {
        hideLoading();
      }
    }
  });

  // 初始化状态文本
  updateFileStatus();

  // 页面加载时默认显示 Cesium 地图
  initCesiumMap();

  console.log("地灾单点制图 Web 应用 v2-cesium 已启动");
})();