export interface Mailbox {
  address: string
  domain: string
  scenario: string | null
  scenario_name: string | null
  created_at: number
  mail_count: number
  last_mail_at: number | null
}

export interface Mail {
  id: number
  address: string
  from: string
  subject: string
  body: string
  verify_code: string | null
  received_at: number
  read: boolean
}

export interface Stats {
  domains: number
  mailboxes: number
  mails: number
  mails_24h: number
  codes: number
}

export type TemplateMode = 'placeholder' | 'regex'

export interface TemplateInput {
  name: string
  mode: TemplateMode
  pattern: string
  sample: string
}

export interface Template extends TemplateInput {
  id: number
  scenario_count: number
  created_at: number
}

export interface ScenarioInput {
  name: string
  key: string
  domain_ids: number[]
  template_id: number | null
}

export interface Scenario extends ScenarioInput {
  id: number
  domains: string[]
  template_name: string | null
  mailbox_count: number
  created_at: number
}

export interface Domain {
  id: number
  name: string
  enabled: boolean
  note: string
  mailbox_count: number
  scenario_count: number
  created_at: number
}

export interface DomainCheck {
  status: 'ok' | 'missing' | 'unverified' | 'mismatch' | 'error'
  message: string
  mx: string[]
}

export interface SendInput {
  from: string
  from_name: string
  to: string
  subject: string
  text: string
}

export interface SentMail {
  id: number
  from: string
  to: string
  subject: string
  body: string
  status: 'sent' | 'failed'
  error: string
  created_at: number
}

export class UnauthorizedError extends Error {}

const KEY_STORAGE = 'mailbox_api_key'

export const getKey = () => localStorage.getItem(KEY_STORAGE) ?? ''
export const setKey = (key: string) => localStorage.setItem(KEY_STORAGE, key)
export const clearKey = () => localStorage.removeItem(KEY_STORAGE)

interface RequestOptions {
  method?: string
  params?: Record<string, string>
  body?: unknown
}

async function request<T>(path: string, { method = 'GET', params = {}, body }: RequestOptions = {}): Promise<T> {
  const query = new URLSearchParams(params).toString()
  const response = await fetch(`/api/${path}${query ? `?${query}` : ''}`, {
    method,
    headers: { 'X-API-Key': getKey(), ...(body === undefined ? {} : { 'Content-Type': 'application/json' }) },
    body: body === undefined ? undefined : JSON.stringify(body),
  })
  if (response.status === 401) throw new UnauthorizedError('API Key 无效')
  const reply: { code: number; msg: string; data: T } = await response.json()
  if (reply.code !== 0) throw new Error(reply.msg || `请求失败 (${response.status})`)
  return reply.data
}

export const api = {
  domains: () => request<{ domains: Domain[] }>('domains').then((r) => r.domains),
  createDomain: (name: string, note: string) =>
    request<{ id: number }>('domains', { method: 'POST', body: { name, note } }),
  updateDomain: (id: number, changes: { enabled?: boolean; note?: string }) =>
    request<{ id: number }>(`domains/${id}`, { method: 'PUT', body: changes }),
  deleteDomain: (id: number) => request<{ id: number }>(`domains/${id}`, { method: 'DELETE' }),
  checkDomain: (name: string) => request<DomainCheck>('domains/check', { method: 'POST', body: { name } }),
  stats: () => request<Stats>('stats'),
  mailboxes: () => request<{ mailboxes: Mailbox[] }>('mailboxes').then((r) => r.mailboxes),
  mails: (address: string) =>
    request<{ mails: Mail[] }>('mails', { params: address ? { address } : {} }).then((r) => r.mails),
  markRead: (id: number) => request<{ id: number }>(`read/${id}`, { method: 'POST' }),
  createMailbox: (scenario: string) =>
    request<{ address: string; domain: string; scenario: string }>('mailbox', { method: 'POST', params: { scenario } }),

  sent: () => request<{ configured: boolean; mails: SentMail[] }>('sent'),
  send: (input: SendInput) => request<{ id: number }>('send', { method: 'POST', body: input }),

  scenarios: () => request<{ scenarios: Scenario[] }>('scenarios').then((r) => r.scenarios),
  saveScenario: (input: ScenarioInput, id?: number) =>
    request<{ id: number }>(id ? `scenarios/${id}` : 'scenarios', { method: id ? 'PUT' : 'POST', body: input }),
  deleteScenario: (id: number) => request<{ id: number }>(`scenarios/${id}`, { method: 'DELETE' }),

  templates: () => request<{ templates: Template[] }>('templates').then((r) => r.templates),
  saveTemplate: (input: TemplateInput, id?: number) =>
    request<{ id: number }>(id ? `templates/${id}` : 'templates', { method: id ? 'PUT' : 'POST', body: input }),
  deleteTemplate: (id: number) => request<{ id: number }>(`templates/${id}`, { method: 'DELETE' }),
  testTemplate: (input: Pick<TemplateInput, 'mode' | 'pattern' | 'sample'>) =>
    request<{ verify_code: string | null }>('templates/test', { method: 'POST', body: input }).then(
      (r) => r.verify_code,
    ),
}
