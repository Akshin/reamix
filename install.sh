#!/bin/sh
# Установка reamix из скачанного репозитория: ставит uv, если его нет, и запускает
# reamix install. Запускать можно повторно — уже сделанное не трогается.
set -e

root=$(cd "$(dirname "$0")" && pwd)

if [ "$(uname)" != "Darwin" ]; then
  echo "reamix работает только на macOS"
  exit 1
fi

if [ ! -d "${REAPER_RESOURCE_PATH:-$HOME/Library/Application Support/REAPER}" ]; then
  echo "REAPER не найден: установи его и запусти один раз, потом повтори установку"
  exit 1
fi

if ! command -v uv >/dev/null 2>&1; then
  if command -v brew >/dev/null 2>&1; then
    echo "ставлю uv через Homebrew"
    brew install uv
  else
    echo "ставлю uv установщиком с astral.sh"
    curl -LsSf https://astral.sh/uv/install.sh | sh
    # Установщик кладёт uv в ~/.local/bin и прописывает его в профиль оболочки;
    # в текущем процессе PATH надо дополнить самим.
    PATH="$HOME/.local/bin:$PATH"
    export PATH
  fi
fi

"$root/bin/reamix" install
