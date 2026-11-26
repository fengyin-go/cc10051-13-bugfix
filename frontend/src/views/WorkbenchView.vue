<template>
  <section class="page" :data-module="meta.key">
    <header class="page-head">
      <div>
        <h2>{{ meta.name }}</h2>
        <p class="page-desc">{{ meta.desc }}</p>
      </div>
      <div class="page-actions">
        <button class="btn primary" data-testid="open-create" type="button" @click="openCreate">
          登记{{ meta.entity }}
        </button>
        <button class="btn" data-testid="export" type="button" @click="exportRows">
          导出{{ meta.name }}清单
        </button>
      </div>
    </header>

    <div class="stat-row">
      <article v-for="item in stats" :key="item.label" class="stat-card">
        <span class="stat-label">{{ item.label }}</span>
        <strong class="stat-value">{{ item.value }}</strong>
      </article>
    </div>

    <p class="status-legend">
      <span v-for="item in statusSummary" :key="item.status" class="legend-item">
        {{ item.status }}：{{ item.count }}
      </span>
    </p>

    <!-- 动作回执：成功/失败提示 + 两个入口共享的同一份停用处置回执 -->
    <div
      v-if="noticeMessage"
      class="action-notice"
      :class="{ 'is-error': !noticeOk }"
      data-testid="action-result"
      role="status"
    >
      <span data-testid="action-message">{{ noticeMessage }}</span>
    </div>
    <p v-if="errorMessage" class="error-text" data-testid="action-error">{{ errorMessage }}</p>

    <div v-if="activeReceipt" class="notice-receipt" data-testid="notice-receipt-wrap">
      <ReceiptCard :receipt="activeReceipt" />
    </div>

    <form class="filter-bar" @submit.prevent="reload">
      <label v-for="field in filterFields" :key="field" class="filter-item">
        <span>{{ field }}</span>
        <input v-model="filters[field]" :data-testid="`filter-${field}`" :placeholder="`按${field}检索`" />
      </label>
      <button class="btn" data-testid="query" type="submit">查询</button>
      <button class="btn ghost" data-testid="reset" type="button" @click="resetFilters">重置条件</button>
    </form>

    <table class="data-table">
      <thead>
        <tr>
          <th v-for="column in meta.fields" :key="column">{{ column }}</th>
          <th v-if="meta.key === 'record_extract'">接入资格 / 确认断点 / 批次</th>
          <th v-else-if="meta.key === 'source_system'">接入/受理资格 / 停用回执</th>
          <th>当前状态</th>
          <th>可执行动作</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="row in rows" :key="String(row.id)" :data-testid="`row-${row.id}`" :data-status="String(row.status)">
          <td v-for="column in meta.fields" :key="column">{{ row[column] ?? '—' }}</td>
          <td v-if="meta.key === 'record_extract'" class="handoff-cell">
            <div
              :data-testid="`qualification-${row.id}`"
              :data-qualification="String(row.qualification ?? '')"
              :class="row.qualification === 'closed' ? 'tag-danger' : 'tag-ok'"
            >
              {{ row.受理资格 || '—' }}
            </div>
            <div :data-testid="`checkpoint-${row.id}`" class="muted">
              确认断点：{{ row.确认断点 ?? row.confirmed_checkpoint ?? '—' }}
            </div>
            <div v-if="row.current_batch_no" :data-testid="`batch-state-${row.id}`" class="muted">
              {{ row.current_batch_no }} · {{ row.批次状态 || '' }}
            </div>
            <div v-if="row.停用回执编号" :data-testid="`row-receipt-${row.id}`" class="receipt-link">
              停用处置回执：{{ row.停用回执编号 }}
            </div>
          </td>
          <td v-else-if="meta.key === 'source_system'" class="handoff-cell">
            <div
              :data-testid="`source-qualification-${row.id}`"
              :data-qualification="String(row.access_open ? 'open' : 'closed')"
              :class="row.access_open ? 'tag-ok' : 'tag-danger'"
            >
              {{ row.接入资格 || '—' }}
            </div>
            <div v-if="row.停用回执编号" :data-testid="`source-row-receipt-${row.id}`" class="receipt-link">
              停用处置回执：{{ row.停用回执编号 }}
            </div>
          </td>
          <td>{{ row.status }}</td>
          <td class="row-actions">
            <button
              v-for="action in meta.actions"
              :key="action"
              class="link"
              type="button"
              :data-testid="`action-${row.id}-${action}`"
              @click="runAction(action, row)"
            >
              {{ action }}
            </button>
            <template v-if="meta.key === 'record_extract'">
              <button class="link" type="button" :data-testid="`action-${row.id}-领取区间`" @click="quickAction('领取区间', row)">
                领取区间
              </button>
              <button class="link" type="button" :data-testid="`action-${row.id}-完成回执`" @click="quickAction('完成回执', row, { count: 4 })">
                上报4条完成回执
              </button>
              <button class="link" type="button" :data-testid="`action-${row.id}-提交区间`" @click="quickAction('提交区间', row)">
                提交完整区间
              </button>
              <button class="link" type="button" :data-testid="`action-${row.id}-断点接续`" @click="quickAction('断点接续', row, { count: 4 })">
                断点接续
              </button>
            </template>
          </td>
        </tr>
        <tr v-if="!rows.length">
          <td :colspan="emptyColspan" class="empty-state">
            暂无{{ meta.name }}数据，可先登记{{ meta.entity }}
          </td>
        </tr>
      </tbody>
    </table>

    <!-- 源系统登记：停用处置回执台账 + 一键演练场景 -->
    <section v-if="meta.key === 'source_system'" class="handoff-panel" data-testid="source-handoff">
      <div class="panel-bar">
        <h3>停用处置回执（与「记录抽取作业」页显示同一份）</h3>
        <div class="panel-tools">
          <button class="btn" type="button" data-testid="prepare-crm-scenario" @click="prepareScenario">
            一键准备 CRM/ERP 演练（CRM 确认断点 800）
          </button>
          <button class="btn ghost" type="button" data-testid="refresh-handoff-source" @click="loadHandoff">
            刷新交接数据
          </button>
        </div>
      </div>
      <p class="hint">
        对已接入系统点击行内「停用系统」即开具停用处置回执：关闭新作业领取资格、只收尾本源系统的在途批次、
        保留确认断点与可接续批次。随后可到「记录抽取作业」页核对同一回执并验证启动校验。
      </p>
      <div class="receipt-list" data-testid="disposal-receipts">
        <ReceiptCard v-for="receipt in receipts" :key="receipt.id" :receipt="receipt" />
        <p v-if="!receipts.length" class="muted" data-testid="no-receipts">暂无停用处置回执</p>
      </div>
    </section>

    <!-- 记录抽取：失败恢复 + 同一份回执台账 -->
    <section v-if="meta.key === 'record_extract'" class="handoff-panel" data-testid="extract-handoff">
      <div class="panel-bar">
        <h3>交接核对与失败恢复</h3>
        <div class="panel-tools">
          <button class="btn primary" type="button" data-testid="recover-state" @click="recoverState">
            模拟服务端失败恢复（核对在途响应/回执/实领，保留确认断点）
          </button>
          <button class="btn ghost" type="button" data-testid="refresh-handoff-extract" @click="loadHandoff">
            刷新交接数据
          </button>
        </div>
      </div>
      <p class="hint">
        区间必须「已领取 → 收齐回执 → 提交完整区间并经服务端确认」才成为可消费批次；半段读入不算完成。
        停用后新作业/新区间不予领取，迟到回执只做核对留痕；恢复接入后可在原作业行从确认断点接续。
      </p>
      <div class="checkpoint-chips" data-testid="checkpoint-chips">
        <span v-for="(value, code) in checkpoints" :key="code" class="chip" :data-testid="`checkpoint-chip-${code}`">
          {{ code }} 当前断点：<strong>{{ value }}</strong>
        </span>
      </div>
      <h4>停用处置回执（与「源系统登记」页同一份）</h4>
      <div class="receipt-list" data-testid="disposal-receipts-extract">
        <ReceiptCard v-for="receipt in receipts" :key="receipt.id" :receipt="receipt" />
        <p v-if="!receipts.length" class="muted">暂无停用处置回执</p>
      </div>
    </section>

    <!-- 标准化清洗：只出现服务端确认可消费的批次 + 实领记录 -->
    <section v-if="meta.key === 'std_cleanse'" class="handoff-panel" data-testid="cleanse-handoff">
      <h3>服务端确认可消费的抽取批次</h3>
      <p class="hint">
        清洗必须按已经确认的抽取批次取得数据和记录范围；半段读入、停用中的候补区间不在此清单，也不能领取。
      </p>
      <table class="data-table" data-testid="consumable-batches">
        <thead>
          <tr><th>批次号</th><th>源系统</th><th>实体编码</th><th>记录范围</th><th>记录数</th><th>操作</th></tr>
        </thead>
        <tbody>
          <tr
            v-for="batch in consumableBatches"
            :key="String(batch.batch_no || batch.id)"
            :data-testid="`consumable-batch-${batch.batch_no || batch.id}`"
            data-consumable="true"
          >
            <td>{{ batch.batch_no || batch.批次号 || batch.id }}</td>
            <td>{{ batch.源系统 || batch.system }}</td>
            <td>{{ batch.实体编码 || batch.entity || '—' }}</td>
            <td data-testid="`consumable-range-${batch.batch_no || batch.id}`">{{ batch.记录范围 }}</td>
            <td>{{ batch.record_count ?? batch.expected_count ?? '—' }}</td>
            <td class="row-actions">
              <button
                class="link"
                type="button"
                :data-testid="`claim-cleanse-${batch.batch_no || batch.id}`"
                @click="claimCleanse(batch)"
              >
                领取清洗（实领）
              </button>
            </td>
          </tr>
          <tr v-if="!consumableBatches.length">
            <td colspan="6" class="empty-state">暂无服务端确认的可消费批次</td>
          </tr>
        </tbody>
      </table>

      <h3>清洗实领数据</h3>
      <table class="data-table" data-testid="cleanse-claims">
        <thead>
          <tr><th>领取编号</th><th>批次号</th><th>源系统</th><th>记录范围</th><th>实领记录数</th><th>领取时间</th></tr>
        </thead>
        <tbody>
          <tr v-for="claim in claims" :key="String(claim.claim_no || claim.id)" :data-testid="`cleanse-claim-${claim.claim_no || claim.id}`">
            <td>{{ claim.claim_no || claim.领取编号 || claim.id }}</td>
            <td>{{ claim.batch_no || claim.批次号 }}</td>
            <td>{{ claim.源系统 }}</td>
            <td>{{ claim.记录范围 }}</td>
            <td>{{ claim.实领记录数 ?? claim.record_count ?? '—' }}</td>
            <td>{{ claim.claimed_at || '—' }}</td>
          </tr>
          <tr v-if="!claims.length">
            <td colspan="6" class="empty-state" data-testid="no-cleanse-claims">暂无清洗实领记录</td>
          </tr>
        </tbody>
      </table>

      <h3>
        不可消费的候补 / 半段 / 已终止区间
        <button class="btn ghost" type="button" data-testid="toggle-excluded-batches" @click="showExcluded = !showExcluded">
          {{ showExcluded ? '收起' : `展开核对（${pendingBatches.length}）` }}
        </button>
      </h3>
      <table v-if="showExcluded" class="data-table" data-testid="excluded-batches">
        <thead>
          <tr><th>批次号</th><th>源系统</th><th>记录范围</th><th>状态</th><th>排除原因</th></tr>
        </thead>
        <tbody>
          <tr
            v-for="batch in pendingBatches"
            :key="String(batch.batch_no || batch.id)"
            :data-testid="`excluded-batch-${batch.batch_no || batch.id}`"
          >
            <td>{{ batch.batch_no || batch.批次号 || batch.id }}</td>
            <td>{{ batch.源系统 || batch.system }}</td>
            <td>{{ batch.记录范围 }}</td>
            <td>{{ batch.状态 || batch.state }}</td>
            <td>{{ batch.terminated ? '停用处置：原提交已终止，仅保留可接续批次' : '区间未完整/未经服务端确认，不发清洗资格' }}</td>
          </tr>
          <tr v-if="!pendingBatches.length">
            <td colspan="5" class="empty-state">暂无候补区间</td>
          </tr>
        </tbody>
      </table>
    </section>

    <div v-if="showCreate" class="modal-backdrop" data-testid="create-modal">
      <form class="modal" @submit.prevent="submitCreate">
        <h3>登记{{ meta.entity }}</h3>
        <label v-for="field in meta.fields" :key="field" class="create-field">
          <span>{{ field }}</span>
          <input v-model="createValues[field]" :data-testid="`create-${field}`" required />
        </label>
        <div class="page-actions">
          <button class="btn" type="button" @click="showCreate = false">取消</button>
          <button class="btn primary" data-testid="submit-create" type="submit" :disabled="creating">
            {{ creating ? '提交中…' : '确认登记' }}
          </button>
        </div>
      </form>
    </div>

    <footer class="page-foot">
      <span>共 {{ total }} 条{{ meta.name }}记录</span>
      <span v-if="errorMessage" class="error-text">{{ errorMessage }}</span>
    </footer>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'

import {
  createEntry,
  downloadEntries,
  listEntries,
  moduleMeta,
  runAction as applyAction,
} from '@/api/backend-service'
import {
  fetchHandoffState,
  postHandoff,
  type CleanseClaim,
  type DisposalReceipt,
  type ExtractBatch,
  type HandoffState,
} from '@/api/handoff'
import ReceiptCard from '@/components/ReceiptCard.vue'
import type { EntryRow } from '@/data/types'

const props = defineProps<{ moduleKey: string }>()
const meta = computed(() => moduleMeta(props.moduleKey))
const rows = ref<EntryRow[]>([])
const total = ref(0)
const errorMessage = ref('')
const notice = ref<Record<string, any> | null>(null)
const filters = ref<Record<string, string>>({})
const filterFields = computed(() => meta.value.fields.slice(0, 3))
const showCreate = ref(false)
const creating = ref(false)
const createValues = ref<Record<string, string>>({})

const handoff = ref<HandoffState | null>(null)
const showExcluded = ref(false)
const receipts = computed<DisposalReceipt[]>(() => handoff.value?.receipts ?? [])
const consumableBatches = computed<ExtractBatch[]>(() => handoff.value?.consumable_batches ?? [])
const pendingBatches = computed<ExtractBatch[]>(() => handoff.value?.pending_batches ?? [])
const claims = computed<CleanseClaim[]>(() => handoff.value?.claims ?? [])
const checkpoints = computed<Record<string, number>>(() => handoff.value?.checkpoints ?? {})

const stats = computed(() => [
  { label: meta.value.metrics[0], value: total.value },
  { label: meta.value.metrics[1], value: rows.value.filter((row) => row.status === meta.value.statuses[1]).length },
  {
    label: meta.value.metrics[2],
    value: rows.value.filter((row) => row.status === meta.value.statuses[meta.value.statuses.length - 1]).length,
  },
])
const statusSummary = computed(() =>
  meta.value.statuses.map((status) => ({
    status,
    count: rows.value.filter((row) => String(row.status) === status).length,
  })),
)
const emptyColspan = computed(() =>
  meta.value.fields.length + 2
  + ((meta.value.key === 'record_extract' || meta.value.key === 'source_system') ? 1 : 0))
const noticeMessage = computed(() => String(notice.value?.message ?? ''))
const noticeOk = computed(() => notice.value?.ok !== false)
const activeReceipt = computed<DisposalReceipt | null>(() => {
  const candidate = notice.value?.receipt ?? notice.value?.['停用处置回执']
  return (candidate as DisposalReceipt) ?? null
})

function resetFilters() {
  filters.value = {}
  void reload()
}

async function exportRows() {
  errorMessage.value = ''
  try {
    await downloadEntries(meta.value.key, filters.value)
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '导出失败'
  }
}

function openCreate() {
  createValues.value = Object.fromEntries(meta.value.fields.map((field) => [field, '']))
  errorMessage.value = ''
  showCreate.value = true
}

async function submitCreate() {
  creating.value = true
  errorMessage.value = ''
  try {
    const result = await createEntry(meta.value.key, createValues.value)
    if (!result.ok) throw new Error(result.message)
    showCreate.value = false
    notice.value = result as unknown as Record<string, any>
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '登记失败'
  } finally {
    creating.value = false
  }
}

async function runAction(action: string, row: EntryRow, extra: Record<string, unknown> = {}) {
  errorMessage.value = ''
  try {
    const result = await applyAction(meta.value.key, Number(row.id), action, extra)
    notice.value = result as Record<string, any>
    if (!result.ok) {
      errorMessage.value = result.message || '操作被服务端拒绝'
    }
    await Promise.all([reload(), loadHandoff()])
  } catch (error) {
    notice.value = null
    errorMessage.value = error instanceof Error ? error.message : '操作失败'
  }
}

function quickAction(action: string, row: EntryRow, extra: Record<string, unknown> = {}) {
  return runAction(action, row, extra)
}

async function reload() {
  errorMessage.value = ''
  try {
    const payload = await listEntries(meta.value.key, filters.value)
    rows.value = payload.items
    total.value = payload.total
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '业务列表读取失败'
  }
}

async function loadHandoff() {
  if (!['source_system', 'record_extract', 'std_cleanse'].includes(meta.value.key)) return
  try {
    handoff.value = await fetchHandoffState()
  } catch {
    // 交接面板是增强信息，加载失败不阻断主表
  }
}

async function recoverState() {
  try {
    const result = await postHandoff('/api/handoff/recover', {})
    notice.value = result as Record<string, any>
    if (!(result as { ok?: boolean }).ok) errorMessage.value = String((result as { message?: string }).message || '恢复失败')
    await Promise.all([reload(), loadHandoff()])
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '失败恢复请求未送达'
  }
}

async function prepareScenario() {
  try {
    const result = await postHandoff('/api/handoff/scenarios/crm-erp-800', {})
    notice.value = result as Record<string, any>
    await Promise.all([reload(), loadHandoff()])
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '演练场景准备失败'
  }
}

async function claimCleanse(batch: ExtractBatch) {
  const ref = String(batch.batch_no || batch.批次号 || batch.id || '')
  try {
    const result = await postHandoff(`/api/handoff/batches/${encodeURIComponent(ref)}/claim-cleanse`, {})
    notice.value = result as Record<string, any>
    if (!(result as { ok?: boolean }).ok) {
      errorMessage.value = String((result as { message?: string }).message || '该批次不可消费')
    }
    await loadHandoff()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '清洗领取失败'
  }
}

// 同一路由组件在 /source_system 与 /record_extract 之间复用时，必须按 moduleKey 重新装载
watch(
  () => props.moduleKey,
  () => {
    notice.value = null
    errorMessage.value = ''
    filters.value = {}
    handoff.value = null
    showExcluded.value = false
    void reload()
    void loadHandoff()
  },
)

onMounted(() => {
  void reload()
  void loadHandoff()
})
</script>

<style scoped>
.action-notice {
  background: #ecfdf3;
  border: 1px solid #abefc6;
  color: #067647;
  border-radius: 8px;
  padding: 8px 12px;
  margin: 8px 0;
  font-size: 13px;
}
.action-notice.is-error {
  background: #fef3f2;
  border-color: #fda29b;
  color: #b42318;
}
.notice-receipt {
  margin: 8px 0 12px;
}
.handoff-cell {
  min-width: 220px;
  font-size: 12px;
  line-height: 1.6;
}
.muted {
  color: var(--muted);
  font-size: 12px;
}
.tag-ok {
  color: #067647;
  font-weight: 600;
}
.tag-danger {
  color: #b42318;
  font-weight: 600;
}
.receipt-link {
  color: #b54708;
}
.handoff-panel {
  margin-top: 20px;
  background: #fff;
  border: 1px solid var(--border);
  border-radius: 10px;
  padding: 14px 16px;
}
.handoff-panel h3 {
  margin: 0 0 6px;
  font-size: 15px;
}
.handoff-panel h4 {
  margin: 14px 0 8px;
  font-size: 13px;
}
.panel-bar {
  display: flex;
  justify-content: space-between;
  align-items: center;
  gap: 12px;
  flex-wrap: wrap;
}
.panel-tools {
  display: flex;
  gap: 8px;
}
.hint {
  color: var(--muted);
  font-size: 12px;
  margin: 4px 0 10px;
}
.receipt-list {
  display: grid;
  gap: 10px;
}
.checkpoint-chips {
  display: flex;
  gap: 8px;
  flex-wrap: wrap;
  margin: 8px 0;
}
.chip {
  background: #eff8ff;
  border: 1px solid #b2ddff;
  border-radius: 999px;
  padding: 4px 12px;
  font-size: 12px;
}
</style>
