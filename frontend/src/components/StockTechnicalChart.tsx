import { useEffect, useRef } from "react"
import type { ECharts, EChartsOption } from "echarts"

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

function buildOption(data: TechnicalPeriod, period: string): EChartsOption {
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
        top: 412,
        textStyle: { fontSize: 10, fontWeight: "normal", color: "#71717a" },
      },
      {
        text: "KDJ(9,3,3)",
        left: GRID_LEFT,
        top: 512,
        textStyle: { fontSize: 10, fontWeight: "normal", color: "#71717a" },
      },
      {
        text: "RSI(6,12,24)",
        left: GRID_LEFT,
        top: 612,
        textStyle: { fontSize: 10, fontWeight: "normal", color: "#71717a" },
      },
    ],
    axisPointer: { link: [{ xAxisIndex: "all" }], label: { backgroundColor: "#52525b" } },
    tooltip: {
      trigger: "axis",
      axisPointer: { type: "cross" },
      borderWidth: 0,
      backgroundColor: "rgba(24,24,27,0.92)",
      textStyle: { color: "#fafafa", fontSize: 11 },
    },
    grid: [
      { left: GRID_LEFT, right: GRID_RIGHT, top: 24, height: 300 },
      { left: GRID_LEFT, right: GRID_RIGHT, top: 338, height: 58 },
      { left: GRID_LEFT, right: GRID_RIGHT, top: 426, height: 76 },
      { left: GRID_LEFT, right: GRID_RIGHT, top: 526, height: 76 },
      { left: GRID_LEFT, right: GRID_RIGHT, top: 626, height: 76 },
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
        fontSize: 10,
        color: "#71717a",
        hideOverlap: true,
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
    dataZoom: [
      {
        type: "inside",
        xAxisIndex: xAxisIndexes,
        start: period === "daily" ? 35 : 0,
        end: 100,
      },
      {
        type: "slider",
        xAxisIndex: xAxisIndexes,
        top: 718,
        height: 18,
        start: period === "daily" ? 35 : 0,
        end: 100,
        borderColor: "#e4e4e7",
        fillerColor: "rgba(24,24,27,0.08)",
        handleSize: 12,
        textStyle: { fontSize: 9, color: "#71717a" },
      },
    ],
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
}: {
  data: TechnicalPeriod
  period: string
}) {
  const containerRef = useRef<HTMLDivElement>(null)

  useEffect(() => {
    let chart: ECharts | null = null
    let disposed = false
    let observer: ResizeObserver | null = null

    void import("echarts").then((echarts) => {
      if (disposed || !containerRef.current) return
      chart = echarts.init(containerRef.current, undefined, { renderer: "canvas" })
      chart.setOption(buildOption(data, period), true)
      observer = new ResizeObserver(() => chart?.resize())
      observer.observe(containerRef.current)
    })

    return () => {
      disposed = true
      observer?.disconnect()
      chart?.dispose()
    }
  }, [data, period])

  return (
    <div className="w-full overflow-x-auto">
      <div ref={containerRef} className="h-[750px] min-w-[680px] w-full" />
    </div>
  )
}
