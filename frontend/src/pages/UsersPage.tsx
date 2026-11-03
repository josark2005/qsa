import { useEffect, useState, type FormEvent } from "react"
import {
  keepPreviousData,
  useMutation,
  useQuery,
} from "@tanstack/react-query"
import {
  AlertTriangle,
  Check,
  Copy,
  KeyRound,
  Loader2,
  MoreHorizontal,
  Pencil,
  Plus,
  RefreshCw,
  Search,
} from "lucide-react"
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
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog"
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu"
import { Input } from "@/components/ui/input"
import { Label } from "@/components/ui/label"
import {
  Select,
  SelectContent,
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
import { api, ApiError } from "@/lib/api"
import { formatDateTime, roleLabel } from "@/lib/format"
import type { QuotaOverrides, User, UserRole } from "@/lib/types"

const PAGE_SIZE = 50

interface TempPasswordInfo {
  username: string
  password: string
}

function errorMessage(error: unknown): string {
  if (error instanceof ApiError) return error.message
  if (error instanceof Error) return error.message
  return "操作失败，请稍后重试"
}

function TempPasswordDialog({
  info,
  onClose,
}: {
  info: TempPasswordInfo | null
  onClose: () => void
}) {
  const [copied, setCopied] = useState(false)

  useEffect(() => {
    setCopied(false)
  }, [info?.password])

  if (!info) return null

  async function handleCopy() {
    try {
      await navigator.clipboard.writeText(info!.password)
      setCopied(true)
      toast.success("临时密码已复制")
    } catch {
      toast.error("复制失败，请手动选择复制")
    }
  }

  return (
    <Dialog open onOpenChange={(open) => !open && onClose()}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>临时密码</DialogTitle>
          <DialogDescription>
            该密码只显示一次，请通过安全渠道交给用户。首次登录后必须修改密码。
          </DialogDescription>
        </DialogHeader>
        <div className="space-y-3">
          <div className="text-sm">
            用户：<span className="font-medium">{info.username}</span>
          </div>
          <div className="flex items-center gap-2 rounded-lg border bg-muted/40 p-3">
            <code className="min-w-0 flex-1 break-all font-mono text-sm">
              {info.password}
            </code>
            <Button variant="outline" size="icon-sm" onClick={handleCopy}>
              {copied ? <Check className="size-4" /> : <Copy className="size-4" />}
            </Button>
          </div>
          <Alert>
            <AlertTriangle className="size-4" />
            <AlertDescription>
              关闭后无法再次查看该密码，只能重新重置。
            </AlertDescription>
          </Alert>
        </div>
        <DialogFooter>
          <Button onClick={onClose}>我已保存</Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}

function CreateUserDialog({
  open,
  onOpenChange,
  onCreated,
}: {
  open: boolean
  onOpenChange: (open: boolean) => void
  onCreated: (info: TempPasswordInfo | null) => void
}) {
  const [username, setUsername] = useState("")
  const [role, setRole] = useState<UserRole>("user")
  const [password, setPassword] = useState("")

  useEffect(() => {
    if (open) {
      setUsername("")
      setRole("user")
      setPassword("")
    }
  }, [open])

  const mutation = useMutation({
    mutationFn: () =>
      api.createUser({
        username: username.trim(),
        role,
        password: password.trim() || null,
      }),
    onSuccess: (data) => {
      onOpenChange(false)
      if (data.temporary_password) {
        onCreated({
          username: data.user.username,
          password: data.temporary_password,
        })
      } else {
        toast.success("用户已创建，首次登录需要修改密码")
        onCreated(null)
      }
    },
    onError: (error) => toast.error(errorMessage(error)),
  })

  function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    mutation.mutate()
  }

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>新建用户</DialogTitle>
          <DialogDescription>
            系统不开放注册。创建后用户首次登录必须修改密码。
          </DialogDescription>
        </DialogHeader>
        <form className="space-y-4" onSubmit={handleSubmit}>
          <div className="space-y-2">
            <Label htmlFor="new-user-username">用户名</Label>
            <Input
              id="new-user-username"
              type="text"
              autoComplete="off"
              placeholder="2-32 位，不含空格和 @"
              value={username}
              onChange={(event) => setUsername(event.target.value)}
              required
            />
          </div>
          <div className="space-y-2">
            <Label>角色</Label>
            <Select value={role} onValueChange={(value) => setRole(value as UserRole)}>
              <SelectTrigger className="w-full">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value="user">普通用户</SelectItem>
                <SelectItem value="admin">管理员</SelectItem>
              </SelectContent>
            </Select>
          </div>
          <div className="space-y-2">
            <Label htmlFor="new-user-password">初始密码</Label>
            <Input
              id="new-user-password"
              type="password"
              autoComplete="new-password"
              placeholder="留空自动生成 16 位临时密码"
              value={password}
              onChange={(event) => setPassword(event.target.value)}
            />
            <p className="text-xs text-muted-foreground">
              手动指定的密码也要求至少 12 位。
            </p>
          </div>
          <DialogFooter>
            <Button
              type="button"
              variant="outline"
              onClick={() => onOpenChange(false)}
            >
              取消
            </Button>
            <Button type="submit" disabled={mutation.isPending || !username.trim()}>
              {mutation.isPending ? (
                <Loader2 className="size-4 animate-spin" />
              ) : (
                <Plus className="size-4" />
              )}
              创建用户
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  )
}

function EditUserDialog({
  user,
  open,
  onOpenChange,
  onSaved,
}: {
  user: User | null
  open: boolean
  onOpenChange: (open: boolean) => void
  onSaved: () => void
}) {
  const [role, setRole] = useState<UserRole>("user")
  const [isActive, setIsActive] = useState(true)
  const [minuteQuota, setMinuteQuota] = useState("")
  const [dayQuota, setDayQuota] = useState("")
  const [exportQuota, setExportQuota] = useState("")

  useEffect(() => {
    if (!open || !user) return
    setRole(user.role)
    setIsActive(user.is_active)
    setMinuteQuota(
      user.quota_overrides?.screen_per_minute !== undefined
        ? String(user.quota_overrides.screen_per_minute)
        : "",
    )
    setDayQuota(
      user.quota_overrides?.screen_per_day !== undefined
        ? String(user.quota_overrides.screen_per_day)
        : "",
    )
    setExportQuota(
      user.quota_overrides?.export_per_day !== undefined
        ? String(user.quota_overrides.export_per_day)
        : "",
    )
  }, [open, user])

  const mutation = useMutation({
    mutationFn: () => {
      if (!user) throw new Error("missing user")
      const overrides: QuotaOverrides = {}
      for (const [key, raw] of [
        ["screen_per_minute", minuteQuota],
        ["screen_per_day", dayQuota],
        ["export_per_day", exportQuota],
      ] as const) {
        if (!raw.trim()) continue
        const parsed = Number(raw)
        if (!Number.isInteger(parsed) || parsed < 0) {
          throw new ApiError(422, "quota_invalid", "配额必须是非负整数")
        }
        overrides[key] = parsed
      }
      return api.updateUser(user.id, {
        role,
        is_active: isActive,
        quota_overrides:
          Object.keys(overrides).length > 0 ? overrides : null,
      })
    },
    onSuccess: () => {
      toast.success("用户已更新")
      onOpenChange(false)
      onSaved()
    },
    onError: (error) => toast.error(errorMessage(error)),
  })

  if (!user) return null

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>编辑用户</DialogTitle>
          <DialogDescription>{user.username}</DialogDescription>
        </DialogHeader>
        <div className="space-y-4">
          <div className="space-y-2">
            <Label>角色</Label>
            <Select value={role} onValueChange={(value) => setRole(value as UserRole)}>
              <SelectTrigger className="w-full">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value="user">普通用户</SelectItem>
                <SelectItem value="admin">管理员</SelectItem>
              </SelectContent>
            </Select>
          </div>
          <div className="flex items-center justify-between rounded-lg border p-3">
            <div>
              <div className="text-sm font-medium">账号启用</div>
              <div className="text-xs text-muted-foreground">
                禁用后该用户的现有会话会立即失效。
              </div>
            </div>
            <Switch checked={isActive} onCheckedChange={setIsActive} />
          </div>
          <div className="space-y-3 rounded-lg border p-3">
            <div>
              <div className="text-sm font-medium">单用户配额覆盖</div>
              <div className="text-xs text-muted-foreground">
                留空表示使用系统设置中的全局配额。
              </div>
            </div>
            <div className="grid gap-3 sm:grid-cols-3">
              <div className="space-y-1.5">
                <Label htmlFor="quota-minute" className="text-xs">
                  每分钟筛选
                </Label>
                <Input
                  id="quota-minute"
                  type="number"
                  min={0}
                  step={1}
                  value={minuteQuota}
                  onChange={(event) => setMinuteQuota(event.target.value)}
                />
              </div>
              <div className="space-y-1.5">
                <Label htmlFor="quota-day" className="text-xs">
                  每日筛选
                </Label>
                <Input
                  id="quota-day"
                  type="number"
                  min={0}
                  step={1}
                  value={dayQuota}
                  onChange={(event) => setDayQuota(event.target.value)}
                />
              </div>
              <div className="space-y-1.5">
                <Label htmlFor="quota-export" className="text-xs">
                  每日导出
                </Label>
                <Input
                  id="quota-export"
                  type="number"
                  min={0}
                  step={1}
                  value={exportQuota}
                  onChange={(event) => setExportQuota(event.target.value)}
                />
              </div>
            </div>
          </div>
        </div>
        <DialogFooter>
          <Button variant="outline" onClick={() => onOpenChange(false)}>
            取消
          </Button>
          <Button
            onClick={() => mutation.mutate()}
            disabled={mutation.isPending}
          >
            {mutation.isPending ? (
              <Loader2 className="size-4 animate-spin" />
            ) : null}
            保存
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}

function ResetPasswordDialog({
  user,
  open,
  onOpenChange,
  onReset,
}: {
  user: User | null
  open: boolean
  onOpenChange: (open: boolean) => void
  onReset: (info: TempPasswordInfo | null) => void
}) {
  const [password, setPassword] = useState("")

  useEffect(() => {
    if (open) setPassword("")
  }, [open])

  const mutation = useMutation({
    mutationFn: () => {
      if (!user) throw new Error("missing user")
      return api.resetUserPassword(user.id, password.trim() || undefined)
    },
    onSuccess: (data) => {
      onOpenChange(false)
      if (data.temporary_password) {
        onReset({
          username: data.user.username,
          password: data.temporary_password,
        })
      } else {
        toast.success("密码已重置，首次登录需要修改密码")
        onReset(null)
      }
    },
    onError: (error) => toast.error(errorMessage(error)),
  })

  if (!user) return null

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>重置密码</DialogTitle>
          <DialogDescription>{user.username}</DialogDescription>
        </DialogHeader>
        <div className="space-y-4">
          <Alert>
            <AlertTriangle className="size-4" />
            <AlertDescription>
              重置后该用户的所有现有会话都会失效，首次登录必须修改密码。
            </AlertDescription>
          </Alert>
          <div className="space-y-2">
            <Label htmlFor="reset-password">新密码</Label>
            <Input
              id="reset-password"
              type="password"
              autoComplete="new-password"
              placeholder="留空自动生成 16 位临时密码"
              value={password}
              onChange={(event) => setPassword(event.target.value)}
            />
          </div>
        </div>
        <DialogFooter>
          <Button variant="outline" onClick={() => onOpenChange(false)}>
            取消
          </Button>
          <Button
            onClick={() => mutation.mutate()}
            disabled={mutation.isPending}
          >
            {mutation.isPending ? (
              <Loader2 className="size-4 animate-spin" />
            ) : (
              <KeyRound className="size-4" />
            )}
            重置密码
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}

export function UsersPage() {
  const [page, setPage] = useState(0)
  const [searchInput, setSearchInput] = useState("")
  const [search, setSearch] = useState("")
  const [createOpen, setCreateOpen] = useState(false)
  const [editUser, setEditUser] = useState<User | null>(null)
  const [resetUser, setResetUser] = useState<User | null>(null)
  const [tempPassword, setTempPassword] = useState<TempPasswordInfo | null>(null)

  const query = useQuery({
    queryKey: ["admin-users", page, search],
    queryFn: ({ signal }) =>
      api.listUsers({
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
  const users = query.data?.items ?? []

  function handleSearch(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    setSearch(searchInput.trim())
    setPage(0)
  }

  function handleCreated(info: TempPasswordInfo | null) {
    setTempPassword(info)
    void query.refetch()
  }

  return (
    <div className="space-y-6">
      <div className="flex flex-col gap-3 sm:flex-row sm:items-end sm:justify-between">
        <div>
          <h1 className="text-2xl font-semibold tracking-tight">用户管理</h1>
          <p className="mt-1 text-sm text-muted-foreground">
            管理员开户制，不开放注册。可创建、禁用用户并重置密码。
          </p>
        </div>
        <div className="flex gap-2">
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
          <Button onClick={() => setCreateOpen(true)}>
            <Plus className="size-4" />
            新建用户
          </Button>
        </div>
      </div>

      <Card>
        <CardHeader>
          <CardTitle className="text-base">账号列表</CardTitle>
          <CardDescription>
            共 {total} 个账号。用户级配额留空时继承系统设置。
          </CardDescription>
        </CardHeader>
        <CardContent className="space-y-4">
          <form className="flex max-w-sm items-center gap-2" onSubmit={handleSearch}>
            <div className="relative flex-1">
              <Search className="pointer-events-none absolute top-1/2 left-2.5 size-3.5 -translate-y-1/2 text-muted-foreground" />
              <Input
                className="pl-8"
                placeholder="按用户名搜索"
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
              <AlertTitle>用户列表加载失败</AlertTitle>
              <AlertDescription>{errorMessage(query.error)}</AlertDescription>
            </Alert>
          ) : (
            <div className="overflow-hidden rounded-lg border">
              <Table>
                <TableHeader>
                  <TableRow>
                    <TableHead>用户名</TableHead>
                    <TableHead>角色</TableHead>
                    <TableHead>状态</TableHead>
                    <TableHead>首次改密</TableHead>
                    <TableHead>最后登录</TableHead>
                    <TableHead>创建时间</TableHead>
                    <TableHead className="w-10" />
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
                    : users.map((user) => (
                        <TableRow key={user.id}>
                          <TableCell className="font-medium">{user.username}</TableCell>
                          <TableCell>
                            <Badge
                              variant={user.role === "admin" ? "default" : "secondary"}
                            >
                              {roleLabel(user.role)}
                            </Badge>
                          </TableCell>
                          <TableCell>
                            <Badge
                              variant={user.is_active ? "outline" : "destructive"}
                            >
                              {user.is_active ? "启用" : "禁用"}
                            </Badge>
                          </TableCell>
                          <TableCell>
                            {user.must_change_password ? (
                              <Badge variant="outline">待修改</Badge>
                            ) : (
                              <span className="text-xs text-muted-foreground">已完成</span>
                            )}
                          </TableCell>
                          <TableCell className="text-xs text-muted-foreground">
                            {formatDateTime(user.last_login_at)}
                          </TableCell>
                          <TableCell className="text-xs text-muted-foreground">
                            {formatDateTime(user.created_at)}
                          </TableCell>
                          <TableCell>
                            <DropdownMenu>
                              <DropdownMenuTrigger asChild>
                                <Button variant="ghost" size="icon-sm">
                                  <MoreHorizontal className="size-4" />
                                </Button>
                              </DropdownMenuTrigger>
                              <DropdownMenuContent align="end">
                                <DropdownMenuItem onClick={() => setEditUser(user)}>
                                  <Pencil className="mr-2 size-4" />
                                  编辑
                                </DropdownMenuItem>
                                <DropdownMenuItem onClick={() => setResetUser(user)}>
                                  <KeyRound className="mr-2 size-4" />
                                  重置密码
                                </DropdownMenuItem>
                              </DropdownMenuContent>
                            </DropdownMenu>
                          </TableCell>
                        </TableRow>
                      ))}
                  {!query.isLoading && users.length === 0 ? (
                    <TableRow>
                      <TableCell
                        colSpan={7}
                        className="h-24 text-center text-sm text-muted-foreground"
                      >
                        没有找到用户
                      </TableCell>
                    </TableRow>
                  ) : null}
                </TableBody>
              </Table>
            </div>
          )}

          <div className="flex items-center justify-between text-xs text-muted-foreground">
            <span>
              共 {total} 个用户 · 第 {Math.min(page + 1, pageCount)} / {pageCount} 页
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

      <CreateUserDialog
        open={createOpen}
        onOpenChange={setCreateOpen}
        onCreated={handleCreated}
      />
      <EditUserDialog
        user={editUser}
        open={Boolean(editUser)}
        onOpenChange={(open) => {
          if (!open) setEditUser(null)
        }}
        onSaved={() => void query.refetch()}
      />
      <ResetPasswordDialog
        user={resetUser}
        open={Boolean(resetUser)}
        onOpenChange={(open) => {
          if (!open) setResetUser(null)
        }}
        onReset={(info) => {
          setTempPassword(info)
          void query.refetch()
        }}
      />
      <TempPasswordDialog
        info={tempPassword}
        onClose={() => setTempPassword(null)}
      />
    </div>
  )
}
