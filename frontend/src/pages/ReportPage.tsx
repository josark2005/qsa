import { useEffect, useMemo, useState } from "react"
import { useQuery } from "@tanstack/react-query"
import {
  AlertTriangle,
  ArrowLeft,
  ChevronLeft,
  ChevronRight,
  Copy,
  Download,
  ExternalLink,
  Loader2,
  Pencil,
  RotateCcw,
  Shield,
} from "lucide-react"
import { useNavigate, useParams } from "react-router-dom"
import { toast } from "sonner"

import { useAuth } from "@/auth/AuthProvider"
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
import { Skeleton } from "@/components/ui/skeleton"
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table"
import { Tabs, TabsList, TabsTrigger } from "@/components/ui/tabs"
import { api } from "@/lib/api"
import { formatDateTime, formatFactorValue, pctTone } from "@/lib/format"
import type { TechnicalPeriod } from "@/lib/types"

const PAGE_SIZE = 50
const BOARD_LABELS: Record<string, string> = {
  main: "主板",
  gem: "创业板",
  star: "科创板",
  bse: "北交所",
}
const PERIOD_LABELS = { daily: "日线", weekly: "周线", monthly: "月线" } as const

function numberText(value: number | null | undefined, digits = 2): string {
  if (value === null || value === undefined || Number.isNaN(value)) return "—"
  return value.toFixed(digits)
}

function Metric({
  label,
  value,
  tone,
}: {
  label: string
  value: string
  tone?: "up" | "down"
}) {
  return (
    <div className="rounded-md border bg-muted/20 px-2.5 py-2">
      <div className="text-[10px] text-muted-foreground">{label}</div>
      <div
        className={
          tone === "up"
            ? "mt-0.5 text-xs font-medium text-red-600"
            : tone === "down"
              ? "mt-0.5 text-xs font-medium text-emerald-600"
              : "mt-0.5 text-xs font-medium"
        }
      >
        {value}
      </div>
    </div>
  )
}

export function ReportPage() {
  const { token = "" } = useParams()
  const navigate = useNavigate()
  const { user } = useAuth()
  const [page, setPage] = useState(1)
  const [selectedSymbol, setSelectedSymbol] = useState<string | null>(null)
  const [period, setPeriod] = useState<keyof typeof PERIOD_LABELS>("daily")

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
      document.title = `${report.title} · QSA`
    }
    const robots = document.createElement("meta")
    robots.name = "robots"
    robots.content = "noindex,nofollow"
    document.head.appendChild(robots)
    return () => {
      document.title = "QSA 选股工作台"
      robots.remove()
    }
  }, [report])

  useEffect(() => {
    if (!report) return
    const symbols = report.rows
      .map((row) => String(row.symbol ?? ""))
      .filter(Boolean)
    if (!selectedSymbol || !symbols.includes(selectedSymbol)) {
      setSelectedSymbol(symbols[0] ?? null)
    }
  }, [report, selectedSymbol])

  const technicalQuery = useQuery({
    queryKey: ["report-technical", token, selectedSymbol],
    queryFn: () => api.reportTechnical(token, selectedSymbol as string),
    enabled: Boolean(report && selectedSymbol),
    staleTime: Number.POSITIVE_INFINITY,
  })

  const pageCount = report ? Math.max(1, Math.ceil(report.rows.length / PAGE_SIZE)) : 1
  const pageRows = report
    ? report.rows.slice((page - 1) * PAGE_SIZE, page * PAGE_SIZE)
    : []
  const selectedRow = useMemo(
    () => report?.rows.find((row) => String(row.symbol) === selectedSymbol) ?? null,
    [report, selectedSymbol],
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
              点击任意标的查看日线、周线、月线及技术指标。第 {page} / {pageCount} 页
            </CardDescription>
          </CardHeader>
          <CardContent>
            <div className="overflow-x-auto rounded-lg border">
              <Table className="min-w-[1080px]">
                <TableHeader>
                  <TableRow>
                    <TableHead>名称 / 代码</TableHead>
                    <TableHead>板块</TableHead>
                    <TableHead>收盘</TableHead>
                    <TableHead>涨跌</TableHead>
                    <TableHead>股东变化</TableHead>
                    <TableHead>营收同比</TableHead>
                    <TableHead>净利同比</TableHead>
                    <TableHead>ROE</TableHead>
                    {report.extra_fields.map((extra) => (
                      <TableHead key={extra.alias}>
                        {extra.label}
                        {extra.params.days ? `(${extra.params.days}日)` : ""}
                      </TableHead>
                    ))}
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {pageRows.map((row, index) => {
                    const symbol = String(row.symbol ?? "")
                    const active = symbol === selectedSymbol
                    return (
                      <TableRow
                        key={`${symbol}-${index}`}
                        className={active ? "bg-muted/60" : "cursor-pointer"}
                        onClick={() => setSelectedSymbol(symbol)}
                      >
                        <TableCell>
                          <div className="font-medium">{String(row.name ?? "—")}</div>
                          <div className="text-[11px] text-muted-foreground">{symbol}</div>
                        </TableCell>
                        <TableCell>
                          <Badge variant="outline">
                            {BOARD_LABELS[String(row.board ?? "")] ??
                              String(row.board ?? "—")}
                          </Badge>
                        </TableCell>
                        <TableCell>{formatFactorValue("close", row.close)}</TableCell>
                        <TableCell className={pctTone(row.pct_chg)}>
                          {formatFactorValue("pct_chg", row.pct_chg)}
                        </TableCell>
                        <TableCell>
                          {formatFactorValue("holder_change_pct", row.holder_change_pct)}
                        </TableCell>
                        <TableCell>{formatFactorValue("revenue_yoy", row.revenue_yoy)}</TableCell>
                        <TableCell>
                          {formatFactorValue("net_profit_yoy", row.net_profit_yoy)}
                        </TableCell>
                        <TableCell>{formatFactorValue("roe", row.roe)}</TableCell>
                        {report.extra_fields.map((extra) => (
                          <TableCell key={extra.alias}>
                            {formatFactorValue(extra.field, row[extra.alias])}
                          </TableCell>
                        ))}
                      </TableRow>
                    )
                  })}
                </TableBody>
              </Table>
            </div>
            <div className="mt-3 flex items-center justify-between text-xs text-muted-foreground">
              <span>
                第 {page} / {pageCount} 页 · 共 {report.included.toLocaleString()} 只
              </span>
              <div className="flex gap-2">
                <Button
                  variant="outline"
                  size="sm"
                  disabled={page <= 1}
                  onClick={() => setPage((value) => Math.max(1, value - 1))}
                >
                  <ChevronLeft className="size-4" />
                  上一页
                </Button>
                <Button
                  variant="outline"
                  size="sm"
                  disabled={page >= pageCount}
                  onClick={() => setPage((value) => Math.min(pageCount, value + 1))}
                >
                  下一页
                  <ChevronRight className="size-4" />
                </Button>
              </div>
            </div>
          </CardContent>
        </Card>

        <Card>
          <CardHeader>
            <div className="flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
              <div>
                <CardTitle className="text-base">
                  {selectedRow ? String(selectedRow.name ?? selectedSymbol) : "个股技术图表"}
                  {selectedSymbol ? (
                    <span className="ml-2 text-xs font-normal text-muted-foreground">
                      {selectedSymbol}
                    </span>
                  ) : null}
                </CardTitle>
                <CardDescription>
                  前复权 K 线；MA5/10/20、EXPMA(20,50)、MACD(12,26,9)、KDJ(9,3,3)、RSI(6,12,24)
                </CardDescription>
              </div>
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
            </div>
          </CardHeader>
          <CardContent className="space-y-3">
            {technicalQuery.isLoading ? (
              <div className="flex h-64 items-center justify-center text-sm text-muted-foreground">
                <Loader2 className="mr-2 size-4 animate-spin" />
                加载技术指标
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
              <>
                <div className="grid grid-cols-2 gap-2 sm:grid-cols-4 lg:grid-cols-8">
                  <Metric
                    label="收盘"
                    value={numberText(periodData.latest.close)}
                  />
                  <Metric
                    label="涨跌"
                    value={`${numberText(periodData.latest.pct_chg)}%`}
                    tone={
                      (periodData.latest.pct_chg ?? 0) >= 0 ? "up" : "down"
                    }
                  />
                  <Metric label="MACD 柱" value={numberText(periodData.latest.macd.hist, 3)} />
                  <Metric label="DIF / DEA" value={`${numberText(periodData.latest.macd.dif, 3)} / ${numberText(periodData.latest.macd.dea, 3)}`} />
                  <Metric label="K / D / J" value={`${numberText(periodData.latest.kdj.k)} / ${numberText(periodData.latest.kdj.d)} / ${numberText(periodData.latest.kdj.j)}`} />
                  <Metric label="EXPMA20 / 50" value={`${numberText(periodData.latest.expma20)} / ${numberText(periodData.latest.expma50)}`} />
                  <Metric label="RSI6 / 12 / 24" value={`${numberText(periodData.latest.rsi.rsi6)} / ${numberText(periodData.latest.rsi.rsi12)} / ${numberText(periodData.latest.rsi.rsi24)}`} />
                  <Metric label="周期数 / 截止" value={`${periodData.bar_count} · ${periodData.latest.trade_date}`} />
                </div>
                {periodData.limited_history ? (
                  <Alert>
                    <AlertTriangle className="size-4" />
                    <AlertDescription>
                      该标的{PERIOD_LABELS[period]}历史样本较少，部分长周期指标可能不完整。
                    </AlertDescription>
                  </Alert>
                ) : null}
                <StockTechnicalChart data={periodData} period={period} />
              </>
            ) : (
              <div className="rounded-lg border border-dashed py-16 text-center text-sm text-muted-foreground">
                {technical?.message ?? "暂无足够历史数据绘制图表"}
              </div>
            )}
          </CardContent>
        </Card>

        <footer className="pb-6 text-center text-[11px] text-muted-foreground">
          该链接为永久只读报表链接 · QSA
        </footer>
      </main>
    </div>
  )
}
