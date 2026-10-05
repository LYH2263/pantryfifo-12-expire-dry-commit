<template>
  <div>
    <div class="alert-bar" v-if="alerts.length">临期预警：{{ alerts.map(a => a.name + '(' + a.level + ')').join(' · ') }}</div>
    <div class="alert-bar" v-else>临期预警带：暂无紧急批次</div>
    <div class="wrap">
      <nav class="layer-tabs">
        <router-link to="/">全层</router-link>
        <router-link to="/layer/upper">上层</router-link>
        <router-link to="/layer/mid">中层</router-link>
        <router-link to="/layer/lower">下层</router-link>
        <router-link to="/inbound">入库</router-link>
        <router-link to="/consume">消费</router-link>
        <router-link to="/settings">设置</router-link>
      </nav>
      <router-view />
    </div>
  </div>
</template>
<script setup>
import { ref, onMounted, onUnmounted } from 'vue'
import { api } from './api'
const alerts = ref([])
async function loadAlerts() { try { alerts.value = await api('/alerts') } catch { alerts.value = [] } }
// 全层提交过期下架后通知顶条同刷，避免顶条与全层落在不同世代
function onAlertsRefresh() { loadAlerts() }
onMounted(() => {
  loadAlerts()
  window.addEventListener('alerts:refresh', onAlertsRefresh)
})
onUnmounted(() => window.removeEventListener('alerts:refresh', onAlertsRefresh))
</script>
