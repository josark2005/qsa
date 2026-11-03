import { useMemo } from "react"
import { useQuery } from "@tanstack/react-query"
import { AlertTriangle, Plus } from "lucide-react"

import { Alert, AlertDescription } from "@/components/ui/alert"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { Skeleton } from "@/components/ui/skeleton"
import {
  ConditionRow,
  type DraftCondition,
  makeCondition,
  newId,
} from "@/pages/ScreenerPage"
import { api } from "@/lib/api"
import type { FactorMeta } from "@/lib/types"

function parseConditions(value: string): {
  conditions: DraftCondition[]
  error: string | null
} {
  try {
    const parsed = JSON.parse(value) as unknown
    if (!Array.isArray(parsed)) {
      return { conditions: [], error: "默认条件不是 JSON 数组" }
    }
    const conditions = parsed
      .filter(
        (item): item is Record<string, unknown> =>
          typeof item === "object" &&
          item !== null &&
          typeof item.field === "string",
      )
      .map((item) => ({
        id: newId(),
        field: String(item.field),
        op: String(item.op ?? "eq"),
        value: item.value,
        params:
          typeof item.params === "object" && item.params !== null
            ? (item.params as Record<string, unknown>)
            : {},
      }))
    return { conditions, error: null }
  } catch {
    return { conditions: [], error: "默认条件不是合法 JSON" }
  }
}

export function DefaultConditionsField({
  value,
  error,
  onChange,
}: {
  value: string
  error?: string
  onChange: (value: string) => void
}) {
  const factorsQuery = useQuery({
    queryKey: ["factor-metadata"],
    queryFn: () => api.listFactors(),
    staleTime: 5 * 60_000,
  })
  const factors = factorsQuery.data?.items ?? []
  const categories = factorsQuery.data?.categories ?? []
  const factorMap = useMemo(
    () => new Map(factors.map((factor) => [factor.key, factor])),
    [factors],
  )
  const parsed = useMemo(() => parseConditions(value), [value])
  const conditions = parsed.conditions.filter((item) =>
    factorMap.has(item.field),
  )

  function emit(next: DraftCondition[]) {
    onChange(
      JSON.stringify(
        next.map((condition) => ({
          field: condition.field,
          op: condition.op,
          value: condition.value,
          params: condition.params,
        })),
      ),
    )
  }

  function addCondition() {
    const factor =
      factorMap.get("holder_change_pct") ??
      factors.find((item) => item.value_type === "number")
    if (!factor) return
    emit([...conditions, makeCondition(factor)])
  }

  return (
    <div className="space-y-3 px-5 py-5">
      <div className="flex flex-col gap-3 sm:flex-row sm:items-start sm:justify-between">
        <div className="space-y-1">
          <div className="flex flex-wrap items-center gap-2">
            <span className="text-sm font-medium">默认筛选条件</span>
            <Badge variant="outline">与选股页一致</Badge>
          </div>
          <p className="text-xs leading-5 text-muted-foreground">
            网页筛选和 QQ 机器人新建会话时自动带入。修改后保存即可生效。
          </p>
          <code className="text-[11px] text-muted-foreground">
            screening.default_conditions
          </code>
        </div>
        <Button
          variant="outline"
          size="sm"
          onClick={addCondition}
          disabled={!factors.length}
        >
          <Plus className="size-4" />
          添加条件
        </Button>
      </div>

      {factorsQuery.isLoading ? (
        <div className="space-y-2">
          <Skeleton className="h-16 w-full" />
          <Skeleton className="h-16 w-full" />
        </div>
      ) : factorsQuery.isError ? (
        <Alert variant="destructive">
          <AlertTriangle className="size-4" />
          <AlertDescription>
            {factorsQuery.error instanceof Error
              ? factorsQuery.error.message
              : "因子加载失败"}
          </AlertDescription>
        </Alert>
      ) : conditions.length === 0 ? (
        <div className="rounded-lg border border-dashed p-6 text-center text-sm text-muted-foreground">
          暂无默认条件，点击“添加条件”开始。
        </div>
      ) : (
        <div className="space-y-2">
          {conditions.map((condition, index) => {
            const factor = factorMap.get(condition.field) as FactorMeta
            return (
              <ConditionRow
                key={`${condition.field}-${index}`}
                condition={condition}
                factor={factor}
                factors={factors}
                categories={categories}
                onChange={(next) =>
                  emit(
                    conditions.map((item, itemIndex) =>
                      itemIndex === index ? next : item,
                    ),
                  )
                }
                onRemove={() =>
                  emit(conditions.filter((_, itemIndex) => itemIndex !== index))
                }
              />
            )
          })}
        </div>
      )}

      {parsed.error ? (
        <p className="text-xs text-destructive">{parsed.error}</p>
      ) : null}
      {error ? <p className="text-xs text-destructive">{error}</p> : null}
    </div>
  )
}
