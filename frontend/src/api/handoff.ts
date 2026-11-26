import { request } from './client'

/** cc10051 交接链路的只读/语义接口封装；动作类操作仍走统一 runAction。 */

export type DisposalReceipt = {
  id: string
  receipt_id?: string
  回执编号?: string
  停用单号?: string
  system?: string
  系统标识?: string
  message?: string
  处置结论?: string
  新作业受理?: string
  确认断点?: number
  checkpoint_at_stop?: number
  confirmed_checkpoint?: number
  scope_systems?: string[]
  收尾范围?: string
  jobs?: Array<Record<string, unknown>>
  在途作业处置?: Array<Record<string, unknown>>
  迟到回执核对?: { total?: number; accepted?: number; rejected?: number }
  created_at?: string
  reason?: string
}

export type ExtractBatch = {
  id?: number
  batch_no?: string
  批次号?: string
  job_id?: number
  job_name?: string
  源系统?: string
  system?: string
  实体编码?: string
  entity?: string
  记录范围?: string
  range_start?: number
  range_end?: number
  state?: string
  状态?: string
  confirmed?: boolean
  consumable?: boolean
  terminated?: boolean
  submitted?: boolean
  complete?: boolean
  receipt_count?: number
  expected_count?: number
  record_count?: number
}

export type CleanseClaim = {
  id?: number
  claim_no?: string
  领取编号?: string
  batch_no?: string
  批次号?: string
  记录范围?: string
  源系统?: string
  实体编码?: string
  record_count?: number
  实领记录数?: number
  claimed_at?: string
}

export type HandoffState = {
  receipts: DisposalReceipt[]
  batches: ExtractBatch[]
  consumable_batches: ExtractBatch[]
  pending_batches: ExtractBatch[]
  claims: CleanseClaim[]
  checkpoints: Record<string, number>
}

async function getJson<T>(path: string): Promise<T> {
  const response = await request(path)
  if (!response.ok) throw new Error('交接数据读取失败')
  return (await response.json()) as T
}

export function fetchHandoffState(): Promise<HandoffState> {
  return getJson<HandoffState>('/api/handoff/state')
}

export function fetchConsumableBatches(): Promise<{ items: ExtractBatch[] }> {
  return getJson<{ items: ExtractBatch[] }>('/api/handoff/consumable-batches')
}

export function fetchCleanseClaims(): Promise<{ items: CleanseClaim[] }> {
  return getJson<{ items: CleanseClaim[] }>('/api/handoff/claims')
}

export async function postHandoff<T = Record<string, unknown>>(
  path: string,
  body: Record<string, unknown> = {},
): Promise<T> {
  const response = await request(path, { method: 'POST', body: JSON.stringify(body) })
  return (await response.json()) as T
}
