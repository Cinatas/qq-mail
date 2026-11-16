# QQ 邮箱排错对照表

按脚本报错的原文查表。脚本每条错误都带一行「建议」，先照建议做，再回来查这里。

## 认证类（退出码 3）

| 现象 | 原因 | 处理 |
|---|---|---|
| `IMAP 登录被拒绝: LOGIN failed` | 填的是 QQ 密码而不是授权码；或授权码被重置 | 网页版 QQ 邮箱 → 设置 → 账号与安全 → 重新生成 16 位授权码 → `accounts add` 覆盖 |
| 同上，且授权码确认正确 | 服务没开 | 同一页面确认「POP3/IMAP/SMTP 服务」是**开启**状态（QQ 默认关闭）；改完等 1～2 分钟 |
| 授权码粘贴后仍失败 | 复制时带了空格/换行/全角字符 | 重新输入，注意 16 位、纯 ASCII；用 `--password-file` 从只含一行的文件读更稳 |
| `SMTP 认证失败` 但 IMAP 正常 | 常见于生成授权码后又被重置，或只开了 IMAP 没开 SMTP 权限 | 重新生成授权码（一次授权码同时管 IMAP 和 SMTP），再 `accounts test` |
| `DPAPI 解密失败：凭据属于其他 Windows 用户` | 配置文件被拷到别的用户/机器 | 用当前用户重新 `accounts add`；或改用 `--plaintext`（不推荐） |

## 网络类（退出码 4）

| 现象 | 原因 | 处理 |
|---|---|---|
| `连接 imap.qq.com:993 失败: timed out` | 网络/代理拦截 993 | 确认能直连外网；公司网可能封 993，改用手机热点验证一次 |
| `SSLError: certificate verify failed` | 中间人代理（抓包工具）替换了证书 | 关掉代理的 HTTPS 解密，或让用户明确知情后自行处理，不要改脚本绕过校验 |
| `SMTP 发送失败: ... too many ...` | QQ 对第三方客户端有发信频率/每日上限 | 降低频率，隔一段时间再发；当天额度用尽只能等次日或用网页版 |
| `SMTP 发送失败: 550 ...` | 收件地址不存在 / 被对方拒收 / 触发了反垃圾 | 核对地址；换纯文本、去掉可疑链接与附件后再试 |

## 协议/数据类（退出码 4/5）

| 现象 | 原因 | 处理 |
|---|---|---|
| `搜索失败` 且关键字含中文 | QQ 对 `SEARCH TEXT/SUBJECT` 的中文支持不稳定 | 改用英文片段、`--since` 缩小范围，或先 `list` 少量再 `read` 逐封看 |
| `无法打开文件夹 X` | 文件夹名不对，或该文件夹是 `\Noselect` 容器 | 先 `folders` 看真实名字；中文名可读名直接用，脚本会做 modified UTF-7 编码 |
| 文件夹名显示成 `&XfJT0ZAB-` | 说明某个环节没走脚本的解码 | 用 `folders --json` 确认；若仍乱码，把该名字原样传给 `--folder` 也能用 |
| `邮件 N 没有返回内容` | uid 属于别的文件夹，或邮件刚被删除 | 用当前文件夹重新 `list` 取 uid；uid 只在单个文件夹内有效 |
| `邮件 N 没有附件` / `没有匹配的附件` | 该封无附件，或 `--name` 片段不匹配 | `read` 的输出里有附件清单，照着名字过滤 |
| 附件下载超时或文件为 0 字节 | 附件过大（>20MB）或网络抖动 | 让用户从网页版下载；不要重试多次 |
| `move` 后原文件夹里还看得到 | 2026-11 实测 QQ 支持 UIDPLUS，默认会 UID EXPUNGE 源邮件；若仍看得到，说明服务端变了或用了 `--keep-source` | 检查是否加了 `--keep-source`；确需清理加 `purge --uid` |
| `move` 报 `复制到 X 失败` | 目标文件夹名不对，或它是 `\Noselect` 容器 | `folders` 看真实可写文件夹；容器名（如「其他文件夹」）不能作为目标 |
| 中文主题/`--text` 搜索结果为 0 | 这两类走本地扫描，只覆盖最近 `--scan` 封 | 调大 `--scan`（如 `--scan 1000`），或用 `--since` 缩小后再搜 |

## 安全边界

- 脚本永不打印授权码；`accounts.json` 里只有 `dpapi:` 密文。不要把该文件内容贴进对话或提交到仓库。
- `--plaintext` 仅用于非 Windows 或特殊排查，落盘即明文，用完应立刻 `accounts remove` 重新添加为 DPAPI。
- 发信默认 dry-run：只有 `--confirm` 才会连 SMTP。看到 `"dry_run": true` 就说明**没有发出去**。

## 自查命令

```sh
python <skill-directory>/scripts/mail.py selftest        # 离线：UTF-7 / DPAPI / MIME / 日期解析
python <skill-directory>/scripts/mail.py accounts test   # 联网：IMAP + SMTP 认证
python <skill-directory>/scripts/mail.py folders         # 联网：文件夹与未读数
```
