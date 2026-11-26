<template>
  <article
    class="receipt-card"
    data-testid="disposal-receipt"
    :data-receipt-id="receiptId"
    :data-system="systemCode"
  >
    <header class="receipt-head">
      <h4>停用处置回执</h4>
      <span class="receipt-no" data-testid="receipt-id">
        回执编号：<strong>{{ receiptId }}</strong>
      </span>
    </header>
    <p class="receipt-message" data-testid="receipt-message">{{ receipt.message || fallbackMessage }}</p>
    <dl class="receipt-grid">
      <div><dt>源系统</dt><dd data-testid="receipt-system">{{ systemCode }}</dd></div>
      <div>
        <dt>受理资格</dt>
        <dd class="danger" data-testid="receipt-new-job-policy">
          {{ receipt.新作业受理 || '关闭（不予受理新作业/新区间领取）' }}
        </dd>
      </div>
      <div>
        <dt>确认断点</dt>
        <dd data-testid="receipt-checkpoint">{{ checkpoint }}</dd>
      </div>
      <div>
        <dt>收尾范围</dt>
        <dd data-testid="receipt-scope">{{ receipt.收尾范围 || `仅 ${systemCode}` }}</dd>
      </div>
      <div v-if="receipt.处置结论">
        <dt>在途处置结论</dt>
        <dd data-testid="receipt-decision">{{ receipt.处置结论 }}</dd>
      </div>
      <div v-if="lateSummary">
        <dt>迟到回执核对</dt>
        <dd data-testid="receipt-late">
          共 {{ lateSummary.total ?? 0 }} 条，接纳 {{ lateSummary.accepted ?? 0 }} 条，
          不予确认 {{ lateSummary.rejected ?? 0 }} 条
        </dd>
      </div>
    </dl>

    <div v-if="jobs.length" class="receipt-jobs" data-testid="receipt-jobs">
      <h5>旧作业处理结果（同一份回执可在源系统登记与记录抽取两处核对）</h5>
      <table>
        <thead>
          <tr><th>作业</th><th>批次</th><th>处置结果</th><th>接续点</th></tr>
        </thead>
        <tbody>
          <tr v-for="(job, idx) in jobs" :key="`${job.job_id ?? idx}`" :data-testid="`receipt-job-${job.job_id ?? idx}`">
            <td>{{ job.job_name || job.job_id }}</td>
            <td>{{ job.batch_no || '—' }}</td>
            <td :data-state="String(job.state || '')">{{ job.result || job.state }}</td>
            <td>{{ job.resume_from != null ? job.resume_from : '—' }}</td>
          </tr>
        </tbody>
      </table>
    </div>
  </article>
</template>

<script setup lang="ts">
import { computed } from 'vue'
import type { DisposalReceipt } from '@/api/handoff'

const props = defineProps<{ receipt: DisposalReceipt }>()

const receiptId = computed(() => String(props.receipt.id || props.receipt.receipt_id || props.receipt.回执编号 || ''))
const systemCode = computed(() => String(props.receipt.system || props.receipt.系统标识 || ''))
const checkpoint = computed(() => props.receipt.确认断点 ?? props.receipt.confirmed_checkpoint
  ?? props.receipt.checkpoint_at_stop ?? '—')
const jobs = computed<Array<Record<string, unknown>>>(
  () => (props.receipt.在途作业处置 as Array<Record<string, unknown>>)
    || (props.receipt.jobs as Array<Record<string, unknown>>) || [],
)
const lateSummary = computed(() => props.receipt.迟到回执核对 as
  | { total?: number; accepted?: number; rejected?: number }
  | undefined)
const fallbackMessage = computed(() =>
  `源系统「${systemCode.value}」已停用，新作业/新区间不予领取；停用处置回执 ${receiptId.value}，确认断点 ${checkpoint.value}。`)
</script>

<style scoped>
.receipt-card {
  border: 1px solid #f0a45a;
  background: #fffaeb;
  border-radius: 10px;
  padding: 12px 14px;
}
.receipt-head {
  display: flex;
  justify-content: space-between;
  align-items: center;
  gap: 12px;
  flex-wrap: wrap;
}
.receipt-head h4 {
  margin: 0;
  font-size: 15px;
  color: #b54708;
}
.receipt-no {
  font-size: 13px;
}
.receipt-message {
  margin: 8px 0;
  font-size: 13px;
  line-height: 1.7;
}
.receipt-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
  gap: 8px 16px;
  margin: 0;
}
.receipt-grid div {
  display: flex;
  gap: 8px;
  font-size: 12px;
}
.receipt-grid dt {
  color: var(--muted, #64748b);
  flex-shrink: 0;
}
.receipt-grid dd {
  margin: 0;
  font-weight: 600;
}
.danger {
  color: #b42318;
}
.receipt-jobs {
  margin-top: 10px;
  overflow-x: auto;
}
.receipt-jobs h5 {
  margin: 0 0 6px;
  font-size: 12px;
  color: #475467;
}
.receipt-jobs table {
  width: 100%;
  border-collapse: collapse;
  background: #fff;
}
.receipt-jobs th,
.receipt-jobs td {
  border: 1px solid var(--border, #d8dee6);
  padding: 4px 8px;
  font-size: 12px;
  text-align: left;
}
</style>
