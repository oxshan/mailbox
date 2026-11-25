<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import { api, clearKey, setKey, UnauthorizedError } from './api'
import Icon from './Icon.vue'
import { logout, ui } from './ui'
import DomainsView from './views/DomainsView.vue'
import ForwardsView from './views/ForwardsView.vue'
import MailboxesView from './views/MailboxesView.vue'
import ScenariosView from './views/ScenariosView.vue'
import SendView from './views/SendView.vue'
import SourcesView from './views/SourcesView.vue'
import AccountsView from './views/AccountsView.vue'
import TemplatesView from './views/TemplatesView.vue'

const pages = [
  { key: 'mailboxes', label: '全部邮箱', icon: 'inbox', view: MailboxesView },
  { key: 'sources', label: '邮箱来源', icon: 'link', view: SourcesView },
  { key: 'accounts', label: '邮箱账号', icon: 'at', view: AccountsView },
  { key: 'send', label: '发送邮件', icon: 'send', view: SendView },
  { key: 'forwards', label: '邮件转发', icon: 'forward', view: ForwardsView },
  { key: 'scenarios', label: '业务场景', icon: 'layers', view: ScenariosView },
  { key: 'templates', label: '邮件模板', icon: 'file', view: TemplatesView },
  { key: 'domains', label: '域名管理', icon: 'globe', view: DomainsView },
] as const

const route = ref(location.hash)
const page = computed(() => pages.find((item) => route.value === `#/${item.key}`) ?? pages[0])
const syncRoute = () => (route.value = location.hash)

const keyInput = ref('')
const loggingIn = ref(false)

async function login() {
  const key = keyInput.value.trim()
  if (!key) return
  loggingIn.value = true
  ui.loginError = ''
  setKey(key)
  try {
    await api.domains()
    ui.authed = true
    keyInput.value = ''
  } catch (err) {
    clearKey()
    ui.loginError = err instanceof UnauthorizedError ? 'API Key 无效' : `连接失败：${(err as Error).message}`
  } finally {
    loggingIn.value = false
  }
}

onMounted(() => window.addEventListener('hashchange', syncRoute))
onBeforeUnmount(() => window.removeEventListener('hashchange', syncRoute))
</script>

<template>
  <main v-if="!ui.authed" class="login">
    <form class="login-card" @submit.prevent="login">
      <div class="brand">
        <span class="logo"><Icon name="mail" :size="18" /></span>
        <strong>Mailbox</strong>
      </div>
      <p class="muted">输入 API Key 进入看板</p>
      <label for="api-key">API Key</label>
      <input id="api-key" v-model="keyInput" type="password" autocomplete="current-password" required />
      <p v-if="ui.loginError" class="error" role="alert">{{ ui.loginError }}</p>
      <button class="btn primary" type="submit" :disabled="loggingIn">{{ loggingIn ? '验证中…' : '进入看板' }}</button>
    </form>
  </main>

  <div v-else class="shell">
    <nav class="nav" aria-label="功能栏">
      <div class="brand">
        <span class="logo"><Icon name="mail" :size="16" /></span>
        <strong>Mailbox</strong>
      </div>
      <ul class="nav-list">
        <li v-for="item in pages" :key="item.key">
          <a
            class="nav-item"
            :class="{ active: item.key === page.key }"
            :href="`#/${item.key}`"
            :aria-current="item.key === page.key ? 'page' : undefined"
          >
            <Icon :name="item.icon" /><span>{{ item.label }}</span>
          </a>
        </li>
      </ul>
      <button class="nav-item nav-foot" type="button" @click="logout()"><Icon name="logout" /><span>退出</span></button>
    </nav>

    <main class="main">
      <component :is="page.view" />
    </main>

    <div class="toast" :class="[{ show: ui.toast }, ui.toastKind]" role="status" aria-live="polite">{{ ui.toast }}</div>
  </div>
</template>
