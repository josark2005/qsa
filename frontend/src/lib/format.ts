import type { SettingMeta, UserRole } from "@/lib/types"

export function formatDateTime(value: string | null | undefined): string {
  if (!value) return "—"
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return value
  return date.toLocaleString("zh-CN", { hour12: false })
}

export function formatDate(value: string | null | undefined): string {
  if (!value) return "—"
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return value
  return date.toLocaleDateString("zh-CN")
}

export function roleLabel(role: UserRole): string {
  return role === "admin" ? "管理员" : "用户"
}

export function formatSettingValue(meta: SettingMeta): string {
  const value = meta.value
  if (typeof value === "boolean") return value ? "开启" : "关闭"
  if (value === null || value === undefined || value === "") return "—"
  if (meta.type === "float") return String(value)
  return String(value)
}

const PERCENT_FIELDS = new Set([
  "pct_chg",
  "turnover_rate",
  "holder_change_pct",
  "revenue_yoy",
  "net_profit_yoy",
  "roe",
  "gross_margin",
  "debt_ratio",
  "window_price_range_pct",
])

export function pctTone(value: unknown): string {
  const number = Number(value)
  if (Number.isNaN(number) || number === 0) return ""
  return number > 0 ? "text-red-600" : "text-emerald-600"
}

export function formatFactorValue(
  field: string,
  value: unknown,
): string {
  if (value === null || value === undefined || value === "") return "—"
  const number = Number(value)
  if (Number.isNaN(number)) return String(value)
  if (field === "close_pct_rank" || field === "price_from_low_pct") {
    return `${(number * 100).toFixed(1)}%`
  }
  if (PERCENT_FIELDS.has(field)) {
    return `${number.toFixed(2)}%`
  }
  if (field === "operating_cash_flow" || field === "avg_amount") {
    const abs = Math.abs(number)
    if (abs >= 100_000_000) return `${(number / 100_000_000).toFixed(2)}亿`
    if (abs >= 10_000) return `${(number / 10_000).toFixed(2)}万`
    return number.toFixed(0)
  }
  if (
    field === "holder_num" ||
    field === "holder_change" ||
    field === "list_days" ||
    field.startsWith("limit_")
  ) {
    return Math.round(number).toLocaleString("zh-CN")
  }
  if (field === "pe_ttm" || field === "pb" || field === "close" || field === "eps") {
    return number.toFixed(2)
  }
  return number.toLocaleString("zh-CN", { maximumFractionDigits: 4 })
}

export function actionLabel(action: string): string {
  const labels: Record<string, string> = {
    "settings.update": "修改系统设置",
    "user.create": "创建用户",
    "user.update": "修改用户",
    "user.reset_password": "重置密码",
    "user.bootstrap_admin": "初始化管理员",
    "sync.request": "提交同步请求",
    "report.create": "生成选股报表",
  }
  return labels[action] ?? action
}
