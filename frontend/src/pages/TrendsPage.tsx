import { useEffect, useState } from "react"
import { useQuery } from "@tanstack/react-query"
import {
  Activity,
  AlertTriangle,
  Loader2,
  Search,
} from "lucide-react"
import { useSearchParams } from "react-router-dom"

import { StockTechnicalChart } from "@/components/StockTechnicalChart"
import { Alert, AlertDescription } from "@/components/ui/alert"
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
import { Skeleton } from "@/components/ui/skeleton"
import { Tabs, TabsList, TabsTrigger } from "@/components/ui/tabs"
import { api } from "@/lib/api"
import type { InstrumentSearchItem } from "@/lib/types"

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

export function TrendsPage() {
  const [searchParams, setSearchParams] = useSearchParams()
  const symbol = searchParams.get("symbol") ?? ""
  const [searchInput, setSearchInput] = useState("")
  const [searchTerm, setSearchTerm] = useState("")
  const [period, setPeriod] = useState<keyof typeof PERIOD_LABELS>("daily")

  useEffect(() => {
    const timer = window.setTimeout(() => {
      setSearchTerm(searchInput.trim())
    }, 250)
    return () => window.clearTimeout(timer)
  }, [searchInput])

  const searchQuery = useQuery({
    queryKey: ["instrument-search", searchTerm],
    queryFn: () => api.searchInstruments(searchTerm),
    enabled: searchTerm.length > 0,
    staleTime: 60_000,
  })
  const technicalQuery = useQuery({
    queryKey: ["instrument-technical", symbol],
    queryFn: () => api.instrumentTechnical(symbol),
    enabled: Boolean(symbol),
    staleTime: 5 * 60_000,
    retry: 1,
  })

  const technical = technicalQuery.data
  const periodData = technical?.periods[period]
  const results = searchQuery.data?.items ?? []

  function selectInstrument(item: InstrumentSearchItem) {
    setSearchParams({ symbol: item.symbol })
    setSearchInput("")
    setSearchTerm("")
  }

  return (
    <div className="space-y-6">
      <div>
        <h1 className="flex items-center gap-2 text-2xl font-semibold tracking-tight">
          <Activity className="size-5" />
          标的走势
        </h1>
        <p className="mt-1 text-sm text-muted-foreground">
          输入名称或代码，查询任意标的的日线、周线、月线及技术指标。
        </p>
      </div>

      <Card>
        <CardHeader className="pb-3">
          <CardTitle className="text-base">标的查询</CardTitle>
          <CardDescription>
            支持名称、代码或完整 symbol，例如 600000、浦发银行、600000.SH。
          </CardDescription>
        </CardHeader>
        <CardContent className="space-y-3">
          <form
            className="flex max-w-xl items-center gap-2"
            onSubmit={(event) => {
              event.preventDefault()
              setSearchTerm(searchInput.trim())
            }}
          >
            <div className="relative flex-1">
              <Search className="pointer-events-none absolute top-1/2 left-2.5 size-3.5 -translate-y-1/2 text-muted-foreground" />
              <Input
                className="pl-8"
                placeholder="输入名称或代码"
                value={searchInput}
                onChange={(event) => setSearchInput(event.target.value)}
              />
            </div>
            <Button type="submit" variant="outline">
              查询
            </Button>
          </form>

          {searchQuery.isFetching ? (
            <div className="flex items-center gap-2 text-xs text-muted-foreground">
              <Loader2 className="size-3.5 animate-spin" />
              正在搜索
            </div>
          ) : null}
          {searchQuery.isError ? (
            <Alert variant="destructive">
              <AlertTriangle className="size-4" />
              <AlertDescription>
                {searchQuery.error instanceof Error
                  ? searchQuery.error.message
                  : "搜索失败"}
              </AlertDescription>
            </Alert>
          ) : null}
          {searchTerm && !searchQuery.isFetching && results.length === 0 ? (
            <div className="text-xs text-muted-foreground">没有找到匹配的标的。</div>
          ) : null}
          {results.length > 0 ? (
            <div className="grid gap-2 sm:grid-cols-2 xl:grid-cols-3">
              {results.map((item) => (
                <button
                  key={item.symbol}
                  type="button"
                  className="rounded-lg border bg-background px-3 py-2 text-left transition-colors hover:bg-muted"
                  onClick={() => selectInstrument(item)}
                >
                  <div className="flex items-center justify-between gap-2">
                    <span className="truncate text-sm font-medium">
                      {item.name}
                    </span>
                    <span className="text-xs text-muted-foreground">
                      {item.symbol}
                    </span>
                  </div>
                  <div className="mt-0.5 truncate text-[11px] text-muted-foreground">
                    {BOARD_LABELS[item.board ?? ""] ?? item.board ?? "—"}
                    {item.industry ? ` · ${item.industry}` : ""}
                  </div>
                </button>
              ))}
            </div>
          ) : null}
        </CardContent>
      </Card>

      {!symbol ? (
        <Card>
          <CardContent className="py-16 text-center text-sm text-muted-foreground">
            搜索并选择一个标的查看走势。
          </CardContent>
        </Card>
      ) : technicalQuery.isLoading ? (
        <Card>
          <CardContent className="space-y-3 py-6">
            <Skeleton className="h-8 w-56" />
            <Skeleton className="h-16 w-full" />
            <Skeleton className="h-96 w-full" />
          </CardContent>
        </Card>
      ) : technicalQuery.isError ? (
        <Alert variant="destructive">
          <AlertTriangle className="size-4" />
          <AlertDescription>
            {technicalQuery.error instanceof Error
              ? technicalQuery.error.message
              : "走势加载失败"}
          </AlertDescription>
        </Alert>
      ) : technical ? (
        <Card>
          <CardHeader className="pb-3">
            <div className="flex flex-col gap-3 sm:flex-row sm:items-start sm:justify-between">
              <div>
                <CardTitle className="flex flex-wrap items-center gap-2 text-xl">
                  {technical.name}
                  <Badge variant="secondary">{technical.symbol}</Badge>
                </CardTitle>
                <CardDescription className="mt-1">
                  数据截至 {technical.as_of}
                  {technical.message ? ` · ${technical.message}` : ""}
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
            {periodData?.latest ? (
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
                  <Metric
                    label="MACD 柱"
                    value={numberText(periodData.latest.macd.hist, 3)}
                  />
                  <Metric
                    label="DIF / DEA"
                    value={`${numberText(periodData.latest.macd.dif, 3)} / ${numberText(periodData.latest.macd.dea, 3)}`}
                  />
                  <Metric
                    label="K / D / J"
                    value={`${numberText(periodData.latest.kdj.k)} / ${numberText(periodData.latest.kdj.d)} / ${numberText(periodData.latest.kdj.j)}`}
                  />
                  <Metric
                    label="EXPMA20 / 50"
                    value={`${numberText(periodData.latest.expma20)} / ${numberText(periodData.latest.expma50)}`}
                  />
                  <Metric
                    label="RSI6 / 12 / 24"
                    value={`${numberText(periodData.latest.rsi.rsi6)} / ${numberText(periodData.latest.rsi.rsi12)} / ${numberText(periodData.latest.rsi.rsi24)}`}
                  />
                  <Metric
                    label="周期数 / 截止"
                    value={`${periodData.bar_count} · ${periodData.latest.trade_date}`}
                  />
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
                {technical.message ?? "暂无足够历史数据绘制图表"}
              </div>
            )}
          </CardContent>
        </Card>
      ) : null}
    </div>
  )
}
