import { useState, type FormEvent, type ReactNode } from "react"
import { useMutation, useQuery } from "@tanstack/react-query"
import {
  AlertTriangle,
  Loader2,
  Plus,
  RefreshCw,
  Send,
  ShieldAlert,
  Trash2,
  XCircle,
} from "lucide-react"
import { toast } from "sonner"

import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
} from "@/components/ui/alert-dialog"
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
import { Skeleton } from "@/components/ui/skeleton"
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table"
import { api, ApiError } from "@/lib/api"
import { formatDateTime } from "@/lib/format"
import type { PersonalQQGroup, PersonalQQStatus } from "@/lib/types"

function errorMessage(error: unknown): string {
  if (error instanceof ApiError) return error.message
  if (error instanceof Error) return error.message
  return "操作失败，请稍后重试"
}

function groupCount(status: PersonalQQStatus | undefined): number {
  if (!status) return 0
  const value: unknown = status.groups
  if (typeof value === "number") return value
  return Array.isArray(value) ? value.length : 0
}

function selfQQText(selfQq: string | null | undefined): string {
  const value = (selfQq ?? "").trim()
  return value || "未获取"
}

function numericGroupId(group: PersonalQQGroup): number {
  return Number(group.group_id)
}

function chatModeLabel(mode: string): string {
  if (mode === "mention") return "仅 @ 触发"
  return mode || "—"
}

function remainingTtlText(ttlSeconds: number): string {
  if (!Number.isFinite(ttlSeconds) || ttlSeconds <= 0) return "—"
  if (ttlSeconds < 60) return "不到 1 分钟"
  return `约 ${Math.round(ttlSeconds / 60)} 分钟`
}

function InfoCell({
  label,
  children,
}: {
  label: string
  children: ReactNode
}) {
  return (
    <div className="rounded-lg border bg-muted/20 px-3 py-2">
      <div className="text-xs text-muted-foreground">{label}</div>
      {children}
    </div>
  )
}

export function PersonalQQCard() {
  const [addOpen, setAddOpen] = useState(false)
  const [addGroupId, setAddGroupId] = useState("")
  const [addGroupName, setAddGroupName] = useState("")
  const [removeTarget, setRemoveTarget] = useState<PersonalQQGroup | null>(null)

  const statusQuery = useQuery({
    queryKey: ["personal-qq-status"],
    queryFn: ({ signal }) => api.personalQQStatus(signal),
    refetchInterval: 10_000,
  })
  const groupsQuery = useQuery({
    queryKey: ["personal-qq-groups"],
    queryFn: ({ signal }) => api.listPersonalQQGroups(signal),
    refetchInterval: 15_000,
  })

  const status = statusQuery.data
  const groups = groupsQuery.data?.items ?? []
  const chatModeUsers = status?.chat_mode_users ?? []
  const chatGroups = status?.chat_groups ?? 0
  const connected = Boolean(status?.enabled && status?.configured && status?.connected)
  const refreshing = statusQuery.isFetching || groupsQuery.isFetching

  function refreshAll() {
    void statusQuery.refetch()
    void groupsQuery.refetch()
  }

  const addMutation = useMutation({
    mutationFn: (payload: { group_id: number; group_name?: string | null }) =>
      api.addPersonalQQGroup(payload),
    onSuccess: (group) => {
      setAddOpen(false)
      setAddGroupId("")
      setAddGroupName("")
      toast.success(
        `已添加推送群 ${group.group_name || group.group_id}`,
      )
      refreshAll()
    },
    onError: (error) => toast.error(errorMessage(error)),
  })

  const removeMutation = useMutation({
    mutationFn: (groupId: number) => api.removePersonalQQGroup(groupId),
    onSuccess: () => {
      setRemoveTarget(null)
      toast.success("已移除该推送群")
      refreshAll()
    },
    onError: (error) => toast.error(errorMessage(error)),
  })

  const closeChatModeMutation = useMutation({
    mutationFn: (groupId: string) => api.closePersonalQQChatMode(groupId),
    onSuccess: () => {
      toast.success("已关闭该群的聊天模式")
      refreshAll()
    },
    onError: (error) => toast.error(errorMessage(error)),
  })

  const testMutation = useMutation({
    mutationFn: (groupId: number) => api.testPersonalQQSend(groupId),
    onSuccess: (result) => {
      if (result.ok) {
        toast.success("测试消息已发送，请在群内确认")
        void groupsQuery.refetch()
        return
      }
      const retcode =
        result.retcode === null || result.retcode === undefined
          ? ""
          : `（retcode ${result.retcode}）`
      toast.error(`测试消息发送失败${retcode}：${result.error ?? "未知错误"}`)
    },
    onError: (error) => toast.error(errorMessage(error)),
  })

  function handleAddGroup(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    const trimmed = addGroupId.trim()
    const parsed = Number(trimmed)
    if (!trimmed || !Number.isInteger(parsed) || parsed <= 0) {
      toast.error("群号必须是正整数")
      return
    }
    const name = addGroupName.trim()
    addMutation.mutate({ group_id: parsed, group_name: name || null })
  }

  const stateLabel = !status
    ? "读取中"
    : status.enabled
      ? "已启用"
      : "未启用"
  const stateVariant = !status
    ? ("secondary" as const)
    : status.enabled
      ? ("default" as const)
      : ("secondary" as const)
  const connectionLabel = !status
    ? "读取中"
    : !status.configured
      ? "未配置"
      : status.connected
        ? "已连接"
        : "未连接"
  const connectionVariant = !status
    ? ("secondary" as const)
    : !status.configured
      ? ("destructive" as const)
      : status.connected
        ? ("default" as const)
        : ("destructive" as const)

  return (
    <Card className="mb-4 border-amber-500/30">
      <CardHeader className="pb-3">
        <div className="flex flex-wrap items-center justify-between gap-3">
          <div>
            <CardTitle className="flex items-center gap-2 text-base">
              <ShieldAlert className="size-4 text-amber-600" />
              个人号机器人（OneBot）
            </CardTitle>
            <CardDescription>
              独立于官方机器人的个人账号通道，通过 OneBot v11
              协议端收发消息，可单独维护推送群。
            </CardDescription>
          </div>
          <Button
            variant="ghost"
            size="sm"
            onClick={refreshAll}
            disabled={refreshing}
          >
            <RefreshCw
              className={refreshing ? "size-4 animate-spin" : "size-4"}
            />
            刷新状态
          </Button>
        </div>
      </CardHeader>
      <CardContent className="space-y-3">
        <Alert className="border-amber-500/40 bg-amber-500/5">
          <AlertTriangle className="size-4" />
          <AlertDescription>
            第三方协议端（SnowLuma/NapCat），非官方链路，账号存在被限制风险；默认关闭，仅建议内部使用。
          </AlertDescription>
        </Alert>

        {statusQuery.isError ? (
          <Alert variant="destructive">
            <AlertTriangle className="size-4" />
            <AlertDescription>
              {statusQuery.error instanceof Error
                ? statusQuery.error.message
                : "个人号状态加载失败"}
            </AlertDescription>
          </Alert>
        ) : statusQuery.isLoading ? (
          <Skeleton className="h-28 w-full" />
        ) : status ? (
          <>
            <div className="flex flex-wrap items-center gap-2">
              <Badge variant={stateVariant}>{stateLabel}</Badge>
              <Badge variant={connectionVariant}>{connectionLabel}</Badge>
              <Badge variant="outline">
                {groupCount(status)} 个群已订阅
              </Badge>
              <Badge variant={status.llm_enabled ? "default" : "secondary"}>
                {status.llm_enabled ? "LLM 对话已启用" : "LLM 对话未启用"}
              </Badge>
              {!status.llm_configured ? (
                <Badge variant="destructive">模型未配置</Badge>
              ) : null}
            </div>
            <div className="grid gap-3 text-sm sm:grid-cols-2 lg:grid-cols-4">
              <InfoCell label="连接地址">
                <div
                  className="mt-1 truncate font-mono text-xs"
                  title={status.http_url ?? undefined}
                >
                  {status.http_url || "—"}
                </div>
                <div
                  className="truncate font-mono text-[11px] text-muted-foreground"
                  title={status.ws_url ?? undefined}
                >
                  {status.ws_url || "—"}
                </div>
              </InfoCell>
              <InfoCell label="机器人 QQ">
                <div className="mt-1 font-medium">
                  {selfQQText(status.self_qq)}
                </div>
              </InfoCell>
              <InfoCell label="最近事件">
                <div
                  className="mt-1 truncate font-medium"
                  title={status.last_event ?? undefined}
                >
                  {status.last_event || "尚未收到"}
                </div>
                {status.last_event_at ? (
                  <div className="mt-0.5 text-[11px] text-muted-foreground">
                    {formatDateTime(status.last_event_at)}
                  </div>
                ) : null}
              </InfoCell>
              <InfoCell label="连接状态">
                <div className="mt-1 font-medium">
                  {!status.enabled
                    ? "通道已停用"
                    : !status.configured
                      ? "尚未配置协议端"
                      : status.connected
                        ? "协议端在线"
                        : "等待协议端连接"}
                </div>
              </InfoCell>
              <InfoCell label="LLM 模型">
                <div
                  className="mt-1 truncate font-medium"
                  title={status.llm_model || undefined}
                >
                  {status.llm_model || "未配置"}
                </div>
                <div
                  className="truncate font-mono text-[11px] text-muted-foreground"
                  title={status.llm_base_url || undefined}
                >
                  {status.llm_base_url || "—"}
                </div>
                {status.llm_last_error ? (
                  <div
                    className="mt-0.5 truncate text-[11px] text-destructive"
                    title={status.llm_last_error}
                  >
                    最近错误：{status.llm_last_error}
                  </div>
                ) : null}
              </InfoCell>
            </div>
            {!status.configured ? (
              <p className="text-xs text-muted-foreground">
                尚未配置 OneBot 协议端地址，请在「个人号机器人」设置页中填写 HTTP /
                WebSocket 地址后保存。
              </p>
            ) : null}
            {status.last_error ? (
              <Alert variant="destructive">
                <AlertTriangle className="size-4" />
                <AlertDescription className="break-all">
                  最近错误：{status.last_error}
                </AlertDescription>
              </Alert>
            ) : null}
          </>
        ) : null}

        <div className="space-y-2 border-t pt-3">
          <div>
            <div className="flex flex-wrap items-center gap-2">
              <div className="text-sm font-medium">聊天模式</div>
              {chatGroups > chatModeUsers.length ? (
                <Badge variant="outline">仅显示前 20 条</Badge>
              ) : null}
            </div>
            <div className="text-xs text-muted-foreground">
              群内 @ 机器人发送「开启聊天模式」/「关闭聊天模式」切换，私聊可直接提问，超过有效期自动关闭。
            </div>
          </div>

          {statusQuery.isError ? (
            <Alert variant="destructive">
              <AlertTriangle className="size-4" />
              <AlertDescription>
                {statusQuery.error instanceof Error
                  ? statusQuery.error.message
                  : "聊天模式状态加载失败"}
              </AlertDescription>
            </Alert>
          ) : statusQuery.isLoading ? (
            <Skeleton className="h-24 w-full" />
          ) : chatModeUsers.length === 0 ? (
            <div className="rounded-lg border border-dashed px-4 py-6 text-center text-sm text-muted-foreground">
              当前没有群处于聊天模式
            </div>
          ) : (
            <div className="overflow-hidden rounded-lg border">
              <Table>
                <TableHeader>
                  <TableRow>
                    <TableHead>群号</TableHead>
                    <TableHead>群名</TableHead>
                    <TableHead>模式</TableHead>
                    <TableHead>开启人</TableHead>
                    <TableHead>开启时间</TableHead>
                    <TableHead>剩余有效期</TableHead>
                    <TableHead className="text-right">操作</TableHead>
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {chatModeUsers.map((user) => {
                    const closing =
                      closeChatModeMutation.isPending &&
                      closeChatModeMutation.variables === user.group_id
                    return (
                      <TableRow key={user.group_id}>
                        <TableCell className="font-mono text-xs">
                          {user.group_id}
                        </TableCell>
                        <TableCell
                          className="max-w-48 truncate text-xs"
                          title={user.group_name ?? undefined}
                        >
                          {user.group_name || "未命名"}
                        </TableCell>
                        <TableCell className="whitespace-nowrap text-xs">
                          {chatModeLabel(user.mode)}
                        </TableCell>
                        <TableCell
                          className="max-w-32 truncate text-xs"
                          title={user.enabled_by || undefined}
                        >
                          {user.enabled_by || "—"}
                        </TableCell>
                        <TableCell className="whitespace-nowrap text-xs text-muted-foreground">
                          {formatDateTime(user.enabled_at)}
                        </TableCell>
                        <TableCell className="whitespace-nowrap text-xs text-muted-foreground">
                          {remainingTtlText(user.ttl_seconds)}
                        </TableCell>
                        <TableCell>
                          <div className="flex items-center justify-end">
                            <Button
                              variant="ghost"
                              size="sm"
                              className="text-destructive"
                              onClick={() =>
                                closeChatModeMutation.mutate(user.group_id)
                              }
                              disabled={closing}
                            >
                              {closing ? (
                                <Loader2 className="size-3.5 animate-spin" />
                              ) : (
                                <XCircle className="size-3.5" />
                              )}
                              关闭
                            </Button>
                          </div>
                        </TableCell>
                      </TableRow>
                    )
                  })}
                </TableBody>
              </Table>
            </div>
          )}
        </div>

        <div className="space-y-2 border-t pt-3">
          <div className="flex flex-wrap items-center justify-between gap-2">
            <div>
              <div className="text-sm font-medium">推送群</div>
              <div className="text-xs text-muted-foreground">
                每日选股结果会通过个人号发送到这些群；可先发送测试消息验证链路。
              </div>
            </div>
            <div className="flex items-center gap-2">
              <Button
                variant="outline"
                size="sm"
                onClick={() => void groupsQuery.refetch()}
                disabled={groupsQuery.isFetching}
              >
                <RefreshCw
                  className={
                    groupsQuery.isFetching
                      ? "size-4 animate-spin"
                      : "size-4"
                  }
                />
                刷新
              </Button>
              <Button size="sm" onClick={() => setAddOpen(true)}>
                <Plus className="size-4" />
                添加群
              </Button>
            </div>
          </div>

          {groupsQuery.isError ? (
            <Alert variant="destructive">
              <AlertTriangle className="size-4" />
              <AlertDescription>
                {groupsQuery.error instanceof Error
                  ? groupsQuery.error.message
                  : "推送群加载失败"}
              </AlertDescription>
            </Alert>
          ) : groupsQuery.isLoading ? (
            <Skeleton className="h-24 w-full" />
          ) : groups.length === 0 ? (
            <div className="rounded-lg border border-dashed px-4 py-6 text-center text-sm text-muted-foreground">
              暂无推送群。点击“添加群”填写群号后即可接收每日选股结果。
            </div>
          ) : (
            <div className="overflow-hidden rounded-lg border">
              <Table>
                <TableHeader>
                  <TableRow>
                    <TableHead>群号</TableHead>
                    <TableHead>群名</TableHead>
                    <TableHead>状态</TableHead>
                    <TableHead>最近推送</TableHead>
                    <TableHead className="text-right">操作</TableHead>
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {groups.map((group) => {
                    const groupId = numericGroupId(group)
                    const testing =
                      testMutation.isPending &&
                      testMutation.variables === groupId
                    return (
                      <TableRow key={group.id}>
                        <TableCell className="font-mono text-xs">
                          {group.group_id}
                        </TableCell>
                        <TableCell
                          className="max-w-48 truncate text-xs"
                          title={group.group_name ?? undefined}
                        >
                          {group.group_name || "未命名"}
                        </TableCell>
                        <TableCell>
                          <Badge
                            variant={group.active ? "outline" : "secondary"}
                          >
                            {group.active ? "订阅中" : "已停用"}
                          </Badge>
                        </TableCell>
                        <TableCell className="whitespace-nowrap text-xs text-muted-foreground">
                          {formatDateTime(group.last_pushed_at)}
                        </TableCell>
                        <TableCell>
                          <div className="flex items-center justify-end gap-1.5">
                            <Button
                              variant="outline"
                              size="sm"
                              onClick={() => testMutation.mutate(groupId)}
                              disabled={!connected || testing}
                            >
                              {testing ? (
                                <Loader2 className="size-3.5 animate-spin" />
                              ) : (
                                <Send className="size-3.5" />
                              )}
                              发送测试消息
                            </Button>
                            <Button
                              variant="ghost"
                              size="sm"
                              className="text-destructive"
                              onClick={() => setRemoveTarget(group)}
                            >
                              <Trash2 className="size-3.5" />
                              移除
                            </Button>
                          </div>
                        </TableCell>
                      </TableRow>
                    )
                  })}
                </TableBody>
              </Table>
            </div>
          )}
        </div>
      </CardContent>

      <Dialog
        open={addOpen}
        onOpenChange={(open) => {
          setAddOpen(open)
          if (!open) {
            setAddGroupId("")
            setAddGroupName("")
          }
        }}
      >
        <DialogContent>
          <form className="space-y-4" onSubmit={handleAddGroup}>
            <DialogHeader>
              <DialogTitle>添加推送群</DialogTitle>
              <DialogDescription>
                填写 QQ 群号，群名可选；添加后即可接收每日选股结果。
              </DialogDescription>
            </DialogHeader>
            <div className="space-y-1.5">
              <Label htmlFor="personal-qq-group-id">群号</Label>
              <Input
                id="personal-qq-group-id"
                type="number"
                min={1}
                placeholder="例如 123456789"
                value={addGroupId}
                onChange={(event) => setAddGroupId(event.target.value)}
              />
            </div>
            <div className="space-y-1.5">
              <Label htmlFor="personal-qq-group-name">群名（可选）</Label>
              <Input
                id="personal-qq-group-name"
                placeholder="例如 内部研究群"
                maxLength={128}
                value={addGroupName}
                onChange={(event) => setAddGroupName(event.target.value)}
              />
            </div>
            <DialogFooter>
              <Button
                type="button"
                variant="outline"
                onClick={() => setAddOpen(false)}
              >
                取消
              </Button>
              <Button type="submit" disabled={addMutation.isPending}>
                {addMutation.isPending ? (
                  <Loader2 className="size-4 animate-spin" />
                ) : (
                  <Plus className="size-4" />
                )}
                添加
              </Button>
            </DialogFooter>
          </form>
        </DialogContent>
      </Dialog>

      <AlertDialog
        open={removeTarget !== null}
        onOpenChange={(open) => {
          if (!open) setRemoveTarget(null)
        }}
      >
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>移除推送群？</AlertDialogTitle>
            <AlertDialogDescription>
              将不再向「
              {removeTarget?.group_name || removeTarget?.group_id}
              」推送每日选股结果，不影响群内已有数据。
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel disabled={removeMutation.isPending}>
              取消
            </AlertDialogCancel>
            <AlertDialogAction
              variant="destructive"
              disabled={removeMutation.isPending}
              onClick={(event) => {
                event.preventDefault()
                if (removeTarget) {
                  removeMutation.mutate(numericGroupId(removeTarget))
                }
              }}
            >
              {removeMutation.isPending ? (
                <Loader2 className="size-4 animate-spin" />
              ) : (
                <Trash2 className="size-4" />
              )}
              移除
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </Card>
  )
}
