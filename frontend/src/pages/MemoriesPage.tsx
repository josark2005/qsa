import { useState } from "react"
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import {
  AlertTriangle,
  Brain,
  ChevronLeft,
  ChevronRight,
  Loader2,
  Pencil,
  RefreshCw,
  Search,
  Trash2,
} from "lucide-react"
import { toast } from "sonner"

import { Alert, AlertDescription } from "@/components/ui/alert"
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
import type { LLMMemoryItem } from "@/lib/types"

const PAGE_SIZE = 20
const MAX_SUMMARY_CHARS = 100000

function errorMessage(error: unknown): string {
  if (error instanceof ApiError) return error.message
  if (error instanceof Error) return error.message
  return "操作失败，请稍后重试"
}

function sessionLabel(key: string): string {
  const parts = key.split(":")
  const kind = parts[0]
  const id = parts.slice(1).join(":")
  if (kind === "group") return "群 " + id
  if (kind === "c2c" || kind === "private") return "私聊 " + id
  return key
}

export function MemoriesPage() {
  const queryClient = useQueryClient()
  const [searchInput, setSearchInput] = useState("")
  const [query, setQuery] = useState("")
  const [page, setPage] = useState(1)
  const [editing, setEditing] = useState<LLMMemoryItem | null>(null)
  const [draft, setDraft] = useState("")
  const [detailLoading, setDetailLoading] = useState(false)
  const [deleteTarget, setDeleteTarget] = useState<LLMMemoryItem | null>(null)

  const listQuery = useQuery({
    queryKey: ["memories", query, page],
    queryFn: ({ signal }) =>
      api.listMemories({ q: query || undefined, page, pageSize: PAGE_SIZE, signal }),
  })

  const items = listQuery.data?.items ?? []
  const total = listQuery.data?.total ?? 0
  const pageCount = Math.max(1, Math.ceil(total / PAGE_SIZE))

  const updateMutation = useMutation({
    mutationFn: (payload: { id: number; summary: string }) =>
      api.updateMemory(payload.id, payload.summary),
    onSuccess: () => {
      toast.success("记忆已更新")
      setEditing(null)
      void queryClient.invalidateQueries({ queryKey: ["memories"] })
    },
    onError: (error) => toast.error(errorMessage(error)),
  })

  const deleteMutation = useMutation({
    mutationFn: (id: number) => api.removeMemory(id),
    onSuccess: () => {
      toast.success("会话记忆已删除")
      setDeleteTarget(null)
      void queryClient.invalidateQueries({ queryKey: ["memories"] })
    },
    onError: (error) => toast.error(errorMessage(error)),
  })

  async function openEditor(item: LLMMemoryItem) {
    setEditing(item)
    setDraft("")
    setDetailLoading(true)
    try {
      const detail = await api.getMemory(item.id)
      setDraft(detail.summary ?? "")
    } catch (error) {
      toast.error(errorMessage(error))
      setEditing(null)
    } finally {
      setDetailLoading(false)
    }
  }

  function submitSearch() {
    setPage(1)
    setQuery(searchInput.trim())
  }

  function saveSummary() {
    if (!editing) return
    const summary = draft.trim()
    if (!summary) {
      toast.error("记忆内容不能为空")
      return
    }
    if (summary.length > MAX_SUMMARY_CHARS) {
      toast.error("记忆内容不能超过 10 万字符")
      return
    }
    updateMutation.mutate({ id: editing.id, summary })
  }

  return (
    <div className="space-y-6">
      <div>
        <h1 className="flex items-center gap-2 text-2xl font-semibold tracking-tight">
          <Brain className="size-5" />
          会话记忆
        </h1>
        <p className="mt-1 text-sm text-muted-foreground">
          每个会话长期记忆摘要的查看与维护。删除会同时清掉缓存和待整理对话，机器人之后会重新积累记忆。
        </p>
      </div>

      <Card>
        <CardHeader className="pb-3">
          <div className="flex flex-wrap items-end justify-between gap-3">
            <div>
              <CardTitle className="text-base">记忆列表</CardTitle>
              <CardDescription>
                共 {total} 条记忆，按最近更新时间排序。
              </CardDescription>
            </div>
            <div className="flex flex-wrap items-center gap-2">
              <form
                className="flex items-center gap-2"
                onSubmit={(event) => {
                  event.preventDefault()
                  submitSearch()
                }}
              >
                <Input
                  className="w-56"
                  placeholder="搜索会话或记忆内容"
                  value={searchInput}
                  onChange={(event) => setSearchInput(event.target.value)}
                />
                <Button type="submit" variant="outline" size="sm">
                  <Search className="size-4" />
                  搜索
                </Button>
              </form>
              <Button
                variant="ghost"
                size="sm"
                onClick={() => void listQuery.refetch()}
                disabled={listQuery.isFetching}
              >
                <RefreshCw
                  className={listQuery.isFetching ? "size-4 animate-spin" : "size-4"}
                />
                刷新
              </Button>
            </div>
          </div>
        </CardHeader>
        <CardContent className="space-y-3">
          {listQuery.isError ? (
            <Alert variant="destructive">
              <AlertTriangle className="size-4" />
              <AlertDescription>{errorMessage(listQuery.error)}</AlertDescription>
            </Alert>
          ) : listQuery.isLoading ? (
            <Skeleton className="h-32 w-full" />
          ) : items.length === 0 ? (
            <div className="rounded-lg border border-dashed px-4 py-10 text-center text-sm text-muted-foreground">
              暂无会话记忆。机器人和群聊积累一段时间后会自动生成摘要。
            </div>
          ) : (
            <div className="overflow-hidden rounded-lg border">
              <Table>
                <TableHeader>
                  <TableRow>
                    <TableHead>会话</TableHead>
                    <TableHead>记忆摘要</TableHead>
                    <TableHead>字符数</TableHead>
                    <TableHead>整理轮数</TableHead>
                    <TableHead>最近整理</TableHead>
                    <TableHead>更新时间</TableHead>
                    <TableHead className="text-right">操作</TableHead>
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {items.map((item) => (
                    <TableRow key={item.id}>
                      <TableCell className="whitespace-nowrap">
                        <div className="text-sm font-medium">
                          {sessionLabel(item.session_key)}
                        </div>
                        <div
                          className="font-mono text-[11px] text-muted-foreground"
                          title={item.session_key}
                        >
                          {item.session_key}
                        </div>
                      </TableCell>
                      <TableCell className="max-w-96">
                        <div
                          className="truncate text-xs text-muted-foreground"
                          title={item.summary_preview}
                        >
                          {item.summary_preview || "（空）"}
                        </div>
                      </TableCell>
                      <TableCell className="whitespace-nowrap text-xs">
                        {item.summary_chars.toLocaleString("zh-CN")}
                      </TableCell>
                      <TableCell className="whitespace-nowrap text-xs">
                        <Badge variant="outline">{item.message_count} 轮</Badge>
                      </TableCell>
                      <TableCell className="whitespace-nowrap text-xs text-muted-foreground">
                        {formatDateTime(item.last_summarized_at)}
                      </TableCell>
                      <TableCell className="whitespace-nowrap text-xs text-muted-foreground">
                        {formatDateTime(item.updated_at)}
                      </TableCell>
                      <TableCell>
                        <div className="flex items-center justify-end gap-1.5">
                          <Button
                            variant="outline"
                            size="sm"
                            onClick={() => void openEditor(item)}
                          >
                            <Pencil className="size-3.5" />
                            编辑
                          </Button>
                          <Button
                            variant="ghost"
                            size="sm"
                            className="text-destructive"
                            onClick={() => setDeleteTarget(item)}
                          >
                            <Trash2 className="size-3.5" />
                            删除
                          </Button>
                        </div>
                      </TableCell>
                    </TableRow>
                  ))}
                </TableBody>
              </Table>
            </div>
          )}

          <div className="flex items-center justify-between gap-2 text-xs text-muted-foreground">
            <span>
              第 {page} / {pageCount} 页
            </span>
            <div className="flex items-center gap-2">
              <Button
                variant="outline"
                size="sm"
                disabled={page <= 1 || listQuery.isFetching}
                onClick={() => setPage((value) => Math.max(1, value - 1))}
              >
                <ChevronLeft className="size-4" />
                上一页
              </Button>
              <Button
                variant="outline"
                size="sm"
                disabled={page >= pageCount || listQuery.isFetching}
                onClick={() => setPage((value) => Math.min(pageCount, value + 1))}
              >
                下一页
                <ChevronRight className="size-4" />
              </Button>
            </div>
          </div>
        </CardContent>
      </Card>

      <Dialog
        open={editing !== null}
        onOpenChange={(open) => {
          if (!open) setEditing(null)
        }}
      >
        <DialogContent className="sm:max-w-3xl">
          <DialogHeader>
            <DialogTitle>编辑会话记忆</DialogTitle>
            <DialogDescription>
              会话 {editing ? editing.session_key : ""}；保存后下一轮对话立即生效。
            </DialogDescription>
          </DialogHeader>
          {detailLoading ? (
            <Skeleton className="h-64 w-full" />
          ) : (
            <div className="space-y-1.5">
              <Label htmlFor="memory-summary">记忆摘要</Label>
              <textarea
                id="memory-summary"
                className="min-h-64 w-full rounded-lg border border-input bg-transparent px-2.5 py-2 text-sm outline-none focus-visible:border-ring focus-visible:ring-3 focus-visible:ring-ring/50"
                value={draft}
                maxLength={MAX_SUMMARY_CHARS}
                onChange={(event) => setDraft(event.target.value)}
              />
              <div className="text-right text-[11px] text-muted-foreground">
                {draft.length.toLocaleString("zh-CN")} /{" "}
                {MAX_SUMMARY_CHARS.toLocaleString("zh-CN")} 字符
              </div>
            </div>
          )}
          <DialogFooter>
            <Button
              type="button"
              variant="outline"
              onClick={() => setEditing(null)}
            >
              取消
            </Button>
            <Button
              type="button"
              disabled={detailLoading || updateMutation.isPending}
              onClick={saveSummary}
            >
              {updateMutation.isPending ? (
                <Loader2 className="size-4 animate-spin" />
              ) : null}
              保存
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      <AlertDialog
        open={deleteTarget !== null}
        onOpenChange={(open) => {
          if (!open) setDeleteTarget(null)
        }}
      >
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>删除这条会话记忆？</AlertDialogTitle>
            <AlertDialogDescription>
              将删除「{deleteTarget ? sessionLabel(deleteTarget.session_key) : ""}
              」的记忆摘要、缓存和待整理对话，删除后无法恢复。
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel disabled={deleteMutation.isPending}>
              取消
            </AlertDialogCancel>
            <AlertDialogAction
              variant="destructive"
              disabled={deleteMutation.isPending}
              onClick={(event) => {
                event.preventDefault()
                if (deleteTarget) deleteMutation.mutate(deleteTarget.id)
              }}
            >
              {deleteMutation.isPending ? (
                <Loader2 className="size-4 animate-spin" />
              ) : (
                <Trash2 className="size-4" />
              )}
              删除
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </div>
  )
}
