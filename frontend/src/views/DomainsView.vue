<script setup lang="ts">
import { onMounted, reactive, ref } from 'vue'
import { api, type Domain, type DomainCheck } from '../api'
import Icon from '../Icon.vue'
import { fail, formatTime, notify } from '../ui'

const statusLabel: Record<DomainCheck['status'], string> = {
  ok: '已指向本机',
  missing: '未配置 MX',
  unverified: '无法确认',
  mismatch: '指向其他服务器',
  error: '查询失败',
}

const domains = ref<Domain[]>([])
const form = reactive({ name: '', note: '' })
const editing = ref<Domain | null>(null)
const saving = ref(false)
const checks = reactive<Record<number, DomainCheck | 'loading'>>({})

async function load() {
  try {
    domains.value = await api.domains()
  } catch (err) {
    fail(err, '加载失败')
  }
}

function reset() {
  editing.value = null
  Object.assign(form, { name: '', note: '' })
}

function edit(item: Domain) {
  editing.value = item
  Object.assign(form, { name: item.name, note: item.note })
}

async function save() {
  saving.value = true
  try {
    if (editing.value) await api.updateDomain(editing.value.id, { note: form.note })
    else await api.createDomain(form.name, form.note)
    notify(editing.value ? '已保存' : '已添加域名，记得配置 MX 记录')
    reset()
    await load()
  } catch (err) {
    fail(err, '保存失败')
  } finally {
    saving.value = false
  }
}

async function toggle(item: Domain) {
  try {
    await api.updateDomain(item.id, { enabled: !item.enabled })
    notify(item.enabled ? `已停用 ${item.name}，不再用它生成新邮箱` : `已启用 ${item.name}`)
    await load()
  } catch (err) {
    fail(err, '操作失败')
  }
}

async function check(item: Domain) {
  checks[item.id] = 'loading'
  try {
    checks[item.id] = await api.checkDomain(item.name)
  } catch (err) {
    delete checks[item.id]
    fail(err, '检测失败')
  }
}

const checkAll = () => Promise.all(domains.value.map(check))

async function remove(item: Domain) {
  const note = item.mailbox_count ? `该域名下已有的 ${item.mailbox_count} 个邮箱将无法再收信。` : ''
  if (!window.confirm(`确定删除域名 ${item.name} 吗？删除后发往该域名的邮件会被拒收。${note}`)) return
  try {
    await api.deleteDomain(item.id)
    if (editing.value?.id === item.id) reset()
    notify('已删除')
    await load()
  } catch (err) {
    fail(err, '删除失败')
  }
}

const result = (id: number) => {
  const value = checks[id]
  return value && value !== 'loading' ? value : null
}

onMounted(load)
</script>

<template>
  <header class="page-head">
    <h1>域名管理</h1>
    <span class="muted">添加后立即可以收信，无需重启；业务场景可以关联其中一个或多个域名</span>
    <button class="btn push" type="button" :disabled="!domains.length" @click="checkAll">
      <Icon name="refresh" :size="14" />检测全部 MX
    </button>
  </header>

  <div class="manage">
    <section class="panel" aria-label="域名列表">
      <div class="table-wrap">
        <table class="table">
          <thead>
            <tr>
              <th>域名</th>
              <th>状态</th>
              <th>MX 检测</th>
              <th>备注</th>
              <th class="num">邮箱数</th>
              <th class="num">关联场景</th>
              <th>添加时间</th>
              <th class="actions">操作</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="item in domains" :key="item.id" :class="{ active: editing?.id === item.id }">
              <td class="strong"><code>{{ item.name }}</code></td>
              <td>
                <button
                  class="switch"
                  type="button"
                  role="switch"
                  :aria-checked="item.enabled"
                  :aria-label="`${item.name} 启用状态`"
                  @click="toggle(item)"
                >
                  <span class="knob"></span>
                </button>
                <span class="switch-label">{{ item.enabled ? '启用' : '停用' }}</span>
              </td>
              <td class="clip">
                <span v-if="checks[item.id] === 'loading'" class="muted">检测中…</span>
                <span v-else-if="result(item.id)" class="tag" :class="result(item.id)!.status" :title="result(item.id)!.message">
                  {{ statusLabel[result(item.id)!.status] }}
                </span>
                <span v-else class="muted">未检测</span>
              </td>
              <td class="clip" :title="item.note">{{ item.note || '—' }}</td>
              <td class="num">{{ item.mailbox_count }}</td>
              <td class="num">{{ item.scenario_count }}</td>
              <td class="time">{{ formatTime(item.created_at) }}</td>
              <td class="actions">
                <button class="icon-btn" type="button" title="检测 MX" :aria-label="`检测 ${item.name}`" @click="check(item)">
                  <Icon name="refresh" :class="{ spin: checks[item.id] === 'loading' }" />
                </button>
                <button class="icon-btn" type="button" title="编辑备注" :aria-label="`编辑 ${item.name}`" @click="edit(item)">
                  <Icon name="pencil" />
                </button>
                <button class="icon-btn danger" type="button" title="删除" :aria-label="`删除 ${item.name}`" @click="remove(item)">
                  <Icon name="trash" />
                </button>
              </td>
            </tr>
          </tbody>
        </table>
        <div v-if="!domains.length" class="empty">
          <Icon name="globe" :size="28" />
          <p>还没有域名</p>
          <p>在右侧添加第一个收信域名</p>
        </div>
      </div>
      <ul v-if="domains.some((item) => result(item.id))" class="check-log">
        <template v-for="item in domains" :key="item.id">
          <li v-if="result(item.id)" :class="result(item.id)!.status">
            <code>{{ item.name }}</code>
            <span>{{ result(item.id)!.message }}</span>
            <span v-if="result(item.id)!.mx.length" class="muted">MX：{{ result(item.id)!.mx.join('、') }}</span>
          </li>
        </template>
      </ul>
    </section>

    <form class="panel form" @submit.prevent="save">
      <div class="panel-head">
        <h2>{{ editing ? `编辑域名：${editing.name}` : '添加域名' }}</h2>
      </div>
      <div class="form-body">
        <div class="field">
          <label for="domain-name">域名</label>
          <input
            id="domain-name"
            v-model="form.name"
            required
            :disabled="Boolean(editing)"
            maxlength="253"
            spellcheck="false"
            placeholder="例如：example.com"
          />
        </div>
        <div class="field">
          <label for="domain-note">备注</label>
          <input id="domain-note" v-model="form.note" maxlength="80" placeholder="可选" />
        </div>
        <div class="field">
          <span class="label">DNS 配置</span>
          <p class="hint">
            域名需要一条 MX 记录指向本服务器的主机名（主机名用 A 记录指向服务器 IP，Cloudflare 上要关闭代理）。配好后点“检测 MX”确认。
          </p>
        </div>
        <div class="form-actions">
          <button class="btn primary" type="submit" :disabled="saving">{{ saving ? '保存中…' : editing ? '保存' : '添加' }}</button>
          <button v-if="editing" class="btn" type="button" @click="reset">取消</button>
        </div>
      </div>
    </form>
  </div>
</template>
