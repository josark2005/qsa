export type UserRole = "admin" | "user";

export interface User {
  id: string;
  username: string;
  role: UserRole;
  is_active: boolean;
  must_change_password: boolean;
  quota_overrides: Record<string, number> | null;
  created_at: string;
  last_login_at: string | null;
}

export interface LoginResponse {
  user: User;
  csrf_token: string;
}

export interface MeResponse {
  user: User;
  csrf_token: string;
}

export interface MessageResponse {
  message: string;
}

export type SettingType = "int" | "float" | "bool" | "str" | "cron";

export interface SettingMeta {
  key: string;
  label: string;
  group: string;
  group_label: string;
  type: SettingType;
  description: string;
  default: unknown;
  minimum: number | null;
  maximum: number | null;
  choices: string[] | null;
  dynamic: boolean;
  requires_restart: boolean;
  public: boolean;
  sensitive: boolean;
  multiline: boolean;
  order: number;
  tags: string[];
  value: unknown;
  version: number;
  is_default: boolean;
  updated_by: string | null;
  updated_by_username: string | null;
  updated_at: string | null;
}

export interface SettingsResponse {
  items: SettingMeta[];
  loaded_at: string | null;
}

export interface SettingUpdateItem {
  key: string;
  value: unknown;
  version: number;
}

export interface PublicSettingsResponse {
  values: Record<string, unknown>;
  loaded_at: string | null;
}

export interface UserListResponse {
  items: User[];
  total: number;
  limit: number;
  offset: number;
}

export interface QuotaOverrides {
  screen_per_minute?: number | null;
  screen_per_day?: number | null;
  export_per_day?: number | null;
}

export interface CreateUserPayload {
  username: string;
  role: UserRole;
  password?: string | null;
}

export interface UpdateUserPayload {
  role: UserRole;
  is_active: boolean;
  quota_overrides: QuotaOverrides | null;
}

export interface UserCreateResponse {
  user: User;
  temporary_password: string | null;
}

export interface AuditLog {
  id: string;
  actor_id: string | null;
  action: string;
  target_type: string;
  target_id: string;
  before: Record<string, unknown> | null;
  after: Record<string, unknown> | null;
  ip: string | null;
  created_at: string;
}

export interface AuditLogListResponse {
  items: AuditLog[];
  total: number;
  limit: number;
  offset: number;
}

export interface SyncRun {
  id: number;
  run_id: string;
  pipeline: string;
  step: string;
  status: string;
  trigger: string;
  target_date: string | null;
  started_at: string;
  finished_at: string | null;
  rows_affected: number | null;
  error: string | null;
  meta: Record<string, unknown> | null;
}

export interface SyncRunListResponse {
  items: SyncRun[];
  total: number;
  limit: number;
  offset: number;
}

export type FactorValueType = "number" | "bool" | "string"

export interface FactorParam {
  name: string
  label: string
  type: "int" | "float" | "str"
  default: unknown
  minimum: number | null
  maximum: number | null
  choices: string[] | null
}

export interface FactorMeta {
  key: string
  label: string
  category: string
  category_label: string
  value_type: FactorValueType
  ops: string[]
  description: string
  unit: string
  params: FactorParam[]
  choices: string[]
  choice_labels: Record<string, string>
  value_min: number | null
  value_max: number | null
  display: boolean
  sortable: boolean
}

export interface FactorListResponse {
  items: FactorMeta[]
  categories: Array<{ key: string; label: string }>
}

export interface ScreenCondition {
  field: string
  op: string
  value: unknown
  params: Record<string, unknown>
}

export interface ScreenSort {
  field: string
  order: "asc" | "desc"
  params: Record<string, unknown>
}

export interface ScreenRequest {
  as_of?: string | null
  logic: "and" | "or"
  conditions: ScreenCondition[]
  sort: ScreenSort[]
  page: number
  page_size: number
}

export interface ScreenExtraField {
  alias: string
  label: string
  field: string
  params: Record<string, unknown>
}

export interface ScreenResponse {
  as_of: string
  total: number
  page: number
  page_size: number
  rows: Array<Record<string, unknown>>
  extra_fields: ScreenExtraField[]
}

export interface ReportConditionView {
  field: string
  label: string
  op: string
  op_label: string
  value: unknown
  value_text: string
  params: Record<string, unknown>
  params_text: string
}

export interface ReportSortView {
  field: string
  label: string
  order: "asc" | "desc"
  order_label: string
  params: Record<string, unknown>
  params_text: string
}

export interface ReportResponse {
  token: string
  title: string
  generated_at: string
  report_date: string
  screen_as_of: string
  data_as_of: string | null
  total: number
  included: number
  truncated: boolean
  rows: Array<Record<string, unknown>>
  condition_views: ReportConditionView[]
  sort_views: ReportSortView[]
  extra_fields: ScreenExtraField[]
  criteria: {
    logic: "and" | "or"
    conditions: ScreenCondition[]
    sort: ScreenSort[]
  }
  stale: boolean
  stale_reason: string | null
  latest_trade_date: string | null
}

export interface ReportHistoryItem {
  token: string
  title: string
  generated_at: string
  report_date: string
  screen_as_of: string
  data_as_of: string | null
  total: number
  included: number
  truncated: boolean
  condition_views: ReportConditionView[]
  stale: boolean
  stale_reason: string | null
  owner_username: string | null
}

export interface ReportHistoryResponse {
  items: ReportHistoryItem[]
  total: number
  limit: number
  offset: number
  latest_trade_date: string | null
}

export interface ReportThumbnailItem {
  symbol: string
  ohlc: Array<[number | null, number | null, number | null, number | null]>
}

export interface ReportThumbnailResponse {
  as_of: string
  items: ReportThumbnailItem[]
}

export interface TechnicalPeriod {
  dates: string[]
  ohlc: Array<[number | null, number | null, number | null, number | null]>
  volume: number[]
  ma: { ma5: Array<number | null>; ma10: Array<number | null>; ma20: Array<number | null> }
  expma: {
    expma20: Array<number | null>
    expma50: Array<number | null>
  }
  macd: {
    dif: Array<number | null>
    dea: Array<number | null>
    hist: Array<number | null>
  }
  kdj: { k: Array<number | null>; d: Array<number | null>; j: Array<number | null> }
  rsi: {
    rsi6: Array<number | null>
    rsi12: Array<number | null>
    rsi24: Array<number | null>
  }
  latest: {
    trade_date: string
    close: number | null
    pct_chg: number | null
    ma5: number | null
    ma10: number | null
    ma20: number | null
    expma20: number | null
    expma50: number | null
    macd: { dif: number | null; dea: number | null; hist: number | null }
    kdj: { k: number | null; d: number | null; j: number | null }
    rsi: { rsi6: number | null; rsi12: number | null; rsi24: number | null }
  } | null
  bar_count: number
  limited_history: boolean
}

export interface TechnicalResponse {
  symbol: string
  name: string
  as_of: string
  periods: Record<"daily" | "weekly" | "monthly", TechnicalPeriod>
  params: Record<string, number[]>
  indicator_params?: Record<string, number[]>
  message: string | null
}

export interface InstrumentSearchItem {
  symbol: string
  code: string
  name: string
  board: string | null
  industry: string | null
}

export interface InstrumentSearchResponse {
  items: InstrumentSearchItem[]
}

export interface QQGroupSubscription {
  id: number
  group_openid: string
  group_name: string | null
  active: boolean
  subscribed_at: string
  last_pushed_at: string | null
}

export interface QQSubscriptionListResponse {
  items: QQGroupSubscription[]
}

export interface QQBotStatus {
  configured: boolean
  enabled: boolean
  transport: "websocket" | "webhook"
  subscribed_groups: number
  websocket_connected: boolean
  last_event: string | null
  last_event_at: string | null
  last_error: string | null
  webhook_path: string
}

export interface PersonalQQStatus {
  enabled: boolean
  configured: boolean
  connected: boolean
  last_event: string | null
  last_event_at: string | null
  last_error: string | null
  groups: number
  http_url: string
  ws_url: string
  self_qq: string
  llm_enabled: boolean
  llm_configured: boolean
  llm_model: string
  llm_base_url: string
  llm_last_error: string | null
  llm_tools_enabled: boolean
  llm_web_search_enabled: boolean
  chat_groups: number
  chat_mode_ttl_seconds: number
  chat_mode_users: PersonalQQChatMode[]
}

export interface PersonalQQChatMode {
  group_id: string
  group_name: string | null
  mode: string
  enabled_by: string
  enabled_at: string | null
  ttl_seconds: number
}

export interface PersonalQQGroup {
  id: number
  group_id: string
  group_name: string | null
  active: boolean
  subscribed_at: string | null
  last_pushed_at: string | null
}

export interface PersonalQQGroupListResponse {
  items: PersonalQQGroup[]
}

export interface PersonalQQTestResponse {
  ok: boolean
  error: string | null
  retcode: number | null
}

export interface SyncRequestCreate {
  kind: "pipeline" | "backfill" | "full_backfill" | "history_import"
  pipeline?: string | null
  target_date?: string | null
  start?: string | null
  end?: string | null
  asset_type?: "stock" | "etf" | "all"
  source?: "tencent" | "baostock" | "full"
  limit?: number | null
  include_delisted?: boolean
  rebuild_limits?: boolean
  full_refresh?: boolean
  force?: boolean
}

export interface SyncRequestProgress {
  phase?: string
  processed?: number
  total?: number
  rows?: number
  errors?: number
  current?: string | null
  percent?: number
  message?: string
  source?: string
  asset_type?: string
  elapsed_seconds?: number
  eta_seconds?: number
  network_bytes?: number
  network_requests?: number
}

export interface SyncRequest {
  id: number
  kind: string
  pipeline: string | null
  payload: Record<string, unknown>
  status: string
  requested_by: string | null
  created_at: string
  started_at: string | null
  finished_at: string | null
  error: string | null
  result: Record<string, unknown> | null
  progress: SyncRequestProgress | null
  updated_at: string | null
}

export interface SyncRequestListResponse {
  items: SyncRequest[]
  total: number
  limit: number
  offset: number
}

export type WorkerLogLevel = "DEBUG" | "INFO" | "WARNING" | "ERROR" | "CRITICAL"

export interface WorkerLogItem {
  id: number
  created_at: string
  level: WorkerLogLevel
  logger: string
  message: string
  request_id: number | null
}

export interface WorkerLogListResponse {
  items: WorkerLogItem[]
  has_more: boolean
}

export interface SyncStatus {
  instruments: Array<{
    asset_type: string;
    status: string;
    count: number;
  }>;
  calendar_latest_open_date: string | null;
  calendar_latest_past_open_date: string | null;
  calendar_min_date: string | null;
  calendar_max_date: string | null;
  calendar_open_days: number;
  daily_bars: {
    stock: { latest_date: string | null; rows: number };
    etf: { latest_date: string | null; rows: number };
    total_rows: number;
    min_date: string | null;
    max_date: string | null;
  };
  financial_reports: {
    latest_period: string | null;
    latest_announce_date: string | null;
    rows: number;
    total_rows: number;
    period_min: string | null;
    period_max: string | null;
  };
  shareholder_counts: {
    latest_period: string | null;
    latest_announce_date: string | null;
    rows: number;
    total_rows: number;
    period_min: string | null;
    period_max: string | null;
    periods: Array<{ period_end: string; rows: number }>;
  };
  history: {
    directory: string;
    archive_files: number;
    archive_bytes: number;
    imported_files: number;
    imported_rows: number;
    archive_expected_rows: number;
    pending_files: number;
    daily_bar_rows: number;
    needs_import: boolean;
    needs_force_import: boolean;
  };
  recent_runs: SyncRun[];
  worker_full_backfill_ready: boolean;
  loaded_at: string | null;
}
