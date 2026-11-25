# Mailbox

轻量级的邮箱管理系统：把域名的邮件收到自己的服务器上，通过接口生成邮箱、查询验证码，并带一个管理后台。除了自有域名，还可以接入 addy.io 和 Duck 的别名邮箱统一管理。

- 后端：Python 3，只用标准库，数据存 SQLite
- 前端：Vue 3 + TypeScript，构建产物由后端直接托管
- 收信由自带的 SMTP 服务完成；发信可选，通过 [Resend](https://resend.com) 的接口转发

## 功能

- **无限邮箱**：已添加域名下的任意地址都能收信，不必预先创建
- **生成邮箱**：接口一次调用返回一个可用地址，可以来自自有域名，也可以来自外部来源
- **外部来源**：接入 addy.io 和 Duck（DuckDuckGo Email Protection），用它们的别名作为邮箱，在同一个后台里生成、停用、删除
- **邮箱账号**：接入 Outlook / Hotmail 邮箱，由本系统登录读取，和其他邮箱在同一个列表里查看；也可以作为 addy、Duck 的收集地址
- **停用和删除邮箱**：停用后不再接收该邮箱的邮件；addy 的别名会同步到 addy 那边
- **查询验证码**：自动从邮件里提取验证码，支持长轮询等待
- **业务场景**：按场景生成邮箱，每个场景可以关联一组域名、一组外部来源和一个邮件模板
- **邮件模板**：用占位符或正则定义验证码的提取方式，支持字母数字混合等任意格式，可在线测试
- **域名管理**：在看板里增删、启用、停用域名，立即生效，并可检测 MX 记录是否指向本机
- **发送邮件**：配置 Resend 后，可以用已添加域名下的任意地址作为发件人发信，并保留发件记录
- **邮件转发**：在看板里添加转发邮箱并逐个开关，收到的邮件会同时通过 Resend 转发过去
- **自动清理**：收到和发出的邮件默认保留 24 小时，`RETENTION_HOURS=0` 则永久保留；外部来源的邮箱和停用的邮箱本身不会被清理

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
| `RETENTION_HOURS` | `24` | 邮件保留时长（小时）；`0` 表示不自动清理，永久保留 |
| `RESEND_API_KEY` | 空 | Resend 的 API Key。留空则不启用发信 |
| `FORWARD_HOURLY_LIMIT` | `60` | 每小时最多转发的邮件数，超出的只收不转；`0` 表示不限制 |
| `MS_CLIENT_ID` | Thunderbird 的公开编号 | 授权添加 Outlook 账号时使用的微软应用编号。换成自己注册的应用时，要在应用里打开“允许公共客户端流” |
| `MS_REDIRECT_URI` | `http://localhost:8080` | 登录授权后微软把浏览器跳转到的地址，不需要真的能访问。换成自己的应用时要和应用里登记的重定向地址一致 |
| `ACCOUNT_SYNC_MINUTES` | `5` | 每隔多少分钟读取一次接入的邮箱账号；`0` 表示不定时读取 |
| `PUBLIC_IP` | 自动探测 | 本服务器的公网 IP，用于 MX 检测。留空时启动后向外部服务查询一次 |
| `MAX_MAIL_BYTES` | `2097152` | 单封邮件大小上限，超过则拒收 |

Docker 方式下，`.env` 里的 `SMTP_PORT` 和 `HTTP_PORT` 指的是宿主机端口。

## 核心概念

```
域名 ──────┐
外部来源 ──┼── 业务场景 ── 邮箱 ── 邮件
模板 ──────┘
```

- **域名**：系统接受哪些域名的邮件。停用的域名不再用于生成新邮箱，但已有邮箱照常收信。
- **外部来源**：一个 addy.io 或 Duck 账户。它的别名收到的邮件转发到一个“收集地址”（自有域名下的一个地址），所以仍然由本系统的 SMTP 收下。详见下一节。
- **邮件模板**：定义如何从邮件里提取验证码。
- **业务场景**：关联一组域名、一组外部来源和一个模板。生成邮箱时指定场景，收到的邮件按该场景的模板解析。
  - 不关联域名：使用全部已启用的域名。
  - 不关联外部来源：只用自有域名，除非生成时用 `source` 指定某个来源。
  - 关联了外部来源：只能用关联的来源；生成时不指定来源，就在这些来源和该场景关联的域名里随机选一个。
  - 不关联模板：使用内置通用规则（按关键词识别 4–8 位数字验证码）。
  - 系统自带一个“默认场景”（标识 `default`），不能删除。
- **邮箱**：有三种来历，都归在同一张列表里。
  - 通过接口或后台生成的。
  - 自有域名下没生成过的地址收到了邮件：自动出现，归入默认场景。
  - 外部来源里不是本系统生成的别名收到了邮件：同样自动出现，归入默认场景。

修改模板或场景的模板后，该场景下已收到的邮件会立即重新解析。

## 外部来源（addy.io 与 Duck）

```
目标网站 → 别名（xxx@duck.com 等）→ 来源转发 → 收集地址（自有域名）→ 本系统
```

本系统收到发往收集地址的邮件后，从邮件头里找出它原本发给哪个别名，把邮件记在那个别名名下。之后查验证码、看邮件都用别名地址，和自有域名的邮箱用法一样。找不出别名的邮件（比如来源发来的确认邮件）记在收集地址自己名下。

每个来源要有自己单独的收集地址，本系统靠它区分邮件属于哪个来源。

| | addy.io | Duck |
|---|---|---|
| 接口 | 官方公开 API | 官方客户端使用的内部接口，没有文档，随时可能变化 |
| 生成 | 支持 | 支持 |
| 停用、启用 | 同步到 addy | 只在本系统生效：停用后本系统丢弃它的邮件，地址在 Duck 仍然有效 |
| 删除 | 同步到 addy | 只删本系统的记录；再来信会重新出现，想拒收请用停用 |
| 令牌 | Settings → API 里创建的 API Key | 浏览器登录 duckduckgo.com/email 后，从生成地址的请求头里复制 `Authorization: Bearer` 后面的内容 |
| 检测 | 校验令牌，并确认收集地址是验证过的收件人 | 没有不生成地址就能校验的接口，令牌是否有效要到生成邮箱时才知道 |

接入步骤：

1. 在“邮箱来源”页面添加来源，填令牌和收集地址（例如 `collect-addy@你的域名`）。
2. 让来源把邮件转发到收集地址：
   - **addy.io**：在来源列表点“检测”，系统会把收集地址加为 addy 的收件人。addy 发来的验证邮件会出现在收集地址的收件箱里，点开其中的链接完成验证，再检测一次确认。
   - **Duck**：在 Duck 的 Email Protection 设置里把转发邮箱改成收集地址，并按它的确认邮件操作。一个 Duck 账户只有一个转发邮箱。
3. 生成邮箱时指定这个来源，或把它关联到业务场景。

注意：

- 来源的令牌明文保存在数据库里，接口和后台只显示首尾各 4 位。
- 两个服务都有额度和频率限制（addy 免费版每小时新建 10 个别名、每月 10 MB 转发流量；Duck 的令牌用得太频繁会被暂时封禁）。超限时生成接口返回 `424` 和对方给出的原因，不会改用别的来源。
- addy.io 的使用条款不允许用别名在其他网站批量注册账号。
- 别名多一跳转发，邮件到达比自有域名慢，查验证码时 `wait` 要相应放宽。

## 邮箱账号（Outlook）

和上面两类不同，Outlook 邮箱的邮件不会送到本系统，而是由本系统登录到邮箱里去读。它只用于统一查看，不参与“生成邮箱”，也不需要关联业务场景。

- **登录授权**：在“邮箱账号”页点“登录授权”，会打开微软登录页；登录 Outlook 并同意后，浏览器跳到 `MS_REDIRECT_URI`（默认 `http://localhost:8080`）并显示无法访问，这是正常的。把地址栏里的完整地址粘贴回页面、点“完成授权”，账号就会添加。登录链接 10 分钟内有效，只能用一次。
- **设备码授权**：点“设备码授权”，页面给出一个网址和一个代码；在任意设备的浏览器里打开网址、输入代码、登录并同意，账号会自动出现，不需要粘贴地址。
- 两种授权用的应用编号由 `MS_CLIENT_ID` 决定，默认是 Thunderbird 邮件客户端的公开编号，所以授权页面上显示的应用名是 Thunderbird。对已接入的账号再授权一次会更新它的令牌。
- **用已有令牌导入**：在同一页粘贴账号，每行一个，格式 `邮箱----密码----client_id----refresh_token`。密码不会被使用和保存；登录靠 `client_id` 和 `refresh_token`，两者必须来自同一次授权。
- **读取**：读收件箱和垃圾箱各最近 20 封。优先用 Microsoft Graph，令牌没有 Graph 权限时改用 IMAP。读取时机有三个：每 `ACCOUNT_SYNC_MINUTES` 分钟一次；在后台打开这个邮箱时；查询它的验证码时（两次读取至少间隔 10 秒）。
- **令牌**：每次读取微软可能换发新的 `refresh_token`，系统会自动保存。定时读取同时避免了令牌因长期不用而过期。改过邮箱密码后令牌失效，需要重新授权并在编辑里换成新的。
- **邮件保留**：账号的邮件不受 `RETENTION_HOURS` 影响，不会被自动清理。
- **作为收集地址**：addy 或 Duck 来源的收集地址可以填一个已接入的账号。这样 addy 的收件人、Duck 的转发邮箱都不用改：别名的邮件照旧转发到这个 Outlook，本系统读到后再记到别名名下。此时这个账号不需要是 addy 的收件人：检测不会去添加它，新别名用 addy 的默认收件人，只要那个收件人的邮件最终进到这个账号即可（例如默认收件人就是它，或者默认收件人再自动转发到它）。

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
- 所有响应都是同一种结构：

  ```json
  {"code": 0, "msg": "ok", "data": { ... }}
  ```

  | 字段 | 说明 |
  |---|---|
  | `code` | `0` 表示成功；失败时等于 HTTP 状态码 |
  | `msg` | 成功时为 `ok`，失败时为错误原因 |
  | `data` | 成功时为返回内容，失败时为 `null` |

  失败时 HTTP 状态码同时反映错误类型：

  | 状态码 | 含义 |
  |---|---|
  | 400 | 参数有误 |
  | 401 | API Key 缺失或错误 |
  | 404 | 接口或记录不存在 |
  | 424 | 外部服务（Resend、addy.io、Duck）拒绝或连接失败 |

  ```json
  {"code": 400, "msg": "业务场景不存在：abc", "data": null}
  ```

下文各接口的“返回”描述的都是 `data` 里的内容。

### 生成邮箱

`POST /api/mailboxes`

| 参数 | 必填 | 说明 |
|---|---|---|
| `scenario` | 否 | 业务场景标识，默认 `default` |
| `source` | 否 | 外部来源的 ID。不传时按场景决定，见“核心概念” |
| `domain` | 否 | 指定自有域名，必须是该场景可用的域名。不能和 `source` 同时传 |

```bash
curl -X POST -H "X-API-Key: $KEY" -H "Content-Type: application/json" \
  -d '{"scenario":"github"}' http://127.0.0.1:8080/api/mailboxes
```

```json
{
  "code": 0,
  "msg": "ok",
  "data": {
    "address": "w5ht2qv1rp@a.com",
    "domain": "a.com",
    "scenario": "github",
    "scenario_name": "GitHub 注册",
    "source": null,
    "source_kind": "domain",
    "source_name": null,
    "active": true,
    "created_at": 1795197731.47,
    "mail_count": 0,
    "last_mail_at": null
  }
}
```

`source_kind` 是 `domain`（自有域名）、`addy` 或 `duck`；外部来源的邮箱另有 `source`（来源 ID）和 `source_name`。

### 查询验证码

`GET /api/mailboxes/{address}/code`

| 参数 | 必填 | 说明 |
|---|---|---|
| `wait` | 否 | 最多等待的秒数（上限 60）。期间一旦收到带验证码的邮件立即返回 |
| `since` | 否 | 只返回该时间戳之后收到的验证码，用于同一邮箱多次收码时避开旧的 |

```bash
curl -H "X-API-Key: $KEY" "http://127.0.0.1:8080/api/mailboxes/w5ht2qv1rp@a.com/code?wait=30"
```

```json
{
  "code": 0,
  "msg": "ok",
  "data": {
    "address": "w5ht2qv1rp@a.com",
    "verify_code": "739104",
    "from": "GitHub <noreply@github.com>",
    "subject": "[GitHub] Please verify your device",
    "received_at": 1795197731.55
  }
}
```

返回的是最近一封带验证码的邮件。没有时外层 `code` 仍为 `0`，`verify_code` 及其余字段为 `null`；地址从没出现过也是如此，不会返回 404。

### 调用示例（Python）

```python
import requests

BASE = "http://127.0.0.1:8080"
HEADERS = {"X-API-Key": "你的密钥"}

box = requests.post(f"{BASE}/api/mailboxes", json={"scenario": "github"}, headers=HEADERS).json()
if box["code"] != 0:
    raise RuntimeError(box["msg"])
address = box["data"]["address"]

# ……用 address 去目标网站注册……

result = requests.get(f"{BASE}/api/mailboxes/{address}/code", params={"wait": 60}, headers=HEADERS, timeout=70).json()
print(result["data"]["verify_code"])
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

成功返回 `{"id": 1, "provider_id": "Resend 返回的邮件 ID"}`。Resend 拒绝或连接失败时返回 `424` 和错误原因，这次尝试也会记入发件记录。

`GET /api/sent`（参数 `limit`，默认 50，上限 200；`offset`，默认 0）返回 `{"configured": 是否已配置发信, "mails": [...], "total": 记录总数}`。每条记录含 `id`、`from`、`to`、`subject`、`body`、`status`（`sent` 或 `failed`）、`error`、`kind`（`send` 手动发送，`forward` 自动转发）、`created_at`。

`sent` 表示 Resend 已接受这封邮件，不代表已送达收件人。

### 邮件转发

配置 `RESEND_API_KEY` 后，每收到一封邮件，都会给所有已开启的转发邮箱发一份。转发在入库之后进行，失败不影响收信。

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/forwards` | 返回 `{"configured": 是否已配置发信, "hourly_limit": 每小时上限, "forwards": [...]}` |
| POST | `/api/forwards` | 添加，请求体 `address`（必填）、`note`、`enabled`，返回 `{"id", "address"}`；最多 20 个 |
| PUT | `/api/forwards/{id}` | 修改 `enabled` 或 `note`，地址本身不可改 |
| DELETE | `/api/forwards/{id}` | 删除 |

列表项：`id`、`address`、`enabled`、`note`、`created_at`。

- 转发出去的是一封新邮件：发件地址是收到这封信的邮箱（它的域名需要在 Resend 里验证过），显示名为“原发件人 via Mailbox”，回复地址是原发件人。
- 正文带纯文本和 HTML；附件不转发，只在正文末尾注明数量。
- 转发邮箱不能是本系统自己域名下的地址；来自某个转发邮箱的邮件不会再转回给它。
- 每次转发的结果都记入发件记录（`kind` 为 `forward`）。一小时内达到 `FORWARD_HOURLY_LIMIT` 后记一条失败记录，之后的邮件不再转发，也不再逐封记录。

### 邮箱与邮件

| 方法 | 路径 | 参数 | 返回 |
|---|---|---|---|
| GET | `/api/mailboxes` | `scenario`（场景标识）、`source`（来源 ID，或 `domain` 表示自有域名）、`active`（`true` / `false`）、`q`（地址包含的文字，不分大小写）、`limit`（默认 100，上限 500）、`offset`（默认 0），都可选 | `{"mailboxes": [...], "total": 符合条件的总数}` |
| GET | `/api/mailboxes/{address}` | 无 | 一个邮箱对象 |
| PUT | `/api/mailboxes/{address}` | `active`（必填，`true` 启用，`false` 停用） | `{"address", "active", "synced"}` |
| DELETE | `/api/mailboxes/{address}` | 无 | `{"address", "synced"}` |
| GET | `/api/mailboxes/{address}/mails` | `limit`（默认 50，上限 200）、`offset`（默认 0） | `{"mails": [...], "total": 邮件总数}` |
| GET | `/api/mails` | `address`（可选，不传返回全部）、`limit`、`offset` | `{"mails": [...], "total": 邮件总数}` |
| POST | `/api/mails/{id}/read` | 无 | 把一封邮件标为已读，返回 `{"id", "read": true}` |
| GET | `/api/stats` | 无 | 统计数字 |

邮箱对象：`address`、`domain`、`scenario`（标识）、`scenario_name`、`source`、`source_kind`、`source_name`、`active`、`created_at`、`mail_count`、`last_mail_at`。按最近活动时间倒序。

停用和删除：

- `synced` 表示这次操作是否同步到了外部来源。addy 的邮箱先改 addy 那边，成功后才改本系统；addy 那边失败时返回 `424`，本系统的状态不变。
- 停用的邮箱不再接收邮件：自有域名的地址在 SMTP 阶段直接拒收，外部别名转发过来的邮件被丢弃。
- 删除自有域名或 Duck 的邮箱只是删掉本系统的记录，之后再有邮件发到这个地址，它会重新出现。

邮件对象：`id`、`address`、`from`、`subject`、`body`（纯文本正文）、`headers`（原始邮件头，最多 8000 字符）、`verify_code`、`received_at`、`read`（是否已读）。按接收时间倒序。查询验证码不会改变已读状态。

统计：`domains`、`sources`、`accounts`、`mailboxes`、`mails`、`mails_24h`、`codes`。

### 外部来源

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/sources` | 列表 |
| POST | `/api/sources` | 添加，返回 `{"id"}` |
| PUT | `/api/sources/{id}` | 修改，只传要改的字段；`token` 留空表示不改 |
| DELETE | `/api/sources/{id}` | 删除；其下还有邮箱时拒绝 |
| POST | `/api/sources/{id}/check` | 检测，返回 `{"status", "message"}` |

请求体字段：

| 字段 | 必填 | 说明 |
|---|---|---|
| `kind` | 是（仅添加时） | `addy` 或 `duck`，添加后不可改 |
| `name` | 是 | 名称 |
| `token` | 是 | 令牌 |
| `collector` | 是 | 收集地址：已添加域名下的邮箱，或一个已接入的邮箱账号。每个来源各用一个 |
| `alias_domain` | 否 | 仅 addy：别名用的域名；留空用 addy 账户的默认域名 |
| `alias_format` | 否 | 仅 addy：`random_characters`、`uuid`、`random_words`、`random_male_name`、`random_female_name`、`random_noun`；留空用 addy 账户的默认格式 |
| `enabled` | 否 | 停用后不能再用它生成邮箱，已有邮箱照常收信 |
| `note` | 否 | 备注 |

列表项不含 `token`，另有 `token_mask`（首尾各 4 位）、`capabilities`（`create`、`toggle`、`delete` 中支持的几项）、`mailbox_count`、`scenario_count`、`created_at`。

检测的 `status`：`ok` 可用；`pending` 令牌有效但收集地址还没在 addy 验证；`unknown` 无法校验（Duck）。令牌无效或连接失败时接口返回 `424`。对 addy 检测时，如果收集地址还不是它的收件人，会顺带把它加上。

### 邮箱账号

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/accounts` | 列表 |
| POST | `/api/accounts` | 添加。单个：`address`、`client_id`、`refresh_token`、`note`；批量：`text`，每行 `邮箱----密码----client_id----refresh_token`。返回 `{"added": [...], "skipped": [...]}`，已存在的账号被跳过 |
| PUT | `/api/accounts/{id}` | 修改 `enabled`、`note`、`client_id`、`refresh_token`；后两项留空表示不改 |
| DELETE | `/api/accounts/{id}` | 删除账号和已读取的邮件；被某个来源用作收集地址时拒绝 |
| POST | `/api/accounts/callback` | 登录授权。不带参数：返回 `{"url", "redirect_uri", "expires_in"}`，`url` 是微软登录链接；带 `callback_url`（登录后浏览器地址栏里的完整地址）：换取令牌并添加账号，返回同下面的 `done` 结果。地址有误或链接过期返回 `400`，微软拒绝返回 `424` |
| POST | `/api/accounts/authorize` | 授权添加。不带参数：发起一次授权，返回 `{"id", "user_code", "verification_uri", "expires_in", "interval"}`；带 `id`：查询结果，返回 `{"status": "pending"}`，或 `{"status": "done", "address", "added"}`（`added` 为 `false` 表示账号已存在、令牌已更新）。被拒绝或超时返回 `424` |
| POST | `/api/accounts/{id}/sync` | 立即读取，返回 `{"fetched", "new", "channel"}`；失败返回 `424` |

列表项不含 `refresh_token`，另有 `token_mask`、`status`（空表示还没读取过，`ok`，`error`）、`error`、`channel`（`graph` 或 `imap`）、`synced_at`、`mail_count`、`source_count`。

账号在邮箱列表里的 `source_kind` 是 `outlook`，`account` 是账号 ID；`GET /api/mailboxes?source=account` 只列出账号。账号的邮箱不能通过 `/api/mailboxes` 删除。

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
| `source_ids` | 否 | 关联的外部来源 ID 数组；空数组表示只用自有域名 |
| `template_id` | 否 | 关联的模板 ID；`null` 表示使用内置通用规则 |

列表项另含 `domains`（域名名称数组）、`sources`（来源名称数组）、`template_name`、`mailbox_count`、`created_at`。

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

试运行传 `mode`、`pattern`、`sample`，返回 `{"verify_code": "提取结果或 null"}`：

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
| DELETE | `/api/domains/{id}` | 删除，之后发往该域名的邮件会被拒收；有来源的收集地址在这个域名下时拒绝 |
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
backend/server.py        入口：启动 SMTP 和 HTTP，收信后的入库与转发
backend/api.py           HTTP 接口
backend/store.py         SQLite 存储
backend/sources.py       外部来源（addy.io、Duck）的接口封装
backend/accounts.py      邮箱账号（Outlook）的登录和读取
backend/smtp.py          SMTP 收信
backend/extract.py       邮件解析和验证码提取
backend/mxcheck.py       MX 检测
backend/outbound.py      通过 Resend 发信
backend/config.py        配置项
backend/test_server.py   测试
frontend/src/            看板源码
Dockerfile               镜像定义
docker-compose.yml       编排
start.sh / stop.sh       启停脚本
```

## 从旧版本升级

这一版的数据库结构和接口都不兼容旧版本：

- 启动时发现旧结构的数据库，会把它改名为 `mail.db.v0-<时间>` 留在数据目录里，然后新建一个空库。旧数据不会被转换，也不会被删除。域名按 `MAIL_DOMAINS` 重新导入，其余配置要在后台重新添加。
- 接口路径变了：`POST /api/mailbox` 改为 `POST /api/mailboxes`，`GET /api/code?address=` 改为 `GET /api/mailboxes/{address}/code`，`POST /api/read/{id}` 改为 `POST /api/mails/{id}/read`。旧路径不再保留。

## 限制

- 别名还原靠邮件头：本系统在 `To`、`Cc`、`Delivered-To` 等收件人头和 `X-` 头里找别名地址。如果某个来源转发时不保留原始别名，邮件会记在收集地址名下；可以在后台展开邮件点“查看邮件头”确认。
- 只有确实由来源服务转发的邮件才会记到别名名下，防止有人直接给收集地址发信、在 `To` 里冒充别名。判断方式：
  - 通过 SMTP 收到的：投递服务器的 IP 反查出的主机名（并能正向解析回同一 IP）要属于 `anonaddy.me`、`anonaddy.com`、`addy.io`（addy）或 `duck.com`、`duckduckgo.com`（Duck），或来源里填的别名域名。
  - 从 Outlook 账号读到的：微软加在最上面的 `Authentication-Results` 里，SPF 或 DKIM 要对上述域名之一判为通过。
  - 不满足的邮件照常收下，但记在收集地址自己名下，容器日志里会打印一行说明。如果真实转发的邮件也落在这里，说明对方换了发信服务器，请把邮件头发给维护者调整。
  - 依赖能看到发信方的真实 IP：用 Docker 部署时 IPv4 没有问题；如果 MX 主机有 IPv6 地址，经过 docker-proxy 的 IPv6 连接会丢失来源 IP，这类邮件会一律留在收集地址名下。
- 接口在校验 API Key 之后才读取请求体，请求体最大 4 MB，超过返回 `413`。
- Outlook 账号的 `refresh_token` 和来源的令牌一样明文保存在数据库里；每个账号每次只读收件箱和垃圾箱最近 20 封，不读更早的邮件，也不支持从账号发信。
- Duck 的接口不是公开接口，可能随时失效；它也不支持远端停用、删除和列出地址。
- 发信和转发依赖 Resend，受其额度和使用政策约束；不支持附件。
- 不支持 STARTTLS，发件方以明文投递。
- 接口和看板是明文 HTTP，API Key 会在网络上明文传输；对公网开放时建议在前面加一层 HTTPS 反向代理。
- 单封邮件最多 20 个收件人；邮件保存纯文本和 HTML 正文（HTML 超过 512 KB 的部分会截掉），不保存附件。
- 内置通用规则只识别 4–8 位数字验证码，其他格式请配置邮件模板。
