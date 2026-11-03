import {
  ChevronDown,
  Database,
  FileText,
  History,
  LayoutDashboard,
  LogOut,
  Search,
  Settings,
  Shield,
  Users,
} from "lucide-react"
import { NavLink, Outlet, useNavigate } from "react-router-dom"
import { toast } from "sonner"

import { useAuth } from "@/auth/AuthProvider"
import { Button } from "@/components/ui/button"
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu"
import { cn } from "@/lib/utils"
import { roleLabel } from "@/lib/format"

const adminNav = [
  { to: "/admin/data", label: "数据管理", icon: Database },
  { to: "/admin/settings", label: "系统设置", icon: Settings },
  { to: "/admin/users", label: "用户管理", icon: Users },
  { to: "/admin/audit", label: "审计日志", icon: FileText },
]

function SidebarLink({
  to,
  label,
  icon: Icon,
}: {
  to: string
  label: string
  icon: typeof LayoutDashboard
}) {
  return (
    <NavLink
      to={to}
      end={to === "/"}
      className={({ isActive }) =>
        cn(
          "flex items-center gap-2.5 rounded-lg px-3 py-2 text-sm transition-colors",
          isActive
            ? "bg-sidebar-accent font-medium text-sidebar-accent-foreground"
            : "text-muted-foreground hover:bg-sidebar-accent/60 hover:text-foreground",
        )
      }
    >
      <Icon className="size-4" />
      {label}
    </NavLink>
  )
}

export function AppLayout() {
  const { user, logout } = useAuth()
  const navigate = useNavigate()
  const isAdmin = user?.role === "admin"

  async function handleLogout() {
    await logout()
    toast.success("已退出登录")
    navigate("/login", { replace: true })
  }

  return (
    <div className="min-h-svh bg-muted/30">
      <aside className="fixed inset-y-0 left-0 z-30 hidden w-60 flex-col border-r bg-background md:flex">
        <div className="flex h-14 items-center gap-2 border-b px-4">
          <div className="flex size-8 items-center justify-center rounded-lg bg-primary text-primary-foreground">
            <Shield className="size-4" />
          </div>
          <div className="leading-tight">
            <div className="text-sm font-semibold">QSA 选股工作台</div>
            <div className="text-[11px] text-muted-foreground">条件筛选</div>
          </div>
        </div>
        <nav className="flex-1 space-y-1 p-3">
          <SidebarLink to="/" label="概览" icon={LayoutDashboard} />
          <SidebarLink to="/screener" label="选股" icon={Search} />
          <SidebarLink to="/reports" label="报表历史" icon={History} />
          {isAdmin ? (
            <>
              <div className="px-3 pt-5 pb-1 text-[11px] font-medium tracking-wider text-muted-foreground">
                管理员
              </div>
              {adminNav.map((item) => (
                <SidebarLink key={item.to} {...item} />
              ))}
            </>
          ) : null}
        </nav>
        <div className="border-t p-3 text-[11px] leading-relaxed text-muted-foreground">
          QSA
        </div>
      </aside>

      <div className="md:pl-60">
        <header className="sticky top-0 z-20 flex h-14 items-center justify-between border-b bg-background/95 px-4 backdrop-blur md:px-8">
          <div className="flex items-center gap-2 md:hidden">
            <Shield className="size-4" />
            <span className="text-sm font-semibold">QSA</span>
          </div>
          <div className="ml-auto">
          <DropdownMenu>
            <DropdownMenuTrigger asChild>
              <Button variant="ghost" className="gap-2">
                <span className="max-w-48 truncate text-sm">{user?.username}</span>
                <ChevronDown className="size-3.5 text-muted-foreground" />
              </Button>
            </DropdownMenuTrigger>
            <DropdownMenuContent align="end" className="w-60">
              <DropdownMenuLabel>
                <div className="truncate text-sm">{user?.username}</div>
                <div className="mt-1 text-xs font-normal text-muted-foreground">
                  {user ? roleLabel(user.role) : ""}
                </div>
              </DropdownMenuLabel>
              <DropdownMenuSeparator />
              <DropdownMenuItem onClick={handleLogout}>
                <LogOut className="mr-2 size-4" />
                退出登录
              </DropdownMenuItem>
            </DropdownMenuContent>
          </DropdownMenu>
          </div>
        </header>

        <nav className="flex gap-1 overflow-x-auto border-b bg-background px-3 py-2 md:hidden">
          <SidebarLink to="/" label="概览" icon={LayoutDashboard} />
          <SidebarLink to="/screener" label="选股" icon={Search} />
          <SidebarLink to="/reports" label="报表历史" icon={History} />
          {isAdmin
            ? adminNav.map((item) => <SidebarLink key={item.to} {...item} />)
            : null}
        </nav>

        <main className="mx-auto w-full max-w-6xl p-4 md:p-8">
          <Outlet />
        </main>
      </div>
    </div>
  )
}
