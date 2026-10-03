# reamix

Инструмент, которым Claude сводит музыку в REAPER: читает состояние проекта, вешает плагины,
крутит параметры, рисует автоматизацию. Интерфейс — сам REAPER; решения на слух принимает человек.
Инструмент раздаётся другим людям: только macOS, всё на русском, ничего личного в репозитории.

## Стек
- Мост: Lua-скрипт внутри REAPER (ReaScript, Lua 5.4), обмен через файлы в `.ipc/`
- CLI: Python ≥ 3.11, uv; анализ — numpy, scipy, soundfile, pyloudnorm; тесты — pytest + lupa (Lua в Python)

## Структура
```
bridge/reamix_bridge.lua  фоновый цикл в REAPER: принять запрос, выполнить, ответить
bridge/lib.lua            функции rx.* над проектом (перечитывается на каждый запрос)
bridge/json.lua           Lua → JSON без потери точности
bridge/import.lua         сборка цепочек плагинов для импорта из Ableton (грузится запросом)
src/reamix/               CLI: client (обмен), store (снимки), fmt (компактный вид), plugins (каталог),
                          analysis (метрики рендеров), groove (атаки и их попадание в сетку),
                          multitrack (проект из папки с мультитреком),
                          rpp (запись проекта REAPER); импорт из Ableton: ableton (разбор .als),
                          importer (пересчёт и порядок работ)
~/.reamix/                данные пользователя, вне репозитория: plugins.tsv (каталог плагинов),
                          defaults/ (умолчания параметров), preferences.md (чем предпочитает сводить)
skill/SKILL.md            этапы сведения и справка по командам для сессий в папках треков
skill-ableton/SKILL.md    скилл ableton-to-reaper: перенос проекта Ableton командой reamix import-als
templates/                шаблоны: документация трека (reamix init), предпочтения
tests/fake_reaper.lua     подделка API REAPER для тестов
```

## Команды
- Тесты: `uv run pytest`
- CLI из репозитория: `bin/reamix <команда> --any-project` (в папке трека — просто `reamix <команда>`)
- Мост запущен? `reamix status`. Правки в `bridge/lib.lua` подхватываются без перезапуска;
  после правки `reamix_bridge.lua` — `reamix reload`
- Проверка окружения и связи с REAPER: `reamix doctor`

## Правила
- Тяжёлые данные идут мимо Claude: на диск — всё без потерь, в вывод команд — сжатая сводка
- Любая правка проекта — через `reamix do`: один undo-блок, откат при ошибке, снимки до и после
- Треки и плагины нумеруются с 1 (как в интерфейсе), параметры плагинов — с 0 (как в API)
- Подделка в тестах проверяет нашу логику, а не REAPER: новое обращение к API проверять вживую
- Пробные правки в живом проекте откатывать (`reamix undo`); служебные действия — через
  `rx.scratch`, чтобы не оставлять записей в undo
- Параметры плагинов менять только через `rx.fx_set`: он проверяет, что плагин принял значение

## Документация
- [docs/architecture.md](docs/architecture.md) · [docs/DECISIONS.md](docs/DECISIONS.md) · [docs/CURRENT.md](docs/CURRENT.md)
- Проект из мультитрека — основной способ начать трек: [docs/features/multitrack.md](docs/features/multitrack.md)
- Импорт из Ableton: [docs/features/ableton-import.md](docs/features/ableton-import.md) — соответствие, форматы, что не проверено
