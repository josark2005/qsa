import { useEffect, useMemo, useRef, useState } from "react"
import { useQuery } from "@tanstack/react-query"
import {
  AlertTriangle,
  Copy,
  Download,
  ExternalLink,
  Loader2,
  Play,
  Plus,
  RotateCcw,
  Share2,
  Trash2,
} from "lucide-react"
import { useSearchParams } from "react-router-dom"
import { toast } from "sonner"

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
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog"
import { Input } from "@/components/ui/input"
import { Label } from "@/components/ui/label"
import {
  Select,
  SelectContent,
  SelectGroup,
  SelectItem,
  SelectLabel,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select"
import { Skeleton } from "@/components/ui/skeleton"
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table"
import { ScreenCompareCard } from "@/components/ScreenCompareCard"
import type { DraftCondition } from "@/components/ScreenCompareCard"
import { api, ApiError } from "@/lib/api"
import { formatFactorValue, pctTone } from "@/lib/format"
import type {
  FactorMeta,
  ScreenCondition,
  ScreenExtraField,
  ScreenResponse,
  ScreenSort,
} from "@/lib/types"

const PAGE_SIZE = 50
const OP_LABELS: Record<string, string> = {
  eq: "等于",
  ne: "不等于",
  gt: "大于",
  gte: "≥",
  lt: "小于",
  lte: "≤",
  between: "介于",
  contains: "包含",
  in: "属于",
}
const BOARD_LABELS: Record<string, string> = {
  main: "主板",
  gem: "创业板",
  star: "科创板",
  bse: "北交所",
}
const MODE_LABELS: Record<string, string> = {
  close: "收盘",
  touch: "触板",
  one_word: "一字",
}

export type { DraftCondition } from "@/components/ScreenCompareCard"

export function newId(): string {
  return typeof crypto !== "undefined" && "randomUUID" in crypto
    ? crypto.randomUUID()
    : `${Date.now()}-${Math.random()}`
}

function defaultParams(factor: FactorMeta): Record<string, unknown> {
  return Object.fromEntries(factor.params.map((param) => [param.name, param.default]))
}

function defaultOperator(factor: FactorMeta): string {
  if (factor.category === "low" || factor.category === "holder") {
    return factor.ops.includes("lte") ? "lte" : factor.ops[0]
  }
  if (factor.category === "financial" || factor.category === "limit") {
    return factor.ops.includes("gte") ? "gte" : factor.ops[0]
  }
  return factor.ops[0]
}

function defaultSingleValue(factor: FactorMeta): unknown {
  if (factor.value_type === "bool") return false
  if (factor.value_type === "string") return factor.choices[0] ?? ""
  const values: Record<string, number> = {
    close_pct_rank: 0.2,
    price_from_low_pct: 0.3,
    holder_change_pct: -5,
    limit_up_count: 1,
    limit_down_count: 0,
    revenue_yoy: 20,
    net_profit_yoy: 20,
    roe: 10,
    gross_margin: 20,
    debt_ratio: 60,
    pe_ttm: 30,
    pb: 3,
    turnover_rate: 3,
    avg_amount: 100_000_000,
    list_days: 365,
    first_limit_up_volume_ratio: 1.5,
    limit_up_volume_ratio: 1.5,
    window_price_range_pct: 30,
    recent_price_amplitude_ratio: 3,
    holder_num: 50_000,
    open_gap_pct: 3,
    intraday_change_pct: -3,
    limit_up_volume_ratio_min: 1.5,
  }
  return values[factor.key] ?? 0
}

function defaultValue(factor: FactorMeta, op: string): unknown {
  if (op === "between") {
    if (factor.key === "close_pct_rank") return [0, 0.2]
    const single = Number(defaultSingleValue(factor))
    return [Math.min(0, single), Math.max(0, single)]
  }
  return defaultSingleValue(factor)
}

export function makeCondition(factor: FactorMeta): DraftCondition {
  const op = defaultOperator(factor)
  return {
    id: newId(),
    field: factor.key,
    op,
    value: defaultValue(factor, op),
    params: defaultParams(factor),
  }
}

function presetConditions(
  raw: unknown,
  factorMap: Map<string, FactorMeta>,
): DraftCondition[] {
  if (typeof raw !== "string") return []
  try {
    const parsed = JSON.parse(raw) as unknown
    if (!Array.isArray(parsed)) return []
    return parsed
      .filter((item): item is Record<string, unknown> => {
        return (
          typeof item === "object" &&
          item !== null &&
          typeof item.field === "string" &&
          factorMap.has(item.field)
        )
      })
      .map((item) => ({
        id: newId(),
        field: String(item.field),
        op: String(item.op ?? "eq"),
        value: item.value,
        params:
          typeof item.params === "object" && item.params !== null
            ? (item.params as Record<string, unknown>)
            : {},
      }))
  } catch {
    return []
  }
}

function factorCaption(
  factor: FactorMeta,
  params: Record<string, unknown>,
): string {
  const parts = factor.params.map((param) => {
    const current = params[param.name] ?? param.default
    if (param.name === "mode") {
      return MODE_LABELS[String(current)] ?? String(current)
    }
    if (param.name === "days") return `${current}日`
    return `${param.label} ${current}`
  })
  return parts.length ? parts.join(" · ") : factor.unit
}

export function ConditionRow({
  condition,
  factor,
  factors,
  categories,
  onChange,
  onRemove,
}: {
  condition: DraftCondition
  factor: FactorMeta
  factors: FactorMeta[]
  categories: Array<{ key: string; label: string }>
  onChange: (next: DraftCondition) => void
  onRemove: () => void
}) {
  const numeric = factor.value_type === "number"
  const between = Array.isArray(condition.value)
  const value = condition.value

  return (
    <div className="rounded-lg border bg-background">
      <div className="grid gap-2 p-3 md:grid-cols-[minmax(0,1.5fr)_110px_minmax(0,1.4fr)_36px]">
        <Select
          value={condition.field}
          onValueChange={(field) => {
            const next = factors.find((item) => item.key === field)
            if (!next) return
            const op = defaultOperator(next)
            onChange({
              ...condition,
              field,
              op,
              value: defaultValue(next, op),
              params: defaultParams(next),
            })
          }}
        >
          <SelectTrigger className="w-full">
            <SelectValue />
          </SelectTrigger>
          <SelectContent>
            {categories.map((category) => (
              <SelectGroup key={category.key}>
                <SelectLabel>{category.label}</SelectLabel>
                {factors
                  .filter((item) => item.category === category.key)
                  .map((item) => (
                    <SelectItem key={item.key} value={item.key}>
                      {item.label}
                    </SelectItem>
                  ))}
              </SelectGroup>
            ))}
          </SelectContent>
        </Select>
        <Select
          value={condition.op}
          onValueChange={(op) =>
            onChange({
              ...condition,
              op,
              value: defaultValue(factor, op),
            })
          }
        >
          <SelectTrigger className="w-full">
            <SelectValue />
          </SelectTrigger>
          <SelectContent>
            {factor.ops
              .filter((op) => op !== "in")
              .map((op) => (
                <SelectItem key={op} value={op}>
                  {OP_LABELS[op] ?? op}
                </SelectItem>
              ))}
          </SelectContent>
        </Select>

        {factor.value_type === "bool" ? (
          <Select
            value={String(Boolean(value))}
            onValueChange={(next) =>
              onChange({ ...condition, value: next === "true" })
            }
          >
            <SelectTrigger className="w-full">
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value="true">是</SelectItem>
              <SelectItem value="false">否</SelectItem>
            </SelectContent>
          </Select>
        ) : factor.value_type === "string" && factor.choices.length > 0 ? (
          <Select
            value={String(value ?? "")}
            onValueChange={(next) => onChange({ ...condition, value: next })}
          >
            <SelectTrigger className="w-full">
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              {factor.choices.map((choice) => (
                <SelectItem key={choice} value={choice}>
                  {factor.choice_labels[choice] ?? choice}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
        ) : numeric && between ? (
          <div className="flex items-center gap-2">
            <Input
              type="number"
              step="any"
              value={String((value as unknown[])[0] ?? "")}
              onChange={(event) =>
                onChange({
                  ...condition,
                  value: [
                    event.target.value === "" ? "" : Number(event.target.value),
                    (value as unknown[])[1] ?? "",
                  ],
                })
              }
            />
            <span className="text-xs text-muted-foreground">至</span>
            <Input
              type="number"
              step="any"
              value={String((value as unknown[])[1] ?? "")}
              onChange={(event) =>
                onChange({
                  ...condition,
                  value: [
                    (value as unknown[])[0] ?? "",
                    event.target.value === "" ? "" : Number(event.target.value),
                  ],
                })
              }
            />
          </div>
        ) : numeric ? (
          <Input
            type="number"
            step="any"
            min={factor.value_min ?? undefined}
            max={factor.value_max ?? undefined}
            value={String(value ?? "")}
            onChange={(event) =>
              onChange({
                ...condition,
                value:
                  event.target.value === "" ? "" : Number(event.target.value),
              })
            }
          />
        ) : (
          <Input
            value={String(value ?? "")}
            onChange={(event) => onChange({ ...condition, value: event.target.value })}
          />
        )}

        <Button
          variant="ghost"
          size="icon-sm"
          className="text-muted-foreground"
          onClick={onRemove}
          aria-label="删除条件"
        >
          <Trash2 className="size-4" />
        </Button>
      </div>

      {factor.params.length > 0 ? (
        <div className="flex flex-wrap gap-3 border-t bg-muted/20 px-3 py-2">
          {factor.params.map((param) => (
            <div key={param.name} className="flex items-center gap-1.5">
              <span className="text-[11px] text-muted-foreground">
                {param.label}
              </span>
              {param.type === "str" && param.choices ? (
                <Select
                  value={String(condition.params[param.name] ?? param.default)}
                  onValueChange={(next) =>
                    onChange({
                      ...condition,
                      params: { ...condition.params, [param.name]: next },
                    })
                  }
                >
                  <SelectTrigger className="h-7 w-28 text-xs">
                    <SelectValue />
                  </SelectTrigger>
                  <SelectContent>
                    {param.choices.map((choice) => (
                      <SelectItem key={choice} value={choice}>
                        {MODE_LABELS[choice] ?? choice}
                      </SelectItem>
                    ))}
                  </SelectContent>
                </Select>
              ) : (
                <Input
                  className="h-7 w-24 text-xs"
                  type="number"
                  min={param.minimum ?? undefined}
                  max={param.maximum ?? undefined}
                  step={param.type === "float" ? 0.05 : 1}
                  value={String(condition.params[param.name] ?? param.default)}
                  onChange={(event) =>
                    onChange({
                      ...condition,
                      params: {
                        ...condition.params,
                        [param.name]:
                          event.target.value === ""
                            ? ""
                            : Number(event.target.value),
                      },
                    })
                  }
                />
              )}
            </div>
          ))}
          <span className="text-[11px] text-muted-foreground">
            {factorCaption(factor, condition.params)}
          </span>
        </div>
      ) : null}
    </div>
  )
}

function extraLabel(extra: ScreenExtraField): string {
  const days = extra.params.days
  const mode = extra.params.mode
  if (days && mode) {
    return `${extra.label}(${days}日/${MODE_LABELS[String(mode)] ?? mode})`
  }
  if (days) return `${extra.label}(${days}日)`
  return extra.label
}

export function ScreenerPage() {
  const factorsQuery = useQuery({
    queryKey: ["factor-metadata"],
    queryFn: () => api.listFactors(),
    staleTime: 5 * 60_000,
  })
  const publicSettingsQuery = useQuery({
    queryKey: ["public-settings"],
    queryFn: () => api.publicSettings(),
    staleTime: 5 * 60_000,
  })
  const factors = factorsQuery.data?.items ?? []
  const categories = factorsQuery.data?.categories ?? []
  const factorMap = useMemo(
    () => new Map(factors.map((factor) => [factor.key, factor])),
    [factors],
  )
  const [searchParams] = useSearchParams()
  const reportToken = searchParams.get("report")
  const autoRun = searchParams.get("run") === "1"
  const reportQuery = useQuery({
    queryKey: ["public-report", reportToken],
    queryFn: () => api.publicReport(reportToken as string),
    enabled: Boolean(reportToken),
    staleTime: 5 * 60_000,
    retry: 1,
  })
  const initialized = useRef(false)
  const autoRan = useRef(false)
  const [reportLoaded, setReportLoaded] = useState(!reportToken)
  const [conditions, setConditions] = useState<DraftCondition[]>([])
  const [logic, setLogic] = useState<"and" | "or">("and")
  const [sortField, setSortField] = useState("")
  const [sortOrder, setSortOrder] = useState<"asc" | "desc">("desc")
  const [result, setResult] = useState<ScreenResponse | null>(null)
  const [page, setPage] = useState(1)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [creatingReport, setCreatingReport] = useState(false)
  const [reportDialogOpen, setReportDialogOpen] = useState(false)
  const [createdReport, setCreatedReport] = useState<{
    token: string
    title: string
  } | null>(null)

  function buildDefaultConditions(): DraftCondition[] {
    const preset = presetConditions(
      publicSettingsQuery.data?.values["screening.default_conditions"],
      factorMap,
    )
    if (preset.length > 0) return preset
    const initial: DraftCondition[] = []
    const holder = factorMap.get("holder_change_pct")
    const growth = factorMap.get("revenue_yoy")
    if (holder) initial.push(makeCondition(holder))
    if (growth) initial.push(makeCondition(growth))
    return initial
  }

  useEffect(() => {
    if (!factorsQuery.data || initialized.current) return
    if (reportToken) {
      if (reportQuery.isLoading) return
      if (reportQuery.isError) {
        setError(
          reportQuery.error instanceof Error
            ? reportQuery.error.message
            : "报表条件加载失败",
        )
        setReportLoaded(true)
        initialized.current = true
        return
      }
      if (!reportQuery.data) return
      const mapped = reportQuery.data.criteria.conditions
        .filter((condition) => factorMap.has(condition.field))
        .map((condition) => ({
          id: newId(),
          field: condition.field,
          op: condition.op,
          value: condition.value,
          params: condition.params ?? {},
        }))
      if (mapped.length === 0) {
        setError("报表条件已失效，请重新设置筛选条件")
      }
      setConditions(mapped)
      setLogic(reportQuery.data.criteria.logic)
      const firstSort = reportQuery.data.criteria.sort?.[0]
      setSortField(firstSort?.field ?? "")
      setSortOrder(firstSort?.order ?? "desc")
      setReportLoaded(true)
      initialized.current = true
      return
    }
    if (publicSettingsQuery.isLoading) return
    setConditions(buildDefaultConditions())
    initialized.current = true
  }, [
    factorsQuery.data,
    factorMap,
    reportToken,
    reportQuery.data,
    reportQuery.isError,
    reportQuery.isLoading,
    reportQuery.error,
    publicSettingsQuery.data,
    publicSettingsQuery.isLoading,
  ])

  const sortableFactors = useMemo(
    () => factors.filter((factor) => factor.sortable && factor.value_type === "number"),
    [factors],
  )

  function addCondition() {
    const factor =
      factorMap.get("revenue_yoy") ?? factors.find((item) => item.value_type === "number")
    if (!factor) return
    setConditions((previous) => [...previous, makeCondition(factor)])
  }

  function resetConditions() {
    setConditions(buildDefaultConditions())
    setResult(null)
    setError(null)
    setPage(1)
  }

  function buildRequest(pageToLoad: number) {
    if (conditions.length === 0) {
      throw new Error("请至少添加一个筛选条件")
    }
    const invalid = conditions.find((condition) => {
      if (Array.isArray(condition.value)) {
        return condition.value.some(
          (item) => item === "" || Number.isNaN(Number(item)),
        )
      }
      if (
        factorMap.get(condition.field)?.value_type === "number" &&
        (condition.value === "" || Number.isNaN(Number(condition.value)))
      ) {
        return true
      }
      return false
    })
    if (invalid) {
      const label = factorMap.get(invalid.field)?.label ?? invalid.field
      throw new Error(`请完善「${label}」的取值`)
    }
    const payloadConditions: ScreenCondition[] = conditions.map((condition) => ({
      field: condition.field,
      op: condition.op,
      value: condition.value,
      params: condition.params,
    }))
    const sort: ScreenSort[] = []
    if (sortField) {
      const factor = factorMap.get(sortField)
      if (factor) {
        sort.push({
          field: factor.key,
          order: sortOrder,
          params: defaultParams(factor),
        })
      }
    }
    return {
      logic,
      conditions: payloadConditions,
      sort,
      page: pageToLoad,
      page_size: PAGE_SIZE,
    }
  }

  async function run(pageToLoad = 1) {
    setError(null)
    setLoading(true)
    try {
      const payload = buildRequest(pageToLoad)
      const response = await api.runScreen(payload)
      setResult(response)
      setPage(response.page)
    } catch (err) {
      if (err instanceof ApiError) {
        setError(err.message)
      } else {
        setError(err instanceof Error ? err.message : "筛选失败，请稍后重试")
      }
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => {
    if (!autoRun || !reportLoaded || autoRan.current || conditions.length === 0) {
      return
    }
    autoRan.current = true
    void run(1)
  }, [autoRun, reportLoaded, conditions.length])

  async function generateReport() {
    setCreatingReport(true)
    setError(null)
    try {
      const payload = buildRequest(1)
      const created = await api.createReport(payload)
      setCreatedReport({ token: created.token, title: created.title })
      setReportDialogOpen(true)
    } catch (err) {
      const message =
        err instanceof Error ? err.message : "报表生成失败，请稍后重试"
      toast.error(message)
    } finally {
      setCreatingReport(false)
    }
  }

  const reportLink = createdReport
    ? `${window.location.origin}/reports/${createdReport.token}`
    : ""

  function copyReportLink() {
    navigator.clipboard
      .writeText(reportLink)
      .then(() => toast.success("报表链接已复制"))
      .catch(() => toast.error("复制失败，请手动复制链接"))
  }

  const pageCount = result
    ? Math.max(1, Math.ceil(result.total / result.page_size))
    : 1

  return (
    <div className="space-y-6">
      <div className="flex flex-col gap-3 sm:flex-row sm:items-end sm:justify-between">
        <div>
          <h1 className="text-2xl font-semibold tracking-tight">条件筛选</h1>
          <p className="mt-1 text-sm text-muted-foreground">
            股东户数、财报、N 日涨跌停、可调窗口历史低位等条件组合筛选。
          </p>
        </div>
      </div>

      <Card>
        <CardHeader>
          <div className="flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
            <div>
              <CardTitle className="text-base">筛选条件</CardTitle>
              <CardDescription>
                条件之间选择全部满足或任一满足；窗口参数可单独调整。
              </CardDescription>
            </div>
            <div className="flex flex-wrap items-center gap-2">
              <Select
                value={logic}
                onValueChange={(value) => setLogic(value as "and" | "or")}
              >
                <SelectTrigger className="h-8 w-32">
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value="and">全部满足</SelectItem>
                  <SelectItem value="or">任一满足</SelectItem>
                </SelectContent>
              </Select>
              <Button variant="outline" size="sm" onClick={addCondition}>
                <Plus className="size-4" />
                添加条件
              </Button>
              <Button variant="ghost" size="sm" onClick={resetConditions}>
                <RotateCcw className="size-4" />
                重置
              </Button>
            </div>
          </div>
        </CardHeader>
        <CardContent className="space-y-3">
          {factorsQuery.isLoading ? (
            <div className="space-y-2">
              <Skeleton className="h-16 w-full" />
              <Skeleton className="h-16 w-full" />
            </div>
          ) : factorsQuery.isError ? (
            <Alert variant="destructive">
              <AlertTriangle className="size-4" />
              <AlertTitle>因子加载失败</AlertTitle>
              <AlertDescription>
                {factorsQuery.error instanceof Error
                  ? factorsQuery.error.message
                  : "请稍后重试"}
              </AlertDescription>
            </Alert>
          ) : conditions.length === 0 ? (
            <div className="rounded-lg border border-dashed p-8 text-center text-sm text-muted-foreground">
              暂无筛选条件，点击“添加条件”开始。
            </div>
          ) : (
            conditions.map((condition) => {
              const factor = factorMap.get(condition.field)
              if (!factor) return null
              return (
                <ConditionRow
                  key={condition.id}
                  condition={condition}
                  factor={factor}
                  factors={factors}
                  categories={categories}
                  onChange={(next) =>
                    setConditions((previous) =>
                      previous.map((item) =>
                        item.id === condition.id ? next : item,
                      ),
                    )
                  }
                  onRemove={() =>
                    setConditions((previous) =>
                      previous.filter((item) => item.id !== condition.id),
                    )
                  }
                />
              )
            })
          )}

          <div className="flex flex-col gap-3 border-t pt-4 sm:flex-row sm:items-end sm:justify-between">
            <div className="flex flex-wrap items-end gap-3">
              <div className="space-y-1">
                <Label className="text-xs">排序字段</Label>
                <Select
                  value={sortField || "none"}
                  onValueChange={(value) =>
                    setSortField(value === "none" ? "" : value)
                  }
                >
                  <SelectTrigger className="h-8 w-44">
                    <SelectValue placeholder="按代码" />
                  </SelectTrigger>
                  <SelectContent>
                    <SelectItem value="none">按代码</SelectItem>
                    {sortableFactors.map((factor) => (
                      <SelectItem key={factor.key} value={factor.key}>
                        {factor.label}
                      </SelectItem>
                    ))}
                  </SelectContent>
                </Select>
              </div>
              <div className="space-y-1">
                <Label className="text-xs">排序方向</Label>
                <Select
                  value={sortOrder}
                  onValueChange={(value) =>
                    setSortOrder(value as "asc" | "desc")
                  }
                  disabled={!sortField}
                >
                  <SelectTrigger className="h-8 w-24">
                    <SelectValue />
                  </SelectTrigger>
                  <SelectContent>
                    <SelectItem value="desc">降序</SelectItem>
                    <SelectItem value="asc">升序</SelectItem>
                  </SelectContent>
                </Select>
              </div>
            </div>
            <Button onClick={() => void run(1)} disabled={loading || !factors.length}>
              {loading ? (
                <Loader2 className="size-4 animate-spin" />
              ) : (
                <Play className="size-4" />
              )}
              运行筛选
            </Button>
          </div>

          {error ? (
            <Alert variant="destructive">
              <AlertTriangle className="size-4" />
              <AlertDescription>{error}</AlertDescription>
            </Alert>
          ) : null}
        </CardContent>
      </Card>

      <ScreenCompareCard
        conditions={conditions}
        logic={logic}
        factors={factors}
        factorMap={factorMap}
        onLogicChange={setLogic}
        onAdjustCondition={(conditionId, next) =>
          setConditions((previous) =>
            previous.map((item) => (item.id === conditionId ? next : item)),
          )
        }
      />

      {result ? (
        <>
          {result.rows.some((row) => row.close === null) ? (
            <Alert>
              <AlertTriangle className="size-4" />
              <AlertTitle>历史日线仍在回补</AlertTitle>
              <AlertDescription>
                部分标的尚未完成历史日线回补，收盘价、涨跌和窗口因子可能为空。
                回补完成后重新运行筛选即可看到完整行情字段。
              </AlertDescription>
            </Alert>
          ) : null}
          <Card>
          <CardHeader>
            <div className="flex flex-col gap-3 sm:flex-row sm:items-start sm:justify-between">
              <div>
                <CardTitle className="text-base">
                  筛选结果
                  <Badge variant="secondary" className="ml-2">
                    {result.total.toLocaleString()} 只
                  </Badge>
                </CardTitle>
                <CardDescription>
                  数据日期 {result.as_of} · 第 {page} / {pageCount} 页
                </CardDescription>
              </div>
              <Button
                variant="outline"
                size="sm"
                onClick={() => void generateReport()}
                disabled={creatingReport}
              >
                {creatingReport ? (
                  <Loader2 className="size-4 animate-spin" />
                ) : (
                  <Share2 className="size-4" />
                )}
                生成分享报表
              </Button>
            </div>
          </CardHeader>
          <CardContent>
            <div className="overflow-x-auto rounded-lg border">
              <Table className="min-w-[1100px]">
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
                    {result.extra_fields.map((extra) => (
                      <TableHead key={extra.alias}>{extraLabel(extra)}</TableHead>
                    ))}
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {loading ? (
                    <TableRow>
                      <TableCell colSpan={8 + result.extra_fields.length}>
                        <Skeleton className="h-6 w-full" />
                      </TableCell>
                    </TableRow>
                  ) : result.rows.length === 0 ? (
                    <TableRow>
                      <TableCell
                        colSpan={8 + result.extra_fields.length}
                        className="h-24 text-center text-sm text-muted-foreground"
                      >
                        没有符合条件的标的
                      </TableCell>
                    </TableRow>
                  ) : (
                    result.rows.map((row, index) => (
                      <TableRow key={`${String(row.symbol ?? "")}-${index}`}>
                        <TableCell>
                          <div className="font-medium">{String(row.name ?? "—")}</div>
                          <div className="text-[11px] text-muted-foreground">
                            {String(row.symbol ?? "")}
                          </div>
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
                          {formatFactorValue(
                            "holder_change_pct",
                            row.holder_change_pct,
                          )}
                        </TableCell>
                        <TableCell>
                          {formatFactorValue("revenue_yoy", row.revenue_yoy)}
                        </TableCell>
                        <TableCell>
                          {formatFactorValue(
                            "net_profit_yoy",
                            row.net_profit_yoy,
                          )}
                        </TableCell>
                        <TableCell>{formatFactorValue("roe", row.roe)}</TableCell>
                        {result.extra_fields.map((extra) => (
                          <TableCell key={extra.alias}>
                            {formatFactorValue(extra.field, row[extra.alias])}
                          </TableCell>
                        ))}
                      </TableRow>
                    ))
                  )}
                </TableBody>
              </Table>
            </div>
            <div className="mt-4 flex items-center justify-between text-xs text-muted-foreground">
              <span>
                共 {result.total.toLocaleString()} 只 · 第 {page} / {pageCount} 页
              </span>
              <div className="flex gap-2">
                <Button
                  variant="outline"
                  size="sm"
                  onClick={() => void run(page - 1)}
                  disabled={loading || page <= 1}
                >
                  上一页
                </Button>
                <Button
                  variant="outline"
                  size="sm"
                  onClick={() => void run(page + 1)}
                  disabled={loading || page >= pageCount}
                >
                  下一页
                </Button>
              </div>
            </div>
          </CardContent>
          </Card>
        </>
      ) : (
        <Card>
          <CardContent className="py-14 text-center text-sm text-muted-foreground">
            设置条件后点击“运行筛选”查看结果。
          </CardContent>
        </Card>
      )}

      <Dialog open={reportDialogOpen} onOpenChange={setReportDialogOpen}>
        <DialogContent className="sm:max-w-lg">
          <DialogHeader>
            <DialogTitle>报表已生成</DialogTitle>
            <DialogDescription>
              {createdReport?.title}。该链接为永久只读链接，持有链接的人均可访问。
            </DialogDescription>
          </DialogHeader>
          <div className="space-y-2">
            <Input value={reportLink} readOnly onFocus={(event) => event.target.select()} />
            <p className="text-xs text-muted-foreground">
              报表保存生成时的筛选条件和入选名单；技术图表固定为报表数据截止日。
            </p>
          </div>
          <DialogFooter>
            <Button variant="outline" onClick={copyReportLink}>
              <Copy className="size-4" />
              复制链接
            </Button>
            {createdReport ? (
              <Button asChild variant="outline">
                <a
                  href={`/api/public/reports/${encodeURIComponent(createdReport.token)}/pdf`}
                  target="_blank"
                  rel="noreferrer"
                >
                  <Download className="size-4" />
                  下载 PDF
                </a>
              </Button>
            ) : null}
            <Button
              onClick={() => window.open(reportLink, "_blank", "noopener,noreferrer")}
            >
              <ExternalLink className="size-4" />
              打开报表
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </div>
  )
}
