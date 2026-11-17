/**
 * 认证状态管理（阶段九）。
 *
 * 令牌存放位置的选择
 * ------------------
 * 用 localStorage 而非内存或 httpOnly Cookie：
 *   - 放内存：刷新页面后丢失登录状态，用户体验差
 *   - httpOnly Cookie：更安全（脚本无法读取），但需要后端配套
 *     Cookie 设置与 CSRF 防护，且当前后端使用 Bearer 令牌方案。
 *     改用 Cookie 需同时调整后端，超出本阶段范围。
 *   - localStorage：实现简单，但存在 XSS 窃取风险。
 *
 * 降低 XSS 风险的配套措施（已在项目中落实）：
 *   1. 所有 LLM 生成的 Markdown 都经 DOMPurify 白名单过滤
 *   2. markdown-it 关闭 html 解析（不允许原始 HTML）
 *   3. 不把令牌拼接到任何 URL 中
 * 若将来需要更高安全性，应迁移到 httpOnly Cookie + CSRF 令牌。
 *
 * 权限缓存
 * --------
 * 登录后把权限集存入 store，前端据此控制按钮显隐。
 * **这只是界面优化，不构成安全边界** —— 真正的校验在每个写接口的服务端。
 */

import { defineStore } from 'pinia'
import { computed, ref } from 'vue'

import { api } from '@/api/client'

const TOKEN_KEY = 'cr_access_token'
const USER_KEY = 'cr_user'

export interface CurrentUser {
  id: string
  username: string
  display_name?: string | null
  role: string
  role_label: string
  department?: string | null
  email?: string | null
  is_active: boolean
  is_locked: boolean
  last_login_at?: string | null
  login_count: number
  permissions: string[]
}

function loadUser(): CurrentUser | null {
  try {
    const raw = localStorage.getItem(USER_KEY)
    return raw ? (JSON.parse(raw) as CurrentUser) : null
  } catch {
    return null
  }
}

export const useAuthStore = defineStore('auth', () => {
  const token = ref<string>(localStorage.getItem(TOKEN_KEY) || '')
  const user = ref<CurrentUser | null>(loadUser())
  const loading = ref(false)

  /** 是否已登录 */
  const isLoggedIn = computed(() => Boolean(token.value && user.value))

  /** 是否管理员 */
  const isAdmin = computed(() => user.value?.role === 'admin')

  /** 是否拥有某权限（用于界面按钮显隐） */
  function can(permission: string): boolean {
    return Boolean(user.value?.permissions?.includes(permission))
  }

  function persist() {
    if (token.value) {
      localStorage.setItem(TOKEN_KEY, token.value)
    } else {
      localStorage.removeItem(TOKEN_KEY)
    }
    if (user.value) {
      localStorage.setItem(USER_KEY, JSON.stringify(user.value))
    } else {
      localStorage.removeItem(USER_KEY)
    }
  }

  /** 登录 */
  async function login(username: string, password: string): Promise<void> {
    loading.value = true
    try {
      const data = await api.login({ username, password })
      token.value = data.access_token
      user.value = data.user
      persist()
    } finally {
      loading.value = false
    }
  }

  /** 退出登录 */
  function logout(): void {
    token.value = ''
    user.value = null
    persist()
  }

  /** 刷新当前用户信息（令牌失效时自动登出） */
  async function refresh(): Promise<void> {
    if (!token.value) return
    try {
      user.value = await api.me()
      persist()
    } catch {
      // 令牌失效或账号被停用 —— 清理本地状态，界面回到未登录
      logout()
    }
  }

  return { token, user, loading, isLoggedIn, isAdmin, can, login, logout, refresh }
})

/** 供 axios 拦截器读取令牌（避免循环依赖，直接读 localStorage） */
export function getStoredToken(): string {
  return localStorage.getItem(TOKEN_KEY) || ''
}

/** 令牌失效时的回调（由拦截器触发，供界面跳转登录页） */
let onUnauthorized: (() => void) | null = null

export function setUnauthorizedHandler(fn: () => void): void {
  onUnauthorized = fn
}

export function notifyUnauthorized(): void {
  onUnauthorized?.()
}
