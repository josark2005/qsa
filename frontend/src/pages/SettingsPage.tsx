import { useEffect, useMemo, useState } from "react"
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import {
  Activity,
  AlertTriangle,
  CheckCircle2,
  Info,
  Loader2,
  RefreshCw,
  Save,
  Wifi,
  WifiOff,
} from "lucide-react"
import { toast } from "sonner"

import { PersonalQQCard } from "@/components/PersonalQQCard"
import { DefaultConditionsField } from "@/components/settings/DefaultConditionsField"
import { SettingField } from "@/components/settings/SettingField"
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
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs"
import { api, ApiError } from "@/lib/api"
import { formatDateTime } from "@/lib/format"
import type { QQBotStatus, SettingMeta, SettingUpdateItem } from "@/lib/types"

interface SettingGroup {
  key: string
  label: string
  items: SettingMeta[]
}

function groupItems(items: SettingMeta[]): SettingGroup[] {
  const groups = new Map<string, SettingGroup>()
  for (const item of items) {
    const existing = groups.get(item.group)
    if (existing) {
      existing.items.push(item)
    } else {
      groups.set(item.group, {
        key: item.group,
        label: item.group_label,
        items: [item],
      })
    }
  }
  return Array.from(groups.values())
}

function validateField(item: SettingMeta, value: unknown): string | null {
  if (item.type === "int" || item.type === "float") {
    if (typeof value !== "number" || Number.isNaN(value)) {
      return "请输入数字"
    }
    if (item.minimum !== null && value < item.minimum) {
      return `不能小于 ${item.minimum}`
    }
    if (item.maximum !== null && value > item.maximum) {
      return `不能大于 ${item.maximum}`
    }
    if (item.type === "int" && !Number.isInteger(value)) {
      return "必须是整数"
    }
  }
  if (item.type === "cron") {
    if (typeof value !== "string" || value.trim().split(/\s+/).length !== 5) {
      return "必须是 5 段 cron 表达式"
    }
  }
  if (item.key === "screening.default_conditions") {
    if (typeof value !== "string") return "必须是 JSON 字符串"
    try {
      const parsed = JSON.parse(value)
      if (!Array.isArray(parsed) || parsed.length === 0) {
        return "至少需要一个筛选条件"
      }
    } catch {
      return "不是合法的 JSON 数组"
    }
  }
  return null
}

const QQ_EVENT_LABELS: Record<string, string> = {
  C2C_MESSAGE_CREATE: "私聊消息",
  GROUP_AT_MESSAGE_CREATE: "群 @ 消息",
  GROUP_MESSAGE_CREATE: "群全量消息",
}

function qqEventText(status: QQBotStatus): string {
  if (!status.last_event) return "尚未收到"
  const [type, ...rest] = status.last_event.split(":")
  let eventId = rest.join(":")
  if (eventId.startsWith(`${type}:`)) {
    eventId = eventId.slice(type.length + 1)
  }
  return `${QQ_EVENT_LABELS[type] ?? type}${eventId ? ` · ${eventId}` : ""}`
}

function QQBotStatusCard({
  status,
  loading,
  error,
  onRefresh,
}: {
  status: QQBotStatus | undefined
  loading: boolean
  error: Error | null
  onRefresh: () => void
}) {
  const connected =
    status?.enabled &&
    status.configured &&
    (status.transport === "webhook" || status.websocket_connected)
  const stateLabel = !status
    ? "读取中"
    : !status.enabled
      ? "已停用"
      : !status.configured
        ? "未配置"
        : connected
          ? "已连接"
          : "未连接"
  const stateVariant = !status
    ? "secondary"
    : !status.enabled
      ? "secondary"
      : !status.configured
        ? "destructive"
        : connected
          ? "default"
          : "destructive"

  return (
    <Card className="mb-4 border-primary/20">
      <CardHeader className="pb-3">
        <div className="flex flex-wrap items-center justify-between gap-3">
          <div>
            <CardTitle className="flex items-center gap-2 text-base">
              {connected ? (
                <Wifi className="size-4 text-emerald-600" />
              ) : (
                <WifiOff className="size-4 text-muted-foreground" />
              )}
              机器人接入状态（官方）
            </CardTitle>
            <CardDescription>
              {status?.transport === "websocket"
                ? "WebSocket 长连接；群聊需要 @ 机器人。"
                : "Webhook 回调；需要 QQ 平台配置公网回调地址。"}
            </CardDescription>
          </div>
          <div className="flex items-center gap-2">
            <Badge variant={stateVariant as "default" | "secondary" | "destructive"}>
              {stateLabel}
            </Badge>
            <Button
              variant="ghost"
              size="sm"
              onClick={onRefresh}
              disabled={loading}
            >
              <RefreshCw className={loading ? "size-4 animate-spin" : "size-4"} />
              刷新状态
            </Button>
          </div>
        </div>
      </CardHeader>
      <CardContent className="space-y-3">
        {error ? (
          <Alert variant="destructive">
            <AlertTriangle className="size-4" />
            <AlertDescription>{error.message}</AlertDescription>
          </Alert>
        ) : null}
        <div className="grid gap-3 text-sm sm:grid-cols-2 lg:grid-cols-4">
          <div className="rounded-lg border bg-muted/20 px-3 py-2">
            <div className="text-xs text-muted-foreground">接入方式</div>
            <div className="mt-1 font-medium">
              {status?.transport === "webhook" ? "Webhook 回调" : "WebSocket 长连接"}
            </div>
          </div>
          <div className="rounded-lg border bg-muted/20 px-3 py-2">
            <div className="text-xs text-muted-foreground">凭据</div>
            <div className="mt-1 flex items-center gap-1.5 font-medium">
              {status?.configured ? (
                <CheckCircle2 className="size-3.5 text-emerald-600" />
              ) : (
                <AlertTriangle className="size-3.5 text-amber-600" />
              )}
              {status?.configured ? "AppID / AppSecret 已配置" : "未配置"}
            </div>
          </div>
          <div className="rounded-lg border bg-muted/20 px-3 py-2">
            <div className="text-xs text-muted-foreground">连接状态</div>
            <div className="mt-1 font-medium">
              {status?.transport === "webhook"
                ? status.configured
                  ? "等待事件回调"
                  : "未配置"
                : status?.websocket_connected
                  ? "长连接在线"
                  : status?.enabled
                    ? "等待建立连接"
                    : "已停用"}
            </div>
          </div>
          <div className="rounded-lg border bg-muted/20 px-3 py-2">
            <div className="text-xs text-muted-foreground">最近事件</div>
            <div
              className="mt-1 truncate font-medium"
              title={status ? qqEventText(status) : undefined}
            >
              {status ? qqEventText(status) : "读取中"}
            </div>
            {status?.last_event_at ? (
              <div className="mt-0.5 text-[11px] text-muted-foreground">
                {formatDateTime(status.last_event_at)}
              </div>
            ) : null}
          </div>
        </div>
        <div className="flex flex-wrap items-center gap-2 text-xs text-muted-foreground">
          <span>每日推送订阅群：</span>
          {status && status.subscribed_groups > 0 ? (
            <Badge variant="outline">
              {status.subscribed_groups} 个群已订阅
            </Badge>
          ) : (
            <span className="text-amber-700">
              暂无订阅；请在目标群 @ 机器人并回复「订阅」
            </span>
          )}
        </div>
        {status?.transport === "webhook" ? (
          <div className="flex items-center gap-2 text-xs text-muted-foreground">
            <Activity className="size-3.5" />
            回调地址：<code className="rounded bg-muted px-1.5 py-0.5">{status.webhook_path}</code>
          </div>
        ) : (
          <p className="text-xs text-muted-foreground">
            WebSocket 模式无需配置回调地址；保存 AppID/AppSecret 后会自动重连。若群内 @
            无反应，请检查 QQ 平台的群聊事件订阅和沙箱群配置。
          </p>
        )}
        {status?.last_error ? (
          <Alert variant="destructive">
            <AlertTriangle className="size-4" />
            <AlertDescription className="break-all">
              最近错误：{status.last_error}
            </AlertDescription>
          </Alert>
        ) : null}
      </CardContent>
    </Card>
  )
}

export function SettingsPage() {
  const queryClient = useQueryClient()
  const query = useQuery({
    queryKey: ["admin-settings"],
    queryFn: () => api.adminSettings(),
  })
  const qqStatusQuery = useQuery({
    queryKey: ["qq-bot-status"],
    queryFn: () => api.qqBotStatus(),
    refetchInterval: 10_000,
  })
  const qqSubscriptionsQuery = useQuery({
    queryKey: ["qq-bot-subscriptions"],
    queryFn: () => api.qqSubscriptions(),
    refetchInterval: 15_000,
  })
  const [drafts, setDrafts] = useState<Record<string, unknown>>({})
  const [activeGroup, setActiveGroup] = useState("")

  const items = useMemo(() => query.data?.items ?? [], [query.data])
  const groups = useMemo(() => groupItems(items), [items])

  useEffect(() => {
    if (!query.data) return
    const next: Record<string, unknown> = {}
    for (const item of query.data.items) {
      next[item.key] = item.value
    }
    setDrafts(next)
  }, [query.data])

  useEffect(() => {
    if (!activeGroup && groups.length > 0) {
      setActiveGroup(groups[0].key)
    }
  }, [activeGroup, groups])

  const errors = useMemo(() => {
    const result: Record<string, string> = {}
    for (const item of items) {
      const error = validateField(item, drafts[item.key])
      if (error) result[item.key] = error
    }
    return result
  }, [drafts, items])

  const dirtyItems = useMemo(
    () => items.filter((item) => !Object.is(drafts[item.key], item.value)),
    [drafts, items],
  )
  const hasErrors = Object.values(errors).some(Boolean)

  const saveMutation = useMutation({
    mutationFn: (updates: SettingUpdateItem[]) => api.updateSettings(updates),
    onSuccess: (data) => {
      queryClient.setQueryData(["admin-settings"], data)
      void qqStatusQuery.refetch()
      void qqSubscriptionsQuery.refetch()
      toast.success("设置已保存，所有进程将立即重载")
    },
    onError: (error) => {
      if (error instanceof ApiError && error.status === 409) {
        toast.error("设置已被其他管理员修改，已刷新最新值")
        void query.refetch()
        return
      }
      toast.error(error instanceof Error ? error.message : "保存设置失败")
    },
  })

  function handleSave() {
    const updates: SettingUpdateItem[] = dirtyItems.map((item) => ({
      key: item.key,
      value: drafts[item.key],
      version: item.version,
    }))
    if (updates.length === 0) return
    saveMutation.mutate(updates)
  }

  return (
    <div className="space-y-6">
      <div className="flex flex-col gap-3 sm:flex-row sm:items-end sm:justify-between">
        <div>
          <h1 className="text-2xl font-semibold tracking-tight">系统设置</h1>
          <p className="mt-1 text-sm text-muted-foreground">
            配额、查询保护、安全策略和同步 cron 均在此维护。
          </p>
        </div>
        <div className="flex gap-2">
          <Button
            variant="outline"
            onClick={() => void query.refetch()}
            disabled={query.isFetching}
          >
            <RefreshCw
              className={query.isFetching ? "size-4 animate-spin" : "size-4"}
            />
            刷新
          </Button>
          <Button
            onClick={handleSave}
            disabled={
              dirtyItems.length === 0 || hasErrors || saveMutation.isPending
            }
          >
            {saveMutation.isPending ? (
              <Loader2 className="size-4 animate-spin" />
            ) : (
              <Save className="size-4" />
            )}
            保存{dirtyItems.length > 0 ? `（${dirtyItems.length}）` : ""}
          </Button>
        </div>
      </div>

      <Alert>
        <Info className="size-4" />
        <AlertTitle>修改后立即生效</AlertTitle>
        <AlertDescription>
          保存后通过 Redis 通知所有 API 和同步进程重载配置；正在运行的同步任务不会被中断。
          带“需重启”标记的配置不在当前动态重载范围内。
        </AlertDescription>
      </Alert>

      {query.isLoading ? (
        <div className="grid gap-4">
          <Skeleton className="h-10 w-full" />
          <Skeleton className="h-48 w-full" />
          <Skeleton className="h-48 w-full" />
        </div>
      ) : query.isError ? (
        <Alert variant="destructive">
          <AlertTriangle className="size-4" />
          <AlertTitle>设置加载失败</AlertTitle>
          <AlertDescription>
            {query.error instanceof Error
              ? query.error.message
              : "请稍后重试"}
          </AlertDescription>
        </Alert>
      ) : (
        <Tabs value={activeGroup} onValueChange={setActiveGroup}>
          <TabsList
            className="flex h-auto w-full flex-wrap items-center justify-start gap-1.5 rounded-none bg-transparent p-0"
            style={{
              width: "100%",
              height: "auto",
              justifyContent: "flex-start",
            }}
          >
            {groups.map((group) => (
              <TabsTrigger
                key={group.key}
                value={group.key}
                className="h-8 flex-none gap-2 whitespace-nowrap rounded-lg border px-3 data-active:bg-background"
                style={{ flex: "0 0 auto" }}
              >
                {group.label}
                <Badge variant="secondary" className="px-1.5">
                  {group.items.length}
                </Badge>
              </TabsTrigger>
            ))}
          </TabsList>

          {groups.map((group) => (
            <TabsContent key={group.key} value={group.key} className="mt-4">
              {group.key === "qq_bot" ? (
                <>
                  <QQBotStatusCard
                    status={qqStatusQuery.data}
                    loading={qqStatusQuery.isFetching}
                    error={
                      qqStatusQuery.error instanceof Error
                        ? qqStatusQuery.error
                        : null
                    }
                    onRefresh={() => void qqStatusQuery.refetch()}
                  />
                  <Card className="mb-4">
                    <CardHeader className="pb-3">
                      <div className="flex items-center justify-between gap-3">
                        <div>
                          <CardTitle className="text-base">已订阅群</CardTitle>
                          <CardDescription>
                            群内 @ 机器人并回复「订阅」后，会出现在这里并接收每日推送。
                          </CardDescription>
                        </div>
                        <Button
                          variant="ghost"
                          size="sm"
                          onClick={() => void qqSubscriptionsQuery.refetch()}
                          disabled={qqSubscriptionsQuery.isFetching}
                        >
                          <RefreshCw
                            className={
                              qqSubscriptionsQuery.isFetching
                                ? "size-4 animate-spin"
                                : "size-4"
                            }
                          />
                          刷新
                        </Button>
                      </div>
                    </CardHeader>
                    <CardContent className="space-y-2">
                      {qqSubscriptionsQuery.isError ? (
                        <Alert variant="destructive">
                          <AlertTriangle className="size-4" />
                          <AlertDescription>
                            {qqSubscriptionsQuery.error instanceof Error
                              ? qqSubscriptionsQuery.error.message
                              : "订阅群加载失败"}
                          </AlertDescription>
                        </Alert>
                      ) : qqSubscriptionsQuery.isLoading ? (
                        <Skeleton className="h-16 w-full" />
                      ) : (qqSubscriptionsQuery.data?.items ?? []).filter(
                          (item) => item.active,
                        ).length === 0 ? (
                        <div className="rounded-lg border border-dashed px-4 py-6 text-center text-sm text-muted-foreground">
                          暂无订阅群。请在目标群 @ 机器人并回复「订阅」。
                        </div>
                      ) : (
                        (qqSubscriptionsQuery.data?.items ?? [])
                          .filter((item) => item.active)
                          .map((item) => (
                            <div
                              key={item.id}
                              className="flex flex-col gap-1 rounded-lg border bg-muted/10 px-3 py-2 sm:flex-row sm:items-center sm:justify-between"
                            >
                              <div className="min-w-0">
                                <div className="truncate text-sm font-medium">
                                  {item.group_name || "未获取群名称"}
                                </div>
                                <div
                                  className="truncate font-mono text-[11px] text-muted-foreground"
                                  title={item.group_openid}
                                >
                                  {item.group_openid}
                                </div>
                              </div>
                              <div className="shrink-0 text-right text-[11px] text-muted-foreground">
                                <div>订阅于 {formatDateTime(item.subscribed_at)}</div>
                                <div>
                                  最近推送 {formatDateTime(item.last_pushed_at)}
                                </div>
                              </div>
                            </div>
                          ))
                      )}
                    </CardContent>
                  </Card>
                </>
              ) : null}
              {group.key === "personal_qq" ? <PersonalQQCard /> : null}
              <Card>
                <CardHeader>
                  <CardTitle className="text-base">{group.label}</CardTitle>
                  <CardDescription>
                    共 {group.items.length} 项，修改后点击右上角保存。
                  </CardDescription>
                </CardHeader>
                <CardContent className="divide-y p-0">
                  {group.items.map((item) =>
                    item.key === "screening.default_conditions" ? (
                      <DefaultConditionsField
                        key={item.key}
                        value={
                          typeof drafts[item.key] === "string"
                            ? (drafts[item.key] as string)
                            : ""
                        }
                        error={errors[item.key]}
                        onChange={(value) =>
                          setDrafts((previous) => ({
                            ...previous,
                            [item.key]: value,
                          }))
                        }
                      />
                    ) : (
                      <SettingField
                        key={item.key}
                        item={item}
                        value={drafts[item.key]}
                        error={errors[item.key]}
                        onChange={(value) =>
                          setDrafts((previous) => ({
                            ...previous,
                            [item.key]: value,
                          }))
                        }
                      />
                    ),
                  )}
                </CardContent>
              </Card>
            </TabsContent>
          ))}
        </Tabs>
      )}
    </div>
  )
}
