"""Где что лежит: репозиторий reamix, папка REAPER, данные пользователя, служебная папка трека."""

import os
from pathlib import Path

# src/reamix/paths.py → корень репозитория
ROOT = Path(os.environ.get("REAMIX_HOME") or Path(__file__).resolve().parents[2])

IPC = ROOT / ".ipc"
BRIDGE = ROOT / "bridge" / "reamix_bridge.lua"
TEMPLATES = ROOT / "templates"
SKILL = ROOT / "skill"
SKILL_ABLETON = ROOT / "skill-ableton"
LAUNCHER = ROOT / "bin" / "reamix"

# Данные конкретного пользователя — вне репозитория: у каждого свои плагины и вкусы.
USER = Path(os.environ.get("REAMIX_DATA") or Path.home() / ".reamix")
CATALOG = USER / "plugins.tsv"
DEFAULTS = USER / "defaults"
PREFERENCES = USER / "preferences.md"

REAPER = Path(os.environ.get("REAPER_RESOURCE_PATH") or Path.home() / "Library/Application Support/REAPER")
STARTUP = REAPER / "Scripts" / "__startup.lua"
SKILL_LINK = Path.home() / ".claude" / "skills" / "reamix"
SKILL_ABLETON_LINK = Path.home() / ".claude" / "skills" / "ableton-to-reaper"
# Папки из PATH, куда можно положить ссылку на команду, в порядке предпочтения.
BIN_DIRS = [Path.home() / ".local/bin", Path("/opt/homebrew/bin"), Path("/usr/local/bin")]

# Служебная папка внутри папки трека: снимки, рендеры, отчёты анализа.
TRACK_DATA = ".reamix"
