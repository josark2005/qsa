import { useEffect, useMemo, useState } from "react"
import { useQuery } from "@tanstack/react-query"
import {
  AlertTriangle,
  ArrowLeft,
  Copy,
  Download,
  ExternalLink,
  Pencil,
  RotateCcw,
  Shield,
} from "lucide-react"
import { useNavigate, useParams } from "react-router-dom"
import { toast } from "sonner"

import { useAuth } from "@/auth/AuthProvider"
import { ReportDataTable } from "@/components/ReportDataTable"
import { StockTechnicalChart } from "@/components/StockTechnicalChart"
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card"
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog"
import { Skeleton } from "@/components/ui/skeleton"
import { Switch } from "@/components/ui/switch"
import { Tabs, TabsList, TabsTrigger } from "@/components/ui/tabs"
import { api } from "@/lib/api"
import { formatDateTime, formatFactorValue, pctTone } from "@/lib/format"
import { cn } from "@/lib/utils"
import type { ReportThumbnailItem, TechnicalPeriod } from "@/lib/types"

const BOARD_LABELS: Record<string, string> = {
  main: "主板",
  gem: "创业板",
  star: "科创板",
  bse: "北交所",
}
const PERIOD_LABELS = { daily: "日线", weekly: "周线", monthly: "月线" } as const
const TREND_STORAGE_KEY = "qsa.report.showTrend"
const INDICATOR_STORAGE_KEY = "qsa.report.showIndicators"
const CHART_BARS = 20

function computeChartHeight(): number {
  if (typeof window === "undefined") return 470
  // 弹窗最大 95vh。窄屏时指标卡会换成两列、标题更高，需要多留一些空间；
  // 目标是让「K 线 + 成交量 + MACD/KDJ/RSI」在弹窗内一屏放下，不需要滚动。
  const headerAllowance = window.innerWidth < 640 ? 345 : 285
  return Math.round(
    Math.min(600, Math.max(340, window.innerHeight * 0.95 - headerAllowance)),
  )
}

function numberText(value: number | null | undefined, digits = 2): string {
  if (value === null || value === undefined || Number.isNaN(value)) return "—"
  return value.toFixed(digits)
}


function formatVolume(value: number | null | undefined): string {
  const numeric = Number(value ?? 0)
  if (!Number.isFinite(numeric) || numeric <= 0) return "—"
  if (numeric >= 1e8) return `${(numeric / 1e8).toFixed(2)} 亿`
  if (numeric >= 1e4) return `${(numeric / 1e4).toFixed(2)} 万`
  return numeric.toFixed(0)
}
function MetricCard({
  label,
  value,
  tone,
}: {
  label: string
  value: string
  tone?: "up" | "down"
}) {
  return (
    <Card className="gap-0.5 py-1.5">
      <CardContent className="px-2.5">
        <div className="text-[10px] text-muted-foreground">{label}</div>
        <div
          className={cn(
            "mt-0.5 text-xs font-medium",
            tone === "up" && "text-red-600",
            tone === "down" && "text-emerald-600",
          )}
        >
          {value}
        </div>
      </CardContent>
    </Card>
  )
}

function readTrendPreference(): boolean {
  try {
    return window.localStorage.getItem(TREND_STORAGE_KEY) !== "0"
  } catch {
    return true
  }
}

function readIndicatorPreference(): boolean {
  try {
    return window.localStorage.getItem(INDICATOR_STORAGE_KEY) === "1"
  } catch {
    return false
  }
}

export function ReportPage() {
  const { token = "" } = useParams()
  const navigate = useNavigate()
  const { user } = useAuth()
  const [detailSymbol, setDetailSymbol] = useState<string | null>(null)
  const [period, setPeriod] = useState<keyof typeof PERIOD_LABELS>("daily")
  const [showTrend, setShowTrend] = useState<boolean>(readTrendPreference)
  const [showIndicators, setShowIndicators] = useState<boolean>(readIndicatorPreference)
  const [visibleSymbols, setVisibleSymbols] = useState<string[]>([])
  const [chartHeight, setChartHeight] = useState<number>(computeChartHeight)

  const reportQuery = useQuery({
    queryKey: ["public-report", token],
    queryFn: () => api.publicReport(token),
    enabled: Boolean(token),
    staleTime: 5 * 60_000,
    retry: 1,
  })
  const report = reportQuery.data

  useEffect(() => {
    if (report) {
      document.title = `${report.title} · QSA工作台`
    }
    const robots = document.createElement("meta")
    robots.name = "robots"
    robots.content = "noindex,nofollow"
    document.head.appendChild(robots)
    return () => {
      document.title = "QSA工作台"
      robots.remove()
    }
  }, [report])

  useEffect(() => {
    try {
      window.localStorage.setItem(TREND_STORAGE_KEY, showTrend ? "1" : "0")
    } catch {
      // localStorage 不可用时仅本次会话生效
    }
  }, [showTrend])

  useEffect(() => {
    try {
      window.localStorage.setItem(INDICATOR_STORAGE_KEY, showIndicators ? "1" : "0")
    } catch {
      // localStorage 不可用时仅本次会话生效
    }
  }, [showIndicators])

  useEffect(() => {
    const onResize = () => setChartHeight(computeChartHeight())
    window.addEventListener("resize", onResize)
    return () => window.removeEventListener("resize", onResize)
  }, [])

  const visibleSymbolsKey = visibleSymbols.join(",")
  const thumbnailsQuery = useQuery({
    queryKey: ["report-thumbnails", token, visibleSymbolsKey],
    queryFn: () => api.reportThumbnails(token, visibleSymbols),
    enabled: Boolean(token && showTrend && visibleSymbols.length),
    staleTime: Number.POSITIVE_INFINITY,
  })

  const technicalQuery = useQuery({
    queryKey: ["report-technical", token, detailSymbol],
    queryFn: () => api.reportTechnical(token, detailSymbol as string),
    enabled: Boolean(report && detailSymbol),
    staleTime: Number.POSITIVE_INFINITY,
  })

  const thumbnailMap = useMemo(() => {
    const map = new Map<string, ReportThumbnailItem["ohlc"]>()
    for (const item of thumbnailsQuery.data?.items ?? []) {
      map.set(item.symbol, item.ohlc)
    }
    return map
  }, [thumbnailsQuery.data])

  const detailRow = useMemo(
    () => report?.rows.find((row) => String(row.symbol) === detailSymbol) ?? null,
    [report, detailSymbol],
  )
  const technical = technicalQuery.data
  const periodData: TechnicalPeriod | null = technical?.periods[period] ?? null

  function copyLink() {
    const url = `${window.location.origin}/reports/${token}`
    navigator.clipboard
      .writeText(url)
      .then(() => toast.success("报表链接已复制"))
      .catch(() => toast.error("复制失败，请手动复制地址栏链接"))
  }

  function rerun(modify: boolean) {
    const suffix = modify ? "" : "&run=1"
    navigate(`/screener?report=${encodeURIComponent(token)}${suffix}`)
  }

  if (reportQuery.isLoading) {
    return (
      <div className="mx-auto max-w-6xl space-y-4 p-4 md:p-8">
        <Skeleton className="h-24 w-full" />
        <Skeleton className="h-48 w-full" />
        <Skeleton className="h-96 w-full" />
      </div>
    )
  }

  if (reportQuery.isError || !report) {
    return (
      <div className="flex min-h-svh items-center justify-center bg-muted/30 p-4">
        <Card className="w-full max-w-md">
          <CardHeader>
            <CardTitle className="flex items-center gap-2 text-lg">
              <AlertTriangle className="size-5 text-amber-600" />
              报表无法打开
            </CardTitle>
            <CardDescription>
              {reportQuery.error instanceof Error
                ? reportQuery.error.message
                : "报表不存在或链接已失效。"}
            </CardDescription>
          </CardHeader>
          <CardContent>
            <Button variant="outline" onClick={() => navigate("/")}>
              <ArrowLeft className="size-4" />
              返回首页
            </Button>
          </CardContent>
        </Card>
      </div>
    )
  }

  return (
    <div className="min-h-svh bg-muted/30">
      <header className="border-b bg-background">
        <div className="mx-auto flex max-w-6xl items-center justify-between gap-3 px-4 py-3 md:px-8">
          <div className="flex items-center gap-2.5">
            <div className="flex size-8 items-center justify-center rounded-lg bg-primary text-primary-foreground">
              <Shield className="size-4" />
            </div>
            <div className="leading-tight">
              <div className="text-sm font-semibold">QSA 选股报表</div>
            </div>
          </div>
          <div className="flex items-center gap-2">
            <Button asChild variant="outline" size="sm">
              <a
                href={`/api/public/reports/${encodeURIComponent(token)}/pdf`}
                target="_blank"
                rel="noreferrer"
              >
                <Download className="size-4" />
                下载 PDF
              </a>
            </Button>
            <Button variant="outline" size="sm" onClick={copyLink}>
              <Copy className="size-4" />
              复制链接
            </Button>
            {user ? (
              <Button variant="ghost" size="sm" onClick={() => navigate("/")}>
                工作台
                <ExternalLink className="size-4" />
              </Button>
            ) : null}
          </div>
        </div>
      </header>

      <main className="mx-auto max-w-6xl space-y-5 p-4 md:p-8">
        {report.stale ? (
          <Alert variant="destructive">
            <AlertTriangle className="size-4" />
            <AlertTitle>非当日报表</AlertTitle>
            <AlertDescription>
              {report.stale_reason}
              。如需最新结果，请使用下方按钮按相同条件重新筛选。
            </AlertDescription>
          </Alert>
        ) : null}

        {report.truncated ? (
          <Alert>
            <AlertTriangle className="size-4" />
            <AlertTitle>报表结果已截断</AlertTitle>
            <AlertDescription>
              符合条件的标的共 {report.total.toLocaleString()} 只，报表保存了前{" "}
              {report.included.toLocaleString()} 只（按筛选排序）。可缩小条件后重新生成。
            </AlertDescription>
          </Alert>
        ) : null}

        <div className="flex flex-col gap-4 rounded-xl border bg-background p-4 md:flex-row md:items-start md:justify-between md:p-6">
          <div className="space-y-2">
            <div className="flex flex-wrap items-center gap-2">
              <h1 className="text-xl font-semibold tracking-tight md:text-2xl">
                {report.title}
              </h1>
              <Badge variant={report.stale ? "destructive" : "secondary"}>
                {report.stale ? "历史报表" : "当日报表"}
              </Badge>
            </div>
            <div className="flex flex-wrap gap-x-4 gap-y-1 text-xs text-muted-foreground">
              <span>生成时间 {formatDateTime(report.generated_at)}</span>
              <span>数据截至 {report.data_as_of ?? "—"}</span>
              <span>
                入选 {report.total.toLocaleString()} 只
                {report.truncated ? ` · 已保存 ${report.included.toLocaleString()} 只` : ""}
              </span>
            </div>
            <p className="text-xs text-muted-foreground">
              成交价与技术指标均固定为该报表数据截止日的前复权口径，后续数据变化不会修改本链接内容。
            </p>
          </div>
          <div className="flex flex-wrap gap-2">
            <Button size="sm" onClick={() => rerun(false)}>
              <RotateCcw className="size-4" />
              按相同条件再筛选
            </Button>
            <Button variant="outline" size="sm" onClick={() => rerun(true)}>
              <Pencil className="size-4" />
              修改条件
            </Button>
          </div>
        </div>

        <Card>
          <CardHeader>
            <CardTitle className="text-base">筛选条件</CardTitle>
            <CardDescription>
              {report.criteria.logic === "and" ? "全部满足" : "任一满足"}
              {report.sort_views.length
                ? ` · 排序：${report.sort_views
                    .map((item) => `${item.label} ${item.order_label}`)
                    .join("，")}`
                : " · 按代码排序"}
            </CardDescription>
          </CardHeader>
          <CardContent className="space-y-2">
            {report.condition_views.map((condition, index) => (
              <div
                key={`${condition.field}-${index}`}
                className="flex flex-col gap-1 rounded-lg border bg-muted/10 px-3 py-2 text-sm sm:flex-row sm:items-center sm:justify-between"
              >
                <div className="flex flex-wrap items-center gap-x-2 gap-y-1">
                  <span className="font-medium">{condition.label}</span>
                  <span className="text-muted-foreground">{condition.op_label}</span>
                  <span>{condition.value_text}</span>
                </div>
                {condition.params_text ? (
                  <span className="text-[11px] text-muted-foreground">
                    {condition.params_text}
                  </span>
                ) : null}
              </div>
            ))}
          </CardContent>
        </Card>

        <Card>
          <CardHeader>
            <CardTitle className="text-base">
              入选标的
              <Badge variant="secondary" className="ml-2">
                {report.included.toLocaleString()} 只
              </Badge>
            </CardTitle>
            <CardDescription>
              支持搜索、板块筛选和点击表头排序；走势列为近 {CHART_BARS} 个交易日 K 线，点击行打开详情。
            </CardDescription>
          </CardHeader>
          <CardContent>
            <ReportDataTable
              activeSymbol={detailSymbol}
              extraFields={report.extra_fields}
              onRowClick={setDetailSymbol}
              onShowTrendChange={setShowTrend}
              onVisibleSymbolsChange={setVisibleSymbols}
              rows={report.rows}
              showTrend={showTrend}
              thumbnails={thumbnailMap}
              thumbnailsLoading={thumbnailsQuery.isLoading}
            />
          </CardContent>
        </Card>

        <Dialog
          open={Boolean(detailSymbol)}
          onOpenChange={(open) => {
            if (!open) setDetailSymbol(null)
          }}
        >
          <DialogContent className="max-h-[95vh] w-[95vw] max-w-5xl overflow-x-hidden overflow-y-auto sm:max-w-5xl">
            <DialogHeader>
              <DialogTitle className="flex flex-wrap items-center gap-2 pr-8">
                {detailRow ? String(detailRow.name ?? detailSymbol) : detailSymbol}
                <span className="text-xs font-normal text-muted-foreground">
                  {detailSymbol}
                </span>
                {detailRow?.board ? (
                  <Badge variant="outline">
                    {BOARD_LABELS[String(detailRow.board)] ?? String(detailRow.board)}
                  </Badge>
                ) : null}
                {report.stale ? <Badge variant="destructive">历史报表</Badge> : null}
              </DialogTitle>
              <DialogDescription>
                前复权 K 线，仅展示最近 {CHART_BARS} 个交易日；默认只显示 K 线与成交量。
                开启「技术指标」后显示 MA5/10/20、EXPMA(20,50)、MACD(12,26,9)、KDJ(9,3,3)、RSI(6,12,24)（按完整历史计算）。
              </DialogDescription>
            </DialogHeader>

            <Card className="py-2">
              <CardContent className="flex flex-wrap items-center gap-x-4 gap-y-1 px-3 text-xs">
                <span>
                  收盘 {formatFactorValue("close", detailRow?.close)}
                </span>
                <span className={pctTone(detailRow?.pct_chg)}>
                  涨跌 {formatFactorValue("pct_chg", detailRow?.pct_chg)}
                </span>
                <span className="text-muted-foreground">
                  报表快照，行情截至 {report.data_as_of ?? report.screen_as_of}
                </span>
              </CardContent>
            </Card>

            <Tabs
              value={period}
              onValueChange={(value) =>
                setPeriod(value as keyof typeof PERIOD_LABELS)
              }
            >
              <TabsList>
                <TabsTrigger value="daily">日线</TabsTrigger>
                <TabsTrigger value="weekly">周线</TabsTrigger>
                <TabsTrigger value="monthly">月线</TabsTrigger>
              </TabsList>
            </Tabs>

            <div className="flex flex-wrap items-center justify-between gap-2">
              <span className="text-xs font-medium text-muted-foreground">技术指标</span>
              <div className="flex items-center gap-2">
                <span className="text-xs text-muted-foreground">
                  {showIndicators ? "显示" : "隐藏"}
                </span>
                <Switch checked={showIndicators} onCheckedChange={setShowIndicators} />
              </div>
            </div>

            {technicalQuery.isLoading ? (
              <div className="flex flex-col gap-2">
                <Skeleton className="h-16 w-full" />
                <Skeleton className="h-[420px] w-full" />
              </div>
            ) : technicalQuery.isError ? (
              <Alert variant="destructive">
                <AlertTriangle className="size-4" />
                <AlertDescription>
                  {technicalQuery.error instanceof Error
                    ? technicalQuery.error.message
                    : "技术指标加载失败"}
                </AlertDescription>
              </Alert>
            ) : periodData && periodData.latest ? (
              <div className="flex flex-col gap-3">
                <div className={cn("grid grid-cols-2 gap-2 sm:grid-cols-4", showIndicators && "lg:grid-cols-8")}>
                  <MetricCard
                    label="收盘"
                    value={numberText(periodData.latest.close)}
                  />
                  <MetricCard
                    label="涨跌"
                    value={`${numberText(periodData.latest.pct_chg)}%`}
                    tone={(periodData.latest.pct_chg ?? 0) >= 0 ? "up" : "down"}
                  />
                  <MetricCard
                    label="成交量"
                    value={formatVolume(periodData.volume[periodData.volume.length - 1])}
                  />
                  {showIndicators ? (
                    <>
                  <MetricCard
                    label="MACD 柱"
                    value={numberText(periodData.latest.macd.hist, 3)}
                  />
                  <MetricCard
                    label="DIF / DEA"
                    value={`${numberText(periodData.latest.macd.dif, 3)} / ${numberText(periodData.latest.macd.dea, 3)}`}
                  />
                  <MetricCard
                    label="K / D / J"
                    value={`${numberText(periodData.latest.kdj.k)} / ${numberText(periodData.latest.kdj.d)} / ${numberText(periodData.latest.kdj.j)}`}
                  />
                  <MetricCard
                    label="EXPMA20 / 50"
                    value={`${numberText(periodData.latest.expma20)} / ${numberText(periodData.latest.expma50)}`}
                  />
                  <MetricCard
                    label="RSI6 / 12 / 24"
                    value={`${numberText(periodData.latest.rsi.rsi6)} / ${numberText(periodData.latest.rsi.rsi12)} / ${numberText(periodData.latest.rsi.rsi24)}`}
                  />
                    </>
                  ) : null}
                  <MetricCard
                    label="周期数 / 截止"
                    value={`${periodData.bar_count} · ${periodData.latest.trade_date}`}
                  />
                </div>
                {periodData.limited_history && showIndicators ? (
                  <Alert>
                    <AlertTriangle className="size-4" />
                    <AlertDescription>
                      该标的{PERIOD_LABELS[period]}历史样本较少，部分长周期指标可能不完整。
                    </AlertDescription>
                  </Alert>
                ) : null}
                <Card className="overflow-hidden py-0">
                  <CardContent className="p-2">
                    <StockTechnicalChart
                      data={periodData}
                      enableZoom={false}
                      height={chartHeight}
                      maxBars={CHART_BARS}
                      period={period}
                      showIndicators={showIndicators}
                    />
                  </CardContent>
                </Card>
              </div>
            ) : (
              <Card>
                <CardContent className="py-12 text-center text-sm text-muted-foreground">
                  {technical?.message ?? "暂无足够历史数据绘制图表"}
                </CardContent>
              </Card>
            )}
          </DialogContent>
        </Dialog>

        <footer className="pb-6 text-center text-[11px] text-muted-foreground">
          该链接为永久只读报表链接 · QSA
        </footer>
      </main>
    </div>
  )
}