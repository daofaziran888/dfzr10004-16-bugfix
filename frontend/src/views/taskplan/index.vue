<template>
  <section class="page" data-module="taskplan">
    <header class="page-head">
      <div>
        <h2>采集任务编排管理</h2>
        <p class="page-desc">启动采集计划前必须先读取合规规则的发布版本限制；访问间隔、并发上限或请求头模板缺失时计划不会启动，并返回待补录项。</p>
      </div>
      <div class="page-actions">
        <button class="btn primary" type="button" :disabled="loading" @click="openCreate">登记采集计划</button>
        <button class="btn" type="button" @click="exportRows">导出采集任务编排清单</button>
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
              v-for="action in actions"
              :key="action"
              class="link"
              type="button"
              :disabled="busyId === Number(row.id)"
              @click="runAction(action, row)"
            >
              {{ action }}
            </button>
            <button class="link" type="button" @click="openDetail(row)">查看详情</button>
          </td>
        </tr>
        <tr v-if="!rows.length">
          <td :colspan="columns.length + 1" class="empty-state">暂无采集任务编排数据，可先登记采集计划</td>
        </tr>
      </tbody>
    </table>

    <footer class="page-foot">
      <span>共 {{ total }} 条采集任务编排记录</span>
      <span v-if="errorMessage" class="error-text">{{ errorMessage }}</span>
      <span v-else-if="successMessage" class="success-text">{{ successMessage }}</span>
    </footer>

    <dialog ref="createDialog" class="dialog">
      <form class="dialog-body" @submit.prevent="submitCreate">
        <h3>登记采集计划</h3>
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
        <h3>采集计划详情</h3>
        <dl v-if="detail" class="detail-grid">
          <template v-for="field in columns" :key="field">
            <dt>{{ field }}</dt>
            <dd>{{ display(detail, field) }}</dd>
          </template>
          <dt>合规规则</dt>
          <dd>{{ detail['合规规则编号'] ?? detail['合规规则ID'] ?? '—' }}</dd>
        </dl>
        <h4>合规限制</h4>
        <dl v-if="limits" class="detail-grid">
          <template v-for="field in limitFields" :key="field">
            <dt>{{ field }}</dt>
            <dd>{{ limits[field] ?? '—' }}</dd>
          </template>
          <dt>限制来源</dt>
          <dd>{{ limits['限制来源'] ?? '—' }}（发布版本 v{{ limits['合规版本'] ?? '—' }}）</dd>
        </dl>
        <p v-else class="error-text">{{ limitsError || '尚无有效的合规限制记录' }}</p>
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

const ENDPOINT = '/api/taskplan'
const columns = ['计划编号', '目标站点', '采集频率', '数据范围', '优先级', '资源配额', '预计用时', '计划状态']
const actions = ['启动计划', '暂停计划', '归档计划']
const requiredFields = ['计划编号', '目标站点', '采集频率']
const formFields = ['计划编号', '目标站点', '采集频率', '数据范围', '优先级', '资源配额', '预计用时', '合规规则ID']
const limitFields = ['访问间隔', '并发上限', '请求头模板']

const rows = ref<Row[]>([])
const total = ref(0)
const loading = ref(false)
const submitting = ref(false)
const busyId = ref<number | null>(null)
const errorMessage = ref('')
const successMessage = ref('')
const filters = reactive<Record<string, string>>({})
const filterFields = columns.slice(0, 3)
const stats = ref([
  { label: '采集计划总数', value: 0 },
  { label: '运行中计划', value: 0 },
  { label: '已暂停计划', value: 0 },
])

const createDialog = ref<HTMLDialogElement | null>(null)
const detailDialog = ref<HTMLDialogElement | null>(null)
const dialogError = ref('')
const detail = ref<Row | null>(null)
const limits = ref<Record<string, string> | null>(null)
const limitsError = ref('')
const form = reactive<Record<string, string>>({})

function display(row: Row, field: string) {
  if (field === '计划状态') {
    return row['status'] ?? row['计划状态'] ?? '—'
  }
  const value = row[field]
  return value === null || value === undefined || value === '' ? '—' : String(value)
}

function resetFilters() {
  Object.keys(filters).forEach((key) => delete filters[key])
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
  return (await response.json().catch(() => ({}))) as Record<string, unknown>
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
    const values: Record<string, string> = { ...form }
    if (!values['合规规则ID']) delete values['合规规则ID']
    const response = await request(ENDPOINT, {
      method: 'POST',
      body: JSON.stringify({ values }),
    })
    const payload = await readJson(response)
    if (!response.ok || payload.ok === false) {
      throw new Error(String(payload.detail ?? payload.message ?? '采集计划登记失败'))
    }
    closeCreate()
    successMessage.value = String(payload.message ?? '采集计划已登记')
    await reload()
  } catch (error) {
    dialogError.value = error instanceof Error ? error.message : '采集计划登记失败'
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
      body: JSON.stringify({ values: { action } }),
    })
    const payload = await readJson(response)
    if (!response.ok || payload.ok === false) {
      const supplement = (payload.supplement as Record<string, string[]> | undefined)?.['待补录项'] ?? []
      const reason = String(payload.detail ?? payload.message ?? `采集计划「${action}」未生效`)
      throw new Error(supplement.length ? `${reason}；待补录项：${supplement.join('、')}` : reason)
    }
    successMessage.value = String(payload.message ?? `采集计划已${action}`)
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '采集任务编排操作失败'
  } finally {
    busyId.value = null
  }
}

async function openDetail(row: Row) {
  const id = Number(row.id)
  errorMessage.value = ''
  limits.value = null
  limitsError.value = ''
  try {
    const response = await request(`${ENDPOINT}/${id}`)
    const payload = await readJson(response)
    if (!response.ok) {
      throw new Error(String(payload.detail ?? '采集计划详情读取失败'))
    }
    detail.value = payload
    const limitsResponse = await request(`${ENDPOINT}/${id}/limits`)
    const limitsPayload = await readJson(limitsResponse)
    if (limitsResponse.ok) {
      limits.value = (limitsPayload.limits as Record<string, string>) ?? null
    } else {
      limitsError.value = String(limitsPayload.detail ?? '合规限制读取失败')
    }
    detailDialog.value?.showModal()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '采集计划详情读取失败'
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
    if (filters['计划编号']) params.set('keyword', filters['计划编号'])
    const response = await request(`${ENDPOINT}?${params.toString()}`)
    const payload = await readJson(response)
    if (!response.ok) {
      throw new Error(String(payload.detail ?? '采集计划列表读取失败'))
    }
    rows.value = (payload.items as Row[]) ?? []
    total.value = Number(payload.total ?? rows.value.length)
    stats.value = [
      { label: '采集计划总数', value: total.value },
      { label: '运行中计划', value: rows.value.filter((row) => row.status === '运行中').length },
      { label: '已暂停计划', value: rows.value.filter((row) => row.status === '已暂停').length },
    ]
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '采集任务编排列表读取失败'
  } finally {
    loading.value = false
  }
}

onMounted(reload)
</script>
