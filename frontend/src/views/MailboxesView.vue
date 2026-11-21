<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { api, type Domain, type Mail, type Mailbox, type Scenario, type Stats } from '../api'
import Icon from '../Icon.vue'
import { copy, fail, formatTime } from '../ui'

const REFRESH_MS = 5000

const stats = ref<Stats | null>(null)
const scenarios = ref<Scenario[]>([])
const domains = ref<Domain[]>([])
const mailboxes = ref<Mailbox[]>([])
const mails = ref<Mail[]>([])
const selected = ref('')
const expanded = ref<number | null>(null)
const filter = ref('')
const scenario = ref('default')
const creating = ref(false)
const refreshing = ref(false)

let timer: number | undefined

const statCards = computed(() => [
  { icon: 'globe', label: '域名', value: stats.value?.domains },
  { icon: 'at', label: '邮箱', value: stats.value?.mailboxes },
  { icon: 'mail', label: '24 小时邮件', value: stats.value?.mails_24h },
  { icon: 'shield', label: '验证码', value: stats.value?.codes },
] as const)

const visibleBoxes = computed(() => {
  const keyword = filter.value.trim().toLowerCase()
  if (!keyword) return mailboxes.value
  return mailboxes.value.filter(
    (box) => box.address.includes(keyword) || (box.scenario_name ?? '').toLowerCase().includes(keyword),
  )
})

async function refresh() {
  refreshing.value = true
  try {
    const [nextStats, nextBoxes, nextMails] = await Promise.all([
      api.stats(),
      api.mailboxes(),
      api.mails(selected.value),
    ])
    stats.value = nextStats
    mailboxes.value = nextBoxes
    mails.value = nextMails
  } catch (err) {
    fail(err, '刷新失败')
  } finally {
    refreshing.value = false
  }
}

async function createMailbox() {
  creating.value = true
  try {
    const box = await api.createMailbox(scenario.value)
    filter.value = ''
    selected.value = box.address
    await copy(box.address, `已生成并复制 ${box.address}`)
  } catch (err) {
    fail(err, '生成失败')
  } finally {
    creating.value = false
  }
}

async function markRead(mail: Mail) {
  if (mail.read) return
  mail.read = true
  try {
    await api.markRead(mail.id)
  } catch (err) {
    mail.read = false
    fail(err, '标记已读失败')
  }
}

function toggle(mail: Mail) {
  expanded.value = expanded.value === mail.id ? null : mail.id
  markRead(mail)
}

function copyCode(mail: Mail) {
  if (!mail.code) return
  copy(mail.code, `已复制验证码 ${mail.code}`)
  markRead(mail)
}

watch(selected, () => {
  expanded.value = null
  refresh()
})

onMounted(async () => {
  try {
    ;[scenarios.value, domains.value] = await Promise.all([api.scenarios(), api.domains()])
  } catch (err) {
    fail(err, '加载失败')
  }
  await refresh()
  timer = window.setInterval(() => {
    if (!document.hidden) refresh()
  }, REFRESH_MS)
})
onBeforeUnmount(() => window.clearInterval(timer))
</script>

<template>
  <header class="page-head">
    <h1>全部邮箱</h1>
    <ul class="chips" aria-label="收信域名">
      <li v-for="domain in domains" :key="domain.id" class="chip" :class="{ off: !domain.enabled }">
        @{{ domain.name }}
      </li>
    </ul>
    <span class="live" title="每 5 秒自动刷新"><span class="dot" aria-hidden="true"></span>实时</span>
    <button class="icon-btn" type="button" title="立即刷新" aria-label="立即刷新" @click="refresh">
      <Icon name="refresh" :class="{ spin: refreshing }" />
    </button>
  </header>

  <section class="stats" aria-label="统计">
    <div v-for="card in statCards" :key="card.label" class="stat">
      <span class="stat-icon"><Icon :name="card.icon" /></span>
      <strong>{{ card.value ?? '—' }}</strong>
      <span class="muted">{{ card.label }}</span>
    </div>
  </section>

  <div class="workspace">
    <aside class="panel" aria-label="邮箱列表">
      <div class="create">
        <label class="sr-only" for="scenario">业务场景</label>
        <select id="scenario" v-model="scenario" title="按业务场景生成邮箱">
          <option v-for="item in scenarios" :key="item.id" :value="item.key">{{ item.name }}</option>
        </select>
        <button class="btn primary" type="button" :disabled="creating" @click="createMailbox">
          <Icon name="plus" />{{ creating ? '生成中' : '生成邮箱' }}
        </button>
      </div>
      <div class="search">
        <Icon name="search" />
        <label class="sr-only" for="filter">筛选邮箱</label>
        <input id="filter" v-model="filter" type="search" placeholder="按地址或场景筛选" />
      </div>
      <ul class="list">
        <li>
          <button class="box single" :class="{ active: selected === '' }" type="button" @click="selected = ''">
            <Icon name="inbox" />
            <span class="box-name">全部邮件</span>
            <span class="box-count">共 {{ stats?.mails ?? 0 }} 封</span>
          </button>
        </li>
        <li v-for="box in visibleBoxes" :key="box.address">
          <button
            class="box"
            :class="{ active: selected === box.address }"
            type="button"
            :title="box.address"
            @click="selected = box.address"
          >
            <span class="box-name mono">{{ box.address }}</span>
            <span class="box-count">共 {{ box.mail_count }} 封</span>
            <span class="box-meta">
              <span class="tag">{{ box.scenario_name ?? '默认场景' }}</span>
              <time>{{ formatTime(box.last_mail_at ?? box.created_at) }}</time>
            </span>
          </button>
        </li>
        <li v-if="!visibleBoxes.length" class="empty">
          {{ mailboxes.length ? '没有匹配的邮箱' : '还没有邮箱，点上方按钮生成' }}
        </li>
      </ul>
    </aside>

    <section class="panel" aria-label="邮件列表">
      <div class="panel-head">
        <h2 class="mono">{{ selected || '全部邮件' }}</h2>
        <button v-if="selected" class="icon-btn" type="button" title="复制地址" aria-label="复制地址" @click="copy(selected)">
          <Icon name="copy" />
        </button>
        <span class="count">{{ mails.length }} 封</span>
      </div>
      <ul v-if="mails.length" class="list">
        <li v-for="mail in mails" :key="mail.id" class="mail" :class="{ open: expanded === mail.id, read: mail.read }">
          <div class="mail-row">
            <button
              v-if="mail.code"
              class="code"
              type="button"
              :title="`点击复制 ${mail.code}`"
              :aria-label="`复制验证码 ${mail.code}`"
              @click="copyCode(mail)"
            >
              {{ mail.code }}
            </button>
            <span v-else class="code none">无</span>
            <button
              class="mail-main"
              type="button"
              :aria-expanded="expanded === mail.id"
              @click="toggle(mail)"
            >
              <span class="subject">
                <span v-if="!mail.read" class="fresh">新</span>{{ mail.subject || '(无主题)' }}
              </span>
              <span class="meta">
                {{ mail.from }}<template v-if="!selected"> → {{ mail.address }}</template>
              </span>
              <time class="time">{{ formatTime(mail.received_at) }}</time>
              <Icon name="chevron" class="chevron" />
            </button>
          </div>
          <pre v-if="expanded === mail.id" class="body">{{ mail.body || '(正文为空)' }}</pre>
        </li>
      </ul>
      <div v-else class="empty fill">
        <Icon name="inbox" :size="28" />
        <p>暂无邮件</p>
        <p v-if="selected">向 <span class="mono">{{ selected }}</span> 发信后会自动出现在这里</p>
      </div>
    </section>
  </div>
</template>
