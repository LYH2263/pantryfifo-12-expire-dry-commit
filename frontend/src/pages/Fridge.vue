<template>
  <div>
    <h1>冰箱分层</h1>
    <p class="muted">竖列分层 · FEFO 消费走「消费」页</p>
    <div class="fridge">
      <section v-for="L in layers" :key="L" class="shelf">
        <h3>{{ label[L] }}</h3>
        <span v-for="x in by(L)" :key="x.id" class="lot">{{ x.name }} ×{{ x.qty_remain }} · {{ x.expiry }}</span>
      </section>
    </div>

    <div style="margin-top:12px">
      <button @click="preview" :disabled="committing">{{ phase === 'preview' ? '重新干跑' : '过期下架干跑' }}</button>
    </div>

    <!-- 干跑预览：只读，全层与顶条集合不变 -->
    <div v-if="phase === 'preview'" class="sweep-panel">
      <h3>干跑预览（尚未提交）· 今天 {{ dry.today }}</h3>
      <p v-if="!dry.expired.length" class="muted">没有到期日早于今天且仍在架的批。</p>
      <span v-for="x in dry.expired" :key="x.id" class="lot">
        {{ x.name }} ×{{ x.qty_remain }}{{ x.unit }} · {{ x.expiry }} · {{ label[x.layer] }}
      </span>
      <div v-if="dry.expired.length" style="margin-top:10px">
        <button @click="commit" :disabled="committing">{{ committing ? '提交中…' : '确认下架 ' + dry.expired.length + ' 批' }}</button>
        <button class="btn-ghost" @click="cancel" :disabled="committing">取消</button>
      </div>
      <p class="muted" style="margin-bottom:0">提交时会按当下重新结算：期间新入库的过期批一并带走，已被消费的批自动跳过。</p>
    </div>

    <!-- 提交结果：提交世代相对干跑名单的对账 -->
    <div v-if="phase === 'done'" class="sweep-panel">
      <h3>已提交 · 实下架 {{ result.swept_count }} 批</h3>
      <p v-if="!result.expired.length" class="muted">本次没有需要下架的批（此前已提交过）。</p>
      <span v-for="x in result.expired" :key="x.id" class="lot">
        {{ x.name }} ×{{ x.qty_remain }}{{ x.unit }} · {{ x.expiry }}
      </span>
      <p v-if="result.new_ids.length" class="muted">干跑后新入库、一并带走：#{{ result.new_ids.join('、#') }}</p>
      <p v-if="result.skipped_ids.length" class="muted">期间已被消费、跳过：#{{ result.skipped_ids.join('、#') }}</p>
      <button class="btn-ghost" @click="phase = 'idle'">收起</button>
    </div>

    <p v-if="error" class="sweep-error">提交失败，已回到提交前：{{ error }}</p>
  </div>
</template>
<script setup>
import { ref, onMounted } from 'vue'
import { api } from '../api'
const rows = ref([])
const layers = ['upper','mid','lower']
const label = { upper: '上层', mid: '中层', lower: '下层' }
const phase = ref('idle')        // idle | preview | done
const dry = ref({ expired: [] })
const result = ref(null)
const error = ref('')
const committing = ref(false)
function by(L) { return rows.value.filter(r => r.layer === L) }
async function load() { rows.value = await api('/fridge') }
async function preview() {
  error.value = ''
  dry.value = await api('/expire-sweep')
  result.value = null
  phase.value = 'preview'
}
function cancel() { phase.value = 'idle'; dry.value = { expired: [] } }
async function commit() {
  committing.value = true
  error.value = ''
  try {
    const previewIds = dry.value.expired.map(x => x.id)
    result.value = await api('/expire-sweep', { method: 'POST', body: JSON.stringify({ preview_ids: previewIds }) })
    phase.value = 'done'
    await load()
    // 全层落库后再刷顶条，两者收成同一世代
    window.dispatchEvent(new CustomEvent('alerts:refresh'))
  } catch (e) {
    // 后端已整体回滚；本地名单不动，界面停在提交前
    error.value = e.message
  } finally {
    committing.value = false
  }
}
onMounted(load)
</script>
