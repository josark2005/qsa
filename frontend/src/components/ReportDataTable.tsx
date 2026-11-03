import { useEffect, useMemo, useState } from "react"
import {
  flexRender,
  getCoreRowModel,
  getFilteredRowModel,
  getPaginationRowModel,
  getSortedRowModel,
  useReactTable,
  type ColumnDef,
  type SortingState,
} from "@tanstack/react-table"
import { ArrowDownIcon, ArrowUpDownIcon, ArrowUpIcon, SearchIcon } from "lucide-react"

import { KlineThumbnail } from "@/components/KlineThumbnail"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
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
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table"
import { formatFactorValue, pctTone } from "@/lib/format"
import type { ReportThumbnailItem, ScreenExtraField } from "@/lib/types"

const PAGE_SIZE = 50
const BOARD_LABELS: Record<string, string> = {
  main: "主板",
  gem: "创业板",
  star: "科创板",
  bse: "北交所",
}

type ReportRow = Record<string, unknown>

function boardLabel(board: unknown): string {
  const value = String(board ?? "")
  return BOARD_LABELS[value] ?? (value || "—")
}

function numericValue(value: unknown): number | null {
  if (value === null || value === undefined || value === "") return null
  const parsed = Number(value)
  return Number.isFinite(parsed) ? parsed : null
}

function numericSort(rowA: { getValue: (id: string) => unknown }, rowB: { getValue: (id: string) => unknown }, columnId: string): number {
  const left = numericValue(rowA.getValue(columnId))
  const right = numericValue(rowB.getValue(columnId))
  if (left === null && right === null) return 0
  if (left === null) return 1
  if (right === null) return -1
  return left - right
}

function SortIcon({ sorted }: { sorted: false | "asc" | "desc" }) {
  if (sorted === "asc") return <ArrowUpIcon className="size-3.5" />
  if (sorted === "desc") return <ArrowDownIcon className="size-3.5" />
  return <ArrowUpDownIcon className="size-3.5 opacity-50" />
}

export function ReportDataTable({
  rows,
  extraFields,
  thumbnails,
  thumbnailsLoading,
  showTrend,
  onShowTrendChange,
  onVisibleSymbolsChange,
  onRowClick,
  activeSymbol,
}: {
  rows: ReportRow[]
  extraFields: ScreenExtraField[]
  thumbnails: Map<string, ReportThumbnailItem["ohlc"]>
  thumbnailsLoading: boolean
  showTrend: boolean
  onShowTrendChange: (value: boolean) => void
  onVisibleSymbolsChange: (symbols: string[]) => void
  onRowClick: (symbol: string) => void
  activeSymbol: string | null
}) {
  const [sorting, setSorting] = useState<SortingState>([])
  const [globalFilter, setGlobalFilter] = useState("")
  const [boardFilter, setBoardFilter] = useState<string | undefined>(undefined)

  const boardOptions = useMemo(() => {
    const seen = new Set<string>()
    for (const row of rows) {
      const board = String(row.board ?? "")
      if (board) seen.add(board)
    }
    return [...seen].sort()
  }, [rows])

  const columns = useMemo<ColumnDef<ReportRow>[]>(() => {
    const base: ColumnDef<ReportRow>[] = [
      {
        id: "name",
        header: "名称 / 代码",
        accessorFn: (row) => `${row.name ?? ""} ${row.symbol ?? ""}`,
        cell: ({ row }) => (
          <div className="min-w-[120px]">
            <div className="font-medium">{String(row.original.name ?? "—")}</div>
            <div className="text-[11px] text-muted-foreground">
              {String(row.original.symbol ?? "")}
            </div>
          </div>
        ),
        sortingFn: (rowA, rowB) =>
          String(rowA.original.name ?? "").localeCompare(
            String(rowB.original.name ?? ""),
            "zh-Hans-CN",
          ),
      },
    ]

    if (showTrend) {
      base.push({
        id: "trend",
        header: "走势",
        enableSorting: false,
        enableGlobalFilter: false,
        cell: ({ row }) => {
          const symbol = String(row.original.symbol ?? "")
          if (thumbnailsLoading) {
            return <Skeleton className="h-[48px] w-[140px]" />
          }
          return <KlineThumbnail bars={thumbnails.get(symbol)} />
        },
      })
    }

    base.push(
      {
        id: "board",
        header: "板块",
        accessorKey: "board",
        sortDescFirst: false,
        filterFn: (row, _columnId, filterValue) =>
          String(row.original.board ?? "") === String(filterValue ?? ""),
        cell: ({ row }) => (
          <Badge variant="outline">{boardLabel(row.original.board)}</Badge>
        ),
        sortingFn: (rowA, rowB) =>
          boardLabel(rowA.original.board).localeCompare(
            boardLabel(rowB.original.board),
            "zh-Hans-CN",
          ),
      },
      {
        id: "close",
        sortDescFirst: false,
        header: "收盘",
        accessorKey: "close",
        cell: ({ row }) => formatFactorValue("close", row.original.close),
        sortingFn: numericSort,
      },
      {
        id: "pct_chg",
        sortDescFirst: false,
        header: "涨跌",
        accessorKey: "pct_chg",
        cell: ({ row }) => (
          <span className={pctTone(row.original.pct_chg)}>
            {formatFactorValue("pct_chg", row.original.pct_chg)}
          </span>
        ),
        sortingFn: numericSort,
      },
      {
        id: "holder_change_pct",
        sortDescFirst: false,
        header: "股东变化",
        accessorKey: "holder_change_pct",
        cell: ({ row }) =>
          formatFactorValue("holder_change_pct", row.original.holder_change_pct),
        sortingFn: numericSort,
      },
      {
        id: "revenue_yoy",
        sortDescFirst: false,
        header: "营收同比",
        accessorKey: "revenue_yoy",
        cell: ({ row }) => formatFactorValue("revenue_yoy", row.original.revenue_yoy),
        sortingFn: numericSort,
      },
      {
        id: "net_profit_yoy",
        sortDescFirst: false,
        header: "净利同比",
        accessorKey: "net_profit_yoy",
        cell: ({ row }) =>
          formatFactorValue("net_profit_yoy", row.original.net_profit_yoy),
        sortingFn: numericSort,
      },
      {
        id: "roe",
        sortDescFirst: false,
        header: "ROE",
        accessorKey: "roe",
        cell: ({ row }) => formatFactorValue("roe", row.original.roe),
        sortingFn: numericSort,
      },
    )

    for (const extra of extraFields) {
      base.push({
        id: extra.alias,
        header: `${extra.label}${extra.params?.days ? `(${extra.params.days}日)` : ""}`,
        accessorKey: extra.alias,
        sortDescFirst: false,
        cell: ({ row }) => formatFactorValue(extra.field, row.original[extra.alias]),
        sortingFn: numericSort,
      })
    }

    return base
  }, [extraFields, showTrend, thumbnails, thumbnailsLoading])

  // 注意：columnFilters 必须保持引用稳定，否则 TanStack Table 会在每次渲染时
  // 认为过滤条件变化并触发 setState，导致无限重渲染。
  const columnFilters = useMemo(
    () => (boardFilter ? [{ id: "board", value: boardFilter }] : []),
    [boardFilter],
  )

  const table = useReactTable({
    data: rows,
    columns,
    state: {
      sorting,
      globalFilter,
      columnFilters,
    },
    onSortingChange: setSorting,
    onGlobalFilterChange: setGlobalFilter,
    globalFilterFn: (row, _columnId, filterValue) => {
      const query = String(filterValue ?? "").trim().toLowerCase()
      if (!query) return true
      return (
        String(row.original.name ?? "").toLowerCase().includes(query) ||
        String(row.original.symbol ?? "").toLowerCase().includes(query)
      )
    },
    getCoreRowModel: getCoreRowModel(),
    getSortedRowModel: getSortedRowModel(),
    getFilteredRowModel: getFilteredRowModel(),
    getPaginationRowModel: getPaginationRowModel(),
    initialState: { pagination: { pageSize: PAGE_SIZE } },
  })

  const visibleRows = table.getRowModel().rows
  const visibleSymbols = visibleRows
    .map((row) => String(row.original.symbol ?? ""))
    .filter(Boolean)
  const visibleKey = visibleSymbols.join(",")

  useEffect(() => {
    onVisibleSymbolsChange(visibleKey ? visibleKey.split(",") : [])
  }, [visibleKey, onVisibleSymbolsChange])

  const filteredCount = table.getFilteredRowModel().rows.length
  const pageCount = Math.max(1, table.getPageCount())
  const pageIndex = table.getState().pagination.pageIndex
  const hasFilters = Boolean(globalFilter) || Boolean(boardFilter)
  const hasSorting = sorting.length > 0

  function resetFilters() {
    setGlobalFilter("")
    setBoardFilter(undefined)
    setSorting([])
    table.setPageIndex(0)
  }

  return (
    <div className="flex flex-col gap-3">
      <div className="flex flex-col gap-2 md:flex-row md:items-center md:justify-between">
        <div className="flex flex-wrap items-center gap-2">
          <div className="relative w-full sm:w-56">
            <SearchIcon className="pointer-events-none absolute top-1/2 left-2.5 size-4 -translate-y-1/2 text-muted-foreground" />
            <Input
              aria-label="搜索名称或代码"
              className="pl-8"
              placeholder="搜索名称 / 代码"
              value={globalFilter}
              onChange={(event) => {
                setGlobalFilter(event.target.value)
                table.setPageIndex(0)
              }}
            />
          </div>
          <Select
            value={boardFilter ?? "all"}
            onValueChange={(value) => {
              setBoardFilter(value === "all" ? undefined : value)
              table.setPageIndex(0)
            }}
          >
            <SelectTrigger aria-label="板块筛选" className="w-32">
              <SelectValue placeholder="全部板块" />
            </SelectTrigger>
            <SelectContent>
              <SelectGroup>
                <SelectItem value="all">全部板块</SelectItem>
                {boardOptions.map((board) => (
                  <SelectItem key={board} value={board}>
                    {boardLabel(board)}
                  </SelectItem>
                ))}
              </SelectGroup>
            </SelectContent>
          </Select>
          <Button
            variant="outline"
            size="sm"
            disabled={!hasFilters && !hasSorting}
            onClick={resetFilters}
          >
            重置
          </Button>
          <Badge variant="secondary">
            筛选出 {filteredCount.toLocaleString()} 只
          </Badge>
        </div>
        <div className="flex items-center gap-2">
          <Switch
            id="report-trend-toggle"
            checked={showTrend}
            onCheckedChange={onShowTrendChange}
          />
          <Label className="text-xs text-muted-foreground" htmlFor="report-trend-toggle">
            走势
          </Label>
        </div>
      </div>

      <div className="overflow-x-auto rounded-lg border">
        <Table className="min-w-[1080px]">
          <TableHeader>
            {table.getHeaderGroups().map((headerGroup) => (
              <TableRow key={headerGroup.id}>
                {headerGroup.headers.map((header) => {
                  const sorted = header.column.getIsSorted()
                  return (
                    <TableHead
                      key={header.id}
                      aria-sort={
                        sorted === "asc"
                          ? "ascending"
                          : sorted === "desc"
                            ? "descending"
                            : undefined
                      }
                    >
                      {header.isPlaceholder ? null : header.column.getCanSort() ? (
                        <Button
                          variant="ghost"
                          size="sm"
                          className="-ml-2 h-7 gap-1 px-1.5 text-xs"
                          onClick={header.column.getToggleSortingHandler()}
                        >
                          {flexRender(
                            header.column.columnDef.header,
                            header.getContext(),
                          )}
                          <SortIcon sorted={sorted} />
                        </Button>
                      ) : (
                        flexRender(
                          header.column.columnDef.header,
                          header.getContext(),
                        )
                      )}
                    </TableHead>
                  )
                })}
              </TableRow>
            ))}
          </TableHeader>
          <TableBody>
            {visibleRows.length ? (
              visibleRows.map((row) => {
                const symbol = String(row.original.symbol ?? "")
                const active = symbol === activeSymbol
                return (
                  <TableRow
                    key={row.id}
                    className={active ? "bg-muted/60" : "cursor-pointer"}
                    onClick={() => onRowClick(symbol)}
                  >
                    {row.getVisibleCells().map((cell) => (
                      <TableCell key={cell.id}>
                        {flexRender(cell.column.columnDef.cell, cell.getContext())}
                      </TableCell>
                    ))}
                  </TableRow>
                )
              })
            ) : (
              <TableRow>
                <TableCell
                  className="py-10 text-center text-sm text-muted-foreground"
                  colSpan={columns.length}
                >
                  没有符合条件的标的
                </TableCell>
              </TableRow>
            )}
          </TableBody>
        </Table>
      </div>

      <div className="flex flex-wrap items-center justify-between gap-2 text-xs text-muted-foreground">
        <span>
          第 {pageIndex + 1} / {pageCount} 页 · 共 {filteredCount.toLocaleString()} 只
          {filteredCount !== rows.length
            ? `（全部 ${rows.length.toLocaleString()} 只）`
            : ""}
        </span>
        <div className="flex gap-2">
          <Button
            variant="outline"
            size="sm"
            disabled={!table.getCanPreviousPage()}
            onClick={() => table.previousPage()}
          >
            上一页
          </Button>
          <Button
            variant="outline"
            size="sm"
            disabled={!table.getCanNextPage()}
            onClick={() => table.nextPage()}
          >
            下一页
          </Button>
        </div>
      </div>
    </div>
  )
}
