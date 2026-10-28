# 授权模型（唯一模型，其他 AI 直接照做，不要再发明第二步）

1. 用户在网站上点“批准” = 直接授权发送。批准时系统自动标记待发送，不需要用户再点“立即发送”，不需要在聊天里再确认，不需要手动在聊天里发邮件。
2. 发邮件只走定时监控任务（每 5 分钟跑一次），不走聊天手动发。聊天手动发会弹 Muse 系统卡，正是用户拒绝的。
3. Muse 系统卡无法由看板、md 文件或助手关掉，也无法在设置里给发邮件设常驻 Allow。这是运行时的安全层，不是看板的问题。
4. 唯一的一次性开通：第一次定时任务发邮件时，Muse 会弹一次系统卡，卡上是定时任务名（例如 Dashboard Send Now Watch），不是聊天手动发。用户点“发送”旁边的下拉箭头，选“始终允许”，之后同一个定时任务发邮件就不再弹卡。只点“发送”不管用，下次还会弹；必须选“始终允许”才是一劳永逸。
5. 之后流程就是：网站点批准 -> 5 分钟内定时任务发出 -> 看板变已发送 -> 日报汇总。中间没有任何聊天确认。
6. 超出标准口径的承诺类（固定费率、免费样品/机器、保底露出、报价、非标合作）即使批了也不自动发，跳过等用户在网站上单独处理。平台已公布的标准口径不算承诺，超出标准的才算承诺，需单独批。


---

# 审批看板 v3 · 部署说明

## 这是什么
通用内容审批看板，不只给 通用业务 正常审批用。分类、分组、类型都是自由文本，其他 AI 不必做同一工作，也能用同一套审批方式：上传包 → 人在网页上批 → 只执行已批准且未执行的条目 → 执行完标记已执行。只做内容审批，不替代任何 AI 自身的发送/执行审批，其他系统必须自己读同一份批准记录，并遵守 API.md 里的规则。

## 文件
- `app.py`：唯一服务代码，Python 标准库 only，无外部依赖，无 CDN。
- `deploy.sh`：首次部署用（创建数据库、写 systemd 服务、启动）。注意：只在首次运行设置管理员初始密码和 API 令牌；已存在数据库时不要用它覆盖。
- `nginx-approve.conf.example`：宝塔反代参考（仅给 approve 子域名加反代，绝不动主站）。
- `test_pack.json`：旧测试包，日期 2099-01-01，4 条假数据，切勿发送。
- `example_pack_generic.json`：通用示例包，日期 2099-01-01，含正常审批、正常审批、合作邀请、内容发布 4 类，切勿发送/发布。
- `API.md`：给其他 AI 用的接口与使用方法（看板里 /api-docs 页也有同样内容）。

## 首次部署（宝塔 VPS，已有主站的情况下）
1. 宝塔新建网站，只建 `approve.example.com`，根目录 `/www/wwwroot/approve.example.com`，不要动现有 `example.com` 站。
2. 在新站根目录下建 `approval-dashboard` 文件夹，把 `app.py`、`deploy.sh` 传进去。
3. 在宝塔终端（或计划任务，只跑一次）里执行：
   ```
   cd /www/wwwroot/approve.example.com/approval-dashboard
   INITIAL_ADMIN_PASSWORD='你的初始密码' APPROVAL_API_TOKEN='你的词组式令牌' bash deploy.sh
   ```
   部署脚本会：首次创建 `data.db`、写 `/etc/systemd/system/approval-dashboard.service`、启动服务（监听 127.0.0.1:8765）。
4. 宝塔网站设置里给新站加反代（参考 nginx 示例），只反代到 127.0.0.1:8765。
5. 宝塔 SSL 里给新站申请 Let's Encrypt，勾选 approve.example.com，开启强制 HTTPS。
6. 打开 https://approve.example.com/login，用 admin + 初始密码登录，首次登录会强制改密码。改完后初始密码失效。
7. API 测试：用测试包 POST /api/packs，日期 2099-01-01，核对 4 条状态、需点名批、含承诺旗标，再在网页上测试批量批门禁（需点名批/承诺未勾确认会被跳过）。

## 更新到 v2（已有 v1 在跑，不要丢数据库）
1. 只替换 `app.py`，不要删除、不要覆盖 `data.db`、`data.db-wal`、`data.db-shm`。
2. 重启服务：`systemctl restart approval-dashboard`，然后 curl https://approve.example.com/api/health 应返回 {"ok": true}。
3. 管理员密码和 API 令牌都不变（v2 不重置已有用户和令牌哈希）。
4. 主站不要动：不要改 example.com 的 Nginx、SSL、目录、PHP。

## 升级后核对清单
- https://approve.example.com/login 能打开，样式正常
- /packs 包列表、/stats 统计、/api-docs 接口文档、/audit 审计日志都能打开
- 旧包（包括 2099 测试包）还在，状态不变
- 主站 https://example.com/ 仍正常


## 邮箱登记（v3.6，简单模型）
- 网页：/emails，导航里叫“邮箱登记”。只填服务商、邮箱地址、用途，点“登记邮箱”即可；删除只删登记记录，不删邮箱账号本身。
- 绝不填密码、验证码、恢复码到看板里。密码只由账号持有人自己保管。
- API：GET /api/emails 列出，POST /api/emails 登记（需 Bearer token，字段 provider/email/purpose/status）。
- 其他 AI 照做：注册好一个邮箱后，立刻用网页或 API 把地址登记进来，不要把密码写进任何文件、聊天或看板。
