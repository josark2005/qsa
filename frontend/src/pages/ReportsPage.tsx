import { useState, type FormEvent } from "react"
import { keepPreviousData, useQuery } from "@tanstack/react-query"
import {
  AlertTriangle,
  Copy,
  Download,
  ExternalLink,
  RefreshCw,
  RotateCcw,
  Search,
} from "lucide-react"
import { useNavigate } from "react-router-dom"
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
import type { ReportHistoryItem } from "@/lib/types"

const PAGE_SIZE = 20

function errorMessage(error: unknown): string {
  if (error instanceof ApiError) return error.message
  if (error instanceof Error) return error.message
  return "操作失败，请稍后重试"
}

function conditionSummary(item: ReportHistoryItem): string {
  const parts = item.condition_views.map(
    (view) =>
      `${view.label} ${view.op_label} ${view.value_text}${
        view.params_text ? `（${view.params_text}）` : ""
      }`,
  )
  return parts.length > 0 ? parts.join("；") : "无筛选条件"
}

function copyReportLink(token: string) {
  const url = `${window.location.origin}/reports/${token}`
  navigator.clipboard
    .writeText(url)
    .then(() => toast.success("报表链接已复制"))
    .catch(() => toast.error("复制失败，请手动复制地址栏链接"))
}

export function ReportsPage() {
  const navigate = useNavigate()
  const [page, setPage] = useState(0)
  const [searchInput, setSearchInput] = useState("")
  const [search, setSearch] = useState("")

  const query = useQuery({
    queryKey: ["report-history", page, search],
    queryFn: ({ signal }) =>
      api.listReports({
        limit: PAGE_SIZE,
        offset: page * PAGE_SIZE,
        q: search || undefined,
        signal,
      }),
    placeholderData: keepPreviousData,
  })

  const total = query.data?.total ?? 0
  const limit = query.data?.limit ?? PAGE_SIZE
  const pageCount = Math.max(1, Math.ceil(total / limit))
  const reports = query.data?.items ?? []

  function handleSearch(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    setSearch(searchInput.trim())
    setPage(0)
  }

  return (
    <div className="space-y-6">
      <div className="flex flex-col gap-3 sm:flex-row sm:items-end sm:justify-between">
        <div>
          <h1 className="text-2xl font-semibold tracking-tight">报表历史</h1>
          <p className="mt-1 text-sm text-muted-foreground">
            已生成的报表会永久保留，可随时回看、复制链接、下载 PDF 或基于原条件重新筛选。
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
        <CardHeader>
          <CardTitle className="text-base">历史报表</CardTitle>
          <CardDescription>
            共 {total} 份报表
            {query.data?.latest_trade_date
              ? ` · 当前最新交易日 ${query.data.latest_trade_date}`
              : ""}
          </CardDescription>
        </CardHeader>
        <CardContent className="space-y-4">
          <form className="flex max-w-sm items-center gap-2" onSubmit={handleSearch}>
            <div className="relative flex-1">
              <Search className="pointer-events-none absolute top-1/2 left-2.5 size-3.5 -translate-y-1/2 text-muted-foreground" />
              <Input
                className="pl-8"
                placeholder="按报表标题搜索"
                value={searchInput}
                onChange={(event) => setSearchInput(event.target.value)}
              />
            </div>
            <Button type="submit" variant="outline">
              搜索
            </Button>
          </form>

          {query.isError ? (
            <Alert variant="destructive">
              <AlertTriangle className="size-4" />
              <AlertTitle>报表历史加载失败</AlertTitle>
              <AlertDescription>{errorMessage(query.error)}</AlertDescription>
            </Alert>
          ) : (
            <div className="overflow-hidden rounded-lg border">
              <Table>
                <TableHeader>
                  <TableRow>
                    <TableHead>报表</TableHead>
                    <TableHead>筛选条件</TableHead>
                    <TableHead className="text-right">入选</TableHead>
                    <TableHead>生成时间</TableHead>
                    <TableHead>状态</TableHead>
                    <TableHead>来源</TableHead>
                    <TableHead className="w-36 text-right">操作</TableHead>
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {query.isLoading
                    ? Array.from({ length: 5 }).map((_, index) => (
                        <TableRow key={`skeleton-${index}`}>
                          <TableCell colSpan={7}>
                            <Skeleton className="h-6 w-full" />
                          </TableCell>
                        </TableRow>
                      ))
                    : reports.map((item) => (
                        <TableRow key={item.token}>
                          <TableCell className="max-w-56">
                            <button
                              className="block max-w-full truncate text-left font-medium hover:underline"
                              onClick={() => navigate(`/reports/${item.token}`)}
                              title={item.title}
                            >
                              {item.title}
                            </button>
                            <div className="mt-0.5 text-[11px] text-muted-foreground">
                              数据截至 {item.data_as_of ?? item.screen_as_of}
                            </div>
                          </TableCell>
                          <TableCell className="max-w-80">
                            <div
                              className="line-clamp-2 text-xs text-muted-foreground"
                              title={conditionSummary(item)}
                            >
                              {conditionSummary(item)}
                            </div>
                          </TableCell>
                          <TableCell className="text-right font-medium">
                            {item.included}
                            {item.truncated ? (
                              <span className="text-xs font-normal text-muted-foreground">
                                {" "}
                                / {item.total}
                              </span>
                            ) : null}
                          </TableCell>
                          <TableCell className="text-xs text-muted-foreground">
                            {formatDateTime(item.generated_at)}
                          </TableCell>
                          <TableCell>
                            {item.stale ? (
                              <Badge variant="outline" title={item.stale_reason ?? ""}>
                                历史
                              </Badge>
                            ) : (
                              <Badge variant="secondary">当日</Badge>
                            )}
                          </TableCell>
                          <TableCell className="text-xs text-muted-foreground">
                            {item.owner_username ?? "系统"}
                          </TableCell>
                          <TableCell>
                            <div className="flex justify-end gap-1">
                              <Button
                                variant="ghost"
                                size="icon-sm"
                                title="查看报表"
                                onClick={() => navigate(`/reports/${item.token}`)}
                              >
                                <ExternalLink className="size-4" />
                              </Button>
                              <Button
                                variant="ghost"
                                size="icon-sm"
                                title="重新筛选"
                                onClick={() =>
                                  navigate(
                                    `/screener?report=${encodeURIComponent(
                                      item.token,
                                    )}&run=1`,
                                  )
                                }
                              >
                                <RotateCcw className="size-4" />
                              </Button>
                              <Button
                                variant="ghost"
                                size="icon-sm"
                                title="复制链接"
                                onClick={() => copyReportLink(item.token)}
                              >
                                <Copy className="size-4" />
                              </Button>
                              <Button
                                variant="ghost"
                                size="icon-sm"
                                asChild
                                title="下载 PDF"
                              >
                                <a
                                  href={`/api/public/reports/${encodeURIComponent(
                                    item.token,
                                  )}/pdf`}
                                  target="_blank"
                                  rel="noreferrer"
                                >
                                  <Download className="size-4" />
                                </a>
                              </Button>
                            </div>
                          </TableCell>
                        </TableRow>
                      ))}
                  {!query.isLoading && reports.length === 0 ? (
                    <TableRow>
                      <TableCell
                        colSpan={7}
                        className="h-24 text-center text-sm text-muted-foreground"
                      >
                        还没有生成过报表
                      </TableCell>
                    </TableRow>
                  ) : null}
                </TableBody>
              </Table>
            </div>
          )}

          <div className="flex items-center justify-between text-xs text-muted-foreground">
            <span>
              共 {total} 份报表 · 第 {Math.min(page + 1, pageCount)} / {pageCount} 页
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
    </div>
  )
}
