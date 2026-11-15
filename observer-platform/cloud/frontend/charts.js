"use strict";

(function exposeCharts(global) {
  const F = global.ObserverFormat;
  const { isFiniteNumber } = F;
  const SERIES_LABELS = Object.freeze({ cpu: "处理器", ram: "内存", temp: "芯片温度", telemetryHz: "遥测频率",
    telemetryAge: "距上次更新", voltage: "母线电压", networkRx: "接收", networkTx: "发送",
    leftWheel: "左轮", rightWheel: "右轮", actualVx: "实际线速度", actualWz: "实际角速度",
    commandVx: "指令线速度", commandWz: "指令角速度", cameraFrontFps: "前视帧率",
    cameraRearFps: "后视帧率", cameraLeftFps: "左视帧率", cameraRightFps: "右视帧率",
    scanPoints: "扫描点数", cloudPoints: "点云点数", npuUtil: "神经网络处理器利用率",
    npuFreq: "神经网络处理器频率", npuTemp: "神经网络处理器温度",
    page00: "兼容数据", page10: "运动数据", page11: "清洁机构", page12: "推杆反馈", page13: "故障状态" });
  const MAX_POINTS = 300;
  const DEFAULT_COLORS = ["#67d7e8", "#77aaff", "#f2bd6b", "#ba9eff", "#ff8798"];
  const UI_FONT = '"Microsoft YaHei", "PingFang SC", "Noto Sans CJK SC", system-ui, sans-serif';
  const DATA_FONT = 'ui-monospace, SFMono-Regular, Consolas, "Microsoft YaHei", monospace';

  class HistoryStore {
    constructor() {
      this.series = new Map();
    }

    record(name, timestamp, value, usable) {
      if (!isFiniteNumber(timestamp)) return;
      const points = this.series.get(name) || [];
      const point = { t: timestamp, v: usable && isFiniteNumber(value) ? value : null };
      if (points.length && points[points.length - 1].t === timestamp) points[points.length - 1] = point;
      else points.push(point);
      while (points.length > MAX_POINTS || (points[0] && points[0].t < timestamp - 300)) points.shift();
      this.series.set(name, points);
    }

    get(name, now, windowSeconds) {
      const points = this.series.get(name) || [];
      return points.filter(point => point.t >= now - windowSeconds);
    }

    lastFinite(name) {
      const points = this.series.get(name) || [];
      for (let index = points.length - 1; index >= 0; index -= 1) {
        if (isFiniteNumber(points[index].v)) return points[index].v;
      }
      return null;
    }

    pointCount(name) {
      return (this.series.get(name) || []).length;
    }
  }

  function setupCanvas(canvas) {
    const rectangle = canvas.getBoundingClientRect();
    if (rectangle.width < 2 || rectangle.height < 2) return null;
    const ratio = Math.min(global.devicePixelRatio || 1, 2);
    const width = Math.round(rectangle.width);
    const height = Math.round(rectangle.height);
    const pixelWidth = Math.round(width * ratio);
    const pixelHeight = Math.round(height * ratio);
    // Resizing a canvas clears all state and reallocates its pixel buffer.
    if (canvas.width !== pixelWidth) canvas.width = pixelWidth;
    if (canvas.height !== pixelHeight) canvas.height = pixelHeight;
    const context = canvas.getContext("2d");
    context.setTransform(ratio, 0, 0, ratio, 0, 0);
    context.clearRect(0, 0, width, height);
    return { context, width, height };
  }

  function seriesConfig(canvas) {
    const names = (canvas.dataset.chartSeries || canvas.dataset.sparkSeries || "").split(",").filter(Boolean);
    const labels = (canvas.dataset.chartLabels || "").split(",");
    const colors = (canvas.dataset.chartColors || "").split(",");
    return names.map((name, index) => ({
      name,
      label: labels[index] || SERIES_LABELS[name] || `其他指标（${name}）`,
      color: colors[index] || DEFAULT_COLORS[index % DEFAULT_COLORS.length]
    }));
  }

  function finiteValues(configs, history, now, windowSeconds) {
    return configs.flatMap(config => history.get(config.name, now, windowSeconds))
      .map(point => point.v)
      .filter(isFiniteNumber);
  }

  function calculateRange(values, canvas) {
    let minimum = Math.min(...values);
    let maximum = Math.max(...values);
    if (minimum === maximum) {
      const spread = Math.max(1, Math.abs(minimum) * 0.08);
      minimum -= spread;
      maximum += spread;
    } else {
      // A nearly constant stream must not turn timing jitter into full-height spikes.
      const padding = Math.max((maximum - minimum) * 0.12,
        Math.max(Math.abs(minimum), Math.abs(maximum)) * 0.025, 0.1);
      minimum -= padding;
      maximum += padding;
    }
    if (canvas) {
      const unit = canvas.dataset.chartUnit || "";
      const explicitMinimum = canvas.dataset.chartMin;
      const explicitMaximum = canvas.dataset.chartMax;
      if (unit === "%") {
        minimum = 0;
        maximum = 100;
      } else if (["Hz", "MHz", "s", "Kbps", "kbps", "Mbps", "KB/s", "KiB/s", "FPS", "点"].includes(unit)) {
        minimum = 0;
        maximum = Math.max(1, maximum);
      }
      if (explicitMinimum !== undefined && explicitMinimum !== "" && Number.isFinite(Number(explicitMinimum))) {
        minimum = Number(explicitMinimum);
      }
      if (explicitMaximum !== undefined && explicitMaximum !== "" && Number.isFinite(Number(explicitMaximum))) {
        maximum = Number(explicitMaximum);
      }
      if (maximum <= minimum) maximum = minimum + 1;
    }
    return { minimum, maximum };
  }

  function tickLabel(value, range) {
    const span = range.maximum - range.minimum;
    const digits = span < 1 ? 2 : span < 10 ? 1 : 0;
    return value.toFixed(digits);
  }

  function valueLabel(value) {
    if (!isFiniteNumber(value)) return "—";
    const digits = Math.abs(value) < 10 ? 2 : Math.abs(value) < 100 ? 1 : 0;
    return value.toFixed(digits).replace(/(\.\d*?[1-9])0+$|\.0+$/, "$1");
  }

  function latestPoint(points) {
    // A disconnected page can retain genuine history without implying that its
    // final sample is still a current observation. Missing flag preserves API use.
    if (global.document?.body?.dataset.observationFresh === "false") return null;
    const point = points[points.length - 1];
    return point && isFiniteNumber(point.v) ? point : null;
  }

  function drawEmpty(context, width, height, compact) {
    context.save();
    context.textAlign = compact ? "right" : "center";
    context.fillStyle = "#a9bacb";
    if (compact) {
      context.font = `12px ${UI_FONT}`;
      context.fillText("等待数据", width - 4, height / 2 + 4);
      context.restore();
      return;
    }

    // An empty plot is intentionally quiet: no illustrative curve or invented samples.
    const spacious = width >= 230 && height >= 150;
    const centerY = height / 2;
    if (spacious) {
      context.fillStyle = "rgba(103, 215, 232, 0.06)";
      context.beginPath();
      context.arc(width / 2, centerY - 33, 21, 0, Math.PI * 2);
      context.fill();
      context.strokeStyle = "#8199ae";
      context.lineWidth = 1.5;
      context.strokeRect(width / 2 - 8, centerY - 41, 16, 16);
      context.beginPath();
      context.moveTo(width / 2 - 4, centerY - 31);
      context.lineTo(width / 2 + 4, centerY - 31);
      context.stroke();
    }
    context.fillStyle = "#b8c8d7";
    context.font = `500 14px ${UI_FONT}`;
    context.fillText("等待数据", width / 2, centerY + (spacious ? 7 : 4));
    context.restore();
  }

  function plotSeries(context, points, color, dimensions, range, now, windowSeconds, lineWidth, area = false, markLatest = false) {
    const { left, right, top, bottom } = dimensions;
    const innerWidth = right - left;
    const innerHeight = bottom - top;
    const segments = [];
    let segment = [];
    for (const point of points) {
      if (!isFiniteNumber(point.v)) {
        if (segment.length) segments.push(segment);
        segment = [];
        continue;
      }
      segment.push({
        x: left + ((point.t - (now - windowSeconds)) / windowSeconds) * innerWidth,
        y: bottom - ((point.v - range.minimum) / (range.maximum - range.minimum)) * innerHeight
      });
    }
    if (segment.length) segments.push(segment);

    context.save();
    context.beginPath();
    context.rect(left - 4, top - 4, innerWidth + 8, innerHeight + 8);
    context.clip();

    // Fill each continuous run separately so nulls remain visible gaps, never interpolated data.
    if (area) {
      const gradient = context.createLinearGradient(0, top, 0, bottom);
      gradient.addColorStop(0, color);
      gradient.addColorStop(1, "rgba(0, 0, 0, 0)");
      context.fillStyle = gradient;
      context.globalAlpha = lineWidth > 2 ? 0.20 : 0.09;
      for (const run of segments) {
        if (run.length < 2) continue;
        context.beginPath();
        context.moveTo(run[0].x, bottom);
        for (const coordinate of run) context.lineTo(coordinate.x, coordinate.y);
        context.lineTo(run[run.length - 1].x, bottom);
        context.closePath();
        context.fill();
      }
      context.globalAlpha = 1;
    }

    context.strokeStyle = color;
    context.lineWidth = lineWidth;
    context.lineJoin = "round";
    context.lineCap = "round";
    context.beginPath();
    for (const run of segments) {
      context.moveTo(run[0].x, run[0].y);
      for (let index = 1; index < run.length; index += 1) {
        context.lineTo(run[index].x, run[index].y);
      }
    }
    // A restrained under-stroke lifts the measured curve off the chart surface;
    // it shares the exact same path, including every real gap in the samples.
    context.strokeStyle = "rgba(0,5,13,.58)";
    context.lineWidth = lineWidth + 2;
    context.shadowColor = "rgba(0,4,12,.5)";
    context.shadowBlur = 3;
    context.shadowOffsetY = 2;
    context.stroke();
    context.shadowBlur = 0;
    context.shadowOffsetY = 0;
    context.strokeStyle = color;
    context.lineWidth = lineWidth;
    context.stroke();
    // Keep isolated real samples visible, including a genuine zero.
    for (const run of segments) {
      if (run.length !== 1) continue;
      context.fillStyle = color;
      context.beginPath();
      context.arc(run[0].x, run[0].y, 2, 0, Math.PI * 2);
      context.fill();
    }

    // Do not mark an old sample as current when the latest sample is null / stale.
    if (markLatest && segment.length && latestPoint(points)) {
      const last = segment[segment.length - 1];
      context.fillStyle = color;
      context.globalAlpha = 0.16;
      context.beginPath();
      context.arc(last.x, last.y, 6, 0, Math.PI * 2);
      context.fill();
      context.globalAlpha = 1;
      context.beginPath();
      context.arc(last.x, last.y, 2.7, 0, Math.PI * 2);
      context.fill();
    }
    context.restore();
  }

  function drawChart(canvas, history, now, windowSeconds) {
    const surface = setupCanvas(canvas);
    if (!surface) return;
    const { context, width, height } = surface;
    const configs = seriesConfig(canvas);
    const values = finiteValues(configs, history, now, windowSeconds);
    const showGrid = canvas.dataset.chartGrid === "true";
    const unit = canvas.dataset.chartUnit || "";
    if (!values.length) {
      drawEmpty(context, width, height, false);
      return;
    }
    const range = calculateRange(values, canvas);
    const axisFontSize = width >= 600 ? 12 : 11;
    context.font = `${axisFontSize}px ${DATA_FONT}`;
    const tickWidth = Math.max(...[range.minimum, range.maximum].map(value =>
      context.measureText(tickLabel(value, range)).width));
    const dimensions = {
      left: showGrid ? Math.ceil(Math.max(40, tickWidth + 11)) : 8,
      right: width - 12,
      top: showGrid ? 30 : 12,
      bottom: height - 26
    };

    if (configs.length > 1) {
      const legendFontSize = width < 380 ? 11 : 12;
      context.font = `${legendFontSize}px ${UI_FONT}`;
      context.textAlign = "left";
      let x = dimensions.left;
      let y = 15;
      for (const config of configs) {
        const point = latestPoint(history.get(config.name, now, windowSeconds));
        const label = `${config.label}  ${point ? valueLabel(point.v) : "—"}`;
        const itemWidth = context.measureText(label).width + 26;
        if (x > dimensions.left && x + itemWidth > dimensions.right) {
          x = dimensions.left;
          y += 20;
        }
        context.fillStyle = config.color;
        context.beginPath();
        context.arc(x + 3, y - 4, 2.5, 0, Math.PI * 2);
        context.fill();
        context.fillStyle = "#bbccda";
        context.fillText(label, x + 12, y);
        x += itemWidth;
      }
      dimensions.top = y + (showGrid ? 25 : 13);
    }

    if (dimensions.bottom <= dimensions.top + 16) dimensions.bottom = dimensions.top + 16;
    const backdrop = context.createLinearGradient(0, dimensions.top, 0, dimensions.bottom);
    backdrop.addColorStop(0, "rgba(91, 168, 205, 0.035)");
    backdrop.addColorStop(1, "rgba(6, 15, 26, 0.08)");
    context.fillStyle = backdrop;
    context.fillRect(dimensions.left, dimensions.top, dimensions.right - dimensions.left,
      dimensions.bottom - dimensions.top);

    if (showGrid) {
      context.strokeStyle = "rgba(152, 183, 207, 0.10)";
      context.lineWidth = 1;
      context.fillStyle = "#819aaf";
      context.font = `${axisFontSize}px ${DATA_FONT}`;
      context.textAlign = "left";
      context.fillText(F.unitLabel(unit), dimensions.left, dimensions.top - 9);
      context.textAlign = "right";
      const ticks = dimensions.bottom - dimensions.top >= 165 ? 5 : 3;
      context.setLineDash([2, 5]);
      for (let index = 0; index < ticks; index += 1) {
        const ratio = index / (ticks - 1);
        const y = dimensions.top + (dimensions.bottom - dimensions.top) * ratio;
        const value = range.maximum - (range.maximum - range.minimum) * ratio;
        context.beginPath();
        context.moveTo(dimensions.left, y);
        context.lineTo(dimensions.right, y);
        context.stroke();
        context.fillText(tickLabel(value, range), dimensions.left - 8, y + 4);
      }
      // Time divisions stay behind the data and never suggest additional samples.
      context.strokeStyle = "rgba(152, 183, 207, 0.06)";
      const divisions = dimensions.right - dimensions.left > 400 ? 4 : 2;
      for (let index = 1; index < divisions; index += 1) {
        const x = dimensions.left + (dimensions.right - dimensions.left) * index / divisions;
        context.beginPath();
        context.moveTo(x, dimensions.top);
        context.lineTo(x, dimensions.bottom);
        context.stroke();
      }
      context.setLineDash([]);
    }

    for (const config of configs) {
      plotSeries(context, history.get(config.name, now, windowSeconds), config.color,
        dimensions, range, now, windowSeconds, configs.length === 1 ? 2.3 : 1.9, true, true);
    }
    if (configs.length === 1) {
      const point = latestPoint(history.get(configs[0].name, now, windowSeconds));
      if (point) {
        const label = valueLabel(point.v);
        context.font = `11px ${DATA_FONT}`;
        const labelWidth = context.measureText(label).width + 12;
        const x = dimensions.left + ((point.t - (now - windowSeconds)) / windowSeconds) *
          (dimensions.right - dimensions.left);
        const y = dimensions.bottom - ((point.v - range.minimum) / (range.maximum - range.minimum)) *
          (dimensions.bottom - dimensions.top);
        const labelX = Math.max(dimensions.left, Math.min(dimensions.right - labelWidth, x - labelWidth - 8));
        const labelY = Math.max(dimensions.top + 1, Math.min(dimensions.bottom - 19, y - 22));
        context.fillStyle = "rgba(9, 24, 38, 0.88)";
        context.fillRect(labelX, labelY, labelWidth, 18);
        context.fillStyle = configs[0].color;
        context.textAlign = "left";
        context.fillText(label, labelX + 6, labelY + 12);
      }
    }
    context.fillStyle = "#718ca3";
    context.font = `${axisFontSize}px ${UI_FONT}`;
    context.textAlign = "left";
    context.fillText(`−${windowSeconds} 秒`, dimensions.left, height - 6);
    if (dimensions.right - dimensions.left > 320) {
      context.textAlign = "center";
      context.fillText(`−${Math.round(windowSeconds / 2)} 秒`,
        (dimensions.left + dimensions.right) / 2, height - 6);
    }
    context.textAlign = "right";
    context.fillText("现在", dimensions.right, height - 6);
  }

  function drawSparkline(canvas, history, now, windowSeconds) {
    const surface = setupCanvas(canvas);
    if (!surface) return;
    const { context, width, height } = surface;
    const config = seriesConfig(canvas)[0];
    if (!config) return;
    const points = history.get(config.name, now, windowSeconds);
    const values = points.map(point => point.v).filter(isFiniteNumber);
    if (!values.length) {
      drawEmpty(context, width, height, true);
      return;
    }
    const range = calculateRange(values, canvas);
    plotSeries(context, points, config.color, { left: 2, right: width - 2, top: 3, bottom: height - 3 },
      range, now, windowSeconds, 1.8);
  }

  function drawAll(history, now, windowSeconds) {
    document.querySelectorAll(".page.active canvas[data-chart-series]")
      .forEach(canvas => drawChart(canvas, history, now, windowSeconds));
    document.querySelectorAll(".page.active canvas[data-spark-series]")
      .forEach(canvas => drawSparkline(canvas, history, now, windowSeconds));
  }

  global.ObserverCharts = Object.freeze({ HistoryStore, drawAll, drawChart, drawSparkline });
})(window);
