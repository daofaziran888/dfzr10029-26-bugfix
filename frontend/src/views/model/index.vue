<template>
  <section class="page" data-module="model">
    <header class="page-head">
      <div><h2>分析模型</h2><p class="page-desc">维护模型配置，围绕模型编号、模型名称、模型类型、训练数据集做登记、筛选与状态流转。</p></div>
      <div class="page-actions">
        <button class="btn primary" type="button" data-testid="open-create" @click="openCreate">登记模型配置</button>
        <button class="btn" type="button" data-testid="export" @click="exportRows">导出关系清单</button>
      </div>
    </header>
    <div class="dag-wrap" ref="dagRef">
      <svg class="dag-svg" :viewBox="`0 0 ${svgW} ${svgH}`">
        <defs><marker id="arrowhead" markerWidth="8" markerHeight="6" refX="8" refY="3" orient="auto"><path d="M0,0 L8,3 L0,6" fill="#94a3b8"/></marker></defs>
        <g v-for="edge in graph.edges" :key="edge.from+edge.to">
          <line class="dag-edge" :x1="nodePos[edge.from]?.x||0" :y1="nodePos[edge.from]?.y||0" :x2="nodePos[edge.to]?.x||0" :y2="nodePos[edge.to]?.y||0"/>
          <text :x="(nodePos[edge.from]?.x+nodePos[edge.to]?.x)/2" :y="(nodePos[edge.from]?.y+nodePos[edge.to]?.y)/2 - 6" font-size="10" fill="#64748b" text-anchor="middle">{{ edge.label }}</text>
        </g>
        <g v-for="node in graph.nodes" :key="node.id" :class="['dag-node', {selected: selectedNode?.id===node.id}]" @click="selectNode(node)" @mousedown="startDrag($event, node)">
          <rect :x="nodePos[node.id]?.x - 60" :y="nodePos[node.id]?.y - 20" width="120" height="40" rx="6" :fill="nodeColor(node.status)"/>
          <text :x="nodePos[node.id]?.x" :y="nodePos[node.id]?.y + 4" text-anchor="middle">{{ node.name || node.label }}</text>
        </g>
      </svg>
    </div>
    <div v-if="selectedNode" class="dag-detail" data-testid="dag-detail">
      <dl><dt>编号</dt><dd>{{ selectedNode.id }}</dd></dl>
      <dl><dt>名称</dt><dd>{{ selectedNode.name || selectedNode.label }}</dd></dl>
      <dl v-for="f in detailFields" :key="f"><dt>{{ f }}</dt><dd>{{ selectedNode[f] ?? '—' }}</dd></dl>
      <div style="grid-column:1/-1; margin-top:8px; display:flex; gap:6px;">
        <button v-for="action in actions" :key="action" class="btn" :data-testid="`action-${selectedNode.id}-${action}`" @click="runAction(action, selectedNode)">{{ action }}</button>
      </div>
    </div>

    <section class="batch-panel" data-testid="batch-panel">
      <header class="batch-head">
        <div><h3>批量训练样本</h3><p class="page-desc">多选模型后按成功样本生成训练结果；样本成功清单与特征汇总一起生成。</p></div>
        <div class="batch-controls">
          <label class="filter-item"><span>样本成功清单版本</span>
            <input v-model="versionInput" data-testid="version-input" list="version-options" placeholder="留空取当前生效版本" />
            <datalist id="version-options"><option v-for="v in versions" :key="`${v.模型编号}-${v.版本}`" :value="v.版本">{{ v.模型编号 }} · {{ v.版本状态 }}</option></datalist>
          </label>
          <label class="filter-item"><span>训练路径</span>
            <select v-model="pathInput" data-testid="path-input">
              <option v-for="p in trainPaths" :key="p" :value="p">{{ p }}</option>
            </select>
          </label>
          <button class="btn primary" type="button" data-testid="batch-train" :disabled="!selectedIds.length || training" @click="submitBatchTrain">
            {{ training ? '训练中…' : `批量训练（${selectedIds.length}）` }}
          </button>
        </div>
      </header>

      <table class="data-table batch-table">
        <thead>
          <tr>
            <th class="check-col"><input type="checkbox" data-testid="select-all" :checked="allSelected" @change="toggleAll" /></th>
            <th>模型编号</th><th>模型名称</th><th>模型类型</th><th>训练数据集</th><th>特征清单版本</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="row in graph.nodes" :key="`sel-${row.id}`" :data-testid="`select-row-${row.id}`">
            <td class="check-col"><input type="checkbox" :data-testid="`select-${row.id}`" :checked="selectedIds.includes(row.id)" @change="toggleSelect(row.id)" /></td>
            <td>{{ row['模型编号'] ?? '—' }}</td>
            <td>{{ row['模型名称'] ?? row.name ?? '—' }}</td>
            <td>{{ row['模型类型'] ?? '—' }}</td>
            <td>{{ row['训练数据集'] ?? '—' }}</td>
            <td>{{ versionsOf(row['模型编号']).map((v: any) => `${v.版本}(${v.版本状态})`).join('、') || '—' }}</td>
          </tr>
          <tr v-if="!graph.nodes.length"><td colspan="6" class="empty-state">暂无可训练的模型配置</td></tr>
        </tbody>
      </table>
      <p v-if="message" :class="['batch-message', { error: hasError }]" data-testid="batch-message">{{ message }}</p>

      <h4 class="batch-title">训练结果</h4>
      <table class="data-table batch-table">
        <thead>
          <tr>
            <th>模型编号</th><th>成功清单版本</th><th>训练路径</th><th>样本总数</th><th>成功样本</th><th>失败样本</th>
            <th>成功率</th><th>本次训练使用特征</th><th>评估指标</th><th>操作</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="run in runs" :key="`run-${run.id}`" :data-testid="`run-${run.id}`" :data-model="run['模型编号']" :data-version="run['样本成功清单版本']" :data-path="run['训练路径']">
            <td>{{ run['模型编号'] }}</td>
            <td>{{ run['样本成功清单版本'] }}</td>
            <td>{{ run['训练路径'] }}</td>
            <td>{{ run['样本总数'] }}</td>
            <td>{{ run['成功样本数'] }}</td>
            <td :class="{ 'error-text': run['失败样本数'] > 0 }">{{ run['失败样本数'] }}</td>
            <td data-testid="success-rate">{{ formatRate(run['成功率']) }}</td>
            <td>{{ (run['本次特征'] || []).join('、') || '—' }}</td>
            <td>{{ formatMetrics(run['评估指标']) }}</td>
            <td class="row-actions">
              <button class="link" type="button" :data-testid="`run-detail-${run.id}`" @click="openRun(run.id)">详情</button>
              <button class="link" type="button" :data-testid="`run-evaluate-${run.id}`" @click="evaluateRun(run.id)">复核评估</button>
              <button class="link" type="button" :data-testid="`run-export-${run.id}`" @click="exportTraining(run)">导出</button>
            </td>
          </tr>
          <tr v-if="!runs.length"><td colspan="10" class="empty-state">暂无训练结果，请选择模型后批量训练</td></tr>
        </tbody>
      </table>

      <div v-if="activeRun" class="run-detail" data-testid="run-detail">
        <header class="run-detail-head">
          <strong>{{ activeRun['模型编号'] }} · {{ activeRun['样本成功清单版本'] }} · {{ activeRun['训练路径'] }}</strong>
          <span class="page-desc">版本输入：{{ activeRun['版本输入'] }}，成功率 {{ formatRate(activeRun['成功率']) }}</span>
        </header>
        <div class="run-detail-grid">
          <div>
            <h5>本次训练使用的特征</h5>
            <p data-testid="used-features">{{ (activeRun['本次特征'] || []).join('、') || '—' }}</p>
            <h5>特征汇总</h5>
            <ul class="feature-summary">
              <li v-for="f in activeRun['特征汇总'] || []" :key="f.特征">{{ f.特征 }}：均值 {{ f.均值 }}，覆盖 {{ f.覆盖样本数 }} 条</li>
              <li v-if="!(activeRun['特征汇总'] || []).length" class="empty-state">无可用特征</li>
            </ul>
            <h5>评估指标</h5>
            <p data-testid="run-metrics">{{ formatMetrics(activeRun['评估指标']) }}</p>
            <p v-if="activeRun['评估复核']" class="page-desc" data-testid="run-recheck">
              复核：清单 {{ activeRun['评估复核']['复核样本数'] }} 条，有效 {{ activeRun['评估复核']['有效样本数'] }} 条
              <span v-if="activeRun['评估复核']['剔除样本']?.length">，剔除 {{ activeRun['评估复核']['剔除样本'].length }} 条</span>
            </p>
          </div>
          <div>
            <h5>失败样本（待补齐）</h5>
            <table class="data-table compact">
              <thead><tr><th>样本编号</th><th>分区</th><th>读取状态</th><th>失败原因</th><th>处理结果</th></tr></thead>
              <tbody>
                <tr v-for="p in activeRun['待补齐样本'] || []" :key="p.样本编号" :data-testid="`pending-${p.样本编号}`">
                  <td>{{ p.样本编号 }}</td><td>{{ p.分区 }}</td><td>{{ p.读取状态 }}</td><td>{{ p.失败原因 || '—' }}</td><td>{{ p.处理结果 }}</td>
                </tr>
                <tr v-if="!(activeRun['待补齐样本'] || []).length"><td colspan="5" class="empty-state">本批次没有失败样本</td></tr>
              </tbody>
            </table>
          </div>
        </div>
        <h5>样本成功清单（逐条处理结果）</h5>
        <table class="data-table compact" data-testid="success-list">
          <thead><tr><th>样本编号</th><th>分区</th><th>读取状态</th><th>处理结果</th><th>有效特征</th><th>清单版本</th></tr></thead>
          <tbody>
            <tr v-for="s in activeRun['样本成功清单'] || []" :key="s.样本编号" :data-testid="`success-${s.样本编号}`">
              <td>{{ s.样本编号 }}</td><td>{{ s.分区 }}</td><td>{{ s.读取状态 }}</td><td>{{ s.处理结果 }}</td>
              <td>{{ (s.有效特征 || []).join('、') }}</td><td>{{ s.清单版本 }}</td>
            </tr>
            <tr v-if="!(activeRun['样本成功清单'] || []).length"><td colspan="6" class="empty-state">没有实际读取成功的样本</td></tr>
          </tbody>
        </table>
      </div>
    </section>

    <div v-if="showCreate" class="modal-backdrop" data-testid="create-modal">
      <form class="modal" @submit.prevent="submitCreate"><h3>登记模型配置</h3>
        <label v-for="field in columns.slice(0,3)" :key="field" class="create-field"><span>{{ field }}</span><input v-model="createValues[field]" :placeholder="`请输入${field}`" required /></label>
        <div style="display:flex;gap:8px;justify-content:flex-end;margin-top:12px">
          <button class="btn" type="button" @click="showCreate=false">取消</button>
          <button class="btn primary" type="submit" data-testid="submit-create">提交</button>
        </div>
      </form>
    </div>
    <footer class="page-foot"><span>{{ graph.nodes.length }} 个节点</span><span v-if="errorMessage" class="error-text">{{ errorMessage }}</span></footer>
  </section>
</template>
<script setup lang="ts">
import { computed, onMounted, reactive, ref } from 'vue'
import { request } from '@/api/client'
const ENDPOINT = '/api/model'
const columns = ["模型编号", "模型名称", "模型类型", "训练数据集", "特征字段", "目标字段", "评估指标", "模型状态"]
const actions = ["提交训练", "启动评估", "部署模型"]
const trainPaths = ["排除失败样本", "补齐失败分片"]
const STATUS_COLORS: Record<string,string> = { '草稿':'#e2e8f0','待校验':'#fef3c7','已启用':'#d1fae5','已停用':'#fee2e2' }
const nodeColor = (s: string) => STATUS_COLORS[s] || '#e2e8f0'
const graph = reactive<{nodes: any[], edges: any[]}>({ nodes: [], edges: [] })
const nodePos = ref<Record<number,{x:number;y:number}>>({})
const selectedNode = ref<any>(null)
const detailFields = computed(() => columns.filter((f: string) => !['id','name','label','status'].includes(f)))
const svgW = ref(800); const svgH = ref(400)
const errorMessage = ref('')
const showCreate = ref(false)
const createValues = ref<Record<string,string>>({})
const versions = ref<any[]>([])
const runs = ref<any[]>([])
const selectedIds = ref<number[]>([])
const versionInput = ref('')
const pathInput = ref(trainPaths[0])
const training = ref(false)
const activeRun = ref<any>(null)
const message = ref('')
const hasError = ref(false)
let dragNode: any = null; const dagRef = ref<HTMLElement|null>(null)
const allSelected = computed(() => graph.nodes.length > 0 && selectedIds.value.length === graph.nodes.length)
function layout(nodes: any[]) {
  const w = 800; const h = Math.max(400, nodes.length * 80)
  svgW.value = w; svgH.value = h
  const pos: Record<number,{x:number;y:number}> = {}
  nodes.forEach((n,i) => { pos[n.id] = { x: 120 + (i % 5) * 150, y: 50 + Math.floor(i / 5) * 80 } })
  nodePos.value = pos
}
function selectNode(node: any) { selectedNode.value = selectedNode.value?.id === node.id ? null : node }
function startDrag(e: MouseEvent, node: any) { dragNode = node; const onMove = (ev: MouseEvent) => { if(!dragNode) return; nodePos.value[node.id] = { x: ev.offsetX, y: ev.offsetY } }; const onUp = () => { dragNode = null; document.removeEventListener('mousemove', onMove); document.removeEventListener('mouseup', onUp) }; document.addEventListener('mousemove', onMove); document.addEventListener('mouseup', onUp) }
function versionsOf(modelCode: string) { return versions.value.filter((v: any) => v['模型编号'] === modelCode) }
function toggleSelect(id: number) {
  selectedIds.value = selectedIds.value.includes(id) ? selectedIds.value.filter(item => item !== id) : [...selectedIds.value, id]
}
function toggleAll(event: Event) {
  const checked = (event.target as HTMLInputElement).checked
  selectedIds.value = checked ? graph.nodes.map(node => node.id) : []
}
function formatRate(rate: number) { return `${Math.round(Number(rate || 0) * 1000) / 10}%` }
function formatMetrics(metrics: Record<string, number> | undefined) {
  if (!metrics) return '—'
  return Object.entries(metrics).map(([key, value]) => `${key} ${value}`).join('；')
}
async function loadVersions() {
  try { const res = await request(`${ENDPOINT}/versions`); versions.value = (await res.json()).items || [] } catch(e) { versions.value = [] }
}
async function loadRuns() {
  try { const res = await request(`${ENDPOINT}/training`); runs.value = (await res.json()).items || [] } catch(e) { runs.value = [] }
}
async function reload() {
  try { const res = await request(ENDPOINT); const data = await res.json(); graph.nodes = data.nodes || []; graph.edges = data.edges || []; layout(graph.nodes); await Promise.all([loadVersions(), loadRuns()]) } catch(e) { errorMessage.value = '加载失败' }
}
async function submitBatchTrain() {
  if (!selectedIds.value.length) { message.value = '请至少选择一个分析模型'; hasError.value = true; return }
  training.value = true; message.value = ''; hasError.value = false
  try {
    const res = await request(`${ENDPOINT}/training/batch`, {
      method: 'POST',
      body: JSON.stringify({ model_ids: selectedIds.value, version: versionInput.value || null, path: pathInput.value }),
    })
    const payload = await res.json()
    message.value = payload.message || (payload.ok ? '批量训练已完成' : '批量训练未生效')
    hasError.value = !payload.ok
    await Promise.all([loadRuns(), reloadGraphOnly()])
    if (payload.ok && payload.items?.length) { await openRun(payload.items[payload.items.length - 1].id) }
  } catch (e) {
    message.value = '批量训练未送达，请稍后重试'; hasError.value = true
  } finally {
    training.value = false
  }
}
async function reloadGraphOnly() {
  try { const res = await request(ENDPOINT); const data = await res.json(); graph.nodes = data.nodes || []; graph.edges = data.edges || []; layout(graph.nodes) } catch(e) {}
}
async function openRun(runId: number) {
  try { const res = await request(`${ENDPOINT}/training/${runId}`); activeRun.value = await res.json() } catch(e) { activeRun.value = null }
}
async function evaluateRun(runId: number) {
  try {
    const res = await request(`${ENDPOINT}/training/${runId}/evaluate`, { method: 'POST' })
    const payload = await res.json()
    message.value = payload.message || '评估复核已完成'; hasError.value = !payload.ok
    if (payload.ok) { await Promise.all([loadRuns(), openRun(runId)]) }
  } catch (e) { message.value = '评估复核失败'; hasError.value = true }
}
function exportTraining(run: any) {
  const query = new URLSearchParams({ model_code: run['模型编号'] || '', version: run['样本成功清单版本'] || '', path: run['训练路径'] || '' }).toString()
  window.open(`${ENDPOINT}/training/export?${query}`, '_blank')
}
async function runAction(action: string, node: any) {
  try { await request(`${ENDPOINT}/${node.id}/actions`, { method: 'POST', body: JSON.stringify({ action }) }); await reload() } catch(e) { errorMessage.value = '操作失败' }
}
function openCreate() { createValues.value = {}; showCreate.value = true }
async function submitCreate() {
  try { const res = await request(ENDPOINT, { method: 'POST', body: JSON.stringify({ values: createValues.value }) }); if(!res.ok) throw new Error('创建失败'); showCreate.value = false; await reload() } catch(e: any) { errorMessage.value = e.message }
}
function exportRows() { window.open(`${ENDPOINT}/export`, '_blank') }
onMounted(reload)
</script>

<style scoped>
.batch-panel { margin-top: 20px; background: #fff; border: 1px solid var(--border); border-radius: 8px; padding: 14px; }
.batch-head { display: flex; justify-content: space-between; align-items: flex-end; gap: 12px; flex-wrap: wrap; }
.batch-head h3 { margin: 0; }
.batch-controls { display: flex; gap: 10px; align-items: flex-end; flex-wrap: wrap; }
.batch-controls input, .batch-controls select { padding: 6px 10px; border: 1px solid var(--border); border-radius: 6px; font-size: 13px; }
.batch-table { margin-top: 10px; }
.batch-table .check-col { width: 36px; text-align: center; }
.batch-message { font-size: 13px; color: #1f6feb; margin: 8px 0 0; }
.batch-message.error { color: #b42318; }
.batch-title { margin: 18px 0 4px; }
.run-detail { margin-top: 14px; padding: 12px; border: 1px solid var(--border); border-radius: 8px; background: #fafbfc; }
.run-detail-head { display: flex; gap: 12px; align-items: baseline; flex-wrap: wrap; margin-bottom: 8px; }
.run-detail-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 16px; }
.run-detail h5 { margin: 10px 0 4px; font-size: 13px; color: var(--muted); }
.feature-summary { margin: 0; padding-left: 18px; font-size: 13px; }
.data-table.compact th, .data-table.compact td { padding: 5px 8px; font-size: 12px; }
</style>
