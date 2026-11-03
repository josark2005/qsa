import { Route, Routes } from "react-router-dom"

import { useAuth } from "@/auth/AuthProvider"
import { AppLayout } from "@/components/AppLayout"
import {
  AdminRoute,
  FullPageLoader,
  ProtectedRoute,
} from "@/components/route-guards"
import { Card, CardContent } from "@/components/ui/card"
import { AuditPage } from "@/pages/AuditPage"
import { ChangePasswordPage } from "@/pages/ChangePasswordPage"
import { DashboardPage } from "@/pages/DashboardPage"
import { DataPage } from "@/pages/DataPage"
import { LoginPage } from "@/pages/LoginPage"
import { ReportPage } from "@/pages/ReportPage"
import { ReportsPage } from "@/pages/ReportsPage"
import { ScreenerPage } from "@/pages/ScreenerPage"
import { SettingsPage } from "@/pages/SettingsPage"
import { TrendsPage } from "@/pages/TrendsPage"
import { UsersPage } from "@/pages/UsersPage"

function NotFoundPage() {
  return (
    <Card>
      <CardContent className="py-16 text-center">
        <div className="text-lg font-semibold">页面不存在</div>
        <p className="mt-2 text-sm text-muted-foreground">
          请检查地址，或返回概览页。
        </p>
      </CardContent>
    </Card>
  )
}

function App() {
  const { isLoading } = useAuth()

  if (isLoading) return <FullPageLoader />

  return (
    <Routes>
      <Route path="/login" element={<LoginPage />} />
      <Route path="/reports/:token" element={<ReportPage />} />
      <Route element={<ProtectedRoute />}>
        <Route path="/change-password" element={<ChangePasswordPage />} />
        <Route element={<AppLayout />}>
          <Route index element={<DashboardPage />} />
          <Route path="screener" element={<ScreenerPage />} />
          <Route path="trends" element={<TrendsPage />} />
          <Route path="reports" element={<ReportsPage />} />
          <Route
            path="admin/data"
            element={
              <AdminRoute>
                <DataPage />
              </AdminRoute>
            }
          />
          <Route
            path="admin/settings"
            element={
              <AdminRoute>
                <SettingsPage />
              </AdminRoute>
            }
          />
          <Route
            path="admin/users"
            element={
              <AdminRoute>
                <UsersPage />
              </AdminRoute>
            }
          />
          <Route
            path="admin/audit"
            element={
              <AdminRoute>
                <AuditPage />
              </AdminRoute>
            }
          />
          <Route path="*" element={<NotFoundPage />} />
        </Route>
      </Route>
    </Routes>
  )
}

export default App
