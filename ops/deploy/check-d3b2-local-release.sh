#!/usr/bin/env bash
#
# check-d3b2-local-release.sh — Read-only Release-Integritaetsgate fuer D3B2.1.
# Stellt VOR jedem Sync/Remote-Aufruf sicher, dass der lokale Worktree exakt dem
# veroeffentlichten main-Stand entspricht, damit der uebergebene D3B2_RELEASE_SHA
# tatsaechlich die gesyncten Dateien beschreibt.
#
# Aendert NICHTS: kein fetch/pull/reset/checkout, keine Ref-Mutation, keine Datei.
# Gibt KEINE Remote-URL/Secrets aus.
#
# usage:  check-d3b2-local-release.sh [erwarteter-40hex-sha]
# exit:   0 sauber/identisch zu main | 1 Abweichung (fail closed) | 2 falsche Args
set -euo pipefail

HEXRE='^[0-9a-f]{40}$'
EXPECT="${1:-}"

fail() { printf '[release-guard] FEHLER: %s\n' "$*" >&2; exit 1; }

command -v git >/dev/null 2>&1 || fail "git nicht gefunden"

# 1) Branch exakt 'main'.
branch="$(git rev-parse --abbrev-ref HEAD 2>/dev/null || echo '')"
[ "${branch}" = "main" ] || fail "nicht auf 'main' (aktiv: ${branch:-unbekannt})."

# 2) Worktree sauber (inkl. untracked, exkl. gitignore).
[ -z "$(git status --porcelain --untracked-files=normal 2>/dev/null)" ] \
  || fail "Worktree nicht sauber (uncommittete, gestagte oder untracked Aenderungen)."

# 3) HEAD = genau 40 Hex.
head="$(git rev-parse HEAD 2>/dev/null || echo '')"
printf '%s' "${head}" | grep -Eq "${HEXRE}" || fail "HEAD-SHA ungueltig."

# 4) HEAD == origin/main (lokale Remote-Tracking-Ref).
omain="$(git rev-parse origin/main 2>/dev/null || echo '')"
[ -n "${omain}" ] || fail "origin/main nicht aufloesbar."
[ "${omain}" = "${head}" ] || fail "HEAD entspricht nicht origin/main."

# 5/6) Tatsaechlicher Remote-Branch refs/heads/main: genau ein SHA == HEAD.
#     ls-remote ist read-only (kein fetch). In Tests ueberschreibbar.
local_ls() {
  if [ -n "${D3B2_LS_REMOTE_CMD:-}" ]; then sh -c "${D3B2_LS_REMOTE_CMD}" 2>/dev/null || true
  else git ls-remote origin refs/heads/main 2>/dev/null || true; fi
}
ls_out="$(local_ls)"
nlines="$(printf '%s\n' "${ls_out}" | grep -c . || true)"
[ "${nlines}" -eq 1 ] || fail "Remote refs/heads/main nicht eindeutig (Treffer: ${nlines})."
rsha="$(printf '%s\n' "${ls_out}" | awk 'NR==1{print $1}')"
printf '%s' "${rsha}" | grep -Eq "${HEXRE}" || fail "Remote-Main-SHA ungueltig."
[ "${rsha}" = "${head}" ] || fail "Remote-Main weicht von HEAD ab."

# 7) Optionaler erwarteter SHA == HEAD.
if [ -n "${EXPECT}" ]; then
  printf '%s' "${EXPECT}" | grep -Eq "${HEXRE}" || fail "erwarteter Release-SHA malformed."
  [ "${EXPECT}" = "${head}" ] || fail "erwarteter Release-SHA entspricht nicht HEAD."
fi

echo "[release-guard] ok: Worktree == main == origin == remote refs/heads/main"
