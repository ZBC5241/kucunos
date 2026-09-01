#!/bin/bash
# 李家村库存工作台 V11.4 一键更新脚本
# 链路：纯HTTP拿数(经理账号) -> xlsx转csv -> 注入V11.4底表(含新鲜度闸门) -> 校验 -> 推送
# 用法：
#   bash update_workbench.sh            # 仅本地更新 index.html（不推送）
#   PUSH=1 bash update_workbench.sh     # 本地更新 + git commit + SSH推送 + 线上SHA校验
set -u

KUCUNOS=/Users/mac/WorkBuddy/2026-08-31-14-14-00/kucunos
CLAW=/Users/mac/WorkBuddy/Claw
PY=/Users/mac/.workbuddy/binaries/python/envs/default/bin/python3

cd "$KUCUNOS" || exit 1

echo "=== [1/4] 拉取最新现存量（纯HTTP, 经理账号 18591910491 + Chrome 9223）==="
"$PY" "$CLAW/update_kucun.py" || {
  echo "!! 拿数失败，尝试 recover_login 重登经理账号"
  "$PY" "$CLAW/recover_login.py" || { echo "!! 重登失败，本次更新中止"; exit 1; }
  "$PY" "$CLAW/update_kucun.py" || { echo "!! 重登后仍失败，中止"; exit 1; }
}

echo "=== [2/4] xlsx -> csv ==="
CSV=$("$PY" "$KUCUNOS/conv_xlsx.py") || { echo "!! 转换失败"; exit 1; }

echo "=== [3/4] 注入 V11.4 底表（含新鲜度闸门）==="
"$PY" "$KUCUNOS/build_v11_inv.py" "$CSV"
# build_v11_inv.py 内部：数据无变化 -> 不写 index.html（git diff 为空 -> 跳过）
#                    数据有变化 -> 写 index.html（git diff 非空 -> 提交）

if git diff --quiet index.html; then
  echo "[skip] index.html 无变化（现存量与当前一致），不提交不推送"
  exit 0
fi

echo "=== [4/4] 提交 + 推送（仅 PUSH=1 时）==="
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
