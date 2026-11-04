import { useCallback, useEffect, useRef, useState } from "react"
import {
  AlertTriangle,
  ChevronUp,
  Loader2,
  RotateCcw,
  ScrollText,
  Search,
  Trash2,
} from "lucide-react"
import { toast } from "sonner"

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
import { Label } from "@/components/ui/label"
import {
  Select,
  SelectContent,
  SelectGroup,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select"
import { Skeleton } from "@/components/ui/skeleton"
import { Switch } from "@/components/ui/switch"
import { api } from "@/lib/api"
import type { WorkerLogItem } from "@/lib/types"

const LIVE_LOG_LIMIT = 200
const LIVE_MAX_LINES = 400
const LIVE_POLL_MS = 2_000

const QUERY_LIMIT = 200
const QUERY_MAX_LINES = 1_000
const QUERY_POLL_MS = 2_000

const LEVEL_OPTIONS = [
  { value: "all", label: "全部" },
  { value: "INFO", label: "INFO" },
  { value: "WARNING", label: "WARNING" },
  { value: "ERROR", label: "ERROR" },
]

interface WorkerLogFilters {
  level: string
  q: string
  requestId: number | null
}

const EMPTY_FILTERS: WorkerLogFilters = { level: "all", q: "", requestId: null }

function toApiParams(filters: WorkerLogFilters) {
  return {
    level: filters.level === "all" ? undefined : filters.level,
    q: filters.q || undefined,
    requestId: filters.requestId ?? undefined,
  }
}

function errorMessage(error: unknown): string {
  return error instanceof Error && error.message ? error.message : "日志加载失败"
}

function appendLogs(
  previous: WorkerLogItem[],
  incoming: WorkerLogItem[],
  maxLines: number,
): WorkerLogItem[] {
  if (incoming.length === 0) return previous
  const seen = new Set(previous.map((item) => item.id))
  const fresh = incoming.filter((item) => !seen.has(item.id))
  if (fresh.length === 0) return previous
  const merged = previous.concat(fresh)
  return merged.length > maxLines ? merged.slice(merged.length - maxLines) : merged
}

function formatTime(value: string): string {
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return value
  return date.toLocaleTimeString("zh-CN", { hour12: false })
}

function formatLogDateTime(value: string): string {
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return value
  const month = String(date.getMonth() + 1).padStart(2, "0")
  const day = String(date.getDate()).padStart(2, "0")
  return `${month}-${day} ${formatTime(value)}`
}

function LevelBadge({ level }: { level: WorkerLogItem["level"] | string }) {
  const tone =
    level === "ERROR" || level === "CRITICAL"
      ? "bg-destructive/10 text-destructive"
      : level === "WARNING"
        ? "bg-amber-500/10 text-amber-600 dark:text-amber-400"
        : "bg-muted text-muted-foreground"
  return (
    <Badge
      variant="secondary"
      className={`h-4 shrink-0 px-1 text-[10px] leading-none ${tone}`}
    >
      {level}
    </Badge>
  )
}

export function WorkerLogPanel({
  requestId,
  running,
}: {
  requestId: number
  running: boolean
}) {
  const [items, setItems] = useState<WorkerLogItem[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [autoScroll, setAutoScroll] = useState(true)
  const scrollRef = useRef<HTMLDivElement | null>(null)
  const stickToBottomRef = useRef(true)
  const lastIdRef = useRef(0)
  const loadedRequestIdRef = useRef<number | null>(null)
  const wasRunningRef = useRef(running)

  useEffect(() => {
    let active = true
    loadedRequestIdRef.current = null
    lastIdRef.current = 0
    stickToBottomRef.current = true
    setItems([])
    setLoading(true)
    setError(null)

    api
      .workerLogs({ requestId, limit: LIVE_LOG_LIMIT })
      .then((response) => {
        if (!active) return
        setItems(response.items.slice(-LIVE_MAX_LINES))
        lastIdRef.current =
          response.items.length > 0
            ? response.items[response.items.length - 1].id
            : 0
      })
      .catch((error: unknown) => {
        if (!active) return
        setError(errorMessage(error))
      })
      .finally(() => {
        if (!active) return
        loadedRequestIdRef.current = requestId
        setLoading(false)
      })

    return () => {
      active = false
    }
  }, [requestId])

  useEffect(() => {
    if (!running || loadedRequestIdRef.current !== requestId) return
    let active = true
    let timer: number | undefined

    async function poll() {
      try {
        const response = await api.workerLogs({
          requestId,
          afterId: lastIdRef.current > 0 ? lastIdRef.current : undefined,
          limit: LIVE_LOG_LIMIT,
        })
        if (!active) return
        setError(null)
        if (response.items.length > 0) {
          lastIdRef.current = response.items[response.items.length - 1].id
          setItems((previous) =>
            appendLogs(previous, response.items, LIVE_MAX_LINES),
          )
        }
      } catch (error) {
        if (!active) return
        setError(errorMessage(error))
      } finally {
        if (active) {
          timer = window.setTimeout(() => {
            void poll()
          }, LIVE_POLL_MS)
        }
      }
    }

    timer = window.setTimeout(() => {
      void poll()
    }, LIVE_POLL_MS)

    return () => {
      active = false
      if (timer !== undefined) window.clearTimeout(timer)
    }
  }, [running, requestId, loading])

  useEffect(() => {
    const wasRunning = wasRunningRef.current
    wasRunningRef.current = running
    if (!wasRunning || running) return
    if (loadedRequestIdRef.current !== requestId) return
    let active = true
    api
      .workerLogs({
        requestId,
        afterId: lastIdRef.current > 0 ? lastIdRef.current : undefined,
        limit: LIVE_LOG_LIMIT,
      })
      .then((response) => {
        if (!active || response.items.length === 0) return
        lastIdRef.current = response.items[response.items.length - 1].id
        setItems((previous) =>
          appendLogs(previous, response.items, LIVE_MAX_LINES),
        )
      })
      .catch(() => {
        // 收尾刷新失败时保留已有日志与错误提示
      })
    return () => {
      active = false
    }
  }, [running, requestId])

  useEffect(() => {
    if (!autoScroll || !stickToBottomRef.current) return
    const element = scrollRef.current
    if (!element) return
    element.scrollTop = element.scrollHeight
  }, [items, autoScroll, loading])

  function handleScroll() {
    const element = scrollRef.current
    if (!element) return
    stickToBottomRef.current =
      element.scrollHeight - element.scrollTop - element.clientHeight < 24
  }

  function handleAutoScrollChange(checked: boolean) {
    setAutoScroll(checked)
    if (!checked) return
    stickToBottomRef.current = true
    const element = scrollRef.current
    if (element) element.scrollTop = element.scrollHeight
  }

  function clearLogs() {
    setItems([])
    setError(null)
  }

  return (
    <div className="space-y-2 border-t pt-3">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div className="flex flex-wrap items-center gap-2">
          <span className="flex items-center gap-1.5 text-sm font-medium">
            <ScrollText className="size-4 text-muted-foreground" />
            数据更新日志
          </span>
          <Badge variant="secondary">{items.length} 条</Badge>
          {running ? (
            <Badge variant="outline" className="gap-1">
              <Loader2 className="size-3 animate-spin" />
              实时
            </Badge>
          ) : null}
        </div>
        <div className="flex items-center gap-2">
          <div className="flex items-center gap-1.5">
            <Switch
              checked={autoScroll}
              onCheckedChange={handleAutoScrollChange}
              aria-label="自动滚动"
            />
            <Label className="text-xs text-muted-foreground">自动滚动</Label>
          </div>
          <Button
            variant="ghost"
            size="sm"
            onClick={clearLogs}
            disabled={items.length === 0}
          >
            <Trash2 className="size-3.5" />
            清空
          </Button>
        </div>
      </div>

      {error ? (
        <Alert variant="destructive" className="px-3 py-2">
          <AlertTriangle className="size-4" />
          <AlertDescription className="text-xs">
            日志加载失败：{error}
          </AlertDescription>
        </Alert>
      ) : null}

      {loading && items.length === 0 ? (
        <div className="space-y-1.5 rounded-lg border p-2">
          {Array.from({ length: 3 }).map((_, index) => (
            <Skeleton key={`log-skeleton-${index}`} className="h-4 w-full" />
          ))}
        </div>
      ) : (
        <div
          ref={scrollRef}
          onScroll={handleScroll}
          className="max-h-64 overflow-y-auto rounded-lg border bg-muted/20 p-2 font-mono text-[11px] leading-5"
        >
          {items.length === 0 ? (
            <div className="py-6 text-center text-xs text-muted-foreground">
              暂无日志
            </div>
          ) : (
            items.map((item) => (
              <div key={item.id} className="flex items-start gap-1.5">
                <span className="shrink-0 text-muted-foreground">
                  {formatTime(item.created_at)}
                </span>
                <LevelBadge level={item.level} />
                <span className="shrink-0 max-w-40 truncate text-muted-foreground">
                  {item.logger}
                </span>
                <span className="min-w-0 break-words whitespace-pre-wrap">
                  {item.message}
                </span>
              </div>
            ))
          )}
        </div>
      )}
    </div>
  )
}

export function WorkerLogQueryCard() {
  const [level, setLevel] = useState("all")
  const [keyword, setKeyword] = useState("")
  const [requestIdInput, setRequestIdInput] = useState("")
  const [applied, setApplied] = useState<WorkerLogFilters>(EMPTY_FILTERS)
  const [items, setItems] = useState<WorkerLogItem[]>([])
  const [hasMore, setHasMore] = useState(false)
  const [loading, setLoading] = useState(true)
  const [olderLoading, setOlderLoading] = useState(false)
  const [autoRefresh, setAutoRefresh] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const scrollRef = useRef<HTMLDivElement | null>(null)
  const stickToBottomRef = useRef(true)
  const lastIdRef = useRef(0)
  const prependAnchorRef = useRef<{ height: number; top: number } | null>(null)

  const fetchLatest = useCallback(async (filters: WorkerLogFilters) => {
    setLoading(true)
    setError(null)
    stickToBottomRef.current = true
    try {
      const response = await api.workerLogs({
        ...toApiParams(filters),
        limit: QUERY_LIMIT,
      })
      setItems(response.items.slice(-QUERY_MAX_LINES))
      setHasMore(response.has_more)
    } catch (error) {
      const message = errorMessage(error)
      setError(message)
      toast.error(message)
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    void fetchLatest(EMPTY_FILTERS)
  }, [fetchLatest])

  useEffect(() => {
    lastIdRef.current =
      items.length > 0 ? items[items.length - 1].id : 0
  }, [items])

  useEffect(() => {
    const element = scrollRef.current
    if (!element) return
    if (prependAnchorRef.current) {
      const anchor = prependAnchorRef.current
      prependAnchorRef.current = null
      element.scrollTop = element.scrollHeight - anchor.height + anchor.top
      return
    }
    if (!stickToBottomRef.current) return
    element.scrollTop = element.scrollHeight
  }, [items])

  useEffect(() => {
    if (!autoRefresh) return
    let active = true
    let timer: number | undefined

    async function poll() {
      try {
        const response = await api.workerLogs({
          ...toApiParams(applied),
          afterId: lastIdRef.current > 0 ? lastIdRef.current : undefined,
          limit: QUERY_LIMIT,
        })
        if (!active) return
        setError(null)
        if (response.items.length > 0) {
          setItems((previous) =>
            appendLogs(previous, response.items, QUERY_MAX_LINES),
          )
        }
      } catch (error) {
        if (!active) return
        setError(errorMessage(error))
      } finally {
        if (active) {
          timer = window.setTimeout(() => {
            void poll()
          }, QUERY_POLL_MS)
        }
      }
    }

    timer = window.setTimeout(() => {
      void poll()
    }, QUERY_POLL_MS)

    return () => {
      active = false
      if (timer !== undefined) window.clearTimeout(timer)
    }
  }, [autoRefresh, applied])

  function applyFilters(filters: WorkerLogFilters) {
    setApplied(filters)
    void fetchLatest(filters)
  }

  function submitQuery() {
    const trimmed = requestIdInput.trim()
    let requestId: number | null = null
    if (trimmed) {
      const parsed = Number(trimmed)
      if (!Number.isInteger(parsed) || parsed <= 0) {
        toast.error("请求 ID 必须是正整数")
        return
      }
      requestId = parsed
    }
    applyFilters({ level, q: keyword.trim(), requestId })
  }

  function resetFilters() {
    setLevel("all")
    setKeyword("")
    setRequestIdInput("")
    setAutoRefresh(false)
    applyFilters(EMPTY_FILTERS)
  }

  function handleAutoRefreshChange(checked: boolean) {
    setAutoRefresh(checked)
    if (!checked) return
    stickToBottomRef.current = true
    const element = scrollRef.current
    if (element) element.scrollTop = element.scrollHeight
  }

  function handleScroll() {
    const element = scrollRef.current
    if (!element) return
    stickToBottomRef.current =
      element.scrollHeight - element.scrollTop - element.clientHeight < 24
  }

  function clearLogs() {
    setItems([])
    setError(null)
  }

  async function loadOlder() {
    if (olderLoading || loading || items.length === 0) return
    const firstId = items[0].id
    const element = scrollRef.current
    const anchor = element
      ? { height: element.scrollHeight, top: element.scrollTop }
      : null
    setOlderLoading(true)
    try {
      const response = await api.workerLogs({
        ...toApiParams(applied),
        beforeId: firstId,
        limit: QUERY_LIMIT,
      })
      setHasMore(response.has_more)
      if (response.items.length > 0) {
        const incoming = response.items
        prependAnchorRef.current = anchor
        setItems((previous) => {
          const seen = new Set(previous.map((item) => item.id))
          const older = incoming.filter((item) => !seen.has(item.id))
          const merged = older.concat(previous)
          return merged.length > QUERY_MAX_LINES
            ? merged.slice(merged.length - QUERY_MAX_LINES)
            : merged
        })
      }
    } catch (error) {
      const message = errorMessage(error)
      setError(message)
      toast.error(message)
    } finally {
      setOlderLoading(false)
    }
  }

  return (
    <Card>
      <CardHeader>
        <CardTitle className="text-base">运行日志</CardTitle>
        <CardDescription>
          后台 worker、同步任务与 LLM 对话/工具调用的运行日志，可按级别、
          关键词与请求 ID 检索；开启自动刷新后每 2 秒追加最新日志。
        </CardDescription>
      </CardHeader>
      <CardContent className="space-y-3">
        <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-[160px_minmax(0,1fr)_150px_auto] xl:items-end">
          <div className="space-y-1.5">
            <Label className="text-xs">级别</Label>
            <Select value={level} onValueChange={setLevel}>
              <SelectTrigger className="w-full">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                <SelectGroup>
                  {LEVEL_OPTIONS.map((option) => (
                    <SelectItem key={option.value} value={option.value}>
                      {option.label}
                    </SelectItem>
                  ))}
                </SelectGroup>
              </SelectContent>
            </Select>
          </div>
          <div className="space-y-1.5">
            <Label className="text-xs">关键词</Label>
            <Input
              placeholder="搜索日志内容或来源"
              value={keyword}
              onChange={(event) => setKeyword(event.target.value)}
              onKeyDown={(event) => {
                if (event.key === "Enter") submitQuery()
              }}
            />
          </div>
          <div className="space-y-1.5">
            <Label className="text-xs">请求 ID</Label>
            <Input
              type="number"
              min={1}
              placeholder="可选"
              value={requestIdInput}
              onChange={(event) => setRequestIdInput(event.target.value)}
              onKeyDown={(event) => {
                if (event.key === "Enter") submitQuery()
              }}
            />
          </div>
          <div className="flex flex-wrap items-center gap-2">
            <Button onClick={submitQuery} disabled={loading}>
              {loading ? (
                <Loader2 className="size-4 animate-spin" />
              ) : (
                <Search className="size-4" />
              )}
              查询
            </Button>
            <Button variant="outline" onClick={resetFilters} disabled={loading}>
              <RotateCcw className="size-4" />
              重置
            </Button>
          </div>
        </div>

        <div className="flex flex-wrap items-center justify-between gap-2">
          <div className="flex flex-wrap items-center gap-3">
            <div className="flex items-center gap-1.5">
              <Switch
                checked={autoRefresh}
                onCheckedChange={handleAutoRefreshChange}
                aria-label="自动刷新"
              />
              <Label className="text-xs text-muted-foreground">自动刷新</Label>
            </div>
            <Badge variant="secondary">已加载 {items.length} 条</Badge>
            {applied.requestId ? (
              <Badge variant="outline">请求 #{applied.requestId}</Badge>
            ) : null}
            {autoRefresh ? (
              <Badge variant="outline" className="gap-1">
                <Loader2 className="size-3 animate-spin" />
                实时
              </Badge>
            ) : null}
          </div>
          <div className="flex items-center gap-2">
            <Button
              variant="outline"
              size="sm"
              onClick={() => {
                void loadOlder()
              }}
              disabled={!hasMore || olderLoading || loading}
            >
              {olderLoading ? (
                <Loader2 className="size-3.5 animate-spin" />
              ) : (
                <ChevronUp className="size-3.5" />
              )}
              加载更早
            </Button>
            <Button
              variant="ghost"
              size="sm"
              onClick={clearLogs}
              disabled={items.length === 0}
            >
              <Trash2 className="size-3.5" />
              清空
            </Button>
          </div>
        </div>

        {error ? (
          <Alert variant="destructive" className="px-3 py-2">
            <AlertTriangle className="size-4" />
            <AlertDescription className="text-xs">
              日志加载失败：{error}
            </AlertDescription>
          </Alert>
        ) : null}

        {loading && items.length === 0 ? (
          <Skeleton className="h-40 w-full" />
        ) : (
          <div
            ref={scrollRef}
            onScroll={handleScroll}
            className="max-h-[420px] overflow-y-auto rounded-lg border bg-muted/20 p-2 font-mono text-[11px] leading-5"
          >
            {items.length === 0 ? (
              <div className="py-8 text-center text-xs text-muted-foreground">
                暂无日志
              </div>
            ) : (
              items.map((item) => (
                <div
                  key={item.id}
                  className="flex flex-wrap items-start gap-1.5"
                >
                  <span className="shrink-0 text-muted-foreground">
                    {formatLogDateTime(item.created_at)}
                  </span>
                  <LevelBadge level={item.level} />
                  <span className="max-w-48 shrink-0 truncate text-muted-foreground">
                    {item.logger}
                  </span>
                  <span className="min-w-0 break-words whitespace-pre-wrap">
                    {item.message}
                  </span>
                  {item.request_id !== null ? (
                    <Badge
                      variant="outline"
                      className="h-4 shrink-0 px-1 text-[10px] leading-none"
                    >
                      #{item.request_id}
                    </Badge>
                  ) : null}
                </div>
              ))
            )}
          </div>
        )}

        {hasMore ? (
          <p className="text-[11px] text-muted-foreground">
            仍有更早的日志，可点击“加载更早”继续查看。
          </p>
        ) : null}
      </CardContent>
    </Card>
  )
}
