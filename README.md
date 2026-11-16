# qq-mail — a DSH skill for QQ Mail

用 IMAP/SMTP 管理 QQ 邮箱的 [DeepSeek Harness](https://github.com/deepseek-ai/deepseek-harness) 技能：看未读、按发件人/主题/时间搜索、读信、下载附件、标记、归档、发信/回复/转发。

纯 Python 标准库实现，无第三方依赖；授权码用 Windows DPAPI 加密保存；发信默认 dry-run。

> A DSH (DeepSeek Harness) skill for managing a QQ Mail mailbox over IMAP/SMTP.
> Stdlib-only Python, DPAPI-encrypted credentials, dry-run by default for sending.

## 功能

| 子命令 | 作用 |
|---|---|
| `accounts list/add/remove/test` | 账号管理；`test` 同时验证 IMAP 与 SMTP |
| `folders` | 列出文件夹、邮件数、未读数（含中文文件夹名解码） |
| `list` | 列出邮件，支持 `--unread --flagged --from --subject --since --limit` |
| `search` | 搜索（`list` 的过滤参数，正文关键字走本地扫描） |
| `read` | 读一封：头部 + 正文（HTML 自动转纯文本）+ 附件清单 |
| `attachment` | 把附件落到工作区 |
| `mark` | 已读/未读/星标 |
| `move` | 归档或移入已删除（真移动，UID EXPUNGE） |
| `purge` | 彻底移除（不可逆，需显式调用） |
| `send/reply/forward` | 发信，默认只打印报文，`--confirm` 才真发 |
| `selftest` | 离线自检：UTF-7 / DPAPI / MIME / 日期解析 |

## 安装

DSH 只扫描技能根的**直接子目录**（`<skills根>/<name>/SKILL.md`，嵌套不扫描）。因此本仓库根目录就是技能目录，克隆到技能根即安装完成。

**1. 克隆安装（推荐）**

```powershell
git clone https://github.com/<you>/qq-mail "$HOME\.agents\skills\qq-mail"
```

**2. 更新已有安装**

```powershell
git -C "$HOME\.agents\skills\qq-mail" pull
```

技能正文改动会被 DSH 的文件监视器自动发现，不需要重启；`SKILL.md` 的 frontmatter 变化会在下一轮刷新目录。

**3. 一键脚本**

```powershell
# Windows
pwsh ./install.ps1
```

```bash
# macOS / Linux
bash ./install.sh
```

脚本会把本目录复制到 `~/.agents/skills/qq-mail`（可用 `-SkillsRoot` / `DSH_SKILLS_ROOT` 覆盖），然后跑一次 `selftest`。

**4. 交给 DSH 装**

把仓库地址发给你的 DSH 会话，让它 clone 到技能根并自检即可。

> 其他技能根同样有效：项目级的 `<项目>/.dsh/skills`、`<项目>/.agents/skills`，用户级的 `~/.dsh/skills`、`~/.agents/skills`。

## 首次配置

需要一个 **16 位授权码**（不是 QQ 密码）：

1. 登录 QQ 邮箱网页版 → 设置 → **账号与安全**
2. 找到「POP3/IMAP/SMTP/Exchange/CardDAV/CalDAV 服务」→ 开启 **IMAP/SMTP 服务**
3. 按提示短信验证，拿到 16 位授权码

然后添加账号（输入不回显）：

```powershell
python scripts/mail.py accounts add --email you@qq.com --display-name "你的名字"
```

或从文件读入，避免交互：

```powershell
python scripts/mail.py accounts add --email you@qq.com --password-file ./authcode.txt
```

保存后会自动验证连通性，也可单独验证：

```powershell
python scripts/mail.py accounts test
```

凭据位置：`%USERPROFILE%\.dsh\qq-mail\accounts.json`（可用 `DSH_QQ_MAIL_HOME` 覆盖目录）。

## 安全模型

- **授权码用 Windows DPAPI 加密**（`CryptProtectData`），绑定当前 Windows 用户；文件被拷到别的用户或别的机器上都解不开。
- **发信默认 dry-run**：`send/reply/forward` 不加 `--confirm` 只打印完整报文，不会连 SMTP。
- **`move` 默认真移动**（COPY + UID EXPUNGE 源邮件），`--keep-source` 才只加删除标记；**`purge` 不可逆**，脚本不会自动调用。
- **附件只列不下载**，要落地必须显式 `attachment --save-dir`；产物写入指定目录，不写技能目录。
- **凭据不进仓库**：`accounts.json` 位于 `~/.dsh/qq-mail/`，`.gitignore` 再兜一层。
- 脚本从不打印授权码；错误信息里也不包含它。

## 用法示例

```powershell
# 未读概览
python scripts/mail.py list --unread --limit 10

# 找某人最近两周的邮件（片段走本地扫描，完整地址走服务端）
python scripts/mail.py search --from boss --since 14d
python scripts/mail.py search --from boss@example.com

# 读信并下载附件
python scripts/mail.py read --uid 1234
python scripts/mail.py attachment --uid 1234 --save-dir ./attachments

# 归档 / 删除
python scripts/mail.py move --uid 1234 --to "工作"
python scripts/mail.py move --uid 1234                 # 进「已删除」
python scripts/mail.py purge --uid 1234                # 彻底删除（不可逆）

# 发信：先预览，确认后加 --confirm
python scripts/mail.py send --to a@b.com --subject "主题" --body "正文" --attach ./report.pdf
python scripts/mail.py reply --uid 1234 --body "收到，谢谢。" --confirm
```

在 DSH 里可以直接说「看看我邮箱有什么新邮件」「找找 Apple 发来的收据」「回复这封说收到」，技能会自动触发。

## 依赖与兼容

- Python ≥ 3.9（在 3.12 上测试；用到 `imaplib` 的 `timeout` 参数）
- 仅标准库，无需 `pip install`
- 服务端：`imap.qq.com:993`（SSL）、`smtp.qq.com:465`（SSL；587 走 STARTTLS）
- 操作系统：Windows（DPAPI 加密）；非 Windows 下加 `--plaintext` 可运行，但凭据会以明文保存

## 已知限制

- **服务端搜索能力有限**（2026-11 实测）：`FROM` 只认完整地址、`TEXT` 被忽略、非 ASCII 条件会失败。因此「发件人片段 / 中文主题 / 正文关键字」改由本地扫描最近 `--scan` 封（默认 300）完成，输出会明确标注扫描范围——这不是全库搜索。
- 只有 HTML 正文的邮件会转成纯文本，版式与内嵌图片丢失。
- 只发纯文本邮件（可带附件），不支持 HTML 富文本正文。
- 不支持 POP3 / Exchange / 日历 / 联系人 / 邮件规则；不解析 `winmail.dat`。
- QQ 对第三方客户端有发信频率与每日上限，触发时脚本会给出明确报错。

## 目录结构

```text
qq-mail/
├─ SKILL.md                    # 技能说明书（frontmatter: name/description）
├─ scripts/mail.py             # 全部功能实现
├─ references/troubleshooting.md
├─ README.md
├─ LICENSE
├─ .gitignore
└─ install.ps1 / install.sh
```

排错对照表见 [references/troubleshooting.md](references/troubleshooting.md)（认证、限流、文件夹乱码、DPAPI 等）。

## License

MIT
