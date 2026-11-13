<template>
  <section class="page" data-module="compliance">
    <header class="page-head">
      <div>
        <h2>采集合规配置管理</h2>
        <p class="page-desc">合规规则必须完成登记、审核、发布后才可用；发布后的访问间隔、并发上限与请求头模板取自审核通过的版本，采集计划按发布版本读取限制。</p>
      </div>
      <div class="page-actions">
        <button class="btn primary" type="button" :disabled="loading" @click="openCreate">登记合规规则</button>
        <button class="btn" type="button" @click="exportRows">导出采集合规配置清单</button>
      </div>
    </header>

    <div class="stat-row">
      <article v-for="item in stats" :key="item.label" class="stat-card">
        <span class="stat-label">{{ item.label }}</span>
        <strong class="stat-value">{{ item.value }}</strong>
      </article>
    </div>

    <form class="filter-bar" @submit.prevent="reload">
      <label v-for="field in filterFields" :key="field" class="filter-item">
        <span>{{ field }}</span>
        <input v-model="filters[field]" :placeholder="`按${field}检索`" />
      </label>
      <label class="filter-item">
        <span>合规状态</span>
        <select v-model="statusFilter">
          <option value="">全部状态</option>
          <option v-for="item in statuses" :key="item" :value="item">{{ item }}</option>
        </select>
      </label>
      <button class="btn" type="submit" :disabled="loading">查询</button>
      <button class="btn ghost" type="button" @click="resetFilters">重置条件</button>
    </form>

    <table class="data-table">
      <thead>
        <tr>
          <th v-for="column in columns" :key="column">{{ column }}</th>
          <th>可执行动作</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="row in rows" :key="String(row.id)">
          <td v-for="column in columns" :key="column">{{ display(row, column) }}</td>
          <td class="row-actions">
            <button
              v-for="action in actionOptions(row)"
              :key="action"
              class="link"
              type="button"
              :disabled="busyId === row.id"
              @click="runAction(action, row)"
            >
              {{ action }}
            </button>
            <button class="link" type="button" @click="openDetail(row)">查看详情</button>
          </td>
        </tr>
        <tr v-if="!rows.length">
          <td :colspan="columns.length + 1" class="empty-state">暂无采集合规配置数据，可先登记合规规则</td>
        </tr>
      </tbody>
    </table>

    <footer class="page-foot">
      <span>共 {{ total }} 条采集合规配置记录</span>
      <span v-if="errorMessage" class="error-text">{{ errorMessage }}</span>
      <span v-else-if="successMessage" class="success-text">{{ successMessage }}</span>
    </footer>

    <dialog ref="createDialog" class="dialog">
      <form class="dialog-body" @submit.prevent="submitCreate">
        <h3>登记合规规则</h3>
        <label v-for="field in formFields" :key="field" class="form-item">
          <span>{{ field }}<em v-if="requiredFields.includes(field)">*</em></span>
          <input v-model="form[field]" :placeholder="`请填写${field}`" />
        </label>
        <p v-if="dialogError" class="error-text">{{ dialogError }}</p>
        <div class="dialog-actions">
          <button class="btn" type="button" @click="closeCreate">取消</button>
          <button class="btn primary" type="submit" :disabled="submitting">提交登记</button>
        </div>
      </form>
    </dialog>

    <dialog ref="detailDialog" class="dialog wide">
      <div class="dialog-body">
        <h3>合规规则详情</h3>
        <dl v-if="detail" class="detail-grid">
          <template v-for="field in detailFields" :key="field">
            <dt>{{ field }}</dt>
            <dd>{{ display(detail, field) }}</dd>
          </template>
          <dt>审核版本</dt>
          <dd>{{ detailApproval?.审批版本 ? `v${detailApproval.审批版本}` : '未审核' }}</dd>
          <dt>审批人/时间</dt>
          <dd>{{ detailApproval ? `${detailApproval.审批人} · ${detailApproval.审批时间}` : '—' }}</dd>
          <dt>发布版本</dt>
          <dd>{{ detail?.publish_version ? `v${detail.publish_version}` : '未发布' }}</dd>
        </dl>
        <h4>审批与操作历史</h4>
        <ul v-if="history.length" class="history-list">
          <li v-for="(item, index) in history" :key="index">
            {{ item.操作时间 }} · {{ item.动作 }} · {{ item.结论 }}（{{ item.操作人 }}）
          </li>
        </ul>
        <p v-else class="muted">暂无历史记录</p>
        <div class="dialog-actions">
          <button class="btn" type="button" @click="closeDetail">关闭</button>
        </div>
      </div>
    </dialog>
  </section>
</template>

<script setup lang="ts">
import { onMounted, reactive, ref } from 'vue'

import { request } from '@/api/client'

type Row = Record<string, unknown>

const ENDPOINT = '/api/compliance'
const columns = ['规则编号', '站点域名', '协议标识', '访问间隔', '并发上限', '请求头模板', '合规状态', '最近审查']
const detailFields = columns
const requiredFields = ['规则编号', '站点域名', '协议标识']
const formFields = [...columns.slice(0, 6)]
const statuses = ['草稿', '待审批', '已生效', '已废弃']
const actionByStatus: Record<string, string[]> = {
  草稿: ['提交审批'],
  待审批: ['审核通过', '发布规则', '撤回审核'],
  已生效: ['废弃规则'],
  已废弃: [],
}

const rows = ref<Row[]>([])
const total = ref(0)
const loading = ref(false)
const submitting = ref(false)
const busyId = ref<number | null>(null)
const errorMessage = ref('')
const successMessage = ref('')
const statusFilter = ref('')
const filters = reactive<Record<string, string>>({})
const filterFields = columns.slice(0, 3)
const statValues = reactive({ total: 0, active: 0, pending: 0 })
const stats = ref([
  { label: '合规规则总数', value: 0 },
  { label: '已生效规则', value: 0 },
  { label: '待审批规则', value: 0 },
])

const createDialog = ref<HTMLDialogElement | null>(null)
const detailDialog = ref<HTMLDialogElement | null>(null)
const dialogError = ref('')
const detail = ref<Row | null>(null)
const detailApproval = ref<Record<string, string> | null>(null)
const history = ref<Record<string, string>[]>([])
const form = reactive<Record<string, string>>({})

function display(row: Row, field: string) {
  if (field === '合规状态') {
    return row['status'] ?? row['合规状态'] ?? '—'
  }
  const value = row[field]
  return value === null || value === undefined || value === '' ? '—' : String(value)
}

function actionOptions(row: Row): string[] {
  const status = String(row['status'] ?? '')
  return actionByStatus[status] ?? []
}

function resetFilters() {
  Object.keys(filters).forEach((key) => delete filters[key])
  statusFilter.value = ''
  void reload()
}

function exportRows() {
  window.open(`${ENDPOINT}/export`, '_blank')
}

function openCreate() {
  Object.keys(form).forEach((key) => delete form[key])
  formFields.forEach((field) => { form[field] = '' })
  dialogError.value = ''
  createDialog.value?.showModal()
}

function closeCreate() {
  dialogError.value = ''
  createDialog.value?.close()
}

async function readJson(response: Response) {
  const payload = await response.json().catch(() => ({}))
  return payload as { detail?: unknown; message?: unknown; ok?: boolean; [key: string]: unknown }
}

async function submitCreate() {
  dialogError.value = ''
  const missing = requiredFields.filter((field) => !(form[field] ?? '').trim())
  if (missing.length) {
    dialogError.value = `缺少必填字段：${missing.join('、')}`
    return
  }
  submitting.value = true
  try {
    const response = await request(ENDPOINT, {
      method: 'POST',
      body: JSON.stringify({ values: { ...form } }),
    })
    const payload = await readJson(response)
    if (!response.ok || payload.ok === false) {
      throw new Error(String(payload.detail ?? payload.message ?? '合规规则登记失败'))
    }
    closeCreate()
    successMessage.value = String(payload.message ?? '合规规则已登记')
    await reload()
  } catch (error) {
    dialogError.value = error instanceof Error ? error.message : '合规规则登记失败'
  } finally {
    submitting.value = false
  }
}

async function runAction(action: string, row: Row) {
  const id = Number(row.id)
  errorMessage.value = ''
  successMessage.value = ''
  busyId.value = id
  try {
    const response = await request(`${ENDPOINT}/${id}/actions`, {
      method: 'POST',
      body: JSON.stringify({ values: { action, expected_version: row.revision } }),
    })
    const payload = await readJson(response)
    if (!response.ok || payload.ok === false) {
      throw new Error(String(payload.detail ?? payload.message ?? `合规规则「${action}」未生效`))
    }
    successMessage.value = String(payload.message ?? `合规规则已${action}`)
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '采集合规配置操作失败'
  } finally {
    busyId.value = null
  }
}

async function openDetail(row: Row) {
  const id = Number(row.id)
  errorMessage.value = ''
  try {
    const response = await request(`${ENDPOINT}/${id}`)
    const payload = await readJson(response)
    if (!response.ok) {
      throw new Error(String(payload.detail ?? '合规规则详情读取失败'))
    }
    detail.value = payload as Row
    detailApproval.value = (payload.approval as Record<string, string> | undefined) ?? null
    const historyResponse = await request(`${ENDPOINT}/${id}/history`)
    const historyPayload = await readJson(historyResponse)
    history.value = historyResponse.ok ? (historyPayload.history as Record<string, string>[]) ?? [] : []
    detailDialog.value?.showModal()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '合规规则详情读取失败'
  }
}

function closeDetail() {
  detailDialog.value?.close()
}

async function reload() {
  errorMessage.value = ''
  loading.value = true
  try {
    const params = new URLSearchParams()
    if (filters['规则编号']) params.set('keyword', filters['规则编号'])
    if (filters['站点域名']) params.set('site', filters['站点域名'])
    if (filters['协议标识']) params.set('protocol', filters['协议标识'])
    if (statusFilter.value) params.set('status', statusFilter.value)
    const response = await request(`${ENDPOINT}?${params.toString()}`)
    const payload = await readJson(response)
    if (!response.ok) {
      throw new Error(String(payload.detail ?? '合规规则列表读取失败'))
    }
    rows.value = (payload.items as Row[]) ?? []
    total.value = Number(payload.total ?? rows.value.length)
    statValues.total = total.value
    statValues.active = rows.value.filter((row) => row.status === '已生效').length
    statValues.pending = rows.value.filter((row) => row.status === '待审批').length
    stats.value = [
      { label: '合规规则总数', value: statValues.total },
      { label: '已生效规则', value: statValues.active },
      { label: '待审批规则', value: statValues.pending },
    ]
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '采集合规配置列表读取失败'
  } finally {
    loading.value = false
  }
}

onMounted(reload)
</script>
