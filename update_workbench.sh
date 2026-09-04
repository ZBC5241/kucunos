#!/bin/bash
# 李家村库存工作台 V11.4 一键更新脚本（接口升级版）
# 链路：确保 9223 经理会话 → 直连接口拉现存量(pull_live 优先，失败 fallback xlsx 老路)
#       → 注入 V11.4 底表(含新鲜度闸门) → 校验 → 推送
# 用法：
#   bash update_workbench.sh            # 仅本地更新 index.html（不推送）
#   PUSH=1 bash update_workbench.sh     # 本地更新 + git commit + SSH推送 + 线上SHA校验
set -u

KUCUNOS=/Users/mac/WorkBuddy/2026-08-31-14-14-00/kucunos
CLAW=/Users/mac/WorkBuddy/Claw
PY=/Users/mac/.workbuddy/binaries/python/envs/default/bin/python3
DL=~/Downloads
TODAY=$(date +%Y%m%d)

cd "$KUCUNOS" || exit 1

# ---------- 确保 9223 经理 Chrome 会话（带经理登录态 + 存量查询已开）----------
ensure_9223() {
  if curl -s --max-time 2 http://127.0.0.1:9223/json/version >/dev/null 2>&1; then
    echo "[9223] 已运行，复用现有会话"
    return 0
  fi
  echo "[9223] 未运行，拉起经理 Chrome 会话（/tmp/mgr-chrome，钥匙串自动 CAS 登录）…"
  "$PY" - <<'PY' || return 1
import sys, time
sys.path.insert(0, "/Users/mac/WorkBuddy/Claw")
import stock_pull as sp
if not sp.ensure_chrome():
    sys.exit(1)
bws = sp.get_bws(); time.sleep(1)
t, sid = sp.find_page(bws); bws.cmd("Page.enable", {}, sid)
if not sp.login(bws, sid):
    sys.exit(1)
app = sp.open_stock(bws, sid)
sys.exit(0 if app else 1)
PY
}

# ---------- 拉数：直连接口优先，xlsx 老路兜底 ----------
ensure_9223 || { echo "!! 9223 会话拉起失败，中止"; exit 1; }

# 拉数逻辑（直连接口优先，xlsx 老路兜底）
# 成功向 stdout 输出 csv 路径；失败向 stdout 输出空，由外层判断是否走重登恢复
pull_csv() {
  local csv=""
  if csv=$("$PY" "$KUCUNOS/pull_live.py") && [ -n "$csv" ]; then
    echo "=== [1/4] 直连接口拉数成功 -> $csv ===" >&2
    printf '%s' "$csv"; return 0
  fi
  echo "!! 直连接口失败，回退 xlsx 老路（update_kucun → stock_pull）" >&2
  if "$PY" "$CLAW/update_kucun.py" >&2; then
    csv=$("$PY" "$KUCUNOS/conv_xlsx.py") || return 1
    printf '%s' "$csv"; return 0
  fi
  # 关键修复：stock_pull 的详细日志必须走 stderr，否则会漏进 CSV 变量
  # 导致 build_v11_inv.py 把日志文本当路径 → FileNotFoundError。
  # 真正的 csv 路径由下方 conv_xlsx.py 兜底输出（仅单行路径走 stdout）。
  "$PY" "$CLAW/stock_pull.py" "$KUCUNOS" >&2 || return 1
  local LATEST=$(ls -t "$DL"/*.xlsx 2>/dev/null | grep -v "现存量_${TODAY}_默认方案.xlsx" | head -1)
  [ -z "$LATEST" ] && return 1
  cp "$LATEST" "$DL/现存量_${TODAY}_默认方案.xlsx"
  csv=$("$PY" "$KUCUNOS/conv_xlsx.py") || return 1
  printf '%s' "$csv"; return 0
}

CSV=$(pull_csv)
if [ -z "$CSV" ]; then
  echo "!! 首次拉数失败，疑似经理 cookie 过期 → 走 recover_login 用钥匙串重登后重试"
  if "$PY" "$CLAW/recover_login.py"; then
    CSV=$(pull_csv) || { echo "!! 重登后仍拉数失败，本次更新中止"; exit 1; }
  else
    echo "!! recover_login 重登失败（钥匙串 yonyou-mgr 可能失效），本次更新中止"; exit 1
  fi
fi

echo "=== [2/4] 注入 V11.4 底表（含新鲜度闸门）==="
"$PY" "$KUCUNOS/build_v11_inv.py" "$CSV"
# build_v11_inv.py 内部：数据无变化 -> 不写 index.html（git diff 为空 -> 跳过）
#                    数据有变化 -> 写 index.html（git diff 非空 -> 提交）

if git diff --quiet index.html; then
  echo "[skip] index.html 无变化（现存量与当前一致），不提交不推送"
  exit 0
fi

echo "=== [3/4] 提交 + 推送（仅 PUSH=1 时）==="
if [ "${PUSH:-}" != "1" ]; then
  echo "[dry] PUSH 未设，跳过 git 提交/推送（仅本地已更新 index.html）"
  exit 0
fi

git fetch origin 2>&1 | tail -2
git add index.html
git commit -m "auto(kucunos): 定时更新 V11.4 现存量 $(date '+%Y-%m-%d %H:%M')" 2>&1 | tail -3
git push origin main 2>&1 | tail -5

echo "=== 线上 SHA 校验（GitHub Pages 有缓存，轮询等待）==="
LOCAL=$(shasum -a 256 index.html | cut -d' ' -f1)
echo "本地 SHA256: $LOCAL"
for i in $(seq 1 6); do
  sleep 15
  ONLINE=$(curl -s --compressed https://zbc5241.github.io/kucunos/ | shasum -a 256 | cut -d' ' -f1)
  if [ "$LOCAL" = "$ONLINE" ]; then
    echo "✅ 第${i}次匹配，逐字节一致，上线成功"
    break
  else
    echo "  第${i}次仍不一致 (online=$ONLINE)"
  fi
done
