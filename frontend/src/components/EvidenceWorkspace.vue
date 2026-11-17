<script setup lang="ts">
import { nextTick, onBeforeUnmount, ref, watch } from 'vue'

defineProps<{ count: number }>()
const open = ref(false)
const width = ref(360)
const root = ref<HTMLElement | null>(null)
const side = ref<HTMLElement | null>(null)
let returnFocus: HTMLElement | null = null
watch(open, async value => {
  if (value) {
    returnFocus = document.activeElement as HTMLElement
    await nextTick()
    side.value?.querySelector<HTMLButtonElement>('button')?.focus()
  } else returnFocus?.focus()
})
function onPanelKey(event: KeyboardEvent) {
  if (event.key === 'Escape') { event.preventDefault(); open.value = false; return }
  if (event.key !== 'Tab' || !window.matchMedia('(max-width: 900px)').matches) return
  const controls = [...(side.value?.querySelectorAll<HTMLElement>('button:not(:disabled), a[href], [tabindex="0"]') || [])].filter(el => el.getClientRects().length > 0)
  const first = controls[0], last = controls[controls.length - 1]
  if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last?.focus() }
  else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first?.focus() }
}
let startX = 0
let startWidth = 0
function resize(event: PointerEvent) {
  const max = Math.min(620, (root.value?.clientWidth || 960) - 380)
  width.value = Math.max(280, Math.min(max, startWidth + startX - event.clientX))
}
function stop() {
  window.removeEventListener('pointermove', resize)
  window.removeEventListener('pointerup', stop)
}
function start(event: PointerEvent) {
  event.preventDefault()
  startX = event.clientX
  startWidth = width.value
  window.addEventListener('pointermove', resize)
  window.addEventListener('pointerup', stop)
}
onBeforeUnmount(stop)
defineExpose({ show: () => { open.value = true } })
</script>

<template>
  <div ref="root" class="evidence-workspace" :style="{ '--evidence-width': width + 'px' }">
    <div class="evidence-main">
      <div class="evidence-toggle"><el-button size="small" :aria-expanded="open" @click="open = !open">{{ open ? '收起证据' : '查看证据' }} · {{ count }} 条</el-button></div>
      <slot />
    </div>
    <div v-if="open" class="evidence-backdrop" @click="open = false" />
    <aside v-if="open" ref="side" class="evidence-side" aria-label="引用证据" @keydown="onPanelKey">
      <div class="evidence-resize" role="separator" tabindex="0" aria-label="调整引用面板宽度" aria-orientation="vertical" :aria-valuenow="width" :aria-valuemin="280" :aria-valuemax="620" @pointerdown="start" @keydown.left.prevent="width = Math.min(620, width + 20)" @keydown.right.prevent="width = Math.max(280, width - 20)" />
      <div class="evidence-controls"><span>对照文献原文</span><el-button size="small" @click="open = false">关闭证据</el-button></div>
      <slot name="evidence" />
    </aside>
  </div>
</template>

<style scoped>
.evidence-workspace { display: flex; height: 100%; min-height: 0; position: relative; overflow: hidden; }
.evidence-main { display: flex; flex-direction: column; min-width: 0; flex: 1; min-height: 0; }
.evidence-toggle { display: flex; justify-content: flex-end; padding: 6px 18px; background: #fff; border-bottom: 1px solid var(--cr-border); }
.evidence-main :slotted(.qa-layout), .evidence-main :slotted(.chat-layout) { flex: 1; min-height: 0; height: auto; }
.evidence-side { position: relative; display: flex; flex-direction: column; width: min(var(--evidence-width), 48%); min-width: 280px; background: #fff; }
.evidence-side :slotted(*) { min-height: 0; flex: 1; }
.evidence-controls { display: flex; justify-content: space-between; align-items: center; padding: 6px 12px; font-size: 13px; border-bottom: 1px solid var(--cr-border); }
.evidence-resize { position: absolute; z-index: 2; left: -3px; top: 0; bottom: 0; width: 6px; cursor: col-resize; touch-action: none; }
.evidence-resize:hover, .evidence-resize:focus-visible { background: var(--cr-primary-light); }
.evidence-backdrop { display: none; }
@media (max-width: 900px) {
  .evidence-side { position: fixed; z-index: 2002; left: 0; right: 0; bottom: 0; height: 72dvh; width: 100%; min-width: 0; border-radius: 16px 16px 0 0; overflow: hidden; box-shadow: 0 -8px 40px #17252a33; }
  .evidence-backdrop { display: block; position: fixed; inset: 0; z-index: 2001; background: #17252a66; }
  .evidence-resize { display: none; }
}
</style>
