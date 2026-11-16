---
name: qq-mail
description: 通过 IMAP/SMTP 管理 QQ 邮箱——查看未读、按发件人/主题/时间搜索、读信、下载附件、标记已读、归档或删除、发信/回复/转发。当用户说"看看我邮箱有没有新邮件""有没有某某发来的邮件""这封邮件讲什么""把附件保存下来""回复这封""发一封邮件给…"，或提到 QQ 邮箱、收件箱、未读邮件、发邮件、附件、归档时使用。动手处理任何 QQ 邮件之前先加载本 skill。
---

# QQ 邮箱管理

按用户要求收发和整理 QQ 邮箱里的邮件：读、搜、标、归档、发信。所有网络操作走 `scripts/mail.py`（IMAP + SMTP），不要临时手写 imaplib/smtplib 代码。

## 环境与路径

- 解释器：`load_workspace_dependencies` 返回的 Python（DSH 自带，含全部标准库）。**不要** `pip install`，本 skill 零第三方依赖。
- 脚本位置：`<skill-directory>/scripts/mail.py`（`<skill-directory>` = 本 SKILL.md 所在目录）。
- 凭据：`~/.dsh/qq-mail/accounts.json`（Windows：`%USERPROFILE%\.dsh\qq-mail\accounts.json`），授权码字段为 Windows DPAPI 加密（绑定当前 Windows 用户）。可用 `DSH_QQ_MAIL_HOME` 改目录。**不要**读取、打印或复制该文件里的 `secret`。
- 附件下载、`.eml` 等产物一律写入任务工作区（当前会话目录），不要写进 skill 目录。
- 已知服务端参数：IMAP `imap.qq.com:993`（SSL），SMTP `smtp.qq.com:465`（SSL，587 走 STARTTLS）。已内置为默认值。

## 首次配置（必须由用户本人输入授权码）

用户第一次让你管邮件时，先跑 `accounts list` 看有没有账号。没有就**让用户自己在终端执行**下面这条，你在旁边等着，不要让用户把授权码贴进对话：

```text
python <skill-directory>/scripts/mail.py accounts add --email <完整QQ邮箱> --display-name <显示名>
```

（`getpass` 隐藏输入；也可 `--password-file` 读完即删。）用户跑完你再用 `accounts test` 验证 IMAP + SMTP 是否都通。

授权码获取路径告诉用户：QQ 邮箱网页版 → 设置 → 账号与安全 → 开启 POP3/IMAP/SMTP 服务 → 短信验证 → 得到 16 位授权码。**这不是 QQ 密码。**

## 工作流程

1. **确认有账号**：`accounts list`；需要时 `accounts test`。
2. **收**：`folders` 看各文件夹未读数 → `list`（或 `search`）拿到 uid 列表。
3. **读**：`read --uid <uid>`（正文默认截断 4000 字；要全文加 `--full`）。附件只列清单，要落地用 `attachment`。
4. **理**：`mark` 改已读/星标；`move` 归档或移入「已删除」（默认真移动，源文件夹不再显示）。`purge` 是不可逆彻底删除，**只有用户明确说"彻底删除/清空"才用**。
5. **发**：`send` / `reply` / `forward` 默认只打印报文（dry-run）。把收件人、主题、正文摘要给用户确认，用户明确同意后才加 `--confirm`。

## 命令速查

```text
mail.py accounts list | add | remove | test [--account]
mail.py folders  --account [--json]
mail.py list     [--folder INBOX] [--limit 20] [--unread] [--flagged]
                 [--from 某人] [--subject 关键词] [--text 关键词] [--since 7d]
mail.py search   （参数同 list）
mail.py read     --uid <uid> [--folder] [--full] [--mark-unread]
mail.py attachment --uid <uid> --save-dir <工作区目录> [--name 片段]
mail.py mark     --uid <uid> --flag seen|unseen|flagged|unflagged
mail.py move     --uid <uid> [--to 文件夹] [--keep-source]
mail.py purge    --uid <uid> [--folder] | --all-flagged   # 彻底移除，不可撤销
mail.py send     --to a@b.com [--cc] --subject ... [--body|--body-file] [--attach 文件]
                 [--confirm] [--save-eml out.eml]
mail.py reply    --uid <uid> [--all] [--body|--body-file] [--confirm]
mail.py forward  --uid <uid> --to a@b.com [--body] [--confirm]
mail.py selftest           # 离线自检，不联网
```

要点：

- 任何子命令都可加 `--json`（放在子命令前后都行），返回结构化结果，便于你解析 uid/字段；给人看时不要贴 JSON。
- `--since` 支持 `7d` / `4w` / `2026-01-01`。
- **搜索条件的执行位置**（实测 QQ 服务端限制，脚本已自动分流）：
  - `--from 完整地址@host`、`--subject`（纯 ASCII）、`--unread`、`--flagged`、`--since` → 服务端搜索，快。
  - `--from 片段`、中文主题、`--text` 正文关键字 → 本地扫描最近 `--scan` 封（默认 300），输出会带一行「已在最近 N 封内本地匹配」的提示。
  - 因此 `--text` 不是全库搜索；结果为空时先看提示里的扫描范围，再决定是否调大 `--scan`。
- `move` 默认是**真移动**（COPY 到目标 + UID EXPUNGE 源文件夹），原文件夹里就不再显示；`--keep-source` 才只加删除标记。
- `--folder` 用可读名即可：`INBOX`、`Sent Messages`、`Drafts`、`Deleted Messages`、`Junk`，或 `folders` 里列出的中文文件夹名（脚本自动做 modified UTF-7 编解码）。
- 退出码：0 成功，2 用法错误，3 认证失败，4 网络/协议错误，5 目标不存在。非 0 时把脚本的「建议」一行转告用户。

## 常用配方

```sh
# 今天有什么没读的
mail.py list --unread --limit 10

# 找某人最近两周的邮件
mail.py search --from boss --since 14d

# 读第 3 封并下载全部附件到工作区
mail.py read --uid 1234
mail.py attachment --uid 1234 --save-dir ./attachments

# 归档（移到已删除以外的自建文件夹，先 folders 查名字）
mail.py move --uid 1234 --to "工作"

# 回复并附一份文件（先预览，用户点头再发）
mail.py reply --uid 1234 --body "收到，附件里是修订版。" --attach ./rev.docx
mail.py reply --uid 1234 --body "收到，附件里是修订版。" --attach ./rev.docx --confirm
```

## 需求不明确时

- 用户说"看看邮件"：默认 `--unread --limit 10`，先给未读概览，再问要不要展开某封。
- 用户说"删掉"：默认 `move` 到「已删除」；只有明确说"彻底删除/清空回收站"才考虑 `--purge`。
- 用户说"回复他"但没给内容：先起草正文并 dry-run 给用户过目，不要自己拍板发出去。
- 收件人不明确（多个候选、群发列表）或涉及对外发送：必须问清，不要猜。

## 能力边界

- 只支持 QQ 邮箱（IMAP/SMTP）。POP3、Exchange、Exchange Online、企业微信邮箱不在范围内。
- 只有 HTML 正文的邮件会被转成纯文本（保留段落结构，版式/图片丢失）；需要看原始 HTML 只能用网页版。
- 没有邮件规则/过滤器、日历、联系人、邮件模板功能；不解析 winmail.dat。
- 不发 HTML 富文本邮件（只发纯文本 + 附件）；需要 HTML 就说明当前不支持，或让用户确认后扩展脚本。
- 单封正文默认截断 4000 字；`--text` 只扫最近 N 封（默认 300）；超大附件（>20MB）下载可能超时，遇到就告诉用户改从网页下载。

## 禁止事项

- 不要未经用户明确同意就 `--confirm` 发信、群发、转发。
- 不要读取或回显 `accounts.json` 里的 `secret`，也不要让用户把授权码贴进对话。
- 不要在用户没要求时 `purge`、批量移文件夹、批量标记。
- 不要把附件或 `.eml` 写到 skill 目录、用户桌面等非工作区位置。
- 不要因为 dry-run 成功就宣称"已发送"——发信成功的唯一证据是 `--confirm` 后返回 `"sent": true`。

## 完成标准

- 收/搜/读：给出 uid、发件人、主题、日期；读到的正文与附件清单一致。
- 附件：文件确实落地（给出绝对路径与字节数），不是只打印文件名。
- 发信：dry-run 与用户确认一致；`--confirm` 后回报收件人与字节数；失败时给出脚本的错误 + 建议。
- 移动/标记：回报操作前后的文件夹名或标记状态。

排错对照表见 `references/troubleshooting.md`，遇到认证、限流、文件夹乱码等问题先查它。
