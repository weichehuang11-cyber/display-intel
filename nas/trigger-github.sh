#!/bin/sh
# NAS 準時「按下」GitHub 的執行按鈕：蒐集與寄信仍由 GitHub 負責，NAS 只負責準時。
# （GitHub 免費排程常延遲數小時；手動觸發通常 1 分鐘內就開始執行）
#
# 用法（NAS 任務排程表，使用者選 root 或你的帳號都可以）：
#   週一、二、三、五、日 19:17：sh /volume1/docker/display-intel/nas/trigger-github.sh collect
#   週日 21:17：                sh /volume1/docker/display-intel/nas/trigger-github.sh report
#
# 需要同一個資料夾裡有 github-token.txt（GitHub 權杖，只開這個 repo 的 Actions 讀寫權限）。
# 權杖只存在你的 NAS 上，不要上傳到 GitHub、不要貼到對話裡。
DIR=$(cd "$(dirname "$0")" && pwd)
TOKEN=$(tr -d ' \r\n' < "$DIR/github-token.txt" 2>/dev/null)
REPO="weichehuang11-cyber/display-intel"
if [ -z "$TOKEN" ]; then
  echo "找不到權杖：請把 GitHub 權杖存成 $DIR/github-token.txt"
  exit 1
fi
case "$1" in
  collect) WF=collect.yml; INPUTS='{"profile":"auto","skip_if_done":"true"}' ;;
  report)  WF=report.yml;  INPUTS='{"mode":"scheduled"}' ;;
  *) echo "用法：sh trigger-github.sh collect 或 sh trigger-github.sh report"; exit 1 ;;
esac
OUT="$DIR/trigger-last.log"
CODE=$(curl -s -o "$OUT" -w "%{http_code}" -X POST \
  -H "Accept: application/vnd.github+json" -H "Authorization: Bearer $TOKEN" \
  "https://api.github.com/repos/$REPO/actions/workflows/$WF/dispatches" \
  -d "{\"ref\":\"main\",\"inputs\":$INPUTS}")
if [ "$CODE" = "204" ]; then
  echo "$(date '+%F %T') 已觸發 $WF" | tee -a "$DIR/trigger-history.log"
else
  echo "$(date '+%F %T') 觸發失敗（HTTP $CODE），內容：$(cat "$OUT")" | tee -a "$DIR/trigger-history.log"
  [ "$CODE" = "401" ] && echo "→ 權杖錯誤或已過期，請到 GitHub 重新產生並更新 github-token.txt"
  [ "$CODE" = "403" ] || [ "$CODE" = "404" ] && echo "→ 權杖沒有這個 repo 的 Actions 讀寫權限"
  exit 1
fi
