import { useEffect, useRef, useState } from "react"
import { keepPreviousData, useMutation, useQuery } from "@tanstack/react-query"
import {
  AlertTriangle,
  CalendarDays,
  CheckCircle2,
  Database,
  FileText,
  Loader2,
  Play,
  RefreshCw,
  Share2,
  Square,
  Timer,
  Users,
  Wrench,
} from "lucide-react"
import { toast } from "sonner"

import { WorkerLogPanel, WorkerLogQueryCard } from "@/components/WorkerLogPanel"
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
import { Input } from "@/components/ui/input"
import { Label } from "@/components/ui/label"
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select"
import { Skeleton } from "@/components/ui/skeleton"
import { Switch } from "@/components/ui/switch"
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table"
import { api, ApiError } from "@/lib/api"
import { formatDate, formatDateTime } from "@/lib/format"
import type { SyncRequest, SyncRequestCreate, SyncRun } from "@/lib/types"

const PAGE_SIZE = 50
const MARKET_OPEN_DATE = "1990-12-19"

function statusVariant(status: string) {
  if (status === "success") return "outline" as const
  if (status === "failed") return "destructive" as const
  if (status === "running") return "default" as const
  return "secondary" as const
}

function statusLabel(status: string): string {
  if (status === "queued") return "排队中"
  if (status === "running") return "执行中"
  if (status === "cancelling") return "停止中"
  if (status === "cancelled") return "已停止"
  if (status === "success") return "完成"
  if (status === "failed") return "失败"
  return status
}

function FreshnessBadge({
  latest,
  expected,
}: {
  latest: string | null
  expected: string | null
}) {
  if (!latest) return <Badge variant="destructive">无数据</Badge>
  if (expected && latest === expected) {
    return (
      <Badge variant="outline" className="gap-1">
        <CheckCircle2 className="size-3" />
        最新
      </Badge>
    )
  }
  return <Badge variant="secondary">滞后</Badge>
}

function formatSpan(min: string | null, max: string | null): string {
  if (!min && !max) return "暂无数据"
  return `${formatDate(min)} ~ ${formatDate(max)}`
}

function DataSpanItem({
  label,
  span,
  count,
}: {
  label: string
  span: string
  count: string
}) {
  return (
    <div className="rounded-lg border bg-muted/20 p-3">
      <div className="text-[11px] text-muted-foreground">{label}</div>
      <div className="mt-1 text-sm font-medium">{span}</div>
      <div className="mt-0.5 text-xs text-muted-foreground">{count}</div>
    </div>
  )
}

function RunRow({ run }: { run: SyncRun }) {
  return (
    <TableRow>
      <TableCell className="whitespace-nowrap text-xs text-muted-foreground">
        {formatDateTime(run.started_at)}
      </TableCell>
      <TableCell>
        <Badge variant="secondary">
          {PIPELINE_LABELS[run.pipeline] ?? run.pipeline}
        </Badge>
      </TableCell>
      <TableCell className="text-xs">{STEP_LABELS[run.step] ?? run.step}</TableCell>
      <TableCell>
        <Badge variant={statusVariant(run.status)}>
          {statusLabel(run.status)}
        </Badge>
      </TableCell>
      <TableCell className="text-xs text-muted-foreground">{run.trigger}</TableCell>
      <TableCell className="text-xs text-muted-foreground">
        {formatDate(run.target_date)}
      </TableCell>
      <TableCell className="text-xs">{run.rows_affected ?? "—"}</TableCell>
      <TableCell
        className="max-w-48 truncate text-xs text-destructive"
        title={run.error ?? undefined}
      >
        {run.error ?? "—"}
      </TableCell>
    </TableRow>
  )
}

const PHASE_LABELS: Record<string, string> = {
  stock: "日线",
  etf: "日线",
  bse: "北交所",
  history: "历史导入",
  limits: "涨跌停",
  pipeline: "Pipeline",
  calendar: "交易日历",
  universe: "证券列表",
  tencent_stock: "Tencent 日线",
  tencent_etf: "Tencent 日线",
  baostock_stock: "BaoStock 核对",
  baostock_etf: "BaoStock 核对",
  financials: "财报",
  shareholders: "股东户数",
  quality: "质量校验",
  verification: "多源核对",
  recovering: "自动续跑",
  completed: "完成",
  failed: "失败",
}

function ProgressBar({ value }: { value: number }) {
  const percent = Math.max(0, Math.min(100, value))
  return (
    <div className="h-1.5 w-full overflow-hidden rounded-full bg-muted">
      <div
        className="h-full rounded-full bg-primary transition-all"
        style={{ width: `${percent}%` }}
      />
    </div>
  )
}

function formatDuration(seconds: number | null | undefined): string {
  if (seconds === null || seconds === undefined || Number.isNaN(seconds)) {
    return "计算中"
  }
  if (seconds <= 0) return "即将完成"
  if (seconds < 60) return `${Math.ceil(seconds)} 秒`
  if (seconds < 3600) return `${Math.ceil(seconds / 60)} 分钟`
  const hours = Math.floor(seconds / 3600)
  const minutes = Math.ceil((seconds % 3600) / 60)
  return `${hours} 小时 ${minutes} 分钟`
}

function formatBytes(bytes: number | null | undefined): string {
  if (bytes === null || bytes === undefined || bytes <= 0) return "—"
  if (bytes >= 1024 ** 3) return `${(bytes / 1024 ** 3).toFixed(2)} GB`
  if (bytes >= 1024 ** 2) return `${(bytes / 1024 ** 2).toFixed(1)} MB`
  if (bytes >= 1024) return `${(bytes / 1024).toFixed(1)} KB`
  return `${bytes} B`
}

function formatRelativeTime(value: string | null | undefined): string {
  if (!value) return "—"
  const timestamp = new Date(value).getTime()
  if (Number.isNaN(timestamp)) return value
  const seconds = Math.max(0, (Date.now() - timestamp) / 1000)
  if (seconds < 10) return "刚刚"
  if (seconds < 60) return `${Math.floor(seconds)} 秒前`
  if (seconds < 3600) return `${Math.floor(seconds / 60)} 分钟前`
  return `${Math.floor(seconds / 3600)} 小时前`
}

function requestProgressText(request: SyncRequest): string {
  if (request.status === "queued") return "排队中"
  if (request.status === "success") return "完成"
  if (request.status === "failed") return "失败"
  if (request.status === "cancelling") return "停止中（超时后自动强制停止）"
  if (request.status === "cancelled") {
    return request.progress?.cancel_forced ? "已停止（强制）" : "已停止"
  }
  const progress = request.progress
  if (!progress) return "等待进度"
  const phase = PHASE_LABELS[String(progress.phase ?? "")] ?? progress.phase
  const percent = Number(progress.percent ?? 0).toFixed(1)
  const base = `${phase ?? "执行"} ${percent}%`
  const stallSeconds = Number(progress.stall_seconds ?? 0)
  return stallSeconds > 0
    ? `${base} · 等待响应 ${Math.round(stallSeconds)}s`
    : base
}

const PIPELINE_LABELS: Record<string, string> = {
  market: "行情数据",
  etf: "行情数据",
  financial: "财报/股东晚间",
  financial_morning: "财报次日补充",
  weekly: "周度校正",
  daily_data: "每日数据拉取",
  daily_report: "每日选股汇报",
  manual: "手动任务",
  backfill: "历史回补",
  full_backfill: "全量回补",
}

const STEP_LABELS: Record<string, string> = {
  bars_stock: "日线",
  bars_etf: "日线",
  incremental_stock: "日线增量",
  incremental_etf: "日线增量",
  tencent_stock: "Tencent 日线",
  tencent_etf: "Tencent 日线",
  baostock_stock: "BaoStock 日线",
  baostock_etf: "BaoStock 日线",
  backfill_tencent_stock: "Tencent 日线回补",
  backfill_tencent_etf: "Tencent 日线回补",
  backfill_baostock_stock: "BaoStock 日线回补",
  backfill_baostock_etf: "BaoStock 日线回补",
  backfill_bars: "日线回补",
  quality_stock: "质量校验",
  quality_etf: "质量校验",
  full_backfill: "全量回补",
}

const PIPELINE_OPTIONS = [
  { value: "market", label: PIPELINE_LABELS.market },
  { value: "financial", label: PIPELINE_LABELS.financial },
  { value: "financial_morning", label: PIPELINE_LABELS.financial_morning },
  { value: "weekly", label: PIPELINE_LABELS.weekly },
  { value: "daily_data", label: PIPELINE_LABELS.daily_data },
  { value: "daily_report", label: PIPELINE_LABELS.daily_report },
]

function requestTaskLabel(request: SyncRequest): string {
  if (request.kind === "pipeline") {
    return PIPELINE_LABELS[String(request.pipeline)] ?? request.pipeline ?? "Pipeline"
  }
  if (request.kind === "history_import") {
    return request.payload.force ? "强制导入历史数据" : "导入历史数据"
  }
  if (request.kind === "repair_adj_factors") {
    return "修复复权因子"
  }
  if (request.kind === "full_backfill" || request.payload.source === "full") {
    return "全量多源回补"
  }
  const start = String(request.payload.start ?? "")
  const end = String(request.payload.end ?? "")
  if (start && start < "2018-01-01" && end && end < "2018-01-01") {
    return "开市至 2017 历史补全"
  }
  if (start && start < "2018-01-01") {
    return "全量历史回补（市场开市起）"
  }
  return "历史日线回补"
}

function requestTarget(request: SyncRequest): string {
  if (request.kind === "pipeline") {
    return PIPELINE_LABELS[String(request.pipeline)] ?? request.pipeline ?? "—"
  }
  if (request.kind === "history_import") {
    return "data/history"
  }
  if (request.kind === "repair_adj_factors") {
    const assetType = String(request.payload.asset_type ?? "all")
    return assetType === "all"
      ? "全部标的"
      : assetType === "etf"
        ? "ETF"
        : "沪深/北交所标的"
  }
  const payload = request.payload
  const source =
    payload.source === "full" ? "多源核对" : String(payload.source ?? "")
  return `${source} / ${formatDate(
    String(payload.start ?? ""),
  )} ~ ${formatDate(String(payload.end ?? ""))}`
}

function requestSummary(request: SyncRequest): string {
  if (request.error) return request.error
  if (!request.result) return "—"
  const result = request.result
  const parts: string[] = []
  const stock = result.stock as { rows?: number } | undefined
  const etf = result.etf as { rows?: number } | undefined
  if (typeof result.imported === "number") {
    parts.push(
      `导入 ${result.imported} 个文件 · 跳过 ${Number(result.skipped ?? 0)}`,
    )
  }
  if (stock?.rows || etf?.rows) {
    parts.push(`日线 ${(stock?.rows ?? 0) + (etf?.rows ?? 0)}`)
  }
  if (typeof result.limit_events === "number") {
    parts.push(`涨跌停 ${result.limit_events}`)
  }
  const verification = result.verification as
    | { checked?: number; mismatched?: number }
    | undefined
  if (verification?.checked !== undefined) {
    parts.push(
      `核对 ${verification.checked} 条 · 差异 ${verification.mismatched ?? 0}`,
    )
  }
  if (typeof result.instruments_fixed === "number") {
    parts.push(
      `扫描 ${Number(result.instruments_scanned ?? 0)} 只 · 修复 ${Number(
        result.instruments_fixed ?? 0,
      )} 只 · ${Number(result.rows_updated ?? 0)} 行`,
    )
  }
  const errors = result.errors as Record<string, string> | undefined
  if (errors && Object.keys(errors).length) {
    parts.push(`步骤异常 ${Object.keys(errors).length}`)
  }
  const stallTimeouts = Number(result.stall_timeouts ?? 0)
  if (stallTimeouts > 0) {
    parts.push(`超时跳过 ${stallTimeouts} 只`)
  }
  if (result.resumed === true) {
    parts.push("断点续传")
  }
  return parts.length ? parts.join(" · ") : "完成"
}

export function DataPage() {
  const [page, setPage] = useState(0)
  const [pipelineName, setPipelineName] = useState("market")
  const [pipelineDate, setPipelineDate] = useState("")
  const [backfillSource, setBackfillSource] = useState<
    "tencent" | "baostock" | "full"
  >("tencent")
  const [backfillStart, setBackfillStart] = useState("2023-01-01")
  const [backfillEnd, setBackfillEnd] = useState(
    new Date().toISOString().slice(0, 10),
  )
  const [backfillLimit, setBackfillLimit] = useState("")
  const [rebuildLimits, setRebuildLimits] = useState(true)
  const [fullRefresh, setFullRefresh] = useState(false)

  const statusQuery = useQuery({
    queryKey: ["sync-status"],
    queryFn: () => api.syncStatus(),
    refetchInterval: 30_000,
  })
  const runsQuery = useQuery({
    queryKey: ["sync-runs", page],
    queryFn: ({ signal }) =>
      api.syncRuns({ limit: PAGE_SIZE, offset: page * PAGE_SIZE, signal }),
    placeholderData: keepPreviousData,
    refetchInterval: 30_000,
  })
  const requestsQuery = useQuery({
    queryKey: ["sync-requests"],
    queryFn: () => api.listSyncRequests({ limit: 20, offset: 0 }),
    refetchInterval: 5_000,
  })
  const requestMutation = useMutation({
    mutationFn: (payload: SyncRequestCreate) => api.createSyncRequest(payload),
    onSuccess: () => {
      toast.success("同步请求已提交，等待 worker 执行")
      void requestsQuery.refetch()
    },
    onError: (error) => {
      toast.error(
        error instanceof ApiError ? error.message : "提交同步请求失败",
      )
    },
  })
  const cancelMutation = useMutation({
    mutationFn: (requestId: number) => api.cancelSyncRequest(requestId),
    onSuccess: (request) => {
      toast.success(
        request.status === "cancelled" ? "任务已停止" : "已请求停止任务",
      )
      void requestsQuery.refetch()
      void statusQuery.refetch()
    },
    onError: (error) => {
      toast.error(
        error instanceof ApiError ? error.message : "停止任务失败",
      )
    },
  })

  function stopRequest(requestId: number) {
    if (!window.confirm("确定停止该任务？已经写入的数据会保留。")) return
    cancelMutation.mutate(requestId)
  }

  const historyMutation = useMutation({
    mutationFn: (force: boolean) =>
      api.createSyncRequest({ kind: "history_import", force }),
    onSuccess: () => {
      toast.success("历史数据导入请求已提交，请在请求列表中查看进度")
      void requestsQuery.refetch()
      void statusQuery.refetch()
    },
    onError: (error) => {
      toast.error(
        error instanceof ApiError ? error.message : "提交历史导入失败",
      )
    },
  })

  function submitHistoryImport(force: boolean) {
    const history = status?.history
    if (!history || history.archive_files === 0) {
      toast.error("没有检测到历史归档文件")
      return
    }
    const message = force
      ? "将强制重新导入全部归档文件，已有数据会被更新。该操作可能持续较长时间，确定继续吗？"
      : "将导入尚未导入或内容已变化的归档文件，确定继续吗？"
    if (!window.confirm(message)) return
    historyMutation.mutate(force)
  }

  const status = statusQuery.data
  const fullBackfillReady = status?.worker_full_backfill_ready ?? false
  const activeStocks =
    status?.instruments.find(
      (item) => item.asset_type === "stock" && item.status === "active",
    )?.count ?? 0
  const delistedStocks =
    status?.instruments.find(
      (item) => item.asset_type === "stock" && item.status === "delisted",
    )?.count ?? 0
  const activeEtfs =
    status?.instruments.find(
      (item) => item.asset_type === "etf" && item.status === "active",
    )?.count ?? 0

  const runs = runsQuery.data?.items ?? []
  const requests = requestsQuery.data?.items ?? []
  const runningRequests = requests.filter(
    (item) =>
      item.status === "running" ||
      item.status === "queued" ||
      item.status === "cancelling",
  )
  const activeRequest = runningRequests[0]
  const activeHistoryImport = requests.some(
    (item) =>
      item.kind === "history_import" &&
      ["queued", "running", "cancelling"].includes(item.status),
  )
  const activeDaily = runningRequests.some(
    (item) =>
      item.kind === "pipeline" &&
      (item.pipeline === "daily_data" || item.pipeline === "daily_report"),
  )
  const activeRepair = runningRequests.some(
    (item) => item.kind === "repair_adj_factors",
  )
  const progressRef = useRef<{
    id: number
    phase: string
    processed: number
    at: number
  } | null>(null)
  const [etaSeconds, setEtaSeconds] = useState<number | null>(null)
  const [processRate, setProcessRate] = useState<number | null>(null)

  useEffect(() => {
    const request = activeRequest
    if (!request || request.status !== "running" || !request.progress) {
      progressRef.current = null
      setEtaSeconds(null)
      setProcessRate(null)
      return
    }
    const processed = Number(request.progress.processed ?? 0)
    const total = Number(request.progress.total ?? 0)
    const phase = String(request.progress.phase ?? "")
    const now = Date.now()
    const previous = progressRef.current
    if (previous && previous.id === request.id && previous.phase === phase) {
      const delta = processed - previous.processed
      const elapsed = (now - previous.at) / 1000
      if (delta > 0 && elapsed > 0.5) {
        const rate = delta / elapsed
        setProcessRate(rate)
        setEtaSeconds(total > processed ? (total - processed) / rate : 0)
      }
    } else {
      setEtaSeconds(null)
      setProcessRate(null)
    }
    progressRef.current = { id: request.id, phase, processed, at: now }
  }, [activeRequest])

  const stockRows = status?.daily_bars.stock.rows ?? 0
  const etfRows = status?.daily_bars.etf.rows ?? 0
  const totalRows = stockRows + etfRows
  const totalInstruments = activeStocks + activeEtfs
  const totalCoverage =
    totalInstruments > 0 ? totalRows / totalInstruments : 0
  const stockCoverage = activeStocks > 0 ? stockRows / activeStocks : 0
  const etfCoverage = activeEtfs > 0 ? etfRows / activeEtfs : 0
  const dataIncomplete =
    (activeStocks > 0 && stockCoverage < 0.9) ||
    (activeEtfs > 0 && etfCoverage < 0.9)
  const lastUpdatedSeconds = activeRequest?.updated_at
    ? (Date.now() - new Date(activeRequest.updated_at).getTime()) / 1000
    : null
  const activeProgress = activeRequest?.progress ?? null
  const stallSeconds = Number(activeProgress?.stall_seconds ?? 0)
  const stallTimeoutSeconds =
    activeProgress?.stall_timeout_seconds === undefined ||
    activeProgress?.stall_timeout_seconds === null
      ? 300
      : Number(activeProgress.stall_timeout_seconds)
  const stallTimeouts = Number(activeProgress?.stall_timeouts ?? 0)
  const stallTargetIndex =
    Number(activeProgress?.processed ?? 0) + stallTimeouts + 1
  const isStalled = activeRequest?.status === "running" && stallSeconds > 0
  const networkState =
    isStalled
      ? "等待数据源"
      : lastUpdatedSeconds === null
        ? "未知"
        : lastUpdatedSeconds < 30
          ? "活跃"
          : lastUpdatedSeconds < 120
            ? "更新变慢"
            : "可能停滞"
  const networkBadgeVariant =
    networkState === "活跃" || networkState === "等待数据源"
      ? "outline"
      : networkState === "可能停滞"
        ? "destructive"
        : "secondary"
  const elapsedSeconds = activeRequest?.started_at
    ? (Date.now() - new Date(activeRequest.started_at).getTime()) / 1000
    : null

  const totalRuns = runsQuery.data?.total ?? 0
  const pageCount = Math.max(1, Math.ceil(totalRuns / PAGE_SIZE))
  const isFetching =
    statusQuery.isFetching || runsQuery.isFetching || requestsQuery.isFetching

  function refresh() {
    void statusQuery.refetch()
    void runsQuery.refetch()
    void requestsQuery.refetch()
  }

  function submitPipeline() {
    requestMutation.mutate({
      kind: "pipeline",
      pipeline: pipelineName,
      target_date: pipelineDate || null,
    })
  }

  function submitDailyRefresh() {
    if (activeDaily) {
      toast.error("已有每日拉取或汇报任务在运行或排队")
      return
    }
    requestMutation.mutate({
      kind: "pipeline",
      pipeline: "daily_data",
      target_date: null,
    })
  }

  function submitDailyReport() {
    if (activeDaily) {
      toast.error("已有每日拉取或汇报任务在运行或排队")
      return
    }
    requestMutation.mutate({
      kind: "pipeline",
      pipeline: "daily_report",
      target_date: null,
    })
  }

  function submitRepair() {
    if (activeRepair) {
      toast.error("已有复权因子修复任务在运行或排队")
      return
    }
    const message =
      "将扫描全部日线的后复权因子，把回退/缺失的因子按前向最大值补齐并重算 close_adj。" +
      "会逐只标的写入数据库，可能持续几分钟，确定继续吗？"
    if (!window.confirm(message)) return
    requestMutation.mutate({ kind: "repair_adj_factors", asset_type: "all" })
  }

  function submitFullBackfill() {
    if (!fullBackfillReady) {
      toast.error("worker 尚未加载多源回补能力，请等待 worker 重启完成")
      return
    }
    if (runningRequests.length > 0) {
      toast.error("已有同步任务在队列或执行中，请等待完成")
      return
    }
    const confirmed = window.confirm(
      "将依次执行：全市场证券与日历更新、Tencent 全量价格、BaoStock 全字段与多源核对、北交所、涨跌停、财报/股东户数、质量校验。会从开市历史重新拉取，可能持续数小时，确定提交吗？",
    )
    if (!confirmed) return
    requestMutation.mutate({
      kind: "full_backfill",
      source: "full",
      asset_type: "all",
      start: MARKET_OPEN_DATE,
      end: new Date().toISOString().slice(0, 10),
      limit: null,
      rebuild_limits: true,
      full_refresh: true,
      include_delisted: false,
    })
  }

  function submitBackfill() {
    let limit: number | null = null
    if (backfillLimit.trim()) {
      const parsed = Number(backfillLimit)
      if (!Number.isInteger(parsed) || parsed < 1) {
        toast.error("回补数量必须是正整数")
        return
      }
      limit = parsed
    }
    if (backfillStart > backfillEnd) {
      toast.error("开始日期不能晚于结束日期")
      return
    }
    requestMutation.mutate({
      kind: backfillSource === "full" ? "full_backfill" : "backfill",
      source: backfillSource,
      asset_type: "all",
      start: backfillStart,
      end: backfillEnd,
      limit,
      rebuild_limits: rebuildLimits,
      full_refresh: fullRefresh,
      include_delisted: false,
    })
  }

  return (
    <div className="space-y-6">
      <div className="flex flex-col gap-3 sm:flex-row sm:items-end sm:justify-between">
        <div>
          <h1 className="text-2xl font-semibold tracking-tight">数据管理</h1>
          <p className="mt-1 text-sm text-muted-foreground">
            行情、财报、股东户数与交易日历的新鲜度与同步运行记录。
          </p>
        </div>
        <Button variant="outline" onClick={refresh} disabled={isFetching}>
          <RefreshCw className={isFetching ? "size-4 animate-spin" : "size-4"} />
          刷新
        </Button>
      </div>

      {dataIncomplete ? (
        <Alert className="border-amber-500/40 bg-amber-500/5">
          <AlertTriangle className="size-4" />
          <AlertTitle>数据不完整</AlertTitle>
          <AlertDescription>
            <div className="flex flex-col gap-2 sm:flex-row sm:items-center sm:justify-between">
              <span>
                行情日线覆盖 {totalRows.toLocaleString()} /{" "}
                {totalInstruments.toLocaleString()}（
                {(totalCoverage * 100).toFixed(1)}%）。
                {activeRequest
                  ? "同步任务正在运行，数据会持续更新。"
                  : "可使用下方“日常回补”补齐最新交易日数据。"}
              </span>
              <Button
                size="sm"
                variant="outline"
                onClick={() =>
                  document
                    .getElementById(
                      activeRequest ? "progress-section" : "daily-section",
                    )
                    ?.scrollIntoView({ behavior: "smooth", block: "start" })
                }
              >
                查看回补进度
              </Button>
            </div>
          </AlertDescription>
        </Alert>
      ) : null}

      <Card id="daily-section" className="border-primary/30">
        <CardHeader className="pb-3">
          <CardTitle className="text-base">每日拉取与汇报</CardTitle>
          <CardDescription>
            两段独立执行：「拉取数据」补齐目标交易日缺失的日线（含北交所）、涨跌停与财报/股东，
            「选股汇报」再用默认条件筛选并推送到已订阅且已启用的 QQ 机器人。
            默认每天 16:00 拉取、17:00 汇报，可在系统设置里分别调整。
          </CardDescription>
        </CardHeader>
        <CardContent className="flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
          <p className="text-xs leading-5 text-muted-foreground">
            {activeDaily
              ? "每日拉取或汇报任务正在运行或排队。"
              : activeRequest
                ? `当前正在执行：${requestTaskLabel(activeRequest)}。每日拉取可并行提交。`
                : "不会重拉全部历史；数据校验通过后才会向订阅群推送选股结果。"}
          </p>
          <div className="flex flex-wrap gap-2">
            <Button
              variant="outline"
              onClick={submitDailyReport}
              disabled={requestMutation.isPending || activeDaily}
            >
              {requestMutation.isPending || activeDaily ? (
                <Loader2 className="size-4 animate-spin" />
              ) : (
                <Share2 className="size-4" />
              )}
              执行选股汇报
            </Button>
            <Button
              onClick={submitDailyRefresh}
              disabled={requestMutation.isPending || activeDaily}
            >
              {requestMutation.isPending || activeDaily ? (
                <Loader2 className="size-4 animate-spin" />
              ) : (
                <RefreshCw className="size-4" />
              )}
              {activeDaily ? "任务执行中" : "执行数据拉取"}
            </Button>
          </div>
        </CardContent>
      </Card>

      <Card id="repair-section">
        <CardHeader className="pb-3">
          <CardTitle className="text-base">数据修复</CardTitle>
          <CardDescription>
            扫描日线后复权因子，把回退/缺失的因子按前向最大值补齐并重算
            close_adj（等同终端执行 repair-adj-factors --apply）。修复后 K
            线与指标刷新即可，旧 PDF 缓存里的指标表不会自动变化。
          </CardDescription>
        </CardHeader>
        <CardContent className="flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
          <p className="text-xs leading-5 text-muted-foreground">
            {activeRepair
              ? "复权因子修复任务正在运行或排队。"
              : "运行日志出现「adjustment factor series is inconsistent」告警、或 K 线出现假跳空时使用。"}
          </p>
          <Button
            variant="outline"
            onClick={submitRepair}
            disabled={requestMutation.isPending || activeRepair}
          >
            {requestMutation.isPending || activeRepair ? (
              <Loader2 className="size-4 animate-spin" />
            ) : (
              <Wrench className="size-4" />
            )}
            {activeRepair ? "修复执行中" : "修复复权因子"}
          </Button>
        </CardContent>
      </Card>

      {activeRequest ? (
        <Card id="progress-section" className="border-primary/30">
          <CardHeader className="pb-3">
            <div className="flex items-center justify-between gap-3">
              <CardTitle className="text-base">
                {requestTaskLabel(activeRequest)}进度
              </CardTitle>
              <div className="flex items-center gap-2">
                <Badge variant={statusVariant(activeRequest.status)}>
                  {statusLabel(activeRequest.status)}
                </Badge>
                {activeRequest.status === "running" ||
                activeRequest.status === "queued" ? (
                  <Button
                    variant="destructive"
                    size="sm"
                    onClick={() => stopRequest(activeRequest.id)}
                    disabled={cancelMutation.isPending}
                  >
                    <Square className="size-3.5" />
                    停止任务
                  </Button>
                ) : null}
              </div>
            </div>
            <CardDescription>{requestTarget(activeRequest)}</CardDescription>
          </CardHeader>
          <CardContent className="space-y-3">
            {activeRequest.status === "queued" ? (
              <div className="text-sm text-muted-foreground">
                请求已进入队列，等待 worker 领取。
              </div>
            ) : (
              <>
                <div className="flex items-center justify-between text-sm">
                  <span>
                    {PHASE_LABELS[String(activeRequest.progress?.phase ?? "")] ??
                      activeRequest.progress?.message ??
                      "执行中"}
                  </span>
                  <span className="font-medium">
                    {Number(activeRequest.progress?.percent ?? 0).toFixed(1)}%
                  </span>
                </div>
                <ProgressBar
                  value={Number(activeRequest.progress?.percent ?? 0)}
                />
                <div className="grid gap-2 text-xs text-muted-foreground sm:grid-cols-4">
                  <span>
                    进度{" "}
                    {Number(activeRequest.progress?.processed ?? 0).toLocaleString()}{" "}
                    / {Number(activeRequest.progress?.total ?? 0).toLocaleString()}
                  </span>
                  <span>
                    写入{" "}
                    {Number(activeRequest.progress?.rows ?? 0).toLocaleString()} 行
                  </span>
                  <span>
                    错误 {Number(activeRequest.progress?.errors ?? 0)} 个
                  </span>
                  <span>当前 {activeRequest.progress?.current ?? "—"}</span>
                  <span>
                    预计剩余 {formatDuration(etaSeconds)}（按当前速率）
                  </span>
                  <span>已运行 {formatDuration(elapsedSeconds)}</span>
                  <span>
                    处理速率{" "}
                    {processRate === null ? "计算中" : `${processRate.toFixed(1)} 只/秒`}
                  </span>
                  <span>数据源 {String(activeRequest.payload.source ?? "—")}</span>
                </div>
                <div className="flex flex-wrap items-center gap-2 text-[11px] text-muted-foreground">
                  <Badge variant={networkBadgeVariant}>
                    网络/写入：{networkState}
                  </Badge>
                  <span>最近更新 {formatRelativeTime(activeRequest.updated_at)}</span>
                  <span>
                    累计写入{" "}
                    {Number(activeRequest.progress?.rows ?? 0).toLocaleString()} 行
                  </span>
                  <span>错误 {Number(activeRequest.progress?.errors ?? 0)} 个</span>
                  {activeRequest.progress?.network_bytes ? (
                    <span>
                      下载 {formatBytes(activeRequest.progress.network_bytes)}
                      {activeRequest.progress.network_requests
                        ? ` / ${activeRequest.progress.network_requests} 请求`
                        : ""}
                    </span>
                  ) : null}
                </div>
              </>
            )}
            {isStalled ? (
              <div className="space-y-1.5 rounded-lg border border-dashed bg-muted/20 px-3 py-2">
                <div className="flex items-start gap-2 text-xs text-muted-foreground">
                  <Timer className="mt-0.5 size-3.5 shrink-0" />
                  <span>
                    第 {stallTargetIndex} 只标的正等待数据源响应，已等待{" "}
                    {Math.round(stallSeconds)} 秒
                    {stallTimeoutSeconds > 0
                      ? `，超过 ${stallTimeoutSeconds} 秒将断开该标的并继续后续标的，稍后自动补拉`
                      : "（当前未设置单次请求超时），可手动停止任务"}
                  </span>
                </div>
                {stallTimeouts > 0 ? (
                  <div className="flex items-center gap-2 pl-5.5">
                    <Badge
                      variant={stallTimeouts >= 50 ? "destructive" : "secondary"}
                    >
                      已因超时跳过 {stallTimeouts} 只
                    </Badge>
                    <span className="text-[11px] text-muted-foreground">
                      稍后自动续传补拉
                    </span>
                  </div>
                ) : null}
                {activeRequest.progress?.resume_after ? (
                  <div className="pl-5.5 text-[11px] text-muted-foreground">
                    断点续传：当前从 {activeRequest.progress.resume_after}{" "}
                    之后继续，已写入的数据会保留
                  </div>
                ) : null}
              </div>
            ) : null}
            {activeRequest.status === "cancelling" ? (
              <div className="flex items-start gap-2 rounded-lg border border-dashed bg-muted/20 px-3 py-2 text-xs text-muted-foreground">
                <Timer className="mt-0.5 size-3.5 shrink-0" />
                <span>等待后台收敛，超过宽限期会自动强制停止</span>
              </div>
            ) : null}
            <WorkerLogPanel
              requestId={activeRequest.id}
              running={
                activeRequest.status === "queued" ||
                activeRequest.status === "running" ||
                activeRequest.status === "cancelling"
              }
            />
          </CardContent>
        </Card>
      ) : null}

      {statusQuery.isLoading ? (
        <div className="grid gap-4 md:grid-cols-2 xl:grid-cols-3">
          {Array.from({ length: 6 }).map((_, index) => (
            <Skeleton key={index} className="h-32 w-full" />
          ))}
        </div>
      ) : statusQuery.isError ? (
        <Alert variant="destructive">
          <AlertTriangle className="size-4" />
          <AlertTitle>同步状态加载失败</AlertTitle>
          <AlertDescription>
            {statusQuery.error instanceof Error
              ? statusQuery.error.message
              : "请稍后重试"}
          </AlertDescription>
        </Alert>
      ) : status ? (
        <>
          <div className="grid gap-4 md:grid-cols-2 xl:grid-cols-3">
            <Card>
              <CardHeader className="pb-2">
                <CardDescription>行情标的</CardDescription>
                <CardTitle className="flex items-center gap-2 text-2xl">
                  <Database className="size-4" />
                  {totalInstruments.toLocaleString()}
                </CardTitle>
              </CardHeader>
              <CardContent className="text-xs text-muted-foreground">
                退市标的 {delistedStocks} 只保留历史数据
              </CardContent>
            </Card>
            <Card>
              <CardHeader className="pb-2">
                <CardDescription>最近交易日历</CardDescription>
                <CardTitle className="flex items-center gap-2 text-2xl">
                  <CalendarDays className="size-4" />
                  {formatDate(status.calendar_latest_past_open_date)}
                </CardTitle>
              </CardHeader>
              <CardContent className="text-xs text-muted-foreground">
                日历已覆盖至 {formatDate(status.calendar_latest_open_date)}
              </CardContent>
            </Card>
            <Card>
              <CardHeader className="pb-2">
                <CardDescription>日线行情</CardDescription>
                <CardTitle className="flex items-center justify-between text-lg">
                  <span>{formatDate(status.daily_bars.stock.latest_date)}</span>
                  <FreshnessBadge
                    latest={status.daily_bars.stock.latest_date}
                    expected={status.calendar_latest_past_open_date}
                  />
                </CardTitle>
              </CardHeader>
              <CardContent className="text-xs text-muted-foreground">
                最近日期 {totalRows.toLocaleString()} 条
              </CardContent>
            </Card>
            <Card>
              <CardHeader className="pb-2">
                <CardDescription>财报</CardDescription>
                <CardTitle className="flex items-center justify-between text-lg">
                  <span>{formatDate(status.financial_reports.latest_period)}</span>
                  <FileText className="size-4" />
                </CardTitle>
              </CardHeader>
              <CardContent className="text-xs text-muted-foreground">
                {status.financial_reports.rows.toLocaleString()} 条 · 公告日{" "}
                {formatDate(status.financial_reports.latest_announce_date)}
              </CardContent>
            </Card>
            <Card>
              <CardHeader className="pb-2">
                <CardDescription>股东户数</CardDescription>
                <CardTitle className="flex items-center justify-between text-lg">
                  <span>
                    {formatDate(status.shareholder_counts.latest_period)}
                  </span>
                  <Users className="size-4" />
                </CardTitle>
              </CardHeader>
              <CardContent className="text-xs text-muted-foreground">
                {status.shareholder_counts.rows.toLocaleString()} 条 · 公告日{" "}
                {formatDate(status.shareholder_counts.latest_announce_date)}
              </CardContent>
            </Card>
            <Card className="md:col-span-2 xl:col-span-3">
              <CardHeader className="pb-2">
                <CardTitle className="text-base">数据规模</CardTitle>
                <CardDescription>已入库数据的时间跨度与总条数</CardDescription>
              </CardHeader>
              <CardContent>
                <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-4">
                  <DataSpanItem
                    label="日线行情"
                    span={formatSpan(
                      status.daily_bars.min_date,
                      status.daily_bars.max_date,
                    )}
                    count={`${status.daily_bars.total_rows.toLocaleString()} 条`}
                  />
                  <DataSpanItem
                    label="财报"
                    span={formatSpan(
                      status.financial_reports.period_min,
                      status.financial_reports.period_max,
                    )}
                    count={`${status.financial_reports.total_rows.toLocaleString()} 条`}
                  />
                  <DataSpanItem
                    label="股东户数"
                    span={formatSpan(
                      status.shareholder_counts.period_min,
                      status.shareholder_counts.period_max,
                    )}
                    count={`${status.shareholder_counts.total_rows.toLocaleString()} 条`}
                  />
                  <DataSpanItem
                    label="交易日历"
                    span={formatSpan(
                      status.calendar_min_date,
                      status.calendar_max_date,
                    )}
                    count={`${status.calendar_open_days.toLocaleString()} 个交易日`}
                  />
                </div>
              </CardContent>
            </Card>
          </div>

          <Card>
            <CardHeader className="pb-3">
              <CardTitle className="text-base">历史数据导入</CardTitle>
              <CardDescription>
                自动检测归档文件与数据库条数，可补导缺失文件或强制重新导入。
              </CardDescription>
            </CardHeader>
            <CardContent className="space-y-3">
              <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-4">
                <DataSpanItem
                  label="归档文件"
                  span={`${status.history.archive_files.toLocaleString()} 个`}
                  count={formatBytes(status.history.archive_bytes)}
                />
                <DataSpanItem
                  label="已导入标记"
                  span={`${status.history.imported_files.toLocaleString()} 个`}
                  count={
                    status.history.archive_expected_rows > 0
                      ? `归档预期 ${status.history.archive_expected_rows.toLocaleString()} 条`
                      : `记录 ${status.history.imported_rows.toLocaleString()} 条`
                  }
                />
                <DataSpanItem
                  label="待导入文件"
                  span={`${status.history.pending_files.toLocaleString()} 个`}
                  count={
                    status.history.needs_import ? "检测到未导入归档" : "已同步"
                  }
                />
                <DataSpanItem
                  label="数据库日线"
                  span={`${status.history.daily_bar_rows.toLocaleString()} 条`}
                  count={
                    status.history.needs_force_import
                      ? "明显少于归档记录"
                      : "正常"
                  }
                />
              </div>
              {status.history.needs_force_import ? (
                <Alert className="border-amber-500/40 bg-amber-500/5">
                  <AlertTriangle className="size-4" />
                  <AlertDescription>
                    数据库日线数量明显少于历史导入记录，可能存在导入标记但数据未落库。建议执行“强制重新导入”。
                  </AlertDescription>
                </Alert>
              ) : status.history.needs_import ? (
                <Alert>
                  <AlertTriangle className="size-4" />
                  <AlertDescription>
                    检测到尚未导入的历史归档，可执行“导入缺失历史”。
                  </AlertDescription>
                </Alert>
              ) : null}
              <div className="flex flex-col gap-2 sm:flex-row sm:items-center sm:justify-between">
                <p className="text-[11px] text-muted-foreground">
                  归档目录：{status.history.directory}
                </p>
                <div className="flex flex-wrap gap-2">
                  <Button
                    variant="outline"
                    onClick={() => submitHistoryImport(false)}
                    disabled={
                      historyMutation.isPending ||
                      activeHistoryImport ||
                      status.history.archive_files === 0
                    }
                  >
                    {historyMutation.isPending ? (
                      <Loader2 className="size-4 animate-spin" />
                    ) : (
                      <RefreshCw className="size-4" />
                    )}
                    导入缺失历史
                  </Button>
                  <Button
                    variant="destructive"
                    onClick={() => submitHistoryImport(true)}
                    disabled={
                      historyMutation.isPending ||
                      activeHistoryImport ||
                      status.history.archive_files === 0
                    }
                  >
                    {historyMutation.isPending ? (
                      <Loader2 className="size-4 animate-spin" />
                    ) : (
                      <RefreshCw className="size-4" />
                    )}
                    强制重新导入
                  </Button>
                </div>
              </div>
            </CardContent>
          </Card>

        </>
      ) : null}

      <Card id="backfill-section">
        <CardHeader>
          <CardTitle className="text-base">手动触发</CardTitle>
          <CardDescription>
            请求进入数据库队列，由 worker 消费；worker 未运行时请求会保持 queued。
          </CardDescription>
        </CardHeader>
        <CardContent className="space-y-5">
          <div className="space-y-2">
            <div className="text-sm font-medium">运行 Pipeline</div>
            <div className="grid gap-3 md:grid-cols-[200px_180px_auto] md:items-end">
              <div className="space-y-1.5">
                <Label className="text-xs">Pipeline</Label>
                <Select value={pipelineName} onValueChange={setPipelineName}>
                  <SelectTrigger className="w-full">
                    <SelectValue />
                  </SelectTrigger>
                  <SelectContent>
                    {PIPELINE_OPTIONS.map((item) => (
                      <SelectItem key={item.value} value={item.value}>
                        {item.label}
                      </SelectItem>
                    ))}
                  </SelectContent>
                </Select>
              </div>
              <div className="space-y-1.5">
                <Label className="text-xs">目标日期</Label>
                <Input
                  type="date"
                  value={pipelineDate}
                  onChange={(event) => setPipelineDate(event.target.value)}
                />
              </div>
              <Button
                onClick={submitPipeline}
                disabled={requestMutation.isPending}
              >
                {requestMutation.isPending ? (
                  <Loader2 className="size-4 animate-spin" />
                ) : (
                  <Play className="size-4" />
                )}
                运行 Pipeline
              </Button>
            </div>
          </div>

          <div className="space-y-3 border-t pt-4">
            <div className="text-sm font-medium">历史日线回补</div>
            <div className="grid gap-3 md:grid-cols-3 xl:grid-cols-6 xl:items-end">
              <div className="space-y-1.5">
                <Label className="text-xs">数据源</Label>
                <Select
                  value={backfillSource}
                  onValueChange={(value) =>
                    setBackfillSource(value as "tencent" | "baostock" | "full")
                  }
                >
                  <SelectTrigger className="w-full">
                    <SelectValue />
                  </SelectTrigger>
                  <SelectContent>
                    <SelectItem value="tencent">Tencent 快速价格</SelectItem>
                    <SelectItem value="baostock">BaoStock 全字段</SelectItem>
                    <SelectItem value="full">多源核对全量</SelectItem>
                  </SelectContent>
                </Select>
              </div>
              <div className="space-y-1.5">
                <Label className="text-xs">开始日期</Label>
                <Input
                  type="date"
                  value={backfillStart}
                  onChange={(event) => setBackfillStart(event.target.value)}
                />
              </div>
              <div className="space-y-1.5">
                <Label className="text-xs">结束日期</Label>
                <Input
                  type="date"
                  value={backfillEnd}
                  onChange={(event) => setBackfillEnd(event.target.value)}
                />
              </div>
              <div className="space-y-1.5">
                <Label className="text-xs">数量上限</Label>
                <Input
                  type="number"
                  min={1}
                  placeholder="不填为全部"
                  value={backfillLimit}
                  onChange={(event) => setBackfillLimit(event.target.value)}
                />
              </div>
              <div className="flex h-8 items-center gap-2">
                <Switch
                  checked={rebuildLimits}
                  onCheckedChange={setRebuildLimits}
                />
                <Label className="text-xs">重算涨跌停</Label>
              </div>
              <div className="flex h-8 items-center gap-2">
                <Switch
                  checked={fullRefresh}
                  onCheckedChange={setFullRefresh}
                />
                <Label className="text-xs">强制全量</Label>
              </div>
            </div>
            <div className="flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
              <p className="text-xs leading-5 text-muted-foreground">
                Tencent 覆盖沪深价格，速度快但不含成交额/PE/ST 历史；BaoStock
                字段完整但可能限流；选择“多源核对全量”会再执行北交所、财报/股东与质量校验。
                全量回补可能耗时数小时，建议先填数量上限验证。强制全量会忽略已有近端日期，按起始日期重新拉取。
              </p>
              <Button
                onClick={submitBackfill}
                disabled={requestMutation.isPending}
              >
                {requestMutation.isPending ? (
                  <Loader2 className="size-4 animate-spin" />
                ) : (
                  <Play className="size-4" />
                )}
                提交回补
              </Button>
            </div>
          </div>
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle className="text-base">手动请求</CardTitle>
          <CardDescription>最近 20 条，每 5 秒刷新。</CardDescription>
        </CardHeader>
        <CardContent>
          {requestsQuery.isError ? (
            <Alert variant="destructive">
              <AlertTriangle className="size-4" />
              <AlertTitle>请求记录加载失败</AlertTitle>
              <AlertDescription>
                {requestsQuery.error instanceof Error
                  ? requestsQuery.error.message
                  : "请稍后重试"}
              </AlertDescription>
            </Alert>
          ) : (
            <div className="overflow-hidden rounded-lg border">
              <Table>
                <TableHeader>
                  <TableRow>
                    <TableHead>ID</TableHead>
                    <TableHead>类型</TableHead>
                    <TableHead>任务</TableHead>
                    <TableHead>状态</TableHead>
                    <TableHead>进度</TableHead>
                    <TableHead>创建时间</TableHead>
                    <TableHead>开始 / 结束</TableHead>
                    <TableHead>结果 / 错误</TableHead>
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {requestsQuery.isLoading
                    ? Array.from({ length: 4 }).map((_, index) => (
                        <TableRow key={`request-skeleton-${index}`}>
                          <TableCell colSpan={8}>
                            <Skeleton className="h-6 w-full" />
                          </TableCell>
                        </TableRow>
                      ))
                    : requests.map((item) => (
                        <TableRow key={item.id}>
                          <TableCell className="text-xs text-muted-foreground">
                            #{item.id}
                          </TableCell>
                          <TableCell>
                            <Badge variant="secondary">{item.kind}</Badge>
                          </TableCell>
                          <TableCell className="max-w-64 text-xs">
                            <div className="truncate" title={requestTarget(item)}>
                              {requestTarget(item)}
                            </div>
                          </TableCell>
                          <TableCell>
                            <div className="flex items-center gap-1.5">
                              <Badge variant={statusVariant(item.status)}>
                                {statusLabel(item.status)}
                              </Badge>
                              {item.status === "running" ||
                              item.status === "queued" ? (
                                <Button
                                  variant="ghost"
                                  size="icon-xs"
                                  title="停止任务"
                                  onClick={() => stopRequest(item.id)}
                                  disabled={cancelMutation.isPending}
                                >
                                  <Square className="size-3" />
                                </Button>
                              ) : null}
                            </div>
                          </TableCell>
                          <TableCell className="min-w-40">
                            {item.status === "running" ? (
                              <div className="space-y-1">
                                <div className="flex justify-between text-[11px]">
                                  <span>{requestProgressText(item)}</span>
                                  <span>
                                    {Number(
                                      item.progress?.percent ?? 0,
                                    ).toFixed(1)}
                                    %
                                  </span>
                                </div>
                                <ProgressBar
                                  value={Number(item.progress?.percent ?? 0)}
                                />
                                <div className="text-[10px] text-muted-foreground">
                                  {Number(
                                    item.progress?.processed ?? 0,
                                  ).toLocaleString()}{" "}
                                  /{" "}
                                  {Number(
                                    item.progress?.total ?? 0,
                                  ).toLocaleString()}{" "}
                                  · {item.progress?.current ?? "—"}
                                </div>
                              </div>
                            ) : (
                              <span className="text-xs text-muted-foreground">
                                {requestProgressText(item)}
                              </span>
                            )}
                          </TableCell>
                          <TableCell className="whitespace-nowrap text-xs text-muted-foreground">
                            {formatDateTime(item.created_at)}
                          </TableCell>
                          <TableCell className="whitespace-nowrap text-xs text-muted-foreground">
                            {formatDateTime(item.started_at)} /{" "}
                            {formatDateTime(item.finished_at)}
                          </TableCell>
                          <TableCell
                            className={
                              item.error
                                ? "max-w-64 truncate text-xs text-destructive"
                                : "max-w-64 truncate text-xs text-muted-foreground"
                            }
                            title={requestSummary(item)}
                          >
                            {requestSummary(item)}
                          </TableCell>
                        </TableRow>
                      ))}
                  {!requestsQuery.isLoading && requests.length === 0 ? (
                    <TableRow>
                      <TableCell
                        colSpan={8}
                        className="h-20 text-center text-sm text-muted-foreground"
                      >
                        暂无手动请求
                      </TableCell>
                    </TableRow>
                  ) : null}
                </TableBody>
              </Table>
            </div>
          )}
        </CardContent>
      </Card>

      <WorkerLogQueryCard />

      <Card>
        <CardHeader>
          <CardTitle className="text-base">最近运行记录</CardTitle>
          <CardDescription>
            共 {totalRuns} 条。运行记录由 worker 和 CLI 共用写入。
          </CardDescription>
        </CardHeader>
        <CardContent>
          {runsQuery.isError ? (
            <Alert variant="destructive">
              <AlertTriangle className="size-4" />
              <AlertTitle>运行记录加载失败</AlertTitle>
              <AlertDescription>
                {runsQuery.error instanceof Error
                  ? runsQuery.error.message
                  : "请稍后重试"}
              </AlertDescription>
            </Alert>
          ) : (
            <div className="overflow-hidden rounded-lg border">
              <Table>
                <TableHeader>
                  <TableRow>
                    <TableHead>开始时间</TableHead>
                    <TableHead>Pipeline</TableHead>
                    <TableHead>步骤</TableHead>
                    <TableHead>状态</TableHead>
                    <TableHead>触发</TableHead>
                    <TableHead>目标日</TableHead>
                    <TableHead>行数</TableHead>
                    <TableHead>错误</TableHead>
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {runsQuery.isLoading
                    ? Array.from({ length: 5 }).map((_, index) => (
                        <TableRow key={`skeleton-${index}`}>
                          <TableCell colSpan={8}>
                            <Skeleton className="h-6 w-full" />
                          </TableCell>
                        </TableRow>
                      ))
                    : runs.map((run) => <RunRow key={run.id} run={run} />)}
                  {!runsQuery.isLoading && runs.length === 0 ? (
                    <TableRow>
                      <TableCell
                        colSpan={8}
                        className="h-24 text-center text-sm text-muted-foreground"
                      >
                        暂无运行记录
                      </TableCell>
                    </TableRow>
                  ) : null}
                </TableBody>
              </Table>
            </div>
          )}
          <div className="mt-4 flex items-center justify-between text-xs text-muted-foreground">
            <span>
              第 {Math.min(page + 1, pageCount)} / {pageCount} 页
            </span>
            <div className="flex gap-2">
              <Button
                variant="outline"
                size="sm"
                onClick={() => setPage((current) => Math.max(0, current - 1))}
                disabled={page === 0}
              >
                上一页
              </Button>
              <Button
                variant="outline"
                size="sm"
                onClick={() => setPage((current) => current + 1)}
                disabled={page + 1 >= pageCount}
              >
                下一页
              </Button>
            </div>
          </div>
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle className="text-base">批量回补命令</CardTitle>
          <CardDescription>
            完整历史回补耗时较长，建议在终端分批执行。
          </CardDescription>
        </CardHeader>
        <CardContent className="space-y-2">
          <pre className="overflow-x-auto rounded-lg bg-muted p-3 text-xs leading-6">
            {`cd backend
# 一键全量多源回补：Tencent 价格 + BaoStock 全字段核对 + 北交所 + 财报/股东 + 质量校验
uv run python -m app.cli sync-bars --source full --start ${MARKET_OPEN_DATE} --asset-type all

# 启动 worker，消费网页端提交的同步/回补请求
uv run python -m app.worker`}
          </pre>
        </CardContent>
      </Card>

      <Card className="border-destructive/40">
        <CardHeader className="pb-3">
          <CardTitle className="text-base">全量回补</CardTitle>
          <CardDescription>
            从 {MARKET_OPEN_DATE} 至今重新拉取完整历史，并执行 Tencent/BaoStock
            多源核对。该操作会重新拉取全部历史数据，耗时可能数小时，通常只在重建数据时使用。
          </CardDescription>
        </CardHeader>
        <CardContent className="flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
          <p className="text-xs leading-5 text-destructive">
            危险操作：会重新拉取全部历史数据，不替代日常 16:00 增量任务。
          </p>
          <Button
            variant="destructive"
            onClick={submitFullBackfill}
            disabled={
              requestMutation.isPending ||
              Boolean(activeRequest) ||
              !fullBackfillReady
            }
          >
            {requestMutation.isPending || activeRequest ? (
              <Loader2 className="size-4 animate-spin" />
            ) : (
              <Play className="size-4" />
            )}
            {activeRequest
              ? "任务执行中"
              : fullBackfillReady
                ? "执行全量回补"
                : "等待 worker 就绪"}
          </Button>
        </CardContent>
      </Card>
    </div>
  )
}
