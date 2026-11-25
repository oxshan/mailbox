<script setup lang="ts">
import { onMounted, reactive, ref, watch } from 'vue'
import { api, type Domain, type SentMail } from '../api'
import Icon from '../Icon.vue'
import Pager from '../Pager.vue'
import { fail, formatTime, notify } from '../ui'

const PAGE_SIZE = 50

const domains = ref<Domain[]>([])
const mails = ref<SentMail[]>([])
const configured = ref(true)
const sending = ref(false)
const expanded = ref<number | null>(null)
const page = ref(1)
const total = ref(0)
const form = reactive({ local: '', domain: '', from_name: '', to: '', subject: '', text: '' })

async function loadSent() {
  try {
    const result = await api.sent(PAGE_SIZE, (page.value - 1) * PAGE_SIZE)
    configured.value = result.configured
    mails.value = result.mails
    total.value = result.total
  } catch (err) {
    fail(err, '加载失败')
  }
}

async function send() {
  sending.value = true
  try {
    await api.send({
      from: `${form.local.trim()}@${form.domain}`,
      from_name: form.from_name,
      to: form.to,
      subject: form.subject,
      text: form.text,
    })
    notify('已发送')
    Object.assign(form, { to: '', subject: '', text: '' })
  } catch (err) {
    fail(err, '发送失败')
  } finally {
    sending.value = false
    if (page.value === 1) await loadSent()
    else page.value = 1
  }
}

watch(page, () => {
  expanded.value = null
  loadSent()
})

onMounted(async () => {
  try {
    domains.value = await api.domains()
    form.domain = domains.value[0]?.name ?? ''
  } catch (err) {
    fail(err, '加载域名失败')
  }
  await loadSent()
})
</script>

<template>
  <header class="page-head">
    <h1>发送邮件</h1>
    <span class="muted">通过 Resend 发信，发件地址可以是已添加域名下的任意邮箱</span>
  </header>

  <p v-if="!configured" class="notice" role="status">
    发信功能尚未配置。请在启动参数或 <code>.env</code> 中设置 <code>RESEND_API_KEY</code>，并在 Resend
    里完成发信域名的验证，然后重启服务。
  </p>

  <div class="manage">
    <section class="panel" aria-label="发件记录">
      <div class="table-wrap">
        <table class="table">
          <thead>
            <tr>
              <th>时间</th>
              <th>发件人</th>
              <th>收件人</th>
              <th>主题</th>
              <th>状态</th>
            </tr>
          </thead>
          <tbody>
            <template v-for="mail in mails" :key="mail.id">
              <tr
                class="clickable"
                tabindex="0"
                :aria-expanded="expanded === mail.id"
                @click="expanded = expanded === mail.id ? null : mail.id"
                @keydown.enter="expanded = expanded === mail.id ? null : mail.id"
              >
                <td class="time">{{ formatTime(mail.created_at) }}</td>
                <td><code>{{ mail.from }}</code></td>
                <td class="clip" :title="mail.to"><code>{{ mail.to }}</code></td>
                <td class="clip strong" :title="mail.subject">
                  <span v-if="mail.kind === 'forward'" class="tag primary">转发</span> {{ mail.subject }}
                </td>
                <td>
                  <span class="tag" :class="mail.status === 'sent' ? 'ok' : 'error'">
                    {{ mail.status === 'sent' ? '已发送' : '失败' }}
                  </span>
                </td>
              </tr>
              <tr v-if="expanded === mail.id" class="detail">
                <td colspan="5">
                  <p v-if="mail.error" class="error">{{ mail.error }}</p>
                  <pre class="body">{{ mail.body }}</pre>
                </td>
              </tr>
            </template>
          </tbody>
        </table>
        <div v-if="!mails.length" class="empty">
          <Icon name="send" :size="28" />
          <p>还没有发件记录</p>
        </div>
      </div>
      <Pager v-model:page="page" :size="PAGE_SIZE" :total="total" />
    </section>

    <form class="panel form" @submit.prevent="send">
      <div class="panel-head">
        <h2>写邮件</h2>
      </div>
      <div class="form-body">
        <div class="field">
          <label for="send-local">发件人</label>
          <div class="addr">
            <input id="send-local" v-model="form.local" required maxlength="64" spellcheck="false" placeholder="名字" />
            <span aria-hidden="true">@</span>
            <label class="sr-only" for="send-domain">发件域名</label>
            <select id="send-domain" v-model="form.domain" required>
              <option v-for="domain in domains" :key="domain.id" :value="domain.name">{{ domain.name }}</option>
            </select>
          </div>
        </div>
        <div class="field">
          <label for="send-name">发件人名称</label>
          <input id="send-name" v-model="form.from_name" maxlength="60" placeholder="可选，显示在收件方的发件人一栏" />
        </div>
        <div class="field">
          <label for="send-to">收件人</label>
          <input id="send-to" v-model="form.to" required spellcheck="false" placeholder="多个地址用逗号分隔，最多 20 个" />
        </div>
        <div class="field">
          <label for="send-subject">主题</label>
          <input id="send-subject" v-model="form.subject" required maxlength="200" />
        </div>
        <div class="field">
          <label for="send-text">正文</label>
          <textarea id="send-text" v-model="form.text" class="prose" required rows="9"></textarea>
        </div>
        <div class="form-actions">
          <button class="btn primary" type="submit" :disabled="sending || !configured || !domains.length">
            <Icon name="send" :size="14" />{{ sending ? '发送中…' : '发送' }}
          </button>
        </div>
      </div>
    </form>
  </div>
</template>
