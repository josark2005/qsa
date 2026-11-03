import { useEffect, useRef } from "react"
import type { ECharts, EChartsOption } from "echarts"

import { cn } from "@/lib/utils"

import type { TechnicalPeriod } from "@/lib/types"

const UP_COLOR = "#dc2626"
const DOWN_COLOR = "#16a34a"
const GRID_LEFT = 58
const GRID_RIGHT = 20

function line(name: string, data: Array<number | null>, color: string, width = 1) {
  return {
    name,
    type: "line" as const,
    data,
    symbol: "none",
    smooth: false,
    lineStyle: { width, color },
    itemStyle: { color },
    emphasis: { disabled: true },
  }
}

function formatNumber(value: number | null | undefined, digits = 2): string {
  if (value === null || value === undefined) return "—"
  const numeric = Number(value)
  if (!Number.isFinite(numeric)) return "—"
  return numeric.toFixed(digits)
}

function formatPct(value: number | null): string {
  if (value === null || !Number.isFinite(value)) return "—"
  return `${value > 0 ? "+" : ""}${value.toFixed(2)}%`
}

function formatVolume(value: number | null | undefined): string {
  const numeric = Number(value ?? 0)
  if (!Number.isFinite(numeric) || numeric <= 0) return "—"
  if (numeric >= 1e8) return `${(numeric / 1e8).toFixed(2)} 亿`
  if (numeric >= 1e4) return `${(numeric / 1e4).toFixed(2)} 万`
  return numeric.toFixed(0)
}

function tooltipFormatter(data: TechnicalPeriod) {
  return (params: unknown): string => {
    const items = Array.isArray(params) ? params : [params]
    const first = items[0] as { dataIndex?: number } | undefined
    const index = first?.dataIndex
    if (typeof index !== "number" || index < 0) return ""
    const bar = data.ohlc[index]
    if (!bar) return ""
    const [open, close, low, high] = bar
    const prevClose = index > 0 ? data.ohlc[index - 1]?.[1] : null
    const pct =
      close !== null && prevClose
        ? (Number(close) / Number(prevClose) - 1) * 100
        : null
    const pctColor = Number(pct ?? 0) >= 0 ? UP_COLOR : DOWN_COLOR
    const ma = data.ma
    const expma = data.expma
    const macd = data.macd
    const kdj = data.kdj
    const rsi = data.rsi
    const muted = 'style="color:#a1a1aa"'
    return [
      '<div style="font-size:11px;line-height:1.5">',
      `<div style="font-weight:600;margin-bottom:3px">${data.dates[index] ?? ""}</div>`,
      `<div>开 ${formatNumber(open)}　高 ${formatNumber(high)}　低 ${formatNumber(low)}　收 <b>${formatNumber(close)}</b>　<span style="color:${pctColor};font-weight:600">${formatPct(pct)}</span></div>`,
      `<div ${muted}>量 ${formatVolume(data.volume[index])}</div>`,
      `<div>MA 5/10/20：${formatNumber(ma.ma5[index])} / ${formatNumber(ma.ma10[index])} / ${formatNumber(ma.ma20[index])}</div>`,
      `<div>EXPMA 20/50：${formatNumber(expma.expma20[index])} / ${formatNumber(expma.expma50[index])}</div>`,
      `<div>MACD：DIF ${formatNumber(macd.dif[index])}　DEA ${formatNumber(macd.dea[index])}　柱 ${formatNumber(macd.hist[index])}</div>`,
      `<div>KDJ：${formatNumber(kdj.k[index])} / ${formatNumber(kdj.d[index])} / ${formatNumber(kdj.j[index])}</div>`,
      `<div>RSI：${formatNumber(rsi.rsi6[index])} / ${formatNumber(rsi.rsi12[index])} / ${formatNumber(rsi.rsi24[index])}</div>`,
      "</div>",
    ].join("")
  }
}

function slicePeriod(data: TechnicalPeriod, maxBars: number): TechnicalPeriod {
  const size = data.dates.length
  if (!maxBars || maxBars <= 0 || size <= maxBars) return data
  const from = size - maxBars
  const tail = <T,>(values: T[]): T[] => values.slice(from)
  return {
    ...data,
    dates: tail(data.dates),
    ohlc: tail(data.ohlc),
    volume: tail(data.volume),
    ma: {
      ma5: tail(data.ma.ma5),
      ma10: tail(data.ma.ma10),
      ma20: tail(data.ma.ma20),
    },
    expma: {
      expma20: tail(data.expma.expma20),
      expma50: tail(data.expma.expma50),
    },
    macd: {
      dif: tail(data.macd.dif),
      dea: tail(data.macd.dea),
      hist: tail(data.macd.hist),
    },
    kdj: {
      k: tail(data.kdj.k),
      d: tail(data.kdj.d),
      j: tail(data.kdj.j),
    },
    rsi: {
      rsi6: tail(data.rsi.rsi6),
      rsi12: tail(data.rsi.rsi12),
      rsi24: tail(data.rsi.rsi24),
    },
  }
}

function computeChartLayout(height: number) {
  const top = 22
  // 底部留出旋转后的日期标签空间，避免标签被裁掉。
  const footer = 30
  const priceGap = 14
  const indicatorGap = 24
  const available = Math.max(280, height - top - footer - priceGap - indicatorGap * 3)
  const unit = available / 8
  const priceHeight = Math.round(unit * 4)
  const volumeHeight = Math.round(unit)
  const indicatorHeight = Math.round(unit)
  const volumeTop = top + priceHeight + priceGap
  const macdTop = volumeTop + volumeHeight + indicatorGap
  const kdjTop = macdTop + indicatorHeight + indicatorGap
  const rsiTop = kdjTop + indicatorHeight + indicatorGap
  return {
    top,
    priceHeight,
    volumeTop,
    volumeHeight,
    macdTop,
    kdjTop,
    rsiTop,
    indicatorHeight,
  }
}

function buildOption(
  source: TechnicalPeriod,
  period: string,
  maxBars: number | undefined,
  enableZoom: boolean,
  height: number,
): EChartsOption {
  const data = maxBars ? slicePeriod(source, maxBars) : source
  const layout = computeChartLayout(height)
  const volumeData = data.volume.map((value, index) => {
    const bar = data.ohlc[index]
    const bullish = bar ? Number(bar[1]) >= Number(bar[0]) : true
    return {
      value,
      itemStyle: { color: bullish ? UP_COLOR : DOWN_COLOR },
    }
  })
  const histData = data.macd.hist.map((value) => ({
    value,
    itemStyle: { color: Number(value) >= 0 ? UP_COLOR : DOWN_COLOR },
  }))
  const xAxisIndexes = [0, 1, 2, 3, 4]

  return {
    animation: false,
    backgroundColor: "transparent",
    legend: {
      top: 2,
      left: GRID_LEFT,
      itemWidth: 12,
      itemHeight: 2,
      textStyle: { fontSize: 10, color: "#71717a" },
      data: ["MA5", "MA10", "MA20", "EXPMA20", "EXPMA50"],
    },
    title: [
      {
        text: "MACD(12,26,9)",
        left: GRID_LEFT,
        top: Math.max(0, layout.macdTop - 16),
        textStyle: { fontSize: 10, fontWeight: "normal", color: "#71717a" },
      },
      {
        text: "KDJ(9,3,3)",
        left: GRID_LEFT,
        top: Math.max(0, layout.kdjTop - 16),
        textStyle: { fontSize: 10, fontWeight: "normal", color: "#71717a" },
      },
      {
        text: "RSI(6,12,24)",
        left: GRID_LEFT,
        top: Math.max(0, layout.rsiTop - 16),
        textStyle: { fontSize: 10, fontWeight: "normal", color: "#71717a" },
      },
    ],
    axisPointer: { link: [{ xAxisIndex: "all" }], label: { backgroundColor: "#52525b" } },
    tooltip: {
      trigger: "axis",
      // 浮层限制在图表区域内，避免在弹窗顶部被裁掉上半部分。
      confine: true,
      axisPointer: { type: "cross" },
      borderWidth: 0,
      backgroundColor: "rgba(24,24,27,0.94)",
      textStyle: { color: "#fafafa", fontSize: 11 },
      extraCssText:
        "border-radius:6px;padding:6px 8px;box-shadow:0 6px 18px rgba(0,0,0,0.28);max-width:340px;white-space:normal;",
      formatter: tooltipFormatter(data),
    },
    grid: [
      { left: GRID_LEFT, right: GRID_RIGHT, top: layout.top, height: layout.priceHeight },
      {
        left: GRID_LEFT,
        right: GRID_RIGHT,
        top: layout.volumeTop,
        height: layout.volumeHeight,
      },
      {
        left: GRID_LEFT,
        right: GRID_RIGHT,
        top: layout.macdTop,
        height: layout.indicatorHeight,
      },
      {
        left: GRID_LEFT,
        right: GRID_RIGHT,
        top: layout.kdjTop,
        height: layout.indicatorHeight,
      },
      {
        left: GRID_LEFT,
        right: GRID_RIGHT,
        top: layout.rsiTop,
        height: layout.indicatorHeight,
      },
    ],
    xAxis: xAxisIndexes.map((index) => ({
      type: "category",
      gridIndex: index,
      data: data.dates,
      boundaryGap: true,
      axisLine: { lineStyle: { color: "#e4e4e7" } },
      axisTick: { show: false },
      axisLabel: {
        show: index === 4,
        fontSize: maxBars ? 9 : 10,
        color: "#71717a",
        rotate: maxBars ? 45 : 0,
        interval: maxBars ? 1 : "auto",
        hideOverlap: !maxBars,
      },
      splitLine: { show: false },
      min: "dataMin",
      max: "dataMax",
    })),
    yAxis: [
      {
        scale: true,
        gridIndex: 0,
        splitNumber: 4,
        axisLabel: { fontSize: 10, color: "#71717a" },
        splitLine: { lineStyle: { color: "#f4f4f5" } },
      },
      {
        scale: true,
        gridIndex: 1,
        splitNumber: 2,
        axisLabel: { show: false },
        splitLine: { show: false },
      },
      {
        scale: true,
        gridIndex: 2,
        splitNumber: 2,
        axisLabel: { fontSize: 9, color: "#a1a1aa" },
        splitLine: { lineStyle: { color: "#f4f4f5" } },
      },
      {
        scale: true,
        gridIndex: 3,
        splitNumber: 2,
        axisLabel: { fontSize: 9, color: "#a1a1aa" },
        splitLine: { lineStyle: { color: "#f4f4f5" } },
      },
      {
        scale: true,
        gridIndex: 4,
        splitNumber: 2,
        min: 0,
        max: 100,
        axisLabel: { fontSize: 9, color: "#a1a1aa" },
        splitLine: { lineStyle: { color: "#f4f4f5" } },
      },
    ],
    dataZoom: enableZoom
      ? [
          {
            type: "inside",
            xAxisIndex: xAxisIndexes,
            start: period === "daily" ? 35 : 0,
            end: 100,
          },
          {
            type: "slider",
            xAxisIndex: xAxisIndexes,
            top: Math.max(0, height - 30),
            height: 18,
            start: period === "daily" ? 35 : 0,
            end: 100,
            borderColor: "#e4e4e7",
            fillerColor: "rgba(24,24,27,0.08)",
            handleSize: 12,
            textStyle: { fontSize: 9, color: "#71717a" },
          },
        ]
      : [],
    series: [
      {
        name: "K线",
        type: "candlestick",
        data: data.ohlc,
        itemStyle: {
          color: UP_COLOR,
          color0: DOWN_COLOR,
          borderColor: UP_COLOR,
          borderColor0: DOWN_COLOR,
        },
      },
      line("MA5", data.ma.ma5, "#f59e0b"),
      line("MA10", data.ma.ma10, "#3b82f6"),
      line("MA20", data.ma.ma20, "#a855f7"),
      line("EXPMA20", data.expma.expma20, "#ec4899", 1.4),
      line("EXPMA50", data.expma.expma50, "#14b8a6", 1.4),
      {
        name: "成交量",
        type: "bar",
        xAxisIndex: 1,
        yAxisIndex: 1,
        data: volumeData,
        barWidth: "60%",
        itemStyle: { borderRadius: [1, 1, 0, 0] },
      },
      {
        name: "MACD柱",
        type: "bar",
        xAxisIndex: 2,
        yAxisIndex: 2,
        data: histData,
        barWidth: "55%",
      },
      { ...line("DIF", data.macd.dif, "#f59e0b"), xAxisIndex: 2, yAxisIndex: 2 },
      { ...line("DEA", data.macd.dea, "#3b82f6"), xAxisIndex: 2, yAxisIndex: 2 },
      { ...line("K", data.kdj.k, "#f59e0b"), xAxisIndex: 3, yAxisIndex: 3 },
      { ...line("D", data.kdj.d, "#3b82f6"), xAxisIndex: 3, yAxisIndex: 3 },
      { ...line("J", data.kdj.j, "#a855f7"), xAxisIndex: 3, yAxisIndex: 3 },
      { ...line("RSI6", data.rsi.rsi6, "#f59e0b"), xAxisIndex: 4, yAxisIndex: 4 },
      { ...line("RSI12", data.rsi.rsi12, "#3b82f6"), xAxisIndex: 4, yAxisIndex: 4 },
      { ...line("RSI24", data.rsi.rsi24, "#a855f7"), xAxisIndex: 4, yAxisIndex: 4 },
    ],
  }
}

export function StockTechnicalChart({
  data,
  period,
  maxBars,
  enableZoom = true,
  height = 750,
}: {
  data: TechnicalPeriod
  period: string
  /** 只展示最近 N 根 K 线（指标仍按完整历史计算后截断显示）。 */
  maxBars?: number
  /** 是否显示 dataZoom 缩放条；只看固定窗口时关闭。 */
  enableZoom?: boolean
  /** 画布高度（像素），5 个子图按比例自适应。 */
  height?: number
}) {
  const containerRef = useRef<HTMLDivElement>(null)

  useEffect(() => {
    let chart: ECharts | null = null
    let disposed = false
    let observer: ResizeObserver | null = null

    void import("echarts").then((echarts) => {
      if (disposed || !containerRef.current) return
      chart = echarts.init(containerRef.current, undefined, { renderer: "canvas" })
      chart.setOption(buildOption(data, period, maxBars, enableZoom, height), true)
      observer = new ResizeObserver(() => chart?.resize())
      observer.observe(containerRef.current)
    })

    return () => {
      disposed = true
      observer?.disconnect()
      chart?.dispose()
    }
  }, [data, period, maxBars, enableZoom, height])

  return (
    <div className={cn("w-full", !maxBars && "overflow-x-auto")}>
      <div
        ref={containerRef}
        className={cn("w-full", !maxBars && "min-w-[680px]")}
        style={{ height }}
      />
    </div>
  )
}
