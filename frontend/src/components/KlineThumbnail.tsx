import { useMemo } from "react"

import type { ReportThumbnailItem } from "@/lib/types"

const UP_COLOR = "#dc2626"
const DOWN_COLOR = "#16a34a"
const WIDTH = 140
const HEIGHT = 48
const PADDING_Y = 3
const BODY_WIDTH = 4
const MIN_BODY_HEIGHT = 1
// 缩略图只画最近 20 个交易日，保证 48px 高度下每根 K 线仍清晰可辨。
const VISIBLE_BARS = 20

type RawBar = ReportThumbnailItem["ohlc"][number]

type KlineBar = {
  open: number
  high: number
  low: number
  close: number
}

type Candle = {
  x: number
  color: string
  wickTop: number
  wickBottom: number
  bodyTop: number
  bodyHeight: number
}

function isNumber(value: number | null): value is number {
  return typeof value === "number" && Number.isFinite(value)
}

function toBar(raw: RawBar): KlineBar | null {
  const [open, high, low, close] = raw
  if (!isNumber(open) || !isNumber(high) || !isNumber(low) || !isNumber(close)) {
    return null
  }
  return { open, high, low, close }
}

export function KlineThumbnail({ bars }: { bars: RawBar[] | undefined }) {
  const candles = useMemo<Candle[] | null>(() => {
    const parsed = (bars ?? [])
      .map(toBar)
      .filter((bar): bar is KlineBar => bar !== null)
      .slice(-VISIBLE_BARS)
    if (!parsed.length) return null

    let min = Number.POSITIVE_INFINITY
    let max = Number.NEGATIVE_INFINITY
    for (const bar of parsed) {
      min = Math.min(min, bar.low)
      max = Math.max(max, bar.high)
    }
    const range = max - min || 1
    const plotHeight = HEIGHT - PADDING_Y * 2
    const scaleY = (value: number) =>
      PADDING_Y + ((max - value) / range) * plotHeight
    const step = parsed.length > 1 ? (WIDTH - BODY_WIDTH) / (parsed.length - 1) : 0
    const offset = parsed.length > 1 ? BODY_WIDTH / 2 : WIDTH / 2

    return parsed.map((bar, index) => {
      const bodyTop = scaleY(Math.max(bar.open, bar.close))
      const bodyBottom = scaleY(Math.min(bar.open, bar.close))
      return {
        x: offset + index * step,
        color: bar.close >= bar.open ? UP_COLOR : DOWN_COLOR,
        wickTop: scaleY(bar.high),
        wickBottom: scaleY(bar.low),
        bodyTop,
        bodyHeight: Math.max(bodyBottom - bodyTop, MIN_BODY_HEIGHT),
      }
    })
  }, [bars])

  if (!candles) {
    return (
      <div
        aria-hidden="true"
        className="h-[48px] w-[140px] shrink-0 rounded bg-muted/40"
      />
    )
  }

  return (
    <svg
      aria-label="近 20 个交易日 K 线缩略图"
      className="shrink-0"
      height={HEIGHT}
      role="img"
      shapeRendering="crispEdges"
      viewBox={`0 0 ${WIDTH} ${HEIGHT}`}
      width={WIDTH}
    >
      {candles.map((candle, index) => (
        <g key={index}>
          <line
            stroke={candle.color}
            strokeWidth={1}
            x1={candle.x}
            x2={candle.x}
            y1={candle.wickTop}
            y2={candle.wickBottom}
          />
          <rect
            fill={candle.color}
            height={candle.bodyHeight}
            width={BODY_WIDTH}
            x={candle.x - BODY_WIDTH / 2}
            y={candle.bodyTop}
          />
        </g>
      ))}
    </svg>
  )
}
