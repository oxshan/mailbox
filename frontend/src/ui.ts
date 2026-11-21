import { reactive } from 'vue'
import { clearKey, getKey, UnauthorizedError } from './api'

export const ui = reactive({
  authed: Boolean(getKey()),
  loginError: '',
  toast: '',
  toastKind: 'ok' as 'ok' | 'error',
})

let toastTimer: number | undefined

export function notify(message: string, kind: 'ok' | 'error' = 'ok') {
  ui.toast = message
  ui.toastKind = kind
  window.clearTimeout(toastTimer)
  toastTimer = window.setTimeout(() => (ui.toast = ''), kind === 'error' ? 4000 : 2000)
}

export function logout(message = '') {
  clearKey()
  ui.authed = false
  ui.loginError = message
}

export function fail(err: unknown, prefix: string) {
  if (err instanceof UnauthorizedError) logout('API Key 无效，请重新输入')
  else notify(`${prefix}：${(err as Error).message}`, 'error')
}

export async function copy(text: string, message = '已复制') {
  try {
    await navigator.clipboard.writeText(text)
  } catch {
    const area = document.createElement('textarea')
    area.value = text
    area.style.position = 'fixed'
    area.style.opacity = '0'
    document.body.appendChild(area)
    area.select()
    document.execCommand('copy')
    area.remove()
  }
  notify(message)
}

const pad = (value: number) => String(value).padStart(2, '0')

export function formatTime(seconds: number | null | undefined) {
  if (!seconds) return '—'
  const d = new Date(seconds * 1000)
  return (
    `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ` +
    `${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`
  )
}
