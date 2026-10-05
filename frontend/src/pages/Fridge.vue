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
    <div style="margin-top:12px; display:flex; gap:8px;">
      <button @click="preview" :disabled="loading">{{ previewLot ? '重新干跑' : '干跑预览' }}</button>
      <button v-if="previewLot" class="btn-ghost" @click="cancelPreview">取消预览</button>
    </div>

    <div v-if="previewLot" class="sweep-card">
      <h3>干跑预览（仅列出，尚未下架）· 基准日 {{ previewLot.as_of }}</h3>
      <p v-if="!previewLot.lots.length" class="muted">没有到期日早于今天且仍在架的批次，未到期批不会被带走。</p>
      <div v-else>
        <p class="muted">以下 {{ previewLot.lots.length }} 批将在确认后下架：</p>
        <span v-for="x in previewLot.lots" :key="x.id" class="lot lot-expired">
          #{{ x.id }} {{ x.name }} ×{{ x.qty_remain }} · 到期 {{ x.expiry }} · {{ label[x.layer] }}
        </span>
      </div>
      <div v-if="commitResult" class="sweep-diff">
        <span v-if="commitResult.added_after_preview.length" class="muted">提交时刻新捕获（干跑后入库的过期批）：#{{ commitResult.added_after_preview.join('、#') }}</span><br/>
        <span v-if="commitResult.gone_after_preview.length" class="muted">提交时刻已不在架（期间被消费完）：#{{ commitResult.gone_after_preview.join('、#') }}</span><br/>
        <span class="muted">本次实际下架 {{ commitResult.expired_ids.length }} 批。</span>
      </div>
      <p v-if="error" class="sweep-error">{{ error }}</p>
      <div style="margin-top:10px; display:flex; gap:8px;">
        <button @click="commit" :disabled="committing || !previewLot.lots.length">确认下架</button>
      </div>
    </div>
  </div>
</template>
<script setup>
import { ref, onMounted } from 'vue'
import { api } from '../api'
const rows = ref([])
const layers = ['upper','mid','lower']
const label = { upper: '上层', mid: '中层', lower: '下层' }
const previewLot = ref(null)
const commitResult = ref(null)
const error = ref('')
const loading = ref(false)
const committing = ref(false)
function by(L) { return rows.value.filter(r => r.layer === L) }
async function load() { rows.value = await api('/fridge') }
async function preview() {
  loading.value = true; error.value = ''; commitResult.value = null
  try { previewLot.value = await api('/expire-sweep/preview') }
  catch (e) { error.value = e.message }
  finally { loading.value = false }
}
function cancelPreview() { previewLot.value = null; commitResult.value = null; error.value = '' }
async function commit() {
  committing.value = true; error.value = ''
  try {
    const ids = previewLot.value.lots.map(l => l.id)
    const r = await api('/expire-sweep', { method: 'POST', body: JSON.stringify({ ids }) })
    commitResult.value = r
    // commit converges to its own generation: refetch the list it actually wrote
    previewLot.value = await api('/expire-sweep/preview')
    await load()
    window.dispatchEvent(new CustomEvent('pantry:generation'))
  } catch (e) {
    error.value = '提交失败，已全部回滚到提交前：' + e.message
    await load()
    window.dispatchEvent(new CustomEvent('pantry:generation'))
  } finally { committing.value = false }
}
onMounted(load)
</script>
