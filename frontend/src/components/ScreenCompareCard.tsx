import { useState } from "react"
import {
  CheckCircle2,
  CircleHelp,
  Loader2,
  Pencil,
  RotateCcw,
  ScanSearch,
  XCircle,
} from "lucide-react"

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
import { Input } from "@/components/ui/input"
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select"
import { api, ApiError } from "@/lib/api"
import { formatFactorValue } from "@/lib/format"
import type {
  CompareConditionResult,
  CompareResponse,
  FactorMeta,
} from "@/lib/types"

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

const MODE_LABELS: Record<string, string> = {
  close: "收盘",
  touch: "触板",
  one_word: "一字",
}

const BOARD_LABELS: Record<string, string> = {
  main: "主板",
  gem: "创业板",
  star: "科创板",
  bse: "北交所",
}

export interface DraftCondition {
  id: string
  field: string
  op: string
  value: unknown
  params: Record<string, unknown>
}

function displayValue(field: string, value: unknown): string {
  if (value === null || value === undefined || value === "") return "—"
  if (Array.isArray(value)) {
    return value.map((item) => displayValue(field, item)).join(" ~ ")
  }
  if (typeof value === "number") return formatFactorValue(field, value)
  if (typeof value === "boolean") return value ? "是" : "否"
  return String(value)
}

function paramCaption(item: CompareConditionResult): string {
  return Object.entries(item.params ?? {})
    .map(([name, value]) => {
      if (name === "mode") return MODE_LABELS[String(value)] ?? String(value)
      if (name === "days") return `${value}日`
      if (name === "years") return `${value}年`
      if (name === "min_obs_ratio") return `最小数据占比 ${value}`
      return `${name} ${value}`
    })
    .join(" · ")
}

function targetText(
  factor: FactorMeta | undefined,
  op: string,
  value: unknown,
): string {
  const rawValues = Array.isArray(value) ? value : [value]
  const texts = rawValues.map((item) => {
    if (factor?.value_type === "number") return formatFactorValue(factor.key, item)
    if (typeof item === "boolean") return item ? "是" : "否"
    if (factor?.choice_labels?.[String(item)]) {
      return factor.choice_labels[String(item)]
    }
    return String(item)
  })
  return texts.join(op === "between" ? " ~ " : " / ")
}

function withUnit(text: string, factor: FactorMeta | undefined): string {
  return factor?.unit && text !== "—" ? `${text}${factor.unit}` : text
}

function VerdictIcon({ verdict }: { verdict: CompareConditionResult["verdict"] }) {
  if (verdict === "pass") return <CheckCircle2 className="size-4 text-emerald-600" />
  if (verdict === "fail") return <XCircle className="size-4 text-destructive" />
  return <CircleHelp className="size-4 text-muted-foreground" />
}

function VerdictBadge({ verdict }: { verdict: CompareConditionResult["verdict"] }) {
  if (verdict === "pass") {
    return (
      <Badge variant="outline" className="border-emerald-600/40 text-emerald-700">
        满足
      </Badge>
    )
  }
  if (verdict === "fail") return <Badge variant="destructive">不满足</Badge>
  return <Badge variant="secondary">数据缺失</Badge>
}

function suggestThreshold(item: CompareConditionResult): number | null {
  if (item.verdict !== "fail") return null
  const actual = Number(item.actual)
  if (Number.isNaN(actual)) return null
  const round = (value: number) => Math.round(value * 100) / 100
  if (item.op === "lte" || item.op === "lt") {
    const current = Number(item.target)
    if (!Number.isFinite(current)) return null
    return round(Math.max(current, actual))
  }
  if (item.op === "gte" || item.op === "gt") {
    const current = Number(item.target)
    if (!Number.isFinite(current)) return null
    return round(Math.min(current, actual))
  }
  return null
}

function lineKey(condition: { field: string; op: string }) {
  return `${condition.field}::${condition.op}`
}

function roundValue(value: number) {
  return Math.round(value * 100) / 100
}

/** 把当前条件换成「能通过该标的」的版本，用于在对比卡片里试算。 */
function relaxCondition(
  condition: DraftCondition,
  item: CompareConditionResult,
  factor: FactorMeta | undefined,
): DraftCondition | null {
  if (!factor) return null
  const actual = Number(item.actual)
  if (Number.isNaN(actual)) return null
  const target = Number(condition.value)
  if (Number.isNaN(target)) return null

  if (condition.op === "lte" || condition.op === "lt") {
    return { ...condition, value: roundValue(Math.max(target, actual)) }
  }
  if (condition.op === "gte" || condition.op === "gt") {
    return { ...condition, value: roundValue(Math.min(target, actual)) }
  }
  if (condition.op === "eq" || condition.op === "between") {
    // 精确匹配类条件：改用实际值（between 则把实际值包成区间）。
    if (condition.op === "eq") {
      return { ...condition, value: typeof item.actual === "boolean" ? item.actual : actual }
    }
    return { ...condition, op: "lte", value: roundValue(actual) }
  }
  return null
}


function TrialConditionEditor({
  condition,
  factor,
  factors,
  onChange,
  onRemove,
}: {
  condition: DraftCondition
  factor: FactorMeta | undefined
  factors: FactorMeta[]
  onChange: (next: Partial<DraftCondition>) => void
  onRemove: () => void
}) {
  if (!factor) return null
  const numeric = factor.value_type === "number"
  const isBetween = condition.op === "between"
  const value = condition.value

  function toNumber(raw: string): unknown {
    return raw === "" ? "" : Number(raw)
  }

  return (
    <div className="rounded-md border bg-background p-2">
      <div className="grid gap-2 md:grid-cols-[minmax(0,1.4fr)_104px_minmax(0,1.3fr)_32px]">
        <Select
          value={condition.field}
          onValueChange={(field) => {
            const next = factors.find((item) => item.key === field)
            if (!next) return
            const op = next.ops[0]
            onChange({
              field,
              op,
              value:
                next.value_type === "bool"
                  ? false
                  : next.value_type === "string"
                    ? (next.choices[0] ?? "")
                    : 0,
              params: Object.fromEntries(
                next.params.map((param) => [param.name, param.default]),
              ),
            })
          }}
        >
          <SelectTrigger className="h-8 w-full text-xs">
            <SelectValue />
          </SelectTrigger>
          <SelectContent>
            {factors.map((item) => (
              <SelectItem key={item.key} value={item.key}>
                {item.label}
              </SelectItem>
            ))}
          </SelectContent>
        </Select>
        <Select
          value={condition.op}
          onValueChange={(op) => {
            let nextValue: unknown = value
            if (op === "between") {
              const single = Number(value)
              nextValue = [Number.isFinite(single) ? single : 0, Number.isFinite(single) ? single : 0]
            } else if (isBetween && Array.isArray(value)) {
              nextValue = Number(value[0])
            }
            onChange({ op, value: nextValue })
          }}
        >
          <SelectTrigger className="h-8 w-full text-xs">
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
            onValueChange={(next) => onChange({ value: next === "true" })}
          >
            <SelectTrigger className="h-8 w-full text-xs">
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
            onValueChange={(next) => onChange({ value: next })}
          >
            <SelectTrigger className="h-8 w-full text-xs">
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
        ) : numeric && isBetween ? (
          <div className="flex items-center gap-1">
            <Input
              className="h-8 text-xs"
              type="number"
              step="any"
              value={String((value as unknown[])[0] ?? "")}
              onChange={(event) =>
                onChange({
                  value: [
                    toNumber(event.target.value),
                    (value as unknown[])[1] ?? "",
                  ],
                })
              }
            />
            <span className="text-xs text-muted-foreground">至</span>
            <Input
              className="h-8 text-xs"
              type="number"
              step="any"
              value={String((value as unknown[])[1] ?? "")}
              onChange={(event) =>
                onChange({
                  value: [
                    (value as unknown[])[0] ?? "",
                    toNumber(event.target.value),
                  ],
                })
              }
            />
          </div>
        ) : numeric ? (
          <Input
            className="h-8 text-xs"
            type="number"
            step="any"
            value={String(value ?? "")}
            onChange={(event) => onChange({ value: toNumber(event.target.value) })}
          />
        ) : (
          <Input
            className="h-8 text-xs"
            value={String(value ?? "")}
            onChange={(event) => onChange({ value: event.target.value })}
          />
        )}

        <Button
          variant="ghost"
          size="icon-sm"
          className="text-muted-foreground"
          onClick={onRemove}
          aria-label="删除试算条件"
        >
          <XCircle className="size-4" />
        </Button>
      </div>
      {factor.params.length > 0 ? (
        <div className="mt-1.5 flex flex-wrap gap-2">
          {factor.params.map((param) => {
            const current = condition.params[param.name] ?? param.default
            if (param.type === "str" && param.choices) {
              return (
                <div key={param.name} className="flex items-center gap-1">
                  <span className="text-[11px] text-muted-foreground">
                    {param.label}
                  </span>
                  <Select
                    value={String(current)}
                    onValueChange={(next) =>
                      onChange({
                        params: { ...condition.params, [param.name]: next },
                      })
                    }
                  >
                    <SelectTrigger className="h-7 w-24 text-xs">
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
                </div>
              )
            }
            return (
              <div key={param.name} className="flex items-center gap-1">
                <span className="text-[11px] text-muted-foreground">
                  {param.label}
                </span>
                <Input
                  className="h-7 w-20 text-xs"
                  type="number"
                  step="any"
                  value={String(current)}
                  onChange={(event) =>
                    onChange({
                      params: {
                        ...condition.params,
                        [param.name]: toNumber(event.target.value),
                      },
                    })
                  }
                />
              </div>
            )
          })}
        </div>
      ) : null}
    </div>
  )
}

export function ScreenCompareCard({
  conditions,
  logic,
  factors,
  factorMap,
  onLogicChange,
  onAdjustCondition,
}: {
  conditions: DraftCondition[]
  logic: "and" | "or"
  factors: FactorMeta[]
  factorMap: Map<string, FactorMeta>
  onLogicChange: (logic: "and" | "or") => void
  onAdjustCondition: (conditionId: string, next: DraftCondition) => void
}) {
  const [symbol, setSymbol] = useState("")
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [result, setResult] = useState<CompareResponse | null>(null)
  const [detailOpen, setDetailOpen] = useState(false)
  // 试算用的条件草稿：只在本卡片内生效，点「应用到筛选」才写回页面条件。
  const [trial, setTrial] = useState<DraftCondition[]>([])
  const [trialDirty, setTrialDirty] = useState(false)

  const effectiveConditions = trialDirty ? trial : conditions

  function startTrial() {
    if (!trialDirty) {
      setTrial(conditions.map((item) => ({ ...item, params: { ...item.params } })))
      setTrialDirty(true)
    }
  }

  function updateTrial(id: string, next: Partial<DraftCondition>) {
    setTrial((previous) =>
      previous.map((item) => (item.id === id ? { ...item, ...next } : item)),
    )
  }

  function removeTrial(id: string) {
    setTrial((previous) => previous.filter((item) => item.id !== id))
  }

  function resetTrial() {
    setTrial([])
    setTrialDirty(false)
    setError(null)
  }

  function applyToScreen() {
    if (!trialDirty) return
    for (const next of trial) {
      onAdjustCondition(next.id, next)
    }
    setTrialDirty(false)
    setTrial([])
    setError("已把试算条件应用到上方筛选")
  }

  function relaxAll() {
    if (!result) return
    startTrial()
    const base = trialDirty ? trial : conditions
    let next = base.map((item) => ({ ...item, params: { ...item.params } }))
    for (const item of result.conditions) {
      if (item.verdict !== "fail") continue
      const condition = next.find((entry) => lineKey(entry) === lineKey(item))
      if (!condition) continue
      const relaxed = relaxCondition(condition, item, factorMap.get(item.field))
      if (relaxed) next = next.map((entry) => (entry.id === relaxed.id ? relaxed : entry))
    }
    setTrial(next)
    setTrialDirty(true)
    setError("已把不满足的条件放宽到该标的实际值，可点「重新测试」查看结果")
  }

  async function compare(options: { conditions?: DraftCondition[]; logic?: "and" | "or" } = {}) {
    const query = symbol.trim()
    const active = options.conditions ?? effectiveConditions
    const activeLogic = options.logic ?? logic
    if (!query) {
      setError("请输入名称或代码")
      return
    }
    if (active.length === 0) {
      setError("请先添加筛选条件")
      return
    }
    setError(null)
    setLoading(true)
    try {
      let target = query
      if (/^\d{6}\.\w{2}$/i.test(query)) {
        target = query.toUpperCase()
      } else {
        const found = await api.searchInstruments(query)
        const exact = /^\d{6}$/.test(query)
          ? found.items.find((item) => item.code === query)
          : found.items[0]
        if (!exact) {
          setError(`没有找到「${query}」对应的标的`)
          return
        }
        target = exact.symbol
      }
      const response = await api.compareScreen({
        symbol: target,
        logic: activeLogic,
        conditions: active.map((condition) => ({
          field: condition.field,
          op: condition.op,
          value: condition.value,
          params: condition.params,
        })),
      })
      setResult(response)
    } catch (err) {
      if (err instanceof ApiError) {
        setError(err.message)
      } else {
        setError(err instanceof Error ? err.message : "对比失败，请稍后重试")
      }
    } finally {
      setLoading(false)
    }
  }

  const failedItems =
    result?.conditions.filter((item) => item.verdict === "fail") ?? []

  function applySuggestion(item: CompareConditionResult) {
    const suggested = suggestThreshold(item)
    if (suggested === null) return
    startTrial()
    const base = trialDirty ? trial : conditions
    const condition = base.find((entry) => lineKey(entry) === lineKey(item))
    if (!condition) return
    setTrial((previous) =>
      previous.map((entry) =>
        entry.id === condition.id ? { ...entry, value: suggested } : entry,
      ),
    )
    setError("已按该标的实际值调整，可点「重新测试」查看结果")
  }

  return (
    <Card id="compare-section">
      <CardHeader className="pb-3">
        <CardTitle className="text-base">标的测试</CardTitle>
        <CardDescription>
          输入名称或代码测试当前条件；可在这里直接修改条件并重新测试，确认后再应用到上方筛选。
          测试不计入筛选配额。
        </CardDescription>
      </CardHeader>
      <CardContent className="space-y-3">
        <div className="flex flex-col gap-2 sm:flex-row sm:items-center">
          <Input
            className="sm:max-w-sm"
            placeholder="输入名称或代码，例如 600519 / 贵州茅台"
            value={symbol}
            onChange={(event) => setSymbol(event.target.value)}
            onKeyDown={(event) => {
              if (event.key === "Enter") void compare()
            }}
          />
          <Button onClick={() => void compare()} disabled={loading}>
            {loading ? (
              <Loader2 className="size-4 animate-spin" />
            ) : (
              <ScanSearch className="size-4" />
            )}
            {trialDirty ? "重新测试" : "测试条件"}
          </Button>
          <Select
            value={trialDirty ? (result?.logic ?? logic) : logic}
            onValueChange={(value) => {
              const next = value as "and" | "or"
              if (trialDirty) {
                // 试算模式下只改本卡片逻辑并立即重测。
                onLogicChange(next)
                void compare({ conditions: trial, logic: next })
              } else {
                onLogicChange(next)
              }
            }}
          >
            <SelectTrigger className="h-8 w-28">
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value="and">全部满足</SelectItem>
              <SelectItem value="or">任一满足</SelectItem>
            </SelectContent>
          </Select>
          {trialDirty ? (
            <Button variant="outline" size="sm" onClick={resetTrial}>
              <RotateCcw className="size-4" />
              放弃修改
            </Button>
          ) : null}
        </div>

        {trialDirty ? (
          <div className="rounded-lg border border-dashed p-3">
            <div className="flex flex-wrap items-center justify-between gap-2">
              <span className="text-xs font-medium">
                试算条件（修改中，尚未应用到上方筛选）
              </span>
              <div className="flex gap-2">
                <Button variant="outline" size="sm" onClick={relaxAll}>
                  一键放宽到实际值
                </Button>
                <Button size="sm" onClick={applyToScreen}>
                  <Pencil className="size-3.5" />
                  应用到筛选
                </Button>
              </div>
            </div>
            <div className="mt-2 space-y-2">
              {trial.map((condition) => (
                <TrialConditionEditor
                  key={condition.id}
                  condition={condition}
                  factor={factorMap.get(condition.field)}
                  factors={factors}
                  onChange={(next) => updateTrial(condition.id, next)}
                  onRemove={() => removeTrial(condition.id)}
                />
              ))}
            </div>
          </div>
        ) : (
          <div className="flex flex-wrap items-center gap-2">
            <Button variant="outline" size="sm" onClick={startTrial}>
              <Pencil className="size-3.5" />
              修改条件测试
            </Button>
            {result && failedItems.length > 0 ? (
              <Button variant="outline" size="sm" onClick={relaxAll}>
                一键放宽到实际值
              </Button>
            ) : null}
            <span className="text-xs text-muted-foreground">
              共 {conditions.length} 条条件
            </span>
          </div>
        )}

        {error ? <p className="text-sm text-destructive">{error}</p> : null}

        {result ? (
          <div className="rounded-lg border bg-background">
            <div className="flex flex-wrap items-center justify-between gap-2 border-b px-3 py-2">
              <div className="flex items-center gap-2">
                <span className="font-medium">{result.name}</span>
                <span className="text-xs text-muted-foreground">
                  {result.symbol}
                  {result.board
                    ? ` · ${BOARD_LABELS[result.board] ?? result.board}`
                    : ""}
                  {result.industry ? ` · ${result.industry}` : ""}
                </span>
              </div>
              <div className="flex items-center gap-2">
                {result.matches ? (
                  <Badge className="bg-emerald-600 hover:bg-emerald-600">
                    满足全部条件
                  </Badge>
                ) : (
                  <Badge variant="destructive">
                    {result.failed} 条不满足
                    {result.unknown > 0 ? ` · ${result.unknown} 条缺数据` : ""}
                  </Badge>
                )}
                <Button variant="outline" size="sm" onClick={() => setDetailOpen(true)}>
                  查看明细
                </Button>
              </div>
            </div>
            <div className="space-y-1.5 px-3 py-2">
              {result.conditions.map((item, index) => {
                const factor = factorMap.get(item.field)
                const suggested = suggestThreshold(item)
                const condition = conditions.find(
                  (entry) => entry.field === item.field && entry.op === item.op,
                )
                return (
                  <div
                    key={`${item.field}-${item.op}-${index}`}
                    className="flex flex-wrap items-center gap-x-2 gap-y-1 text-xs"
                  >
                    <VerdictIcon verdict={item.verdict} />
                    <span className="min-w-28 font-medium">{item.label}</span>
                    <span className="text-muted-foreground">
                      {OP_LABELS[item.op] ?? item.op}{" "}
                      {withUnit(targetText(factor, item.op, item.target), factor)}
                    </span>
                    <span className="text-muted-foreground">实际</span>
                    <span
                      className={
                        item.verdict === "fail" ? "font-medium text-destructive" : ""
                      }
                    >
                      {withUnit(displayValue(item.field, item.actual), factor)}
                    </span>
                    <span className="text-muted-foreground">{item.reason}</span>
                    {item.detail ? (
                      <span className="text-muted-foreground">
                        （近 {item.detail.days} 日涨停 {item.detail.up} 次 / 跌停{" "}
                        {item.detail.down} 次）
                      </span>
                    ) : null}
                    {suggested !== null && condition ? (
                      <Button
                        variant="link"
                        size="sm"
                        className="h-auto px-0 text-xs"
                        onClick={() => applySuggestion(item)}
                      >
                        改为 {formatFactorValue(item.field, suggested)}
                      </Button>
                    ) : null}
                  </div>
                )
              })}
            </div>
            {failedItems.length > 0 ? (
              <div className="border-t px-3 py-2 text-xs text-muted-foreground">
                提示：可点「改为 …」把阈值放宽到该标的的实际值，再运行筛选复核效果。
              </div>
            ) : null}
          </div>
        ) : null}
      </CardContent>

      <Dialog open={detailOpen} onOpenChange={setDetailOpen}>
        <DialogContent className="sm:max-w-2xl">
          <DialogHeader>
            <DialogTitle>
              对比明细{result ? ` · ${result.name}（${result.symbol}）` : ""}
            </DialogTitle>
            <DialogDescription>
              数据日期 {result?.as_of ?? "—"} · {result?.passed ?? 0} 条满足 ·{" "}
              {result?.failed ?? 0} 条不满足 · {result?.unknown ?? 0} 条数据缺失
            </DialogDescription>
          </DialogHeader>
          <div className="max-h-[60vh] space-y-2 overflow-y-auto">
            {(result?.conditions ?? []).map((item, index) => {
              const factor = factorMap.get(item.field)
              return (
                <div
                  key={`detail-${item.field}-${item.op}-${index}`}
                  className="rounded-lg border p-3 text-sm"
                >
                  <div className="flex flex-wrap items-center justify-between gap-2">
                    <div className="flex items-center gap-2">
                      <VerdictIcon verdict={item.verdict} />
                      <span className="font-medium">{item.label}</span>
                      <Badge variant="outline">{item.category_label}</Badge>
                    </div>
                    <VerdictBadge verdict={item.verdict} />
                  </div>
                  <div className="mt-1.5 grid gap-1 text-xs text-muted-foreground sm:grid-cols-2">
                    <span>
                      目标 {OP_LABELS[item.op] ?? item.op}{" "}
                      {withUnit(targetText(factor, item.op, item.target), factor)}
                    </span>
                    <span>
                      实际 {withUnit(displayValue(item.field, item.actual), factor)}
                    </span>
                    {item.gap !== null &&
                    item.gap !== undefined &&
                    Number(item.gap) > 0 ? (
                      <span>
                        差距 {withUnit(String(Number(item.gap.toFixed(4))), factor)}
                      </span>
                    ) : null}
                    {paramCaption(item) ? <span>口径 {paramCaption(item)}</span> : null}
                    {item.detail ? (
                      <span>
                        近 {item.detail.days} 日涨停 {item.detail.up} 次、跌停{" "}
                        {item.detail.down} 次
                      </span>
                    ) : null}
                  </div>
                </div>
              )
            })}
          </div>
        </DialogContent>
      </Dialog>
    </Card>
  )
}
