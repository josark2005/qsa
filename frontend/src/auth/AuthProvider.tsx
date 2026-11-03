import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useState,
  type ReactNode,
} from "react"
import { useQueryClient } from "@tanstack/react-query"
import { toast } from "sonner"

import { api, ApiError, setCsrfToken } from "@/lib/api"
import type { User } from "@/lib/types"

interface AuthContextValue {
  user: User | null
  csrfToken: string | null
  isLoading: boolean
  login: (username: string, password: string) => Promise<void>
  logout: () => Promise<void>
  refresh: () => Promise<void>
  changePassword: (currentPassword: string, newPassword: string) => Promise<void>
}

const AuthContext = createContext<AuthContextValue | null>(null)

export function AuthProvider({ children }: { children: ReactNode }) {
  const queryClient = useQueryClient()
  const [user, setUser] = useState<User | null>(null)
  const [csrfToken, setLocalCsrf] = useState<string | null>(null)
  const [isLoading, setIsLoading] = useState(true)

  const applySession = useCallback(
    (nextUser: User, nextCsrf: string) => {
      setUser(nextUser)
      setLocalCsrf(nextCsrf)
      setCsrfToken(nextCsrf)
    },
    [],
  )

  const clearSession = useCallback(() => {
    setUser(null)
    setLocalCsrf(null)
    setCsrfToken(null)
    queryClient.clear()
  }, [queryClient])

  const refresh = useCallback(async () => {
    try {
      const session = await api.me()
      applySession(session.user, session.csrf_token)
    } catch (error) {
      if (!(error instanceof ApiError) || error.status !== 401) {
        throw error
      }
      clearSession()
    }
  }, [applySession, clearSession])

  useEffect(() => {
    let cancelled = false
    api
      .me()
      .then((session) => {
        if (!cancelled) applySession(session.user, session.csrf_token)
      })
      .catch((error) => {
        if (cancelled) return
        if (!(error instanceof ApiError) || error.status !== 401) {
          toast.error(error instanceof Error ? error.message : "无法获取登录状态")
        }
        clearSession()
      })
      .finally(() => {
        if (!cancelled) setIsLoading(false)
      })
    return () => {
      cancelled = true
    }
  }, [applySession, clearSession])

  useEffect(() => {
    const handler = () => clearSession()
    window.addEventListener("qsa:unauthorized", handler)
    return () => window.removeEventListener("qsa:unauthorized", handler)
  }, [clearSession])

  const login = useCallback(
    async (username: string, password: string) => {
      const session = await api.login(username, password)
      queryClient.clear()
      applySession(session.user, session.csrf_token)
    },
    [applySession, queryClient],
  )

  const logout = useCallback(async () => {
    try {
      await api.logout()
    } catch {
      // The local session is cleared even if the server call fails.
    } finally {
      clearSession()
    }
  }, [clearSession])

  const changePassword = useCallback(
    async (currentPassword: string, newPassword: string) => {
      await api.changePassword(currentPassword, newPassword)
      await refresh()
    },
    [refresh],
  )

  const value = useMemo<AuthContextValue>(
    () => ({
      user,
      csrfToken,
      isLoading,
      login,
      logout,
      refresh,
      changePassword,
    }),
    [user, csrfToken, isLoading, login, logout, refresh, changePassword],
  )

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>
}

export function useAuth(): AuthContextValue {
  const context = useContext(AuthContext)
  if (!context) {
    throw new Error("useAuth must be used inside AuthProvider")
  }
  return context
}
