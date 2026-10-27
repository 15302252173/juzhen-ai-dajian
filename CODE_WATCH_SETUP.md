# 真实验证码监控配置（新 AI 照抄版）

本文件是“助手自己做好”的配置模板，不是手工说明。所有邮箱、域名、ID 都是占位符，换成你自己的再用。仓库里不含真实邮箱、真实域名、真实人名、密码、验证码、token。

## 目标

- 监控邮箱：`your-mailbox@outlook.com`（换成你自己的 Outlook 邮箱）
- 看板邮箱登记 ID：`5`（换成你自己看板 `/emails` 里那条的 ID）
- 看板地址：`https://approve.example.com`（换成你自己的看板域名）
- 频率：每 10 分钟一次，静默运行
- 结果：只更新看板上的验证码状态（已到达 / 等待中、最后检查时间、最近邮件发件人、主题、到达时间），验证码原文不显示、不存储、不写进任何文件。

## 1. 看板端（已在代码里）

- 数据库 `email_accounts` 有字段：`code_status`、`code_last_checked_at`、`code_last_email_at`、`code_last_email_from`、`code_last_email_subject`
- 接口：`POST /api/emails/<id>/code-status`，body 只接受上面 5 个状态/元数据字段，任何验证码字段都会被忽略，绝不入库。
- 网页：`/emails` 每条卡片显示验证码状态，`/ ` 首页显示验证码已到达数和分布。

## 2. 定时任务（助手自己建，不用用户本地连）

- 任务名：`outlook-code-watch`，间隔 `10m`，默认开启
- 每轮步骤：
  1. 读状态文件（例如 `hidden_files/outlook_code_watch_state.json`），记下上次验证码邮件的主题 + 发件人 + 到达时间。
  2. 用浏览器任务打开 `outlook.live.com` 收件箱。浏览器资料里必须已经登录了监控邮箱，不用重新登录，不发邮件，不点邮件里的链接，不标记已读。
  3. 只看最新收件箱列表，找验证码类邮件：主题或发件人含 `verification / verify / code / OTP / security code / 验证码 / 验证代码`。
  4. 如果最新验证码邮件和状态文件一致，或者没有新验证码邮件：什么都不更新，不调看板接口，不在聊天里发消息，静默结束。
  5. 如果是新的验证码邮件：
     - 读看板 API token 文件（本地 600 权限文件，不要回显 token，不要写进仓库）。
     - `POST https://approve.example.com/api/emails/5/code-status`，body 只含 `code_status=已到达`、`code_last_checked_at=当前时间`、`code_last_email_at`、`code_last_email_from`、`code_last_email_subject`。绝对不要带验证码数字。
     - 更新状态文件里的 `last_code_email_*` 和 `last_checked_at`。
     - 聊天里只报一句话：新验证码邮件已到（发件人、主题、到达时间），看板状态已更新，验证码原文不显示。
  6. 如果 Outlook 网页要求重新登录：不要编密码、不要代填，第一次失效报一次，之后保持静默，直到会话恢复。

## 3. 状态文件模板（不要把真实邮件地址写进仓库，模板里的值都是占位）

```json
{
  "auxiliary_email": "auxiliary@example.com",
  "code_status": "等待中",
  "email": "your-mailbox@outlook.com",
  "email_id": 5,
  "last_checked_at": "",
  "last_code_email_at": "",
  "last_code_email_from": "",
  "last_code_email_subject": "",
  "login_failure_reported": false
}
```

## 4. 验证方法（助手自己核验，不用用户看）

- 看板健康检查返回 `{"ok": true}`
- 看板接口 `GET /api/emails/stats` 里 `by_code_status` 的“已到达”数和“等待中”数之和等于邮箱总数
- 定时任务状态：enabled=true，上轮 succeeded，下一轮在 10 分钟后
- 状态文件里的 `last_checked_at` 和看板上该邮箱的“最后检查”一致
- 全程没有验证码数字出现在看板、状态文件、日志、聊天记录里

## 5. 新 AI 接手 checklist

- [ ] 看板已部署，`/ ` 首页和 `/emails` 能打开
- [ ] 监控邮箱已在浏览器资料里登录，`outlook.live.com` 能直接打开收件箱
- [ ] 定时任务已建，间隔 10 分钟，静默规则已写进任务正文
- [ ] 第一次检查后，看板状态从“等待中”变成“已到达”或保持“等待中”，且没有验证码原文
- [ ] 仓库里没有真实邮箱、真实域名、真实人名、密码、验证码、token
