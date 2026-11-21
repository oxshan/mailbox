<script setup lang="ts">
import { computed, onMounted, reactive, ref } from 'vue'
import { api, type Domain, type Scenario, type ScenarioInput, type Template } from '../api'
import Icon from '../Icon.vue'
import { copy, fail, formatTime, notify } from '../ui'

const DEFAULT_KEY = 'default'
const blank = (): ScenarioInput => ({ name: '', key: '', domain_ids: [], template_id: null })

const scenarios = ref<Scenario[]>([])
const templates = ref<Template[]>([])
const domains = ref<Domain[]>([])
const form = reactive<ScenarioInput>(blank())
const editing = ref<Scenario | null>(null)
const saving = ref(false)

const isDefault = computed(() => editing.value?.key === DEFAULT_KEY)
const usage = computed(() => `POST /api/mailbox?scenario=${form.key || '<标识>'}`)

async function load() {
  try {
    ;[scenarios.value, templates.value, domains.value] = await Promise.all([
      api.scenarios(),
      api.templates(),
      api.domains(),
    ])
  } catch (err) {
    fail(err, '加载失败')
  }
}

function reset() {
  editing.value = null
  Object.assign(form, blank())
}

function edit(item: Scenario) {
  editing.value = item
  Object.assign(form, {
    name: item.name,
    key: item.key,
    domain_ids: [...item.domain_ids],
    template_id: item.template_id,
  })
}

async function save() {
  saving.value = true
  try {
    await api.saveScenario({ ...form, domain_ids: [...form.domain_ids] }, editing.value?.id)
    notify(editing.value ? '已保存，该场景下的邮件已按新模板重新解析' : '已创建业务场景')
    reset()
    await load()
  } catch (err) {
    fail(err, '保存失败')
  } finally {
    saving.value = false
  }
}

async function remove(item: Scenario) {
  const note = item.mailbox_count ? `其下 ${item.mailbox_count} 个邮箱会移到默认场景。` : ''
  if (!window.confirm(`确定删除业务场景「${item.name}」吗？${note}`)) return
  try {
    await api.deleteScenario(item.id)
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
    <h1>业务场景</h1>
    <span class="muted">生成邮箱时指定场景，场景决定使用的域名和验证码解析模板</span>
  </header>

  <div class="manage">
    <section class="panel" aria-label="场景列表">
      <div class="table-wrap">
        <table class="table">
          <thead>
            <tr>
              <th>名称</th>
              <th>标识</th>
              <th>域名</th>
              <th>邮件模板</th>
              <th class="num">邮箱数</th>
              <th>创建时间</th>
              <th class="actions">操作</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="item in scenarios" :key="item.id" :class="{ active: editing?.id === item.id }">
              <td class="strong">{{ item.name }}</td>
              <td><code>{{ item.key }}</code></td>
              <td class="clip">
                <code v-if="item.domains.length" :title="item.domains.join('\n')">
                  {{ item.domains.map((name) => `@${name}`).join(' ') }}
                </code>
                <span v-else class="muted">全部已启用域名</span>
              </td>
              <td>
                <span v-if="item.template_name" class="tag primary">{{ item.template_name }}</span>
                <span v-else class="muted">内置通用规则</span>
              </td>
              <td class="num">{{ item.mailbox_count }}</td>
              <td class="time">{{ formatTime(item.created_at) }}</td>
              <td class="actions">
                <button class="icon-btn" type="button" title="编辑" :aria-label="`编辑 ${item.name}`" @click="edit(item)">
                  <Icon name="pencil" />
                </button>
                <button
                  class="icon-btn danger"
                  type="button"
                  title="删除"
                  :aria-label="`删除 ${item.name}`"
                  :disabled="item.key === DEFAULT_KEY"
                  @click="remove(item)"
                >
                  <Icon name="trash" />
                </button>
              </td>
            </tr>
          </tbody>
        </table>
      </div>
    </section>

    <form class="panel form" @submit.prevent="save">
      <div class="panel-head">
        <h2>{{ editing ? `编辑场景：${editing.name}` : '新建场景' }}</h2>
      </div>
      <div class="form-body">
        <div class="field">
          <label for="scenario-name">名称</label>
          <input id="scenario-name" v-model="form.name" required maxlength="40" placeholder="例如：GitHub 注册" />
        </div>
        <div class="field">
          <label for="scenario-key">标识</label>
          <input
            id="scenario-key"
            v-model="form.key"
            :disabled="isDefault"
            maxlength="32"
            pattern="[a-z0-9][a-z0-9_\-]*"
            placeholder="例如：github，留空自动生成"
          />
          <p class="hint">接口调用时用它指定场景，只能用小写字母、数字、下划线和连字符。</p>
        </div>
        <fieldset class="field">
          <legend class="label">域名</legend>
          <div class="checks">
            <label v-for="domain in domains" :key="domain.id" class="check" :class="{ off: !domain.enabled }">
              <input v-model="form.domain_ids" type="checkbox" :value="domain.id" />
              <code>@{{ domain.name }}</code>
              <span v-if="!domain.enabled" class="tag">已停用</span>
            </label>
          </div>
          <p class="hint">
            生成邮箱时从勾选的域名里随机选一个；都不勾选表示使用全部已启用的域名。<a href="#/domains">管理域名</a>
          </p>
        </fieldset>
        <div class="field">
          <label for="scenario-template">邮件模板</label>
          <select id="scenario-template" v-model="form.template_id">
            <option :value="null">内置通用规则</option>
            <option v-for="item in templates" :key="item.id" :value="item.id">{{ item.name }}</option>
          </select>
          <p class="hint">
            该场景下邮箱收到的邮件按此模板提取验证码。<a href="#/templates">管理邮件模板</a>
          </p>
        </div>
        <div class="field">
          <span class="label">接口调用</span>
          <button class="snippet" type="button" title="点击复制" @click="copy(usage)">
            <code>{{ usage }}</code><Icon name="copy" />
          </button>
        </div>
        <div class="form-actions">
          <button class="btn primary" type="submit" :disabled="saving">{{ saving ? '保存中…' : '保存' }}</button>
          <button v-if="editing" class="btn" type="button" @click="reset">取消</button>
        </div>
      </div>
    </form>
  </div>
</template>
