import { createRouter, createWebHistory, type RouteRecordRaw } from 'vue-router'

/**
 * 路由表。
 *
 * 使用动态 import 做路由级懒加载，首屏只加载问答页所需的代码。
 */
const routes: RouteRecordRaw[] = [
  {
    path: '/',
    redirect: '/qa',
  },
  {
    path: '/qa',
    name: 'qa',
    component: () => import('@/views/QaView.vue'),
    meta: { title: '智能问答', icon: 'ChatDotRound' },
  },
  {
    path: '/chat',
    name: 'chat',
    component: () => import('@/views/ChatView.vue'),
    meta: { title: '多轮会诊', icon: 'ChatLineSquare' },
  },
  {
    path: '/images',
    name: 'images',
    component: () => import('@/views/ImagesView.vue'),
    meta: { title: '影像解读', icon: 'Picture' },
  },
  {
    path: '/cases',
    name: 'cases',
    component: () => import('@/views/CasesView.vue'),
    meta: { title: '典型病例', icon: 'Tickets' },
  },
  {
    path: '/knowledge',
    name: 'knowledge',
    component: () => import('@/views/KnowledgeView.vue'),
    meta: { title: '知识库', icon: 'Collection' },
  },
  {
    path: '/system',
    name: 'system',
    component: () => import('@/views/SystemView.vue'),
    meta: { title: '系统状态', icon: 'Monitor' },
  },
  {
    path: '/admin',
    name: 'admin',
    component: () => import('@/views/AdminView.vue'),
    meta: { title: '系统管理', icon: 'Setting', requiresAuth: true },
  },
  {
    path: '/:pathMatch(.*)*',
    name: 'not-found',
    component: () => import('@/views/NotFoundView.vue'),
    meta: { title: '页面不存在' },
  },
]

const router = createRouter({
  history: createWebHistory(),
  routes,
})

router.afterEach((to) => {
  const title = (to.meta?.title as string) || ''
  document.title = title ? `${title} — 结直肠手术围术期知识库` : '结直肠手术围术期知识库'
})


/**
 * 全局前置守卫（阶段九）。
 *
 * 只做「已登录才可进入」的粗粒度拦截，**不校验角色权限**：
 * 角色权限由服务端接口保证，前端拦截只是体验优化。
 * 若在此重复实现一遍权限判断，会出现「前端规则与服务端不一致」
 * 的隐蔽问题 —— 用户能进页面但所有操作都报 403。
 *
 * ⚠️ 未登录时**不能重定向回目标路由自身**（那会形成无效跳转），
 * 而是放行进入页面，由 AdminView 根据 store 状态自行渲染登录表单。
 * 这样处理的原因是：
 *   - /admin 的设计是「未登录时显示登录表单，登录后显示管理面板」，
 *     本身就是免跳转的自包含页面
 *   - 若在此重定向，会与 AdminView 的登录渲染逻辑重复，且
 *     一旦目标路由带 requiresAuth，守卫重定向到同一路径毫无意义
 */
router.beforeEach((to) => {
  // 目前所有需登录的页面都自行处理未登录状态（渲染登录表单），
  // 因此守卫只做记录，不做跳转。
  // 将来若新增「必须登录才能看」且不自带登录表单的页面，
  // 应在此重定向到 /admin（登录页），而不是 to.fullPath。
  if (to.meta?.requiresAuth) {
    const token = localStorage.getItem('cr_access_token')
    if (!token) {
      // 留痕便于排查：说明访问了需登录页面且当前无令牌
      console.info('[router] 访问需登录页面但未登录，交由页面自行渲染登录表单:', to.path)
    }
  }
  return true
})

/**
 * 响应 401 时的处理：清空本地用户缓存。
 * 真正的界面切换由 AdminView 内部根据 store 状态完成，
 * 这里不做跳转，避免与用户正在进行的操作冲突。
 */
if (typeof window !== 'undefined') {
  window.addEventListener('cr:unauthorized', () => {
    localStorage.removeItem('cr_user')
  })
}
export default router
