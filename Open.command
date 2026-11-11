#!/bin/bash
# Finder opens this script in Terminal. Never install Python or fall back to Safari.
set -u
cd -- "$(dirname -- "$0")" || exit 1
printf 'ことばの窓: PythonとGoogle Chromeを確認します。\n'
launcher="$PWD/beginner_launcher.py"
if [ ! -f "$launcher" ]; then
  printf 'アプリのファイルが見つかりません。ZIPを展開し、フォルダ全体を一緒に置いてください。\n'
  read -r -p 'Enterで閉じます。' _ || true
  exit 5
fi
selected=''
try_python() {
  [ -n "$selected" ] && return
  [ -f "$1" ] && [ -x "$1" ] || return
  if "$1" -I -B -X utf8 "$launcher" --check-python; then
    selected="$1"
  fi
}
for version in 3.14 3.13 3.12; do
  try_python "/Library/Frameworks/Python.framework/Versions/$version/bin/python$version"
  try_python "/opt/homebrew/bin/python$version"
  try_python "/usr/local/bin/python$version"
  candidate="$(command -v "python$version" 2>/dev/null || true)"
  [ -z "$candidate" ] || try_python "$candidate"
done
if [ -z "$selected" ]; then
  candidate="$(command -v python3 2>/dev/null || true)"
  # Apple /usr/bin/python3 may trigger Command Line Tools installation. Skip it.
  if [ -n "$candidate" ] && [ "$candidate" != /usr/bin/python3 ]; then
    try_python "$candidate"
  fi
fi
if [ -n "$selected" ]; then
  "$selected" -I -B -X utf8 "$launcher"
  result=$?
else
  printf '\n通常版のPython 3.12〜3.14（64bit・Apple Silicon用）が必要です。\n'
  printf 'Apple付属のPythonは使いません。自動インストールもしません。\n'
  printf '公式: https://www.python.org/downloads/macos/\n'
  printf 'BEGINNER-START-ja.mdのMac手順を確認してください。\n'
  result=2
fi
printf '\nEnterでこの起動プログラムを閉じます。\n'
read -r _ || true
exit "$result"
