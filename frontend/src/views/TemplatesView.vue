<script setup lang="ts">
import { onMounted, reactive, ref, watch } from 'vue'
import { api, type Template, type TemplateInput } from '../api'
import Icon from '../Icon.vue'
import { fail, formatTime, notify } from '../ui'

const blank = (): TemplateInput => ({ name: '', mode: 'placeholder', pattern: '', sample: '' })

const modes = [
  { value: 'placeholder', label: '占位符' },
  { value: 'regex', label: '正则表达式' },
] as const

const templates = ref<Template[]>([])
const form = reactive<TemplateInput>(blank())
const editing = ref<Template | null>(null)
const saving = ref(false)
const testing = ref(false)
const result = ref<{ code: string | null } | null>(null)

async function load() {
  try {
    templates.value = await api.templates()
  } catch (err) {
    fail(err, '加载失败')
  }
}

function reset() {
  editing.value = null
  Object.assign(form, blank())
}

function edit(item: Template) {
  editing.value = item
  Object.assign(form, { name: item.name, mode: item.mode, pattern: item.pattern, sample: item.sample })
}

async function test() {
  testing.value = true
  try {
    result.value = { code: await api.testTemplate({ mode: form.mode, pattern: form.pattern, sample: form.sample }) }
  } catch (err) {
    fail(err, '测试失败')
  } finally {
    testing.value = false
  }
}

async function save() {
  saving.value = true
  try {
    await api.saveTemplate({ ...form }, editing.value?.id)
    notify(editing.value ? '已保存，关联场景的邮件已重新解析' : '已创建邮件模板')
    reset()
    await load()
  } catch (err) {
    fail(err, '保存失败')
  } finally {
    saving.value = false
  }
}

async function remove(item: Template) {
  const note = item.scenario_count ? `${item.scenario_count} 个关联场景会改用内置通用规则。` : ''
  if (!window.confirm(`确定删除邮件模板「${item.name}」吗？${note}`)) return
  try {
    await api.deleteTemplate(item.id)
    if (editing.value?.id === item.id) reset()
    notify('已删除')
    await load()
  } catch (err) {
    fail(err, '删除失败')
  }
}

watch(
  () => [form.mode, form.pattern, form.sample],
  () => (result.value = null),
)

onMounted(load)
</script>

<template>
  <header class="page-head">
    <h1>邮件模板</h1>
    <span class="muted">定义如何从邮件里提取验证码，关联到业务场景后生效</span>
  </header>

  <div class="manage">
    <section class="panel" aria-label="模板列表">
      <div class="table-wrap">
        <table class="table">
          <thead>
            <tr>
              <th>名称</th>
              <th>类型</th>
              <th>模板内容</th>
              <th class="num">关联场景</th>
              <th>创建时间</th>
              <th class="actions">操作</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="item in templates" :key="item.id" :class="{ active: editing?.id === item.id }">
              <td class="strong">{{ item.name }}</td>
              <td>
                <span class="tag">{{ item.mode === 'regex' ? '正则' : '占位符' }}</span>
              </td>
              <td class="clip"><code :title="item.pattern">{{ item.pattern }}</code></td>
              <td class="num">{{ item.scenario_count }}</td>
              <td class="time">{{ formatTime(item.created_at) }}</td>
              <td class="actions">
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
        <div v-if="!templates.length" class="empty">
          <Icon name="file" :size="28" />
          <p>还没有邮件模板</p>
          <p>未关联模板的场景使用内置通用规则（识别 4–8 位数字验证码）</p>
        </div>
      </div>
    </section>

    <form class="panel form" @submit.prevent="save">
      <div class="panel-head">
        <h2>{{ editing ? `编辑模板：${editing.name}` : '新建模板' }}</h2>
      </div>
      <div class="form-body">
        <div class="field">
          <label for="template-name">名称</label>
          <input id="template-name" v-model="form.name" required maxlength="40" placeholder="例如：GitHub 设备验证" />
        </div>
        <div class="field">
          <span class="label">类型</span>
          <div class="segmented" role="radiogroup" aria-label="模板类型">
            <label v-for="mode in modes" :key="mode.value" :class="{ active: form.mode === mode.value }">
              <input v-model="form.mode" type="radio" name="template-mode" :value="mode.value" />{{ mode.label }}
            </label>
          </div>
        </div>
        <div class="field">
          <label for="template-pattern">模板内容</label>
          <textarea
            id="template-pattern"
            v-model="form.pattern"
            required
            rows="3"
            spellcheck="false"
            :placeholder="form.mode === 'regex' ? 'Verification code:\\s*([A-Z0-9-]+)' : 'Verification code: {code}'"
          ></textarea>
          <p v-if="form.mode === 'placeholder'" class="hint">
            粘贴邮件里验证码所在的那句话，把验证码换成 <code>{code}</code>，内容不固定的部分换成
            <code>{*}</code>。不区分大小写，空白数量不敏感。
          </p>
          <p v-else class="hint">Python 正则，取第一个括号分组；没有分组时取整个匹配。不区分大小写。</p>
        </div>
        <div class="field">
          <label for="template-sample">示例邮件（用于测试）</label>
          <textarea
            id="template-sample"
            v-model="form.sample"
            rows="5"
            spellcheck="false"
            placeholder="粘贴一封真实邮件的正文"
          ></textarea>
        </div>
        <div v-if="result" class="result" :class="result.code ? 'hit' : 'miss'" role="status">
          <template v-if="result.code">提取结果：<code>{{ result.code }}</code></template>
          <template v-else>没有匹配到验证码</template>
        </div>
        <div class="form-actions">
          <button class="btn primary" type="submit" :disabled="saving">{{ saving ? '保存中…' : '保存' }}</button>
          <button class="btn" type="button" :disabled="testing || !form.pattern || !form.sample" @click="test">
            <Icon name="play" :size="14" />{{ testing ? '测试中…' : '测试' }}
          </button>
          <button v-if="editing" class="btn" type="button" @click="reset">取消</button>
        </div>
      </div>
    </form>
  </div>
</template>
