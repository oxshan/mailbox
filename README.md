# Mailbox

轻量级的自建域名邮箱系统：把域名的邮件收到自己的服务器上，通过接口生成随机邮箱、查询验证码，并带一个管理看板。

- 后端：Python 3 单文件，只用标准库，数据存 SQLite
- 前端：Vue 3 + TypeScript，构建产物由后端直接托管
- 收信由自带的 SMTP 服务完成；发信可选，通过 [Resend](https://resend.com) 的接口转发

## 功能

- **无限邮箱**：已添加域名下的任意地址都能收信，不必预先创建
- **生成随机邮箱**：接口一次调用返回一个可用地址
- **查询验证码**：自动从邮件里提取验证码，支持长轮询等待
- **业务场景**：按场景生成邮箱，每个场景可以关联一组域名和一个邮件模板
- **邮件模板**：用占位符或正则定义验证码的提取方式，支持字母数字混合等任意格式，可在线测试
- **域名管理**：在看板里增删、启用、停用域名，立即生效，并可检测 MX 记录是否指向本机
- **发送邮件**：配置 Resend 后，可以用已添加域名下的任意地址作为发件人发信，并保留发件记录
- **自动清理**：收到和发出的邮件默认保留 24 小时

## 快速开始

### Docker（推荐）

```bash
cp .env.example .env    # 按需填写 MAIL_DOMAINS、API_KEY
./start.sh              # 构建镜像并启动，打印看板地址和 API Key
./stop.sh               # 停止
```

改了 `.env` 之后重新执行 `./start.sh` 即可。数据保存在数据卷 `mailbox-data` 中，停止和重建镜像都不会丢失；彻底清空数据用 `docker compose down -v`。

### 直接运行

需要 Python 3.8+。前端要先构建一次（需要 Node 和 yarn）：

```bash
cd frontend && yarn && yarn build && cd ..
cd backend
MAIL_DOMAINS=a.com,b.com API_KEY=你的密钥 python3 server.py
```

监听 25 端口需要 root 权限。本机试用可以加 `SMTP_PORT=2525`。

启动后访问 `http://服务器:8080`，输入 API Key 进入看板。

## DNS 配置

要收到外部邮件，每个域名需要一条 MX 记录指向运行本服务的服务器。

1. 在任意一个域名下加一条 A 记录，把主机名指向服务器公网 IP：

   | 类型 | 名称 | 内容 |
   |---|---|---|
   | A | `mail` | 服务器 IP |

2. 每个收信域名各加一条 MX 记录，都可以指向同一个主机名：

   | 类型 | 名称 | 内容 | 优先级 |
   |---|---|---|---|
   | MX | `@` | `mail.a.com` | 10 |

注意事项：

- 使用 Cloudflare 时，A 记录的代理状态必须是“仅 DNS”（灰色云朵），并且要关闭域名的 Email Routing，否则邮件会被 Cloudflare 收走。
- 服务器的安全组或防火墙要放行入站 TCP 25。
- 配好后可以在看板的“域名管理”里点“检测 MX”确认。

## 配置项

通过环境变量配置（Docker 方式写在 `.env` 里）。

| 变量 | 默认值 | 说明 |
|---|---|---|
| `MAIL_DOMAINS` | 空 | 收信域名，逗号分隔。只在首次启动、域名表为空时导入，之后以看板里的为准 |
| `API_KEY` | 自动生成 | 接口密钥。留空则首次启动随机生成，保存在数据目录的 `api_key` 文件里 |
| `SMTP_PORT` | `25` | 收信端口 |
| `HTTP_PORT` | `8080` | 接口和看板端口 |
| `SMTP_HOST` / `HTTP_HOST` | `0.0.0.0` | 监听地址 |
| `DATA_DIR` | `backend/data` | 数据库和密钥文件所在目录 |
| `STATIC_DIR` | `frontend/dist` | 看板静态文件目录 |
| `RETENTION_HOURS` | `24` | 邮件保留时长（小时） |
| `RESEND_API_KEY` | 空 | Resend 的 API Key。留空则不启用发信 |
| `PUBLIC_IP` | 自动探测 | 本服务器的公网 IP，用于 MX 检测。留空时启动后向外部服务查询一次 |
| `MAX_MAIL_BYTES` | `2097152` | 单封邮件大小上限，超过则拒收 |

Docker 方式下，`.env` 里的 `SMTP_PORT` 和 `HTTP_PORT` 指的是宿主机端口。

## 核心概念

```
域名 ──┐
       ├── 业务场景 ── 邮箱 ── 邮件
模板 ──┘
```

- **域名**：系统接受哪些域名的邮件。停用的域名不再用于生成新邮箱，但已有邮箱照常收信。
- **邮件模板**：定义如何从邮件里提取验证码。
- **业务场景**：关联一组域名和一个模板。生成邮箱时指定场景，邮箱的域名从该场景的域名里随机选，收到的邮件按该场景的模板解析。
  - 不关联域名：使用全部已启用的域名。
  - 不关联模板：使用内置通用规则（按关键词识别 4–8 位数字验证码）。
  - 系统自带一个“默认场景”（标识 `default`），不能删除。
- **邮箱**：直接向未生成过的地址发信也能收到，这类邮箱归入默认场景。

修改模板或场景的模板后，该场景下已收到的邮件会立即重新解析。

## 邮件模板写法

### 占位符

把邮件里验证码所在的那句话粘进来，验证码换成 `{code}`，内容不固定的部分换成 `{*}`。不区分大小写，空白数量不敏感。

| 邮件内容 | 模板 | 提取结果 |
|---|---|---|
| `Verification code: 739104` | `Verification code: {code}` | `739104` |
| `您的验证码：X9K2P，10 分钟内有效` | `验证码：{code}，{*}有效` | `X9K2P` |
| `Your invite code is AB12-CD34.` | `invite code is {code}.` | `AB12-CD34` |

`{code}` 匹配连续的字母、数字和连字符。

### 正则表达式

使用 Python 正则语法，取第一个括号分组；没有分组时取整个匹配。不区分大小写。

```
invite code[:：]\s*([A-Z0-9]{4}-[A-Z0-9]{4})
```

模板会依次在“主题 + 纯文本正文”和“HTML 正文转出的文本”里查找。关联了模板的场景只按模板解析，匹配不上时验证码为空。

## 接口文档

### 约定

- 所有接口以 `/api/` 开头，返回 JSON。
- 鉴权：请求头带 `X-API-Key: <密钥>` 或 `Authorization: Bearer <密钥>`。
- 参数可以放在查询串里，`POST`/`PUT` 也可以用 JSON 请求体。
- 时间字段都是 Unix 时间戳（秒，浮点数）。
- 出错时返回 `{"error": "原因"}`：

  | 状态码 | 含义 |
  |---|---|
  | 400 | 参数有误 |
  | 401 | API Key 缺失或错误 |
  | 404 | 接口或记录不存在 |

### 生成随机邮箱

`POST /api/mailbox`（也接受 `GET`）

| 参数 | 必填 | 说明 |
|---|---|---|
| `scenario` | 否 | 业务场景标识，默认 `default` |
| `domain` | 否 | 指定域名，必须是该场景可用的域名；不传则随机 |

```bash
curl -X POST -H "X-API-Key: $KEY" "http://127.0.0.1:8080/api/mailbox?scenario=github"
```

```json
{"address": "w5ht2qv1rp@a.com", "domain": "a.com", "scenario": "github", "created_at": 1795197731.47}
```

### 查询验证码

`GET /api/code`

| 参数 | 必填 | 说明 |
|---|---|---|
| `address` | 是 | 邮箱地址 |
| `wait` | 否 | 最多等待的秒数（上限 60）。期间一旦收到带验证码的邮件立即返回 |
| `since` | 否 | 只返回该时间戳之后收到的验证码，用于同一邮箱多次收码时避开旧的 |

```bash
curl -H "X-API-Key: $KEY" "http://127.0.0.1:8080/api/code?address=w5ht2qv1rp@a.com&wait=30"
```

```json
{
  "address": "w5ht2qv1rp@a.com",
  "code": "739104",
  "from": "GitHub <noreply@github.com>",
  "subject": "[GitHub] Please verify your device",
  "received_at": 1795197731.55
}
```

返回的是最近一封带验证码的邮件。没有时 `code` 及其余字段为 `null`。

### 调用示例（Python）

```python
import requests

BASE = "http://127.0.0.1:8080"
HEADERS = {"X-API-Key": "你的密钥"}

address = requests.post(f"{BASE}/api/mailbox", params={"scenario": "github"}, headers=HEADERS).json()["address"]

# ……用 address 去目标网站注册……

result = requests.get(f"{BASE}/api/code", params={"address": address, "wait": 60}, headers=HEADERS, timeout=70).json()
print(result["code"])
```

### 发送邮件

需要先配置 `RESEND_API_KEY`，并在 Resend 里验证发信域名（按它给出的清单添加 SPF、DKIM 等 DNS 记录）。发信走 HTTPS，不受云厂商封锁出站 25 端口的影响。

`POST /api/send`

| 字段 | 必填 | 说明 |
|---|---|---|
| `from` | 是 | 发件地址，域名必须是已添加的域名 |
| `from_name` | 否 | 发件人显示名称 |
| `to` | 是 | 收件地址，字符串（逗号分隔）或数组，最多 20 个 |
| `subject` | 是 | 主题 |
| `text` | `text` 和 `html` 至少一项 | 纯文本正文 |
| `html` | 同上 | HTML 正文 |

```bash
curl -X POST -H "X-API-Key: $KEY" -H "Content-Type: application/json" \
  -d '{"from":"hello@a.com","to":"someone@example.com","subject":"Hi","text":"Hello"}' \
  http://127.0.0.1:8080/api/send
```

成功返回 `{"id": 1, "provider_id": "Resend 返回的邮件 ID"}`。Resend 拒绝或连接失败时返回 `502` 和错误原因，这次尝试也会记入发件记录。

`GET /api/sent`（参数 `limit`，默认 50，上限 200）返回 `{"configured": 是否已配置发信, "mails": [...]}`。每条记录含 `id`、`from`、`to`、`subject`、`body`、`status`（`sent` 或 `failed`）、`error`、`created_at`。

`sent` 表示 Resend 已接受这封邮件，不代表已送达收件人。

### 邮箱与邮件

| 方法 | 路径 | 参数 | 返回 |
|---|---|---|---|
| GET | `/api/mailboxes` | `scenario`（可选，按场景标识筛选）、`limit`（默认 100，上限 500） | `{"mailboxes": [...]}` |
| GET | `/api/mails` | `address`（可选，不传返回全部）、`limit`（默认 50，上限 200） | `{"mails": [...]}` |
| GET | `/api/stats` | 无 | 统计数字 |

邮箱对象：`address`、`domain`、`scenario`（标识）、`scenario_name`、`created_at`、`mail_count`、`last_mail_at`。按最近活动时间倒序。

邮件对象：`id`、`address`、`from`、`subject`、`body`（纯文本正文）、`code`、`received_at`。按接收时间倒序。

统计：`domains`、`mailboxes`、`mails`、`mails_24h`、`codes`。

### 业务场景

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/scenarios` | 列表 |
| POST | `/api/scenarios` | 新建，返回 `{"id", "key"}` |
| PUT | `/api/scenarios/{id}` | 修改，并重新解析该场景下的邮件 |
| DELETE | `/api/scenarios/{id}` | 删除，其下邮箱移到默认场景 |

请求体字段：

| 字段 | 必填 | 说明 |
|---|---|---|
| `name` | 是 | 名称 |
| `key` | 否 | 标识，小写字母、数字、下划线、连字符，最长 32 位，全局唯一；留空自动生成 |
| `domain_ids` | 否 | 关联的域名 ID 数组；空数组表示使用全部已启用域名 |
| `template_id` | 否 | 关联的模板 ID；`null` 表示使用内置通用规则 |

列表项另含 `domains`（域名名称数组）、`template_name`、`mailbox_count`、`created_at`。

### 邮件模板

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/templates` | 列表 |
| POST | `/api/templates` | 新建，返回 `{"id"}` |
| PUT | `/api/templates/{id}` | 修改，并重新解析关联场景下的邮件 |
| DELETE | `/api/templates/{id}` | 删除，关联场景改用内置通用规则 |
| POST | `/api/templates/test` | 试运行，不保存 |

请求体字段：

| 字段 | 必填 | 说明 |
|---|---|---|
| `name` | 是 | 名称 |
| `mode` | 否 | `placeholder`（默认）或 `regex` |
| `pattern` | 是 | 模板内容 |
| `sample` | 否 | 示例邮件，仅用于在看板里测试 |

试运行传 `mode`、`pattern`、`sample`，返回 `{"code": "提取结果或 null"}`：

```bash
curl -X POST -H "X-API-Key: $KEY" -H "Content-Type: application/json" \
  -d '{"mode":"placeholder","pattern":"code is {code}","sample":"Your code is A7K9Q2"}' \
  http://127.0.0.1:8080/api/templates/test
```

列表项另含 `scenario_count`、`created_at`。

### 域名

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/domains` | 列表 |
| POST | `/api/domains` | 添加，请求体 `name`（必填）、`note`、`enabled`，返回 `{"id", "name"}` |
| PUT | `/api/domains/{id}` | 修改 `enabled` 或 `note`，域名本身不可改 |
| DELETE | `/api/domains/{id}` | 删除，之后发往该域名的邮件会被拒收 |
| POST | `/api/domains/check` | 检测 MX，请求体 `name` |

列表项：`id`、`name`、`enabled`、`note`、`mailbox_count`、`scenario_count`、`created_at`。

MX 检测返回 `status`、`message`、`mx`（记录列表，如 `"10 mail.a.com"`）。`status` 取值：

| 值 | 含义 |
|---|---|
| `ok` | MX 指向本服务器 |
| `missing` | 没有 MX 记录 |
| `mismatch` | MX 指向了另一台服务器 |
| `unverified` | 有 MX 记录，但不知道本机公网 IP，也连不上对方的 25 端口，无法确认 |
| `error` | DNS 查询失败，或 MX 指向的主机名解析不出 IP |

判定方式：MX 指向的主机解析出的 IP 等于本服务器的公网 IP 时直接判为 `ok`，不需要出站连接，所以云厂商封锁出站 25 端口不影响检测。IP 不一致时才会尝试连接对方的 25 端口核对身份。公网 IP 取自 `PUBLIC_IP`，未设置时自动探测。

## 开发

```bash
# 后端测试
cd backend && python3 -m unittest test_server

# 前端开发（/api 代理到 127.0.0.1:8080，需要后端已启动）
cd frontend && yarn && yarn dev

# 前端类型检查 + 构建
yarn build
```

目录结构：

```
backend/server.py        后端全部代码
backend/test_server.py   测试
frontend/src/            看板源码
Dockerfile               镜像定义
docker-compose.yml       编排
start.sh / stop.sh       启停脚本
```

## 限制

- 发信依赖 Resend，受其额度和使用政策约束；不支持附件。
- 不支持 STARTTLS，发件方以明文投递。
- 接口和看板是明文 HTTP，API Key 会在网络上明文传输；对公网开放时建议在前面加一层 HTTPS 反向代理。
- 单封邮件最多 20 个收件人；邮件只保存纯文本正文，不保存附件。
- 内置通用规则只识别 4–8 位数字验证码，其他格式请配置邮件模板。
