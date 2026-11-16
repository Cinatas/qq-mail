#!/usr/bin/env bash
# 把 qq-mail 技能安装到 DSH 技能根，并跑一次离线自检。
# 用法：bash ./install.sh
# 环境变量：DSH_SKILLS_ROOT 覆盖技能根，DSH_PYTHON 指定解释器
set -euo pipefail

SKILL_NAME="qq-mail"
SOURCE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SKILLS_ROOT="${DSH_SKILLS_ROOT:-$HOME/.agents/skills}"
TARGET="$SKILLS_ROOT/$SKILL_NAME"

echo "源目录 : $SOURCE"
echo "技能根 : $SKILLS_ROOT"
echo "目标   : $TARGET"

if [ "$SOURCE" != "$TARGET" ]; then
  mkdir -p "$TARGET"
  for item in "$SOURCE"/* "$SOURCE"/.[!.]*; do
    [ -e "$item" ] || continue
    base="$(basename "$item")"
    [ "$base" = ".git" ] && continue
    cp -R "$item" "$TARGET/"
  done
  echo "已复制技能文件"
else
  echo "当前目录就是技能目录，跳过复制"
fi

is_python39() {
  [ -n "${1:-}" ] || return 1
  case "$1" in *WindowsApps*) return 1 ;; esac   # 应用商店占位符，不是解释器
  [ -x "$1" ] || return 1
  "$1" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)' >/dev/null 2>&1
}

PY=""
for candidate in \
  "${DSH_PYTHON:-}" \
  "${DSH_HOME:-$HOME/.dsh}"/dsh-runtimes/*/dependencies/python/python.exe \
  "$(command -v python3 2>/dev/null || true)" \
  "$(command -v python 2>/dev/null || true)"
do
  if is_python39 "$candidate"; then PY="$candidate"; break; fi
done
if [ -z "$PY" ]; then
  echo "找不到 Python 3.9+，请设置 DSH_PYTHON 或安装 python3。" >&2
  exit 1
fi
echo "Python : $PY"

"$PY" "$TARGET/scripts/mail.py" selftest

cat <<EOF

安装完成。下一步配置 QQ 邮箱授权码（输入不回显）：
  "$PY" "$TARGET/scripts/mail.py" accounts add --email you@qq.com
然后验证：
  "$PY" "$TARGET/scripts/mail.py" accounts test
EOF
