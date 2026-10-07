# cchat — browse your Claude Code chat history.
# Sourced from ~/.zshrc. Delete that source line to remove it.
#
# Deliberately does NOT touch `history`: that is a zsh builtin and Oh My Zsh
# aliases it to omz_history, so shadowing it would break listing your commands.

CCHAT_HOME="${CCHAT_HOME:-$HOME/code/cchat}"
CCHAT_PORT="${CCHAT_PORT:-7842}"

cchat() {
  local url="http://127.0.0.1:${CCHAT_PORT}"
  # already serving? just open it rather than failing on the busy port
  if lsof -nP -iTCP:"${CCHAT_PORT}" -sTCP:LISTEN >/dev/null 2>&1; then
    echo "cchat is already running — opening ${url}"
    command open "${url}" 2>/dev/null
    return 0
  fi
  (
    cd "${CCHAT_HOME}" || { echo "cchat: no such directory: ${CCHAT_HOME}" >&2; return 1; }
    # incremental first (a fraction of a second); full rebuild only if the schema
    # moved on, which is the one case that actually needs it
    python3 cchat.py index || python3 cchat.py index --full || return 1
    python3 cchat.py serve --port "${CCHAT_PORT}" "$@"
  )
}

alias hist=cchat      # what you asked for
alias his=cchat       # same, as typed
alias chats=cchat     # spelled out
