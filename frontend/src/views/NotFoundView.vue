<script setup lang="ts">
import { useRouter } from 'vue-router'

const router = useRouter()
</script>

<template>
  <div class="not-found">
    <el-icon class="nf-icon"><DocumentDelete /></el-icon>
    <h2>页面不存在</h2>
    <p>您访问的页面可能已被移动或删除。</p>
    <el-button type="primary" @click="router.push('/qa')">
      <el-icon><Back /></el-icon>
      返回智能问答
    </el-button>
  </div>
</template>

<style scoped>
.not-found {
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  height: 100%;
  color: var(--cr-text-secondary);
}

.nf-icon {
  font-size: 64px;
  color: var(--cr-text-muted);
  opacity: 0.4;
}

.not-found h2 {
  margin: 16px 0 6px;
  font-size: 18px;
  color: var(--cr-primary-dark);
}

.not-found p {
  margin: 0 0 20px;
  font-size: 13.5px;
}
</style>
