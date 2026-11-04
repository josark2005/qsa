import type {
  AuditLogListResponse,
  CompareRequest,
  CompareResponse,
  CreateUserPayload,
  LoginResponse,
  MeResponse,
  MessageResponse,
  PublicSettingsResponse,
  SettingUpdateItem,
  SettingsResponse,
  FactorListResponse,
  InstrumentSearchResponse,
  LLMMemoryItem,
  LLMMemoryListResponse,
  PersonalQQGroup,
  PersonalQQGroupListResponse,
  PersonalQQStatus,
  PersonalQQTestResponse,
  QQBotStatus,
  QQSubscriptionListResponse,
  ReportHistoryResponse,
  ReportResponse,
  ReportThumbnailResponse,
  ScreenRequest,
  ScreenResponse,
  SyncRequest,
  TechnicalResponse,
  SyncRequestCreate,
  SyncRequestListResponse,
  SyncRunListResponse,
  SyncStatus,
  UpdateUserPayload,
  UserCreateResponse,
  UserListResponse,
  User,
  WatchItem,
  WatchListResponse,
  WorkerLogListResponse,
} from "@/lib/types"

const API_BASE = "/api"
let csrfToken: string | null = null

export class ApiError extends Error {
  status: number
  code: string
  details: unknown

  constructor(status: number, code: string, message: string, details?: unknown) {
    super(message)
    this.name = "ApiError"
    this.status = status
    this.code = code
    this.details = details
  }
}

export function setCsrfToken(token: string | null) {
  csrfToken = token
}

interface RequestOptions {
  method?: string
  body?: unknown
  signal?: AbortSignal
}

async function request<T>(path: string, options: RequestOptions = {}): Promise<T> {
  const method = options.method ?? "GET"
  const headers = new Headers()
  if (options.body !== undefined) {
    headers.set("Content-Type", "application/json")
  }
  if (csrfToken && !["GET", "HEAD", "OPTIONS"].includes(method)) {
    headers.set("X-CSRF-Token", csrfToken)
  }

  let response: Response
  try {
    response = await fetch(`${API_BASE}${path}`, {
      method,
      headers,
      credentials: "include",
      body: options.body === undefined ? undefined : JSON.stringify(options.body),
      signal: options.signal,
    })
  } catch (error) {
    if (error instanceof DOMException && error.name === "AbortError") {
      throw error
    }
    throw new ApiError(0, "network_error", "网络错误，请稍后重试")
  }

  const text = await response.text()
  let payload: Record<string, unknown> | null = null
  if (text) {
    try {
      payload = JSON.parse(text) as Record<string, unknown>
    } catch {
      payload = null
    }
  }

  if (!response.ok) {
    if (response.status === 401) {
      window.dispatchEvent(new CustomEvent("qsa:unauthorized"))
    }
    const code = typeof payload?.code === "string" ? payload.code : "http_error"
    const message =
      typeof payload?.message === "string"
        ? payload.message
        : `请求失败（${response.status}）`
    throw new ApiError(response.status, code, message, payload?.details)
  }

  return payload as T
}

function queryString(params: Record<string, string | number | undefined>): string {
  const search = new URLSearchParams()
  for (const [key, value] of Object.entries(params)) {
    if (value !== undefined && value !== "") {
      search.set(key, String(value))
    }
  }
  const encoded = search.toString()
  return encoded ? `?${encoded}` : ""
}

export const api = {
  login(username: string, password: string): Promise<LoginResponse> {
    return request<LoginResponse>("/auth/login", {
      method: "POST",
      body: { username, password },
    })
  },

  logout(): Promise<MessageResponse> {
    return request<MessageResponse>("/auth/logout", { method: "POST" })
  },

  me(): Promise<MeResponse> {
    return request<MeResponse>("/auth/me")
  },

  changePassword(
    currentPassword: string,
    newPassword: string,
  ): Promise<MessageResponse> {
    return request<MessageResponse>("/auth/change-password", {
      method: "POST",
      body: {
        current_password: currentPassword,
        new_password: newPassword,
      },
    })
  },

  adminSettings(): Promise<SettingsResponse> {
    return request<SettingsResponse>("/admin/settings")
  },

  updateSettings(items: SettingUpdateItem[]): Promise<SettingsResponse> {
    return request<SettingsResponse>("/admin/settings", {
      method: "PATCH",
      body: { items },
    })
  },

  publicSettings(): Promise<PublicSettingsResponse> {
    return request<PublicSettingsResponse>("/settings/public")
  },

  listUsers(params: {
    limit: number
    offset: number
    q?: string
    signal?: AbortSignal
  }): Promise<UserListResponse> {
    return request<UserListResponse>(
      `/admin/users${queryString({
        limit: params.limit,
        offset: params.offset,
        q: params.q,
      })}`,
      { signal: params.signal },
    )
  },

  createUser(payload: CreateUserPayload): Promise<UserCreateResponse> {
    return request<UserCreateResponse>("/admin/users", {
      method: "POST",
      body: payload,
    })
  },

  updateUser(userId: string, payload: UpdateUserPayload): Promise<User> {
    return request<User>(`/admin/users/${userId}`, {
      method: "PATCH",
      body: payload,
    })
  },

  resetUserPassword(
    userId: string,
    password?: string,
  ): Promise<UserCreateResponse> {
    return request<UserCreateResponse>(`/admin/users/${userId}/reset-password`, {
      method: "POST",
      body: { password: password || null },
    })
  },

  listAuditLogs(params: {
    limit: number
    offset: number
    action?: string
    signal?: AbortSignal
  }): Promise<AuditLogListResponse> {
    return request<AuditLogListResponse>(
      `/admin/audit-logs${queryString({
        limit: params.limit,
        offset: params.offset,
        action: params.action,
      })}`,
      { signal: params.signal },
    )
  },

  listFactors(): Promise<FactorListResponse> {
    return request<FactorListResponse>("/meta/factors")
  },

  searchInstruments(q: string): Promise<InstrumentSearchResponse> {
    return request<InstrumentSearchResponse>(
      `/instruments/search${queryString({ q })}`,
    )
  },

  instrumentTechnical(symbol: string): Promise<TechnicalResponse> {
    return request<TechnicalResponse>(
      `/instruments/${encodeURIComponent(symbol)}/technical`,
    )
  },

  listWatchItems(signal?: AbortSignal): Promise<WatchListResponse> {
    return request<WatchListResponse>("/watchlist", { signal })
  },

  addWatchItem(payload: {
    symbol: string
    note?: string | null
  }): Promise<WatchItem> {
    return request<WatchItem>("/watchlist", {
      method: "POST",
      body: payload,
    })
  },

  updateWatchItem(
    id: number,
    payload: { note?: string | null; active?: boolean },
  ): Promise<WatchItem> {
    return request<WatchItem>(`/watchlist/${id}`, {
      method: "PATCH",
      body: payload,
    })
  },

  removeWatchItem(id: number): Promise<{ ok: boolean }> {
    return request<{ ok: boolean }>(`/watchlist/${id}`, {
      method: "DELETE",
    })
  },

  runScreen(payload: ScreenRequest): Promise<ScreenResponse> {
    return request<ScreenResponse>("/screen", {
      method: "POST",
      body: payload,
    })
  },

  compareScreen(payload: CompareRequest): Promise<CompareResponse> {
    return request<CompareResponse>("/screen/compare", {
      method: "POST",
      body: payload,
    })
  },

  listReports(params: {
    limit: number
    offset: number
    q?: string
    signal?: AbortSignal
  }): Promise<ReportHistoryResponse> {
    return request<ReportHistoryResponse>(
      `/reports${queryString({
        limit: params.limit,
        offset: params.offset,
        q: params.q,
      })}`,
      { signal: params.signal },
    )
  },

  createReport(payload: ScreenRequest & { title?: string }): Promise<ReportResponse> {
    return request<ReportResponse>("/reports", {
      method: "POST",
      body: payload,
    })
  },

  publicReport(token: string): Promise<ReportResponse> {
    return request<ReportResponse>(`/public/reports/${encodeURIComponent(token)}`)
  },

  qqBotStatus(): Promise<QQBotStatus> {
    return request<QQBotStatus>("/qq/status")
  },

  qqSubscriptions(): Promise<QQSubscriptionListResponse> {
    return request<QQSubscriptionListResponse>("/admin/qq/subscriptions")
  },

  personalQQStatus(signal?: AbortSignal): Promise<PersonalQQStatus> {
    return request<PersonalQQStatus>("/admin/personal-qq/status", { signal })
  },

  listPersonalQQGroups(signal?: AbortSignal): Promise<PersonalQQGroupListResponse> {
    return request<PersonalQQGroupListResponse>("/admin/personal-qq/groups", {
      signal,
    })
  },

  addPersonalQQGroup(payload: {
    group_id: number
    group_name?: string | null
  }): Promise<PersonalQQGroup> {
    return request<PersonalQQGroup>("/admin/personal-qq/groups", {
      method: "POST",
      body: payload,
    })
  },

  removePersonalQQGroup(groupId: number): Promise<{ ok: boolean }> {
    return request<{ ok: boolean }>(
      `/admin/personal-qq/groups/${groupId}`,
      { method: "DELETE" },
    )
  },

  listMemories(params: {
    q?: string
    page?: number
    pageSize?: number
    signal?: AbortSignal
  }): Promise<LLMMemoryListResponse> {
    const query = new URLSearchParams()
    if (params.q) query.set("q", params.q)
    if (params.page) query.set("page", String(params.page))
    if (params.pageSize) query.set("page_size", String(params.pageSize))
    const suffix = query.toString() ? `?${query.toString()}` : ""
    return request<LLMMemoryListResponse>(`/admin/memories${suffix}`, {
      signal: params.signal,
    })
  },

  getMemory(id: number, signal?: AbortSignal): Promise<LLMMemoryItem> {
    return request<LLMMemoryItem>(`/admin/memories/${id}`, { signal })
  },

  updateMemory(id: number, summary: string): Promise<LLMMemoryItem> {
    return request<LLMMemoryItem>(`/admin/memories/${id}`, {
      method: "PATCH",
      body: { summary },
    })
  },

  removeMemory(id: number): Promise<{ ok: boolean }> {
    return request<{ ok: boolean }>(`/admin/memories/${id}`, {
      method: "DELETE",
    })
  },

  closePersonalQQChatMode(groupId: string): Promise<{ ok: boolean }> {
    return request<{ ok: boolean }>(
      `/admin/personal-qq/chat-mode?group_id=${encodeURIComponent(groupId)}`,
      { method: "DELETE" },
    )
  },

  testPersonalQQSend(groupId: number): Promise<PersonalQQTestResponse> {
    return request<PersonalQQTestResponse>("/admin/personal-qq/test", {
      method: "POST",
      body: { group_id: groupId },
    })
  },

  reportTechnical(token: string, symbol: string): Promise<TechnicalResponse> {
    return request<TechnicalResponse>(
      `/public/reports/${encodeURIComponent(token)}/technical${queryString({
        symbol,
      })}`,
    )
  },

  reportThumbnails(
    token: string,
    symbols: string[],
  ): Promise<ReportThumbnailResponse> {
    return request<ReportThumbnailResponse>(
      `/public/reports/${encodeURIComponent(token)}/thumbnails${queryString({
        symbols: symbols.join(","),
      })}`,
    )
  },

  syncStatus(): Promise<SyncStatus> {
    return request<SyncStatus>("/admin/sync/status")
  },

  createSyncRequest(payload: SyncRequestCreate): Promise<SyncRequest> {
    return request<SyncRequest>("/admin/sync/requests", {
      method: "POST",
      body: payload,
    })
  },

  cancelSyncRequest(requestId: number): Promise<SyncRequest> {
    return request<SyncRequest>(`/admin/sync/requests/${requestId}/cancel`, {
      method: "POST",
    })
  },

  listSyncRequests(params: {
    limit: number
    offset: number
    signal?: AbortSignal
  }): Promise<SyncRequestListResponse> {
    return request<SyncRequestListResponse>(
      `/admin/sync/requests${queryString({
        limit: params.limit,
        offset: params.offset,
      })}`,
      { signal: params.signal },
    )
  },

  syncRuns(params: {
    limit: number
    offset: number
    pipeline?: string
    status?: string
    signal?: AbortSignal
  }): Promise<SyncRunListResponse> {
    return request<SyncRunListResponse>(
      `/admin/sync/runs${queryString({
        limit: params.limit,
        offset: params.offset,
        pipeline: params.pipeline,
        status: params.status,
      })}`,
      { signal: params.signal },
    )
  },

  workerLogs(params: {
    level?: string
    requestId?: number
    q?: string
    afterId?: number
    beforeId?: number
    limit?: number
    signal?: AbortSignal
  }): Promise<WorkerLogListResponse> {
    return request<WorkerLogListResponse>(
      `/admin/logs${queryString({
        level: params.level,
        request_id: params.requestId,
        q: params.q,
        after_id: params.afterId,
        before_id: params.beforeId,
        limit: params.limit,
      })}`,
      { signal: params.signal },
    )
  },
}
