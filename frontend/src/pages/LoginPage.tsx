import { useState, type FormEvent } from "react"
import { AlertTriangle, Loader2, Shield } from "lucide-react"
import { Navigate, useLocation, useNavigate } from "react-router-dom"

import { useAuth } from "@/auth/AuthProvider"
import { Alert, AlertDescription } from "@/components/ui/alert"
import { Button } from "@/components/ui/button"
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card"
import { Input } from "@/components/ui/input"
import { Label } from "@/components/ui/label"
import { ApiError } from "@/lib/api"

export function LoginPage() {
  const { user, login } = useAuth()
  const navigate = useNavigate()
  const location = useLocation()
  const [username, setUsername] = useState("")
  const [password, setPassword] = useState("")
  const [error, setError] = useState<string | null>(null)
  const [submitting, setSubmitting] = useState(false)

  if (user) {
    return (
      <Navigate
        to={user.must_change_password ? "/change-password" : "/"}
        replace
      />
    )
  }

  async function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    setError(null)
    setSubmitting(true)
    try {
      await login(username.trim(), password)
      const from = (location.state as { from?: string } | null)?.from
      navigate(from && from !== "/login" ? from : "/", { replace: true })
    } catch (err) {
      if (err instanceof ApiError) {
        setError(err.message)
      } else {
        setError("登录失败，请稍后重试")
      }
    } finally {
      setSubmitting(false)
    }
  }

  return (
    <div className="grid min-h-svh bg-muted/30 lg:grid-cols-2">
      <div className="relative hidden overflow-hidden bg-zinc-950 p-12 text-zinc-50 lg:flex lg:flex-col lg:justify-between">
        <div className="absolute inset-0 bg-[radial-gradient(circle_at_20%_10%,rgba(120,120,255,0.22),transparent_45%),radial-gradient(circle_at_80%_80%,rgba(16,185,129,0.14),transparent_40%)]" />
        <div className="relative flex items-center gap-3">
          <div className="flex size-10 items-center justify-center rounded-xl bg-white/10">
            <Shield className="size-5" />
          </div>
          <div>
            <div className="text-lg font-semibold">QSA 选股工作台</div>
            <div className="text-xs text-zinc-400">screening workspace</div>
          </div>
        </div>
        <div className="relative max-w-md space-y-6">
          <h1 className="text-4xl font-semibold tracking-tight">
            用统一的数据口径，
            <br />
            筛选你关心的标的。
          </h1>
          <p className="text-sm leading-6 text-zinc-400">
            股东户数、财报、涨跌停次数、历史低位等条件集中管理。
            数据下载与条件筛选分离，系统设置由管理员统一维护。
          </p>
          <div className="grid gap-3 text-sm text-zinc-300">
            <div className="flex items-center gap-2">
              <span className="size-1.5 rounded-full bg-emerald-400" />
              管理员开户制，不开放注册
            </div>
            <div className="flex items-center gap-2">
              <span className="size-1.5 rounded-full bg-emerald-400" />
              参数热更新，修改后立即生效
            </div>
          </div>
        </div>
        <div className="relative text-xs text-zinc-500">
          QSA · 数据源：腾讯行情 / BaoStock / AkShare
        </div>
      </div>

      <div className="flex items-center justify-center p-6">
        <Card className="w-full max-w-md">
          <CardHeader>
            <CardTitle className="text-xl">登录工作台</CardTitle>
            <CardDescription>
              账号由管理员创建。如需开通，请联系系统管理员。
            </CardDescription>
          </CardHeader>
          <CardContent>
            <form className="space-y-5" onSubmit={handleSubmit}>
              {error ? (
                <Alert variant="destructive">
                  <AlertTriangle className="size-4" />
                  <AlertDescription>{error}</AlertDescription>
                </Alert>
              ) : null}
              <div className="space-y-2">
                <Label htmlFor="username">用户名</Label>
                <Input
                  id="username"
                  type="text"
                  autoComplete="username"
                  placeholder="请输入用户名"
                  value={username}
                  onChange={(event) => setUsername(event.target.value)}
                  required
                />
              </div>
              <div className="space-y-2">
                <Label htmlFor="password">密码</Label>
                <Input
                  id="password"
                  type="password"
                  autoComplete="current-password"
                  value={password}
                  onChange={(event) => setPassword(event.target.value)}
                  required
                />
              </div>
              <Button className="w-full" type="submit" disabled={submitting}>
                {submitting ? <Loader2 className="size-4 animate-spin" /> : null}
                登录
              </Button>
              <p className="text-center text-xs text-muted-foreground">
                系统不开放注册，所有账号均由管理员创建。
              </p>
            </form>
          </CardContent>
        </Card>
      </div>
    </div>
  )
}
