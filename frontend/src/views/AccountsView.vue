<script setup lang="ts">
import { onBeforeUnmount, onMounted, reactive, ref } from 'vue'
import { api, type Account, type AccountAuthorization } from '../api'
import Icon from '../Icon.vue'
import { copy, fail, formatTime, notify } from '../ui'

const accounts = ref<Account[]>([])
const text = ref('')
const editing = ref<Account | null>(null)
const form = reactive({ note: '', client_id: '', refresh_token: '' })
const saving = ref(false)
const syncing = reactive<Record<number, boolean>>({})
const authorization = ref<AccountAuthorization | null>(null)
const authorizing = ref(false)
let pollTimer: number | undefined

function stopAuthorization() {
  window.clearTimeout(pollTimer)
  authorization.value = null
}

async function pollAuthorization() {
  const pending = authorization.value
  if (!pending) return
  try {
    const result = await api.checkAuthorization(pending.id)
    if (result.status === 'done') {
      stopAuthorization()
      notify(result.added ? `已添加 ${result.address}` : `${result.address} 已存在，令牌已更新`)
      await load()
      return
    }
  } catch (err) {
    stopAuthorization()
    fail(err, '授权失败')
    return
  }
  if (authorization.value === pending) pollTimer = window.setTimeout(pollAuthorization, pending.interval * 1000)
}

async function authorize() {
  authorizing.value = true
  try {
    stopAuthorization()
    link.value = null
    authorization.value = await api.startAuthorization()
    pollTimer = window.setTimeout(pollAuthorization, authorization.value.interval * 1000)
  } catch (err) {
    fail(err, '发起授权失败')
  } finally {
    authorizing.value = false
  }
}

const link = ref<{ url: string; redirect_uri: string } | null>(null)
const callbackUrl = ref('')
const linking = ref(false)

async function openLink() {
  linking.value = true
  try {
    stopAuthorization()
    link.value = await api.authorizationLink()
    callbackUrl.value = ''
    window.open(link.value.url, '_blank', 'noopener')
  } catch (err) {
    fail(err, '获取登录链接失败')
  } finally {
    linking.value = false
  }
}

async function submitCallback() {
  if (!callbackUrl.value.trim()) return
  linking.value = true
  try {
    const result = await api.submitCallback(callbackUrl.value)
    link.value = null
    notify(result.added ? `已添加 ${result.address}` : `${result.address} 已存在，令牌已更新`)
    await load()
  } catch (err) {
    fail(err, '授权失败')
  } finally {
    linking.value = false
  }
}

async function load() {
  try {
    accounts.value = await api.accounts()
  } catch (err) {
    fail(err, '加载失败')
  }
}

function reset() {
  editing.value = null
  text.value = ''
  Object.assign(form, { note: '', client_id: '', refresh_token: '' })
}

function edit(item: Account) {
  editing.value = item
  Object.assign(form, { note: item.note, client_id: item.client_id, refresh_token: '' })
}

async function save() {
  saving.value = true
  try {
    if (editing.value) {
      await api.updateAccount(editing.value.id, { ...form })
      notify('已保存')
    } else {
      const { added, skipped } = await api.importAccounts(text.value)
      notify(`已导入 ${added.length} 个账号${skipped.length ? `，${skipped.length} 个已存在被跳过` : ''}`)
    }
    reset()
    await load()
  } catch (err) {
    fail(err, '保存失败')
  } finally {
    saving.value = false
  }
}

async function toggle(item: Account) {
  try {
    await api.updateAccount(item.id, { enabled: !item.enabled })
    notify(item.enabled ? `已停用 ${item.address}，不再读取它的邮件` : `已启用 ${item.address}`)
    await load()
  } catch (err) {
    fail(err, '操作失败')
  }
}

async function sync(item: Account) {
  syncing[item.id] = true
  try {
    const result = await api.syncAccount(item.id)
    notify(`${item.address}：读到 ${result.fetched} 封，新增 ${result.new} 封`)
  } catch (err) {
    fail(err, '同步失败')
  } finally {
    syncing[item.id] = false
    await load()
  }
}

async function remove(item: Account) {
  if (!window.confirm(`确定删除账号 ${item.address} 吗？只删除本系统里的记录和已读取的邮件，Outlook 邮箱本身不受影响。`)) return
  try {
    await api.deleteAccount(item.id)
    if (editing.value?.id === item.id) reset()
    notify('已删除')
    await load()
  } catch (err) {
    fail(err, '删除失败')
  }
}

onMounted(load)
onBeforeUnmount(stopAuthorization)
</script>

<template>
  <header class="page-head">
    <h1>邮箱账号</h1>
    <span class="muted">接入 Outlook / Hotmail 邮箱，由本系统登录读取，在“全部邮箱”里统一查看</span>
    <button class="btn push" type="button" :disabled="authorizing" @click="authorize">
      {{ authorizing ? '请稍候…' : '设备码授权' }}
    </button>
    <button class="btn primary" type="button" :disabled="linking" @click="openLink">
      <Icon name="plus" :size="14" />登录授权
    </button>
  </header>

  <form v-if="link" class="notice" @submit.prevent="submitCallback">
    <p>
      1. 已在新标签页打开微软登录页（没有打开的话<a :href="link.url" target="_blank" rel="noopener">点这里</a>），登录要接入的
      Outlook 并同意授权。页面上显示的应用名是 Thunderbird，这是正常的。
    </p>
    <p>
      2. 同意后浏览器会跳到 <code>{{ link.redirect_uri }}</code> 并显示“无法访问此网站”，这是正常的。把地址栏里的完整地址复制下来，粘贴到下面：
    </p>
    <p class="callback">
      <input
        v-model="callbackUrl"
        required
        spellcheck="false"
        autocomplete="off"
        :placeholder="`${link.redirect_uri}/?code=…&state=…`"
        aria-label="回调地址"
      />
      <button class="btn primary" type="submit" :disabled="linking">{{ linking ? '提交中…' : '完成授权' }}</button>
      <button class="btn" type="button" @click="link = null">取消</button>
    </p>
    <p class="muted">登录链接 10 分钟内有效，每个链接只能用一次。</p>
  </form>

  <div v-if="authorization" class="notice" role="status">
    <p>
      1. 打开 <a :href="authorization.verification_uri" target="_blank" rel="noopener">{{ authorization.verification_uri }}</a>
      ，输入代码
      <button class="snippet" type="button" title="点击复制" @click="copy(authorization.user_code)">
        <code>{{ authorization.user_code }}</code><Icon name="copy" />
      </button>
    </p>
    <p>2. 登录要接入的 Outlook 邮箱并同意授权。授权页面上显示的应用名是 Thunderbird，这是正常的。</p>
    <p>
      3. 回到这里等待，完成后账号会自动出现在列表里。代码 {{ Math.round(authorization.expires_in / 60) }} 分钟内有效。
      <button class="btn" type="button" @click="stopAuthorization">取消</button>
    </p>
  </div>

  <div class="manage">
    <section class="panel" aria-label="账号列表">
      <div class="table-wrap">
        <table class="table">
          <thead>
            <tr>
              <th>邮箱</th>
              <th>启用</th>
              <th>状态</th>
              <th class="num">邮件</th>
              <th>上次读取</th>
              <th>备注</th>
              <th class="actions">操作</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="item in accounts" :key="item.id" :class="{ active: editing?.id === item.id }">
              <td class="strong"><code>{{ item.address }}</code></td>
              <td>
                <button
                  class="switch"
                  type="button"
                  role="switch"
                  :aria-checked="item.enabled"
                  :aria-label="`${item.address} 启用开关`"
                  @click="toggle(item)"
                >
                  <span class="knob"></span>
                </button>
              </td>
              <td>
                <span v-if="item.status === 'ok'" class="tag ok">正常 · {{ item.channel === 'imap' ? 'IMAP' : 'Graph' }}</span>
                <span v-else-if="item.status === 'error'" class="tag error" :title="item.error">失败</span>
                <span v-else class="muted">未读取</span>
              </td>
              <td class="num">{{ item.mail_count }}</td>
              <td class="time">{{ formatTime(item.synced_at) }}</td>
              <td class="clip" :title="item.note">{{ item.note || '—' }}</td>
              <td class="actions">
                <button class="icon-btn" type="button" title="立即读取" :aria-label="`读取 ${item.address}`" @click="sync(item)">
                  <Icon name="refresh" :class="{ spin: syncing[item.id] }" />
                </button>
                <button class="icon-btn" type="button" title="编辑" :aria-label="`编辑 ${item.address}`" @click="edit(item)">
                  <Icon name="pencil" />
                </button>
                <button class="icon-btn danger" type="button" title="删除" :aria-label="`删除 ${item.address}`" @click="remove(item)">
                  <Icon name="trash" />
                </button>
              </td>
            </tr>
          </tbody>
        </table>
        <div v-if="!accounts.length" class="empty">
          <Icon name="at" :size="28" />
          <p>还没有接入邮箱账号</p>
          <p>点右上角“授权添加”，或在右侧粘贴已有的令牌导入</p>
        </div>
      </div>
      <ul v-if="accounts.some((item) => item.status === 'error')" class="check-log">
        <template v-for="item in accounts" :key="item.id">
          <li v-if="item.status === 'error'" class="error">
            <code>{{ item.address }}</code>
            <span>{{ item.error }}</span>
          </li>
        </template>
      </ul>
    </section>

    <form class="panel form" @submit.prevent="save">
      <div class="panel-head">
        <h2>{{ editing ? `编辑：${editing.address}` : '用已有令牌导入' }}</h2>
      </div>
      <div class="form-body">
        <template v-if="editing">
          <div class="field">
            <label for="account-note">备注</label>
            <input id="account-note" v-model="form.note" maxlength="80" placeholder="可选" />
          </div>
          <div class="field">
            <label for="account-client">Client ID</label>
            <input id="account-client" v-model="form.client_id" required spellcheck="false" />
          </div>
          <div class="field">
            <label for="account-token">Refresh Token</label>
            <input
              id="account-token"
              v-model="form.refresh_token"
              type="password"
              autocomplete="off"
              spellcheck="false"
              :placeholder="`留空则不修改（当前 ${editing.token_mask}）`"
            />
            <p class="hint">改过 Outlook 密码后原来的令牌会失效，需要重新授权并在这里换成新的。</p>
          </div>
        </template>
        <div v-else class="field">
          <label for="account-text">账号，每行一个</label>
          <textarea
            id="account-text"
            v-model="text"
            required
            rows="8"
            spellcheck="false"
            placeholder="邮箱----密码----client_id----refresh_token"
          ></textarea>
          <p class="hint">
            四段用 <code>----</code> 分隔。密码不会被使用和保存，可以留空；登录靠的是 client_id 和 refresh_token，两者必须来自同一次授权。
          </p>
          <p class="hint">
            导入后在“全部邮箱”里查看邮件；系统每隔几分钟读取一次，也可以点列表里的刷新立即读取。接入的账号还可以填作
            addy、Duck 来源的收集地址。
          </p>
        </div>
        <div class="form-actions">
          <button class="btn primary" type="submit" :disabled="saving">{{ saving ? '保存中…' : editing ? '保存' : '导入' }}</button>
          <button v-if="editing" class="btn" type="button" @click="reset">取消</button>
        </div>
      </div>
    </form>
  </div>
</template>
