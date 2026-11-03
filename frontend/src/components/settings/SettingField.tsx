import { Badge } from "@/components/ui/badge"
import { Input } from "@/components/ui/input"
import { Label } from "@/components/ui/label"
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select"
import { Switch } from "@/components/ui/switch"
import { formatDateTime } from "@/lib/format"
import type { SettingMeta } from "@/lib/types"
import { cn } from "@/lib/utils"

interface SettingFieldProps {
  item: SettingMeta
  value: unknown
  error?: string
  onChange: (value: unknown) => void
}

function formatDefault(meta: SettingMeta): string {
  if (typeof meta.default === "boolean") {
    return meta.default ? "开启" : "关闭"
  }
  if (meta.default === null || meta.default === undefined || meta.default === "") {
    return "—"
  }
  return String(meta.default)
}

export function SettingField({
  item,
  value,
  error,
  onChange,
}: SettingFieldProps) {
  const numericValue =
    typeof value === "number" || typeof value === "string" ? value : ""

  return (
    <div className="grid gap-4 px-5 py-5 md:grid-cols-[minmax(0,1fr)_280px] md:items-start">
      <div className="min-w-0 space-y-1.5">
        <div className="flex flex-wrap items-center gap-2">
          <Label className="text-sm font-medium">{item.label}</Label>
          {item.is_default ? (
            <Badge variant="outline">默认</Badge>
          ) : (
            <Badge variant="secondary">已修改</Badge>
          )}
          {item.public ? <Badge variant="outline">公开</Badge> : null}
          {item.sensitive ? <Badge variant="outline">敏感</Badge> : null}
          {item.requires_restart ? (
            <Badge variant="destructive">需重启</Badge>
          ) : null}
        </div>
        <p className="text-xs leading-5 text-muted-foreground">
          {item.description}
        </p>
        <div className="flex flex-wrap gap-x-3 gap-y-1 text-[11px] text-muted-foreground">
          <code className="rounded bg-muted px-1.5 py-0.5">{item.key}</code>
          <span>默认：{formatDefault(item)}</span>
          {item.updated_at ? (
            <span>
              更新：{formatDateTime(item.updated_at)}
              {item.updated_by_username ? ` · ${item.updated_by_username}` : ""}
            </span>
          ) : null}
        </div>
      </div>

      <div className="space-y-1.5">
        {item.type === "bool" ? (
          <div className="flex items-center gap-2">
            <Switch
              checked={Boolean(value)}
              onCheckedChange={(checked) => onChange(checked)}
            />
            <span className="text-sm text-muted-foreground">
              {value ? "开启" : "关闭"}
            </span>
          </div>
        ) : item.type === "int" || item.type === "float" ? (
          <Input
            type="number"
            inputMode={item.type === "int" ? "numeric" : "decimal"}
            min={item.minimum ?? undefined}
            max={item.maximum ?? undefined}
            step={item.type === "int" ? 1 : 0.01}
            value={String(numericValue)}
            aria-invalid={Boolean(error)}
            className={cn(error && "border-destructive")}
            onChange={(event) => {
              const next = event.target.value
              if (next === "") {
                onChange("")
                return
              }
              const parsed = Number(next)
              onChange(Number.isNaN(parsed) ? next : parsed)
            }}
          />
        ) : item.type === "cron" ? (
          <Input
            value={typeof value === "string" ? value : ""}
            aria-invalid={Boolean(error)}
            className={cn("font-mono text-xs", error && "border-destructive")}
            placeholder="分 时 日 月 周"
            onChange={(event) => onChange(event.target.value)}
          />
        ) : item.choices && item.choices.length > 0 ? (
          <Select
            value={typeof value === "string" ? value : undefined}
            onValueChange={onChange}
          >
            <SelectTrigger className="w-full">
              <SelectValue placeholder="请选择" />
            </SelectTrigger>
            <SelectContent>
              {item.choices.map((choice) => (
                <SelectItem key={choice} value={choice}>
                  {choice}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
        ) : item.multiline ? (
          <textarea
            rows={7}
            value={typeof value === "string" ? value : ""}
            aria-invalid={Boolean(error)}
            className={cn(
              "min-h-32 w-full resize-y rounded-md border bg-transparent px-3 py-2 font-mono text-xs shadow-xs outline-none focus-visible:border-ring focus-visible:ring-[3px] focus-visible:ring-ring/50",
              error && "border-destructive",
            )}
            onChange={(event) => onChange(event.target.value)}
          />
        ) : (
          <Input
            type={item.sensitive ? "password" : "text"}
            autoComplete={item.sensitive ? "new-password" : "off"}
            value={typeof value === "string" ? value : ""}
            aria-invalid={Boolean(error)}
            className={cn(error && "border-destructive")}
            onChange={(event) => onChange(event.target.value)}
          />
        )}

        {error ? <p className="text-xs text-destructive">{error}</p> : null}
        {item.type === "cron" ? (
          <p className="text-[11px] text-muted-foreground">
            5 段 cron，时区 Asia/Shanghai，执行间隔不小于 5 分钟。
          </p>
        ) : null}
      </div>
    </div>
  )
}
