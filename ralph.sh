#!/usr/bin/env bash
# Work through the ready GitHub issues on the current branch, one OpenCode session per issue.
set -euo pipefail

started=$SECONDS
issue_started=$SECONDS
issue_number=''
session_id=''
context='—'
agent_pid=''
AGENT_REPLY=''

clock() {
  local elapsed=$1
  printf '%02d:%02d:%02d' "$((elapsed / 3600))" "$(((elapsed / 60) % 60))" "$((elapsed % 60))"
}

progress() {
  printf 'всего %s | issue #%s %s | контекст %s' \
    "$(clock "$((SECONDS - started))")" "${issue_number:--}" \
    "$(clock "$((SECONDS - issue_started))")" "$context"
}

show_timer() {
  if [[ -t 2 ]]; then
    printf '\r\033[K%s' "$(progress)" >&2
  fi
}

log() {
  if [[ -t 2 ]]; then printf '\r\033[K' >&2; fi
  printf '[%(%H:%M:%S)T] %s | %s\n' -1 "$(progress)" "$*"
  show_timer
}

fail() {
  log "ОШИБКА: $* (сессия: ${session_id:--})" >&2
  exit 1
}

stop_agent() {
  if [[ -n "$agent_pid" ]]; then
    kill "$agent_pid" 2>/dev/null || true
    wait "$agent_pid" 2>/dev/null || true
  fi
  if [[ -t 2 ]]; then printf '\r\033[K' >&2; fi
}
trap 'stop_agent; exit 130' INT
trap 'stop_agent; exit 143' TERM
trap 'if [[ -t 2 ]]; then printf "\r\033[K" >&2; fi' EXIT

for tool in gh git jq opencode python3; do
  command -v "$tool" >/dev/null 2>&1 || fail "не найден $tool"
done
[[ $# -eq 0 ]] || fail 'использование: ./ralph.sh (без аргументов)'
git rev-parse --is-inside-work-tree >/dev/null 2>&1 || fail 'нужен Git-репозиторий'
branch=$(git symbolic-ref --quiet --short HEAD) || fail 'нужна текущая ветка, не detached HEAD'
git rev-parse --verify '@{upstream}' >/dev/null 2>&1 || fail 'для ветки не настроен upstream'
[[ -z "$(git status --porcelain --untracked-files=normal)" ]] || fail 'рабочая копия не чистая; сначала сохраните свои изменения'
log "Старт: ветка $branch, upstream $(git rev-parse --abbrev-ref --symbolic-full-name '@{upstream}')"

# Match only blockers explicitly listed in a "Blocked by" section or line.
text_blockers() {
  local body=$1 line blocker section=0
  while IFS= read -r line; do
    if [[ $line =~ ^##[[:space:]]+Blocked[[:space:]]+by ]]; then
      section=1
      continue
    fi
    if [[ $line =~ ^##[[:space:]] ]]; then section=0; fi
    if (( section )) || [[ $line =~ ^[[:space:]]*Blocked[[:space:]]by: ]]; then
      while [[ $line =~ \#([0-9]+) ]]; do
        blocker=${BASH_REMATCH[1]}
        printf '%s\n' "$blocker"
        line=${line#*"#$blocker"}
      done
    fi
  done <<< "$body"
}

# Read all pages; sort numeric issue numbers instead of relying on API ordering.
next_issue() {
  local pages candidates issue data assigned blocked body blocker state
  log 'Получаю открытые issues с меткой ready-for-agent'
  pages=$(gh api --method GET --paginate --slurp \
    'repos/{owner}/{repo}/issues?state=open&labels=ready-for-agent&per_page=100') \
    || fail 'не удалось получить список issues'
  jq -e 'type == "array" and all(.[]; type == "array")' <<< "$pages" >/dev/null \
    || fail 'неожиданный ответ GitHub при получении issues'
  candidates=$(jq -r '[.[][] | select(has("pull_request") | not) | .number] | sort | unique | .[]' <<< "$pages") \
    || fail 'не удалось разобрать номера issues'
  NEXT_ISSUE=''
  while IFS= read -r issue; do
    [[ -n $issue ]] || continue
    data=$(jq -c --argjson n "$issue" 'first(.[][] | select(.number == $n))' <<< "$pages")
    assigned=$(jq -r '.assignees | length' <<< "$data")
    if (( assigned > 0 )); then
      log "Пропуск #$issue: назначен исполнитель"
      continue
    fi
    blocked=$(jq -r '.issue_dependencies_summary.blocked_by // 0' <<< "$data")
    if (( blocked > 0 )); then
      log "Пропуск #$issue: открытых зависимостей GitHub: $blocked"
      continue
    fi
    body=$(jq -r '.body // ""' <<< "$data")
    blocked=0
    while IFS= read -r blocker; do
      [[ -n $blocker ]] || continue
      state=$(gh api "repos/{owner}/{repo}/issues/$blocker" --jq .state) \
        || fail "не удалось проверить блокирующий issue #$blocker"
      [[ $state == open || $state == closed ]] || fail "неизвестное состояние issue #$blocker: $state"
      if [[ $state == open ]]; then
        log "Пропуск #$issue: ожидает #$blocker"
        blocked=1
        break
      fi
    done < <(text_blockers "$body")
    if (( blocked )); then continue; fi
    NEXT_ISSUE=$issue
    log "Выбран #$issue: $(jq -r '.title' <<< "$data")"
    return
  done <<< "$candidates"
}

# A coprocess keeps OpenCode's JSON event stream live and lets Bash retain the
# session ID and final assistant text without a log file or a buffered pipeline.
run_agent() {
  local prompt=$1 read_fd write_fd stream_fd event type rc last_heartbeat=$SECONDS
  local -a options=(--format json --auto)
  AGENT_REPLY=''
  if [[ -n $session_id ]]; then options+=(--session "$session_id"); fi
  log "Запуск opencode run ${session_id:+--session $session_id}"
  coproc AGENT { opencode run "${options[@]}" "$prompt" </dev/null; }
  agent_pid=$AGENT_PID
  read_fd=${AGENT[0]}
  write_fd=${AGENT[1]}
  # Bash closes coprocess descriptors when it exits; keep our own descriptor
  # so fast-running commands can still be drained after the child exits.
  exec {stream_fd}<&"$read_fd"
  exec {read_fd}<&-
  read_fd=$stream_fd
  exec {write_fd}>&-

  while true; do
    if IFS= read -r -t 1 event <&"$read_fd"; then
      type=$(jq -r '.type // empty' <<< "$event") || fail 'некорректный JSON от OpenCode'
      [[ -n $type ]] || fail 'событие OpenCode без типа'
      if [[ -z $session_id ]]; then
        session_id=$(jq -r '.sessionID // empty' <<< "$event")
        [[ -z $session_id ]] || log "Сессия: $session_id"
      fi
      case $type in
        step_start) log 'Шаг агента начат' ;;
        step_finish)
          context=$(jq -r '.part.tokens | if . then "≈\((.input // 0) + (.cache.read // 0) + (.cache.write // 0)) токенов (вход + кэш), выход \(.output // 0)" else "—" end' <<< "$event")
          log "Шаг агента завершён; $context"
          ;;
        tool_use)
          log "Инструмент: $(jq -r '.part.tool // "?"' <<< "$event") [$(jq -r '.part.state.status // "?"' <<< "$event")]"
          jq -r '.part.state | "Вход: \(.input // {} | tojson)\nВыход: \(.output // .error // "")"' <<< "$event"
          ;;
        text)
          AGENT_REPLY=$(jq -r '.part.text // ""' <<< "$event")
          log 'Ответ агента:'
          printf '%s\n' "$AGENT_REPLY"
          ;;
        error)
          log "Ошибка OpenCode: $(jq -r '.error.data.message // .error.name // .error // "?"' <<< "$event")"
          ;;
        *) log "Событие OpenCode: $type" ;;
      esac
      show_timer
    else
      rc=$?
      if (( rc <= 128 )); then break; fi
      show_timer
      if (( SECONDS - last_heartbeat >= 30 )); then
        if [[ ! -t 2 ]]; then log 'Агент работает…'; fi
        last_heartbeat=$SECONDS
      fi
    fi
  done
  exec {read_fd}<&-
  if wait "$agent_pid"; then rc=0; else rc=$?; fi
  agent_pid=''
  log "opencode завершился с кодом $rc"
  (( rc == 0 )) || return "$rc"
  [[ -n $session_id ]] || fail 'OpenCode не сообщил ID сессии'
}

# Require exactly one XML result; no prose, Markdown or guessed success.
xml_status() {
  printf '%s' "$AGENT_REPLY" | python3 -c '
import sys
import xml.etree.ElementTree as ET
try:
    root = ET.fromstring(sys.stdin.read())
    status = root.attrib.get("status")
    if (root.tag != "result" or status not in ("success", "failure") or not (root.findtext("summary") or "").strip()):
        raise ValueError("expected <result status=\"success|failure\"><summary>...</summary></result>")
except (ET.ParseError, ValueError) as exc:
    print(f"Некорректный XML: {exc}", file=sys.stderr)
    sys.exit(1)
print(status)
'
}

while true; do
  next_issue
  if [[ -z $NEXT_ISSUE ]]; then
    log 'Готово: доступных issues больше нет'
    break
  fi
  issue_number=$NEXT_ISSUE
  issue_started=$SECONDS
  session_id=''
  context='—'
  before=$(git rev-parse HEAD)
  log "Начинаю #$issue_number (HEAD $before)"
  run_agent "Implement issue #$issue_number. Read its full body and comments. Run the relevant checks. Do not close, commit or push yet. End with exactly one XML element: <result status=\"success\"><summary>What was implemented and verified</summary></result> or <result status=\"failure\"><summary>What prevented completion</summary></result>. No text outside the XML." \
    || fail "реализация #$issue_number не завершилась успешно"

  for attempt in 1 2 3; do
    if status=$(xml_status); then break; fi
    (( attempt < 3 )) || fail "после $attempt попыток нет корректного XML для #$issue_number"
    log "Уточняю XML в сессии $session_id (попытка $((attempt + 1))/3)"
    run_agent 'Return only the XML result for the issue implementation, exactly <result status="success|failure"><summary>...</summary></result>. Do not do more work.' \
      || fail "не удалось уточнить ответ для #$issue_number"
  done
  [[ $status == success ]] || fail "агент сообщил о неудаче для #$issue_number: $AGENT_REPLY"
  log "Реализация #$issue_number успешна; закрываю issue, создаю коммит и отправляю изменения"
  run_agent 'Close the issue, commit all changes and push' \
    || fail "не удалось завершить #$issue_number; проверьте состояние issue и push"

  after=$(git rev-parse HEAD)
  [[ $after != "$before" ]] || fail "для #$issue_number не создан новый коммит"
  [[ -z "$(git status --porcelain --untracked-files=normal)" ]] || fail "после #$issue_number остались незакоммиченные изменения"
  [[ $after == "$(git rev-parse '@{upstream}')" ]] || fail "коммит #$issue_number не совпадает с upstream"
  [[ $(git symbolic-ref --quiet --short HEAD) == "$branch" ]] || fail 'агент переключил ветку'
  state=$(gh api "repos/{owner}/{repo}/issues/$issue_number" --jq .state) \
    || fail "не удалось проверить состояние #$issue_number"
  [[ $state == closed ]] || fail "issue #$issue_number не закрыт (состояние: $state)"
  log "Завершено #$issue_number: коммит $after отправлен, issue закрыт"
done
