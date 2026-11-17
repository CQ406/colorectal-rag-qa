<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { useRoute } from 'vue-router'
import { useAuthStore } from '@/stores/auth'
import { useChatStore } from '@/stores/chat'
const route = useRoute()
const auth = useAuthStore()
const chat = useChatStore()
const menuOpen = ref(false)
const activeMenu = computed(() => route.path === '/chat' ? '/qa' : route.path)
const isQa = computed(() => ['/qa', '/chat'].includes(route.path))
watch(() => route.path, () => { menuOpen.value = false })

// 切换账号时清空问答页状态。
//
// QaView 被 keep-alive 缓存，退出后换个账号登录仍会看到上一位用户的历史问答、
// 引用面板和分期筛选。ChatView / ImagesView / KnowledgeView 都按 token 复位，
// 唯独问答页没有——这里统一在用户身份变化时清一次。
watch(
  () => auth.user?.id ?? null,
  (curr, prev) => {
    if (prev !== null && curr !== prev) chat.clear()
  },
)
const navGroups = [
 { title: '知识应用', items: [
  { path: '/qa', title: '知识问答', icon: 'ChatDotRound' },
  { path: '/images', title: '影像解读', icon: 'Picture' },
  { path: '/cases', title: '典型病例', icon: 'Tickets' },
  { path: '/knowledge', title: '知识库', icon: 'Collection' },
 ] },
 { title: '管理与维护', items: [
  { path: '/system', title: '系统状态', icon: 'Monitor' },
  { path: '/admin', title: '系统管理', icon: 'Setting' },
 ] },
]
</script>

<template>
  <el-container class="app-shell">
    <button v-if="menuOpen" class="nav-backdrop" aria-label="关闭导航" @click="menuOpen = false" />
    <el-aside width="208px" class="app-aside" :class="{ 'is-open': menuOpen }">
      <div class="brand"><div class="brand-mark">CR</div><div class="brand-text"><div class="brand-title">结直肠手术</div><div class="brand-sub">围术期知识库问答</div></div><el-button class="mobile-nav-close" text aria-label="关闭菜单" @click="menuOpen = false">✕</el-button></div>
      <el-menu :default-active="activeMenu" router class="app-menu">
        <div v-for="group in navGroups" :key="group.title">
          <div class="nav-group-label">{{ group.title }}</div>
          <el-menu-item v-for="item in group.items" :key="item.path" :index="item.path"><el-icon><component :is="item.icon" /></el-icon><span>{{ item.title }}</span></el-menu-item>
        </div>
      </el-menu>
      <div class="aside-note">循证查阅 · 来源可溯</div>
    </el-aside>
    <el-container class="app-main-wrap">
      <el-header height="58px" class="app-header">
        <div class="header-left"><el-button class="mobile-nav-toggle" aria-label="打开导航" @click="menuOpen = true"><el-icon><Menu /></el-icon></el-button><div class="header-title">{{ isQa ? '知识问答' : route.meta?.title || '首页' }}</div></div>
        <div class="header-right">
          <el-tag class="usage-tag" size="small" type="info" effect="plain">教学科研用途</el-tag>
          <template v-if="auth.isLoggedIn"><span class="account-name">{{ auth.user?.display_name || auth.user?.username }} · {{ auth.user?.role_label }}</span><el-button link @click="auth.logout()">退出登录</el-button></template>
          <el-button v-else link type="primary" @click="$router.push('/admin')">登录账户</el-button>
        </div>
      </el-header>
      <nav v-if="isQa" class="question-tabs" aria-label="问答模式">
        <router-link to="/qa" :class="{ selected: route.path === '/qa' }">单次问答</router-link>
        <router-link to="/chat" :class="{ selected: route.path === '/chat' }">连续对话</router-link>
        <span>检索文献，核对证据</span>
      </nav>
      <!-- QaView 与 ChatView 都缓存：
           问答页缓存是为了换账号前的历史不残留（配合下面 watch 清空）；
           多轮会诊缓存是为了**生成中切页不丢当前轮** —— 此前 ChatView 未缓存，
           路由切走立即 onBeforeUnmount → abort 流并清空 turns，而服务端只在
           done 时才写入会话，这一轮问题与已生成内容就永久丢失了。 -->
      <el-main class="app-main"><router-view v-slot="{ Component }"><keep-alive :include="['QaView', 'ChatView']"><component :is="Component" /></keep-alive></router-view></el-main>
      <el-footer height="auto" class="app-footer"><el-icon class="footer-icon"><WarningFilled /></el-icon><span>AI 输出仅供教学与科研参考，<strong>不构成任何诊疗建议</strong>。临床决策请结合最新指南与患者情况。</span></el-footer>
    </el-container>
  </el-container>
</template>

<style scoped>
.app-shell {
  height: 100vh;
  overflow: hidden;
}

/* ---------- 侧边栏 ---------- */
.app-aside {
  display: flex;
  flex-direction: column;
  background: var(--cr-primary-dark);
  border-right: none;
}

.brand {
  display: flex;
  gap: 10px;
  align-items: center;
  padding: 16px 14px;
  border-bottom: 1px solid rgba(255, 255, 255, 0.08);
}

.brand-mark {
  display: flex;
  flex-shrink: 0;
  align-items: center;
  justify-content: center;
  width: 34px;
  height: 34px;
  font-size: 14px;
  font-weight: 700;
  color: var(--cr-primary-dark);
  background: var(--cr-primary-light);
  border-radius: 8px;
}

.brand-text {
  min-width: 0;
}

.brand-title {
  font-size: 14px;
  font-weight: 600;
  color: #fff;
  letter-spacing: 0.5px;
}

.brand-sub {
  font-size: 11px;
  color: rgba(255, 255, 255, 0.55);
  white-space: nowrap;
}

.app-menu {
  flex: 1;
  padding-top: 8px;
  background: transparent;
  border-right: none;
}

.app-menu :deep(.el-menu-item) {
  color: rgba(255, 255, 255, 0.7);
}

.app-menu :deep(.el-menu-item:hover) {
  color: #fff;
  background: rgba(255, 255, 255, 0.06);
}

.app-menu :deep(.el-menu-item.is-active) {
  color: #fff;
  background: var(--cr-primary);
}

/* ---------- 主区 ---------- */
.app-main-wrap {
  min-width: 0;
}

.app-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 0 20px;
  background: var(--cr-surface);
  border-bottom: 1px solid var(--cr-border);
}

.header-title {
  font-size: 15px;
  font-weight: 600;
  color: var(--cr-primary-dark);
}

.app-main {
  padding: 0;
  overflow: hidden;
  background: var(--cr-bg);
}

/* ---------- 底部免责声明 ---------- */
.app-footer {
  display: flex;
  gap: 6px;
  align-items: center;
  justify-content: center;
  font-size: 12px;
  line-height: 1.4;
  color: var(--cr-text-secondary);
  background: #fdf6e3;
  border-top: 1px solid #f0e0b8;
}

.app-footer strong {
  color: #b9770e;
}

.footer-icon {
  flex-shrink: 0;
  color: #d68910;
}
.app-shell { height: 100dvh; }
.app-main-wrap, .app-main { min-height: 0; }
.app-main { overflow: auto; }
.app-header { flex-shrink: 0; gap: 12px; }
.header-left, .header-right { display: flex; align-items: center; gap: 14px; min-width: 0; }
.account-name { font-size: 13px; color: var(--cr-text-secondary); max-width: 240px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.nav-group-label { padding: 20px 20px 6px; color: #b1c7cb; font-size: 12px; letter-spacing: 1px; }
.aside-note { padding: 20px; color: #b1c7cb; font-size: 12px; }
.question-tabs { display: flex; flex-shrink: 0; align-items: center; gap: 24px; padding: 0 24px; background: white; border-bottom: 1px solid var(--cr-border); }
.question-tabs a { padding: 10px 0; text-decoration: none; color: var(--cr-text-secondary); border-bottom: 3px solid transparent; }
.question-tabs a.selected { color: var(--cr-primary); border-color: var(--cr-primary); font-weight: 600; }
.question-tabs span { font-size: 12px; color: var(--cr-text-secondary); margin-left: auto; }
.app-footer { flex-shrink: 0; padding: 7px 16px; min-height: 34px; }
.mobile-nav-toggle, .mobile-nav-close, .nav-backdrop { display: none; }
@media (max-width: 900px) {
 .app-aside { position: fixed; z-index: 2005; inset: 0 auto 0 0; transform: translateX(-100%); transition: transform .2s; }
 .app-aside.is-open { transform: translateX(0); }
 .mobile-nav-toggle, .mobile-nav-close { display: inline-flex; }
 .mobile-nav-close { color: white; padding: 4px; margin-left: auto; }
 .nav-backdrop { display: block; position: fixed; inset: 0; z-index: 2004; border: 0; background: #17252a77; }
 .app-header { padding: 0 12px; }
 .header-left, .header-right { gap: 8px; }
 .question-tabs { padding: 0 16px; gap: 20px; }
}
@media (max-width: 600px) {
 .usage-tag, .question-tabs span { display: none; }
 .account-name { max-width: 110px; }
 .app-footer { font-size: 11px; line-height: 1.5; padding: 6px 10px; }
 .brand-sub { white-space: normal; }
}
</style>
