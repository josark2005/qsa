import { ArrowRight, Database, SlidersHorizontal, Users } from "lucide-react"
import { Link } from "react-router-dom"

import { useAuth } from "@/auth/AuthProvider"
import { Button } from "@/components/ui/button"
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card"
import { roleLabel } from "@/lib/format"

export function DashboardPage() {
  const { user } = useAuth()
  const isAdmin = user?.role === "admin"

  return (
    <div className="space-y-6">
      <div className="flex flex-col gap-3 sm:flex-row sm:items-end sm:justify-between">
        <div>
          <h1 className="text-2xl font-semibold tracking-tight">概览</h1>
          <p className="mt-1 text-sm text-muted-foreground">
            欢迎回来，{user?.username}（{user ? roleLabel(user.role) : ""}）
          </p>
        </div>
      </div>

      <div className="grid gap-4 md:grid-cols-2 xl:grid-cols-4">
        <Card>
          <CardHeader className="pb-2">
            <CardDescription>数据源</CardDescription>
            <CardTitle className="flex items-center gap-2 text-lg">
              <Database className="size-4" />
              腾讯 / BaoStock / AkShare
            </CardTitle>
          </CardHeader>
          <CardContent className="text-xs leading-5 text-muted-foreground">
            腾讯批量行情负责每日增量；BaoStock 负责沪深历史；AkShare 补充北交所、
            财报和股东户数。
          </CardContent>
        </Card>
        <Card>
          <CardHeader className="pb-2">
            <CardDescription>同步管道</CardDescription>
            <CardTitle className="text-lg">6 条</CardTitle>
          </CardHeader>
          <CardContent className="text-xs leading-5 text-muted-foreground">
            行情数据、财报与股东晚间同步、次日补充、周度校正、每日拉取与群汇报。
          </CardContent>
        </Card>
        <Card>
          <CardHeader className="pb-2">
            <CardDescription>筛选条件</CardDescription>
            <CardTitle className="flex items-center gap-2 text-lg">
              <SlidersHorizontal className="size-4" />
              已接入
            </CardTitle>
          </CardHeader>
          <CardContent className="text-xs leading-5 text-muted-foreground">
            股东户数、财报指标、N 日涨跌停次数、可调窗口的历史低位。
          </CardContent>
        </Card>
        <Card>
          <CardHeader className="pb-2">
            <CardDescription>账号体系</CardDescription>
            <CardTitle className="flex items-center gap-2 text-lg">
              <Users className="size-4" />
              管理员开户
            </CardTitle>
          </CardHeader>
          <CardContent className="text-xs leading-5 text-muted-foreground">
            不开放注册。管理员创建、禁用、重置密码；首登强制改密。
          </CardContent>
        </Card>
      </div>

      <Card>
        <CardHeader>
          <CardTitle className="text-base">快捷入口</CardTitle>
          <CardDescription>常用操作</CardDescription>
        </CardHeader>
        <CardContent className="grid gap-2 sm:grid-cols-2 lg:grid-cols-4">
          <Button asChild className="w-full justify-between">
            <Link to="/screener">
              开始选股
              <ArrowRight className="size-4" />
            </Link>
          </Button>
          {isAdmin ? (
            <>
              <Button asChild variant="outline" className="w-full justify-between">
                <Link to="/admin/data">
                  数据管理
                  <ArrowRight className="size-4" />
                </Link>
              </Button>
              <Button asChild variant="outline" className="w-full justify-between">
                <Link to="/admin/settings">
                  系统设置
                  <ArrowRight className="size-4" />
                </Link>
              </Button>
              <Button asChild variant="outline" className="w-full justify-between">
                <Link to="/admin/users">
                  用户管理
                  <ArrowRight className="size-4" />
                </Link>
              </Button>
              <Button asChild variant="outline" className="w-full justify-between">
                <Link to="/admin/audit">
                  审计日志
                  <ArrowRight className="size-4" />
                </Link>
              </Button>
            </>
          ) : null}
        </CardContent>
      </Card>
    </div>
  )
}
