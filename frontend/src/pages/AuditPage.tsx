import { useState } from "react"
import {
  keepPreviousData,
  useQuery,
} from "@tanstack/react-query"
import { AlertTriangle, Eye, FileText, RefreshCw } from "lucide-react"

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
import {
  Select,
  SelectContent,
  SelectItem,
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
import { api } from "@/lib/api"
import { actionLabel, formatDateTime } from "@/lib/format"
import type { AuditLog } from "@/lib/types"

const PAGE_SIZE = 50
const ACTION_OPTIONS = [
  "settings.update",
  "user.create",
  "user.update",
  "user.reset_password",
  "user.bootstrap_admin",
]

function JsonBlock({ value }: { value: Record<string, unknown> | null }) {
  if (!value) {
    return <span className="text-sm text-muted-foreground">—</span>
  }
  return (
    <pre className="max-h-80 overflow-auto rounded-lg bg-muted p-3 text-xs leading-5">
      {JSON.stringify(value, null, 2)}
    </pre>
  )
}

export function AuditPage() {
  const [page, setPage] = useState(0)
  const [action, setAction] = useState("all")
  const [selected, setSelected] = useState<AuditLog | null>(null)

  const query = useQuery({
    queryKey: ["audit-logs", page, action],
    queryFn: ({ signal }) =>
      api.listAuditLogs({
        limit: PAGE_SIZE,
        offset: page * PAGE_SIZE,
        action: action === "all" ? undefined : action,
        signal,
      }),
    placeholderData: keepPreviousData,
  })

  const total = query.data?.total ?? 0
  const limit = query.data?.limit ?? PAGE_SIZE
  const pageCount = Math.max(1, Math.ceil(total / limit))
  const logs = query.data?.items ?? []

  return (
    <div className="space-y-6">
      <div className="flex flex-col gap-3 sm:flex-row sm:items-end sm:justify-between">
        <div>
          <h1 className="text-2xl font-semibold tracking-tight">审计日志</h1>
          <p className="mt-1 text-sm text-muted-foreground">
            记录管理员对设置和用户的关键操作，便于追溯。
          </p>
        </div>
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
      </div>

      <Card>
        <CardHeader className="flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
          <div>
            <CardTitle className="text-base">操作记录</CardTitle>
            <CardDescription>共 {total} 条记录</CardDescription>
          </div>
          <Select
            value={action}
            onValueChange={(value) => {
              setAction(value)
              setPage(0)
            }}
          >
            <SelectTrigger className="w-52">
              <SelectValue placeholder="全部操作" />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value="all">全部操作</SelectItem>
              {ACTION_OPTIONS.map((item) => (
                <SelectItem key={item} value={item}>
                  {actionLabel(item)}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
        </CardHeader>
        <CardContent>
          {query.isError ? (
            <Alert variant="destructive">
              <AlertTriangle className="size-4" />
              <AlertTitle>审计日志加载失败</AlertTitle>
              <AlertDescription>
                {query.error instanceof Error
                  ? query.error.message
                  : "请稍后重试"}
              </AlertDescription>
            </Alert>
          ) : (
            <div className="overflow-hidden rounded-lg border">
              <Table>
                <TableHeader>
                  <TableRow>
                    <TableHead>时间</TableHead>
                    <TableHead>操作</TableHead>
                    <TableHead>对象</TableHead>
                    <TableHead>操作者</TableHead>
                    <TableHead>IP</TableHead>
                    <TableHead className="w-16" />
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {query.isLoading
                    ? Array.from({ length: 6 }).map((_, index) => (
                        <TableRow key={`skeleton-${index}`}>
                          <TableCell colSpan={6}>
                            <Skeleton className="h-6 w-full" />
                          </TableCell>
                        </TableRow>
                      ))
                    : logs.map((log) => (
                        <TableRow key={log.id}>
                          <TableCell className="text-xs text-muted-foreground">
                            {formatDateTime(log.created_at)}
                          </TableCell>
                          <TableCell>
                            <Badge variant="secondary">{actionLabel(log.action)}</Badge>
                          </TableCell>
                          <TableCell className="max-w-56">
                            <div className="truncate text-xs">
                              {log.target_type}:{log.target_id}
                            </div>
                          </TableCell>
                          <TableCell className="font-mono text-[11px] text-muted-foreground">
                            {log.actor_id ? log.actor_id.slice(0, 8) : "系统"}
                          </TableCell>
                          <TableCell className="text-xs text-muted-foreground">
                            {log.ip ?? "—"}
                          </TableCell>
                          <TableCell>
                            <Button
                              variant="ghost"
                              size="sm"
                              onClick={() => setSelected(log)}
                            >
                              <Eye className="size-3.5" />
                              详情
                            </Button>
                          </TableCell>
                        </TableRow>
                      ))}
                  {!query.isLoading && logs.length === 0 ? (
                    <TableRow>
                      <TableCell
                        colSpan={6}
                        className="h-24 text-center text-sm text-muted-foreground"
                      >
                        暂无审计记录
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

      <Dialog
        open={Boolean(selected)}
        onOpenChange={(open) => !open && setSelected(null)}
      >
        <DialogContent className="sm:max-w-2xl">
          <DialogHeader>
            <DialogTitle className="flex items-center gap-2">
              <FileText className="size-4" />
              {selected ? actionLabel(selected.action) : "审计详情"}
            </DialogTitle>
            <DialogDescription>
              {selected
                ? `${formatDateTime(selected.created_at)} · ${
                    selected.actor_id ?? "系统"
                  }`
                : ""}
            </DialogDescription>
          </DialogHeader>
          {selected ? (
            <div className="grid gap-4 sm:grid-cols-2">
              <div className="space-y-2">
                <div className="text-xs font-medium text-muted-foreground">变更前</div>
                <JsonBlock value={selected.before} />
              </div>
              <div className="space-y-2">
                <div className="text-xs font-medium text-muted-foreground">变更后</div>
                <JsonBlock value={selected.after} />
              </div>
            </div>
          ) : null}
        </DialogContent>
      </Dialog>
    </div>
  )
}
