<script setup lang="ts">
import { computed, onMounted, reactive, ref } from 'vue'
import { api, type Source, type SourceCheck, type SourceInput, type SourceKind } from '../api'
import Icon from '../Icon.vue'
import { fail, notify } from '../ui'

const KINDS: Record<SourceKind, string> = { addy: 'addy.io', duck: 'Duck' }
const FORMATS = [
  ['', '按 addy 账户的默认设置'],
  ['random_characters', '随机字符'],
  ['random_words', '随机单词'],
  ['random_noun', '随机名词'],
  ['random_male_name', '随机男名'],
  ['random_female_name', '随机女名'],
  ['uuid', 'UUID'],
] as const
const blank = (): SourceInput => ({
  kind: 'addy',
  name: '',
  token: '',
  collector: '',
  alias_domain: '',
  alias_format: '',
  note: '',
})

const sources = ref<Source[]>([])
const form = reactive<SourceInput>(blank())
const editing = ref<Source | null>(null)
const saving = ref(false)
const checks = reactive<Record<number, SourceCheck | 'loading'>>({})

const isAddy = computed(() => form.kind === 'addy')
// The check endpoint reports "pending" and "unknown"; both are shown like an unverified domain.
const tone = (status: SourceCheck['status']) => (status === 'ok' || status === 'error' ? status : 'unverified')
const result = (id: number) => {
  const value = checks[id]
  return value && value !== 'loading' ? value : null
}

async function load() {
  try {
    sources.value = await api.sources()
  } catch (err) {
    fail(err, '加载失败')
  }
}

function reset() {
  editing.value = null
  Object.assign(form, blank())
}

function edit(item: Source) {
  editing.value = item
  Object.assign(form, {
    kind: item.kind,
    name: item.name,
    token: '',
    collector: item.collector,
    alias_domain: item.alias_domain,
    alias_format: item.alias_format,
    note: item.note,
  })
}

async function save() {
  saving.value = true
  try {
    if (editing.value) await api.updateSource(editing.value.id, { ...form })
    else await api.createSource({ ...form })
    notify(editing.value ? '已保存' : '已添加，请检测一次确认可用')
    reset()
    await load()
  } catch (err) {
    fail(err, '保存失败')
  } finally {
    saving.value = false
  }
}

async function toggle(item: Source) {
  try {
    await api.updateSource(item.id, { enabled: !item.enabled })
    notify(item.enabled ? `已停用 ${item.name}，不再用它生成邮箱` : `已启用 ${item.name}`)
    await load()
  } catch (err) {
    fail(err, '操作失败')
  }
}

async function check(item: Source) {
  checks[item.id] = 'loading'
  try {
    checks[item.id] = await api.checkSource(item.id)
  } catch (err) {
    checks[item.id] = { status: 'error', message: (err as Error).message }
  }
}

async function remove(item: Source) {
  if (!window.confirm(`确定删除来源「${item.name}」吗？`)) return
  try {
    await api.deleteSource(item.id)
    if (editing.value?.id === item.id) reset()
    notify('已删除')
    await load()
  } catch (err) {
    fail(err, '删除失败')
  }
}

onMounted(load)
</script>

<template>
  <header class="page-head">
    <h1>邮箱来源</h1>
    <span class="muted">接入 addy.io 和 Duck，用它们的别名作为邮箱；别名收到的邮件转发到收集地址，由本系统收下</span>
  </header>

  <div class="manage">
    <section class="panel" aria-label="来源列表">
      <div class="table-wrap">
        <table class="table">
          <thead>
            <tr>
              <th>名称</th>
              <th>类型</th>
              <th>状态</th>
              <th>收集地址</th>
              <th>令牌</th>
              <th class="num">邮箱数</th>
              <th>检测</th>
              <th class="actions">操作</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="item in sources" :key="item.id" :class="{ active: editing?.id === item.id }">
              <td class="strong" :title="item.note">{{ item.name }}</td>
              <td><span class="tag primary">{{ KINDS[item.kind] }}</span></td>
              <td>
                <button
                  class="switch"
                  type="button"
                  role="switch"
                  :aria-checked="item.enabled"
                  :aria-label="`${item.name} 启用开关`"
                  @click="toggle(item)"
                >
                  <span class="knob"></span>
                </button>
                <span class="switch-label">{{ item.enabled ? '启用' : '已停用' }}</span>
              </td>
              <td><code>{{ item.collector }}</code></td>
              <td><code>{{ item.token_mask }}</code></td>
              <td class="num">{{ item.mailbox_count }}</td>
              <td>
                <span v-if="checks[item.id] === 'loading'" class="muted">检测中…</span>
                <span v-else-if="result(item.id)" class="tag" :class="tone(result(item.id)!.status)">
                  {{ { ok: '可用', pending: '待验证', unknown: '无法校验', error: '失败' }[result(item.id)!.status] }}
                </span>
                <span v-else class="muted">—</span>
              </td>
              <td class="actions">
                <button class="icon-btn" type="button" title="检测" :aria-label="`检测 ${item.name}`" @click="check(item)">
                  <Icon name="refresh" :class="{ spin: checks[item.id] === 'loading' }" />
                </button>
                <button class="icon-btn" type="button" title="编辑" :aria-label="`编辑 ${item.name}`" @click="edit(item)">
                  <Icon name="pencil" />
                </button>
                <button class="icon-btn danger" type="button" title="删除" :aria-label="`删除 ${item.name}`" @click="remove(item)">
                  <Icon name="trash" />
                </button>
              </td>
            </tr>
          </tbody>
        </table>
        <div v-if="!sources.length" class="empty">
          <Icon name="link" :size="28" />
          <p>还没有外部来源</p>
          <p>不添加也能用：自有域名下的邮箱不需要来源</p>
        </div>
      </div>
      <ul v-if="sources.some((item) => result(item.id))" class="check-log">
        <template v-for="item in sources" :key="item.id">
          <li v-if="result(item.id)" :class="tone(result(item.id)!.status)">
            <code>{{ item.name }}</code>
            <span>{{ result(item.id)!.message }}</span>
          </li>
        </template>
      </ul>
    </section>

    <form class="panel form" @submit.prevent="save">
      <div class="panel-head">
        <h2>{{ editing ? `编辑来源：${editing.name}` : '添加来源' }}</h2>
      </div>
      <div class="form-body">
        <div class="field">
          <label for="source-kind">类型</label>
          <select id="source-kind" v-model="form.kind" :disabled="Boolean(editing)">
            <option value="addy">addy.io（可生成、停用、删除）</option>
            <option value="duck">Duck（只能生成）</option>
          </select>
        </div>
        <div class="field">
          <label for="source-name">名称</label>
          <input id="source-name" v-model="form.name" required maxlength="40" placeholder="例如：addy 主号" />
        </div>
        <div class="field">
          <label for="source-token">令牌</label>
          <input
            id="source-token"
            v-model="form.token"
            type="password"
            autocomplete="off"
            spellcheck="false"
            :required="!editing"
            :placeholder="editing ? `留空则不修改（当前 ${editing.token_mask}）` : ''"
          />
          <p v-if="isAddy" class="hint">在 addy.io 的 Settings → API 里创建的 API Key。</p>
          <p v-else class="hint">
            在浏览器登录 duckduckgo.com/email，打开开发者工具的 Network，点一次“Generate Private Duck
            Address”，复制 addresses 请求头里 <code>Authorization: Bearer</code> 后面的内容。
          </p>
        </div>
        <div class="field">
          <label for="source-collector">收集地址</label>
          <input
            id="source-collector"
            v-model="form.collector"
            type="email"
            required
            maxlength="254"
            spellcheck="false"
            placeholder="例如：collect-addy@你的域名"
          />
          <p v-if="isAddy" class="hint">
            自有域名下的一个地址，每个来源单独用一个。点“检测”会把它加为 addy
            的收件人，验证邮件会进到这个地址的收件箱，点开链接后才能生成邮箱。也可以填一个已接入的邮箱账号（见“邮箱账号”），这样
            addy 的收件人不用改。
          </p>
          <p v-else class="hint">
            自有域名下的一个地址，每个来源单独用一个。需要你自己在 Duck 的 Email Protection
            设置里把转发邮箱改成它。也可以填一个已接入的邮箱账号（见“邮箱账号”），这样 Duck 的转发邮箱不用改。
          </p>
        </div>
        <template v-if="isAddy">
          <div class="field">
            <label for="source-domain">别名域名</label>
            <input
              id="source-domain"
              v-model="form.alias_domain"
              maxlength="253"
              spellcheck="false"
              placeholder="留空用 addy 账户的默认域名"
            />
            <p class="hint">例如 <code>anonaddy.me</code>，或你的用户名域名 <code>用户名.anonaddy.com</code>。</p>
          </div>
          <div class="field">
            <label for="source-format">别名格式</label>
            <select id="source-format" v-model="form.alias_format">
              <option v-for="[value, label] in FORMATS" :key="value" :value="value">{{ label }}</option>
            </select>
          </div>
        </template>
        <div class="field">
          <label for="source-note">备注</label>
          <input id="source-note" v-model="form.note" maxlength="80" placeholder="可选" />
        </div>
        <div class="form-actions">
          <button class="btn primary" type="submit" :disabled="saving">{{ saving ? '保存中…' : editing ? '保存' : '添加' }}</button>
          <button v-if="editing" class="btn" type="button" @click="reset">取消</button>
        </div>
      </div>
    </form>
  </div>
</template>
