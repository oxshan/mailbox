<script setup lang="ts">
import { onMounted, reactive, ref } from 'vue'
import { api, type Forward } from '../api'
import Icon from '../Icon.vue'
import { fail, formatTime, notify } from '../ui'

const forwards = ref<Forward[]>([])
const configured = ref(true)
const hourlyLimit = ref(0)
const form = reactive({ address: '', note: '' })
const editing = ref<Forward | null>(null)
const saving = ref(false)

async function load() {
  try {
    const result = await api.forwards()
    configured.value = result.configured
    hourlyLimit.value = result.hourly_limit
    forwards.value = result.forwards
  } catch (err) {
    fail(err, '加载失败')
  }
}

function reset() {
  editing.value = null
  Object.assign(form, { address: '', note: '' })
}

function edit(item: Forward) {
  editing.value = item
  Object.assign(form, { address: item.address, note: item.note })
}

async function save() {
  saving.value = true
  try {
    if (editing.value) await api.updateForward(editing.value.id, { note: form.note })
    else await api.createForward(form.address, form.note)
    notify(editing.value ? '已保存' : '已添加，之后收到的邮件会转发到这个邮箱')
    reset()
    await load()
  } catch (err) {
    fail(err, '保存失败')
  } finally {
    saving.value = false
  }
}

async function toggle(item: Forward) {
  try {
    await api.updateForward(item.id, { enabled: !item.enabled })
    notify(item.enabled ? `已停止转发到 ${item.address}` : `已开启转发到 ${item.address}`)
    await load()
  } catch (err) {
    fail(err, '操作失败')
  }
}

async function remove(item: Forward) {
  if (!window.confirm(`确定删除转发邮箱 ${item.address} 吗？`)) return
  try {
    await api.deleteForward(item.id)
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
    <h1>邮件转发</h1>
    <span class="muted">收到的每封邮件都会通过 Resend 转发到已开启的邮箱，转发结果记在“发送邮件”的发件记录里</span>
  </header>

  <p v-if="!configured" class="notice" role="status">
    转发尚未生效。请在启动参数或 <code>.env</code> 中设置 <code>RESEND_API_KEY</code>，并在 Resend
    里完成收信域名的验证，然后重启服务。
  </p>

  <div class="manage">
    <section class="panel" aria-label="转发邮箱列表">
      <div class="table-wrap">
        <table class="table">
          <thead>
            <tr>
              <th>转发邮箱</th>
              <th>状态</th>
              <th>备注</th>
              <th>添加时间</th>
              <th class="actions">操作</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="item in forwards" :key="item.id" :class="{ active: editing?.id === item.id }">
              <td class="strong"><code>{{ item.address }}</code></td>
              <td>
                <button
                  class="switch"
                  type="button"
                  role="switch"
                  :aria-checked="item.enabled"
                  :aria-label="`${item.address} 转发开关`"
                  @click="toggle(item)"
                >
                  <span class="knob"></span>
                </button>
                <span class="switch-label">{{ item.enabled ? '转发中' : '已关闭' }}</span>
              </td>
              <td class="clip" :title="item.note">{{ item.note || '—' }}</td>
              <td class="time">{{ formatTime(item.created_at) }}</td>
              <td class="actions">
                <button class="icon-btn" type="button" title="编辑备注" :aria-label="`编辑 ${item.address}`" @click="edit(item)">
                  <Icon name="pencil" />
                </button>
                <button class="icon-btn danger" type="button" title="删除" :aria-label="`删除 ${item.address}`" @click="remove(item)">
                  <Icon name="trash" />
                </button>
              </td>
            </tr>
          </tbody>
        </table>
        <div v-if="!forwards.length" class="empty">
          <Icon name="forward" :size="28" />
          <p>还没有转发邮箱</p>
          <p>在右侧添加一个，收到的邮件就会同时转发过去</p>
        </div>
      </div>
    </section>

    <form class="panel form" @submit.prevent="save">
      <div class="panel-head">
        <h2>{{ editing ? `编辑：${editing.address}` : '添加转发邮箱' }}</h2>
      </div>
      <div class="form-body">
        <div class="field">
          <label for="forward-address">邮箱地址</label>
          <input
            id="forward-address"
            v-model="form.address"
            type="email"
            required
            :disabled="Boolean(editing)"
            maxlength="254"
            spellcheck="false"
            placeholder="例如：me@gmail.com"
          />
        </div>
        <div class="field">
          <label for="forward-note">备注</label>
          <input id="forward-note" v-model="form.note" maxlength="80" placeholder="可选" />
        </div>
        <div class="field">
          <span class="label">转发说明</span>
          <p class="hint">
            转发出去的邮件以收信地址作为发件人，回复会发给原发件人；附件不会转发。不能填本系统自己域名下的邮箱。<template
              v-if="hourlyLimit > 0"
              >每小时最多转发 {{ hourlyLimit }} 封，超出的只收不转。</template
            >
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
