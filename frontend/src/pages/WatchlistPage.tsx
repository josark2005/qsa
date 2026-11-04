import { useEffect, useState, type FormEvent } from "react"
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query"
import {
  AlertTriangle,
  Check,
  Loader2,
  Pencil,
  Plus,
  Power,
  Search,
  Star,
  Trash2,
  X,
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
import { Input } from "@/components/ui/input"
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
import type { InstrumentSearchItem, WatchItem } from "@/lib/types"

const BOARD_LABELS: Record<string, string> = {
  main: "主板",
  gem: "创业板",
  star: "科创板",
  bse: "北交所",
}

function errorMessage(error: unknown): string {
  if (error instanceof ApiError) return error.message
  if (error instanceof Error) return error.message
  return "操作失败，请稍后重试"
}

function boardText(board: string | null): string {
  if (!board) return "—"
  return BOARD_LABELS[board] ?? board
}

export function WatchlistPage() {
  const queryClient = useQueryClient()
  const [symbolInput, setSymbolInput] = useState("")
  const [noteInput, setNoteInput] = useState("")
  const [searchTerm, setSearchTerm] = useState("")
  const [showSuggestions, setShowSuggestions] = useState(false)
  const [editingId, setEditingId] = useState<number | null>(null)
  const [editingNote, setEditingNote] = useState("")
  const [removeTarget, setRemoveTarget] = useState<WatchItem | null>(null)

  useEffect(() => {
    const timer = window.setTimeout(() => {
      setSearchTerm(symbolInput.trim())
    }, 250)
    return () => window.clearTimeout(timer)
  }, [symbolInput])

  const watchlistQuery = useQuery({
    queryKey: ["watchlist"],
    queryFn: ({ signal }) => api.listWatchItems(signal),
  })

  const searchQuery = useQuery({
    queryKey: ["watchlist-instrument-search", searchTerm],
    queryFn: () => api.searchInstruments(searchTerm),
    enabled: searchTerm.length > 0,
    staleTime: 60_000,
  })

  const items = watchlistQuery.data?.items ?? []
  const suggestions = (searchQuery.data?.items ?? []).slice(0, 5)

  function invalidateWatchlist() {
    void queryClient.invalidateQueries({ queryKey: ["watchlist"] })
  }

  function symbolExists(symbol: string): boolean {
    const value = symbol.trim().toUpperCase()
    return items.some(
      (item) =>
        item.symbol.toUpperCase() === value ||
        item.code.toUpperCase() === value ||
        item.name === symbol.trim(),
    )
  }

  const addMutation = useMutation({
    mutationFn: (payload: { symbol: string; note?: string | null }) =>
      api.addWatchItem(payload),
    onSuccess: (item) => {
      toast.success(`已添加关注：${item.name}`)
      setSymbolInput("")
      setNoteInput("")
      setShowSuggestions(false)
      invalidateWatchlist()
    },
    onError: (error) => {
      toast.error(errorMessage(error))
    },
  })

  const updateMutation = useMutation({
    mutationFn: (variables: {
      id: number
      payload: { note?: string | null; active?: boolean }
    }) => api.updateWatchItem(variables.id, variables.payload),
    onSuccess: (item, variables) => {
      if (variables.payload.active === undefined) {
        toast.success(`已更新「${item.name}」的备注`)
        setEditingId(null)
      } else {
        toast.success(
          `已${variables.payload.active ? "启用" : "停用"}「${item.name}」的关注`,
        )
      }
      invalidateWatchlist()
    },
    onError: (error) => {
      toast.error(errorMessage(error))
    },
  })

  const removeMutation = useMutation({
    mutationFn: (id: number) => api.removeWatchItem(id),
    onSuccess: () => {
      toast.success("已移除关注标的")
      setRemoveTarget(null)
      invalidateWatchlist()
    },
    onError: (error) => {
      toast.error(errorMessage(error))
      setRemoveTarget(null)
    },
  })

  function handleAdd(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    const symbol = symbolInput.trim()
    if (!symbol) {
      toast.error("请输入代码或名称")
      return
    }
    if (symbolExists(symbol)) {
      toast.error("该标的已在关注列表中")
      return
    }
    const note = noteInput.trim()
    addMutation.mutate({ symbol, note: note || null })
  }

  function selectSuggestion(item: InstrumentSearchItem) {
    setSymbolInput(item.symbol)
    setShowSuggestions(false)
  }

  function startEditNote(item: WatchItem) {
    setEditingId(item.id)
    setEditingNote(item.note ?? "")
  }

  function cancelEditNote() {
    setEditingId(null)
    setEditingNote("")
  }

  function saveNote(item: WatchItem) {
    const note = editingNote.trim()
    updateMutation.mutate({ id: item.id, payload: { note: note || null } })
  }

  function toggleActive(item: WatchItem) {
    updateMutation.mutate({ id: item.id, payload: { active: !item.active } })
  }

  return (
    <div className="space-y-6">
      <div>
        <h1 className="flex items-center gap-2 text-2xl font-semibold tracking-tight">
          <Star className="size-5" />
          关注标的
        </h1>
        <p className="mt-1 text-sm text-muted-foreground">
          LLM 也可以在对话里增删关注标的；获得关注标的的财报/公告时，会在订阅群里主动通知。
        </p>
      </div>

      {/* Card 默认 overflow-hidden 会裁掉搜索候选下拉，这里放开。 */}
      <Card className="overflow-visible">
        <CardHeader className="pb-3">
          <CardTitle className="text-base">添加关注</CardTitle>
          <CardDescription>
            支持代码或名称，例如 600000、浦发银行、600000.SH。
          </CardDescription>
        </CardHeader>
        <CardContent className="space-y-2">
          <form className="flex flex-col gap-2 sm:flex-row" onSubmit={handleAdd}>
            <div
              className="relative flex-1"
              onBlur={(event) => {
                if (!event.currentTarget.contains(event.relatedTarget as Node | null)) {
                  setShowSuggestions(false)
                }
              }}
            >
              <Search className="pointer-events-none absolute top-1/2 left-2.5 size-3.5 -translate-y-1/2 text-muted-foreground" />
              <Input
                className="pl-8"
                placeholder="输入代码或名称"
                value={symbolInput}
                onChange={(event) => {
                  setSymbolInput(event.target.value)
                  setShowSuggestions(true)
                }}
              />
              {showSuggestions &&
              symbolInput.trim().length > 0 &&
              suggestions.length > 0 ? (
                <div className="absolute top-full z-20 mt-1 w-full overflow-hidden rounded-lg border bg-popover shadow-md">
                  {suggestions.map((item) => (
                    <button
                      key={item.symbol}
                      type="button"
                      className="flex w-full items-center justify-between gap-2 px-3 py-2 text-left text-sm transition-colors hover:bg-muted"
                      onClick={() => selectSuggestion(item)}
                    >
                      <span className="truncate font-medium">{item.name}</span>
                      <span className="shrink-0 text-xs text-muted-foreground">
                        {item.symbol}
                      </span>
                    </button>
                  ))}
                </div>
              ) : null}
            </div>
            <Input
              className="sm:max-w-64"
              placeholder="备注（可选）"
              value={noteInput}
              onChange={(event) => setNoteInput(event.target.value)}
            />
            <Button type="submit" disabled={addMutation.isPending}>
              {addMutation.isPending ? (
                <Loader2 className="animate-spin" />
              ) : (
                <Plus />
              )}
              添加关注
            </Button>
          </form>
          {searchQuery.isFetching ? (
            <div className="flex items-center gap-1.5 text-xs text-muted-foreground">
              <Loader2 className="size-3 animate-spin" />
              正在搜索候选
            </div>
          ) : null}
          {searchQuery.isError ? (
            <Alert variant="destructive">
              <AlertTriangle />
              <AlertDescription>{errorMessage(searchQuery.error)}</AlertDescription>
            </Alert>
          ) : null}
        </CardContent>
      </Card>

      <Card>
        <CardHeader className="pb-3">
          <div className="flex flex-wrap items-center justify-between gap-2">
            <div>
              <CardTitle className="text-base">关注列表</CardTitle>
              <CardDescription>
                共 {items.length} 个标的，{items.filter((item) => item.active).length}{" "}
                个关注中
              </CardDescription>
            </div>
            <Badge variant="secondary">关注标的</Badge>
          </div>
        </CardHeader>
        <CardContent>
          {watchlistQuery.isLoading ? (
            <div className="space-y-2">
              {Array.from({ length: 5 }, (_, index) => (
                <Skeleton key={index} className="h-10 w-full" />
              ))}
            </div>
          ) : watchlistQuery.isError ? (
            <Alert variant="destructive">
              <AlertTriangle />
              <AlertDescription>
                {errorMessage(watchlistQuery.error)}
              </AlertDescription>
            </Alert>
          ) : items.length === 0 ? (
            <div className="rounded-lg border border-dashed py-16 text-center text-sm text-muted-foreground">
              还没有关注标的
            </div>
          ) : (
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>代码</TableHead>
                  <TableHead>名称</TableHead>
                  <TableHead>板块</TableHead>
                  <TableHead>行业</TableHead>
                  <TableHead>备注</TableHead>
                  <TableHead>状态</TableHead>
                  <TableHead>添加时间</TableHead>
                  <TableHead className="text-right">操作</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {items.map((item) => (
                  <TableRow
                    key={item.id}
                    className={item.active ? undefined : "text-muted-foreground"}
                  >
                    <TableCell className="font-mono text-xs">
                      {item.code}
                    </TableCell>
                    <TableCell className="font-medium">{item.name}</TableCell>
                    <TableCell className="text-xs">
                      {boardText(item.board)}
                    </TableCell>
                    <TableCell className="text-xs">
                      {item.industry ?? "—"}
                    </TableCell>
                    <TableCell>
                      {editingId === item.id ? (
                        <div className="flex items-center gap-1">
                          <Input
                            className="h-7 w-44"
                            placeholder="备注"
                            value={editingNote}
                            autoFocus
                            onChange={(event) =>
                              setEditingNote(event.target.value)
                            }
                            onKeyDown={(event) => {
                              if (event.key === "Enter") saveNote(item)
                              if (event.key === "Escape") cancelEditNote()
                            }}
                          />
                          <Button
                            size="xs"
                            variant="outline"
                            disabled={updateMutation.isPending}
                            onClick={() => saveNote(item)}
                          >
                            <Check />
                            保存
                          </Button>
                          <Button
                            size="xs"
                            variant="ghost"
                            onClick={cancelEditNote}
                          >
                            <X />
                            取消
                          </Button>
                        </div>
                      ) : (
                        <div className="flex items-center gap-1.5">
                          <span className="max-w-40 truncate text-xs">
                            {item.note || "—"}
                          </span>
                          <Button
                            size="xs"
                            variant="ghost"
                            onClick={() => startEditNote(item)}
                          >
                            <Pencil />
                            编辑备注
                          </Button>
                        </div>
                      )}
                    </TableCell>
                    <TableCell>
                      <Badge variant={item.active ? "default" : "secondary"}>
                        {item.active ? "关注中" : "已停用"}
                      </Badge>
                    </TableCell>
                    <TableCell className="text-xs text-muted-foreground">
                      {formatDateTime(item.created_at)}
                    </TableCell>
                    <TableCell>
                      <div className="flex items-center justify-end gap-1">
                        <Button
                          size="xs"
                          variant="ghost"
                          disabled={updateMutation.isPending}
                          onClick={() => toggleActive(item)}
                        >
                          <Power />
                          {item.active ? "停用" : "启用"}
                        </Button>
                        <Button
                          size="xs"
                          variant="ghost"
                          className="text-destructive hover:text-destructive"
                          onClick={() => setRemoveTarget(item)}
                        >
                          <Trash2 />
                          移除
                        </Button>
                      </div>
                    </TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          )}
        </CardContent>
      </Card>

      <AlertDialog
        open={removeTarget !== null}
        onOpenChange={(open) => {
          if (!open) setRemoveTarget(null)
        }}
      >
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>移除关注标的？</AlertDialogTitle>
            <AlertDialogDescription>
              {removeTarget
                ? `将移除「${removeTarget.name}」（${removeTarget.symbol}），移除后不再收到该标的的财报/公告通知。`
                : ""}
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
                if (removeTarget) removeMutation.mutate(removeTarget.id)
              }}
            >
              {removeMutation.isPending ? "移除中…" : "确认移除"}
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </div>
  )
}
