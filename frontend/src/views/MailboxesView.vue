<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, reactive, ref, watch } from 'vue'
import { api, type Domain, type Mail, type Mailbox, type Scenario, type Source, type Stats } from '../api'
import Icon from '../Icon.vue'
import { copy, fail, formatTime, notify } from '../ui'

const REFRESH_MS = 5000

const stats = ref<Stats | null>(null)
const scenarios = ref<Scenario[]>([])
const domains = ref<Domain[]>([])
const sources = ref<Source[]>([])
const mailboxes = ref<Mailbox[]>([])
const mails = ref<Mail[]>([])
const selected = ref('')
const expanded = ref<number | null>(null)
const filter = ref('')
const scenario = ref('default')
const source = ref<number | null>(null)
const headersOf = ref<number | null>(null)
const plainOf = reactive(new Set<number>())
const imagesOf = reactive(new Set<number>())

const flip = (set: Set<number>, id: number) => (set.has(id) ? set.delete(id) : set.add(id))

// Mail HTML runs in a sandboxed frame without scripts. Remote images are blocked unless asked for, so
// opening a mail does not tell the sender it was read; links open in a new tab.
function htmlDocument(html: string, images: boolean) {
  const policy = `default-src 'none'; style-src 'unsafe-inline'; font-src data:; img-src data: cid:${images ? ' https: http:' : ''}`
  return (
    `<!doctype html><html><head><meta charset="utf-8">` +
    `<meta http-equiv="Content-Security-Policy" content="${policy}">` +
    `<base target="_blank"><style>body{margin:8px;font:14px/1.5 -apple-system,'Segoe UI','PingFang SC',sans-serif;` +
    `color:#0f172a;background:#fff;word-break:break-word}img{max-width:100%;height:auto}</style></head>` +
    `<body>${html}</body></html>`
  )
}
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
  return mailboxes.value.filter((box) =>
    [box.address, box.scenario_name ?? '', box.source_name ?? ''].some((text) => text.toLowerCase().includes(keyword)),
  )
})
const current = computed(() => mailboxes.value.find((box) => box.address === selected.value) ?? null)

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
    const box = await api.createMailbox(scenario.value, source.value)
    filter.value = ''
    selected.value = box.address
    await copy(box.address, `已生成并复制 ${box.address}`)
  } catch (err) {
    fail(err, '生成失败')
  } finally {
    creating.value = false
  }
}

async function setActive(box: Mailbox) {
  try {
    const { synced } = await api.setMailboxActive(box.address, !box.active)
    const where = synced ? `，已同步到 ${box.source_name}` : box.source === null ? '' : '（仅本系统，来源不支持远端停用）'
    notify(box.active ? `已停用，不再接收它的邮件${where}` : `已启用${where}`)
    await refresh()
  } catch (err) {
    fail(err, '操作失败')
  }
}

async function remove(box: Mailbox) {
  const note = {
    domain: '之后再有邮件发到这个地址，它会重新出现；想拒收请改用停用。',
    addy: '同时会在 addy.io 删除这个别名。',
    outlook: '',
    duck: 'Duck 不支持删除，这个地址在 Duck 仍然有效，再来信会重新出现；想拒收请改用停用。',
  }[box.source_kind]
  if (!window.confirm(`确定删除邮箱 ${box.address} 吗？${note}`)) return
  try {
    await api.deleteMailbox(box.address)
    selected.value = ''
    notify('已删除')
    await refresh()
  } catch (err) {
    fail(err, '删除失败')
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
  headersOf.value = null
  markRead(mail)
}

function copyCode(mail: Mail) {
  if (!mail.verify_code) return
  copy(mail.verify_code, `已复制验证码 ${mail.verify_code}`)
  markRead(mail)
}

watch(selected, () => {
  expanded.value = null
  refresh()
})

onMounted(async () => {
  try {
    ;[scenarios.value, domains.value, sources.value] = await Promise.all([api.scenarios(), api.domains(), api.sources()])
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
        <label class="sr-only" for="source">来源</label>
        <select id="source" v-model="source" title="邮箱来源">
          <option :value="null">按场景</option>
          <option v-for="item in sources" :key="item.id" :value="item.id" :disabled="!item.enabled">{{ item.name }}</option>
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
              <span>
                <span class="tag">{{ box.scenario_name ?? '默认场景' }}</span>
                <span v-if="box.source_name" class="tag primary">{{ box.source_name }}</span>
                <span v-if="box.account !== null" class="tag primary">Outlook</span>
                <span v-if="!box.active" class="tag error">已停用</span>
              </span>
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
        <template v-if="current">
          <button
            class="icon-btn"
            type="button"
            :title="current.active ? '停用：不再接收它的邮件' : '启用'"
            :aria-label="current.active ? '停用邮箱' : '启用邮箱'"
            @click="setActive(current)"
          >
            <Icon name="power" />
          </button>
          <button
            v-if="current.account === null"
            class="icon-btn danger"
            type="button"
            title="删除邮箱"
            aria-label="删除邮箱"
            @click="remove(current)"
          >
            <Icon name="trash" />
          </button>
        </template>
        <span class="count">{{ mails.length }} 封</span>
      </div>
      <ul v-if="mails.length" class="list">
        <li v-for="mail in mails" :key="mail.id" class="mail" :class="{ open: expanded === mail.id, read: mail.read }">
          <div class="mail-row">
            <button
              v-if="mail.verify_code"
              class="code"
              type="button"
              :title="`点击复制 ${mail.verify_code}`"
              :aria-label="`复制验证码 ${mail.verify_code}`"
              @click="copyCode(mail)"
            >
              {{ mail.verify_code }}
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
          <template v-if="expanded === mail.id">
            <iframe
              v-if="mail.html && !plainOf.has(mail.id)"
              class="html-body"
              title="邮件正文"
              sandbox="allow-popups allow-popups-to-escape-sandbox"
              referrerpolicy="no-referrer"
              :srcdoc="htmlDocument(mail.html, imagesOf.has(mail.id))"
            ></iframe>
            <pre v-else class="body">{{ mail.body || '(正文为空)' }}</pre>
            <div class="body-actions">
              <template v-if="mail.html">
                <button class="btn" type="button" @click="flip(plainOf, mail.id)">
                  {{ plainOf.has(mail.id) ? '显示 HTML' : '显示纯文本' }}
                </button>
                <button v-if="!plainOf.has(mail.id)" class="btn" type="button" @click="flip(imagesOf, mail.id)">
                  {{ imagesOf.has(mail.id) ? '隐藏远程图片' : '显示远程图片' }}
                </button>
              </template>
              <button class="btn" type="button" @click="headersOf = headersOf === mail.id ? null : mail.id">
                {{ headersOf === mail.id ? '收起邮件头' : '查看邮件头' }}
              </button>
            </div>
            <pre v-if="headersOf === mail.id" class="body">{{ mail.headers || '(没有记录邮件头)' }}</pre>
          </template>
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
