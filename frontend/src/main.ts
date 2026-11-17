import { createPinia } from 'pinia'
import { createApp } from 'vue'

import ElementPlus from 'element-plus'
import zhCn from 'element-plus/es/locale/lang/zh-cn'
import * as ElementPlusIconsVue from '@element-plus/icons-vue'

import App from './App.vue'
import router from './router'
import { setUnauthorizedHandler, useAuthStore } from './stores/auth'

// 样式顺序很重要：Element Plus 样式先引入，自定义样式后引入才能覆盖
import 'element-plus/dist/index.css'
import 'highlight.js/styles/github.css'
import './styles/main.css'

const app = createApp(App)

// 注册全部 Element Plus 图标（按需引入需逐个 import，此处为开发便利全量注册）
for (const [key, component] of Object.entries(ElementPlusIconsVue)) {
  app.component(key, component)
}

app.use(createPinia())
app.use(router)
// 使用中文语言包（分页、日期选择等组件文案）
app.use(ElementPlus, { locale: zhCn })

// 401 的界面联动。
//
// axios 拦截器只清了 localStorage 并广播 cr:unauthorized，但内存里的 Pinia 状态
// 仍是"已登录"——顶栏继续显示用户名、管理页继续按权限渲染按钮，用户只有手动刷新
// 才会回到登录页。这里把事件接到 store 与路由：清状态并跳转登录。
window.addEventListener('cr:unauthorized', () => {
  try {
    useAuthStore().logout()
  } catch {
    /* store 尚未就绪时忽略 */
  }
  if (router.currentRoute.value.path !== '/admin') {
    void router.replace({ path: '/admin', query: { redirect: router.currentRoute.value.fullPath } })
  }
})
// 供其它模块（如需要显式触发）复用同一处理逻辑
setUnauthorizedHandler(() => window.dispatchEvent(new CustomEvent('cr:unauthorized')))

app.mount('#app')
