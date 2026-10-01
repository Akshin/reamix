"""Каталог установленных плагинов.

Источник — списки, которые REAPER сам собирает при сканировании папок плагинов
(reaper-vstplugins, reaper-auplugins, reaper-clap, reaper-jsfx), сверенные с
файлами на диске. Категории — из reaper-fxtags.ini.
"""

import configparser
import csv
import re
from dataclasses import dataclass, field
from pathlib import Path

from . import paths

# Порядок предпочтения форматов, когда плагин стоит в нескольких.
FORMATS = ["VST3", "VST", "CLAP", "AU", "JS"]
COLUMNS = ["category", "name", "vendor", "kind", "formats", "add", "recent"]

_CHANNELS = re.compile(r" \((\d+ out|\d+ch|\d+->\d+ch)\)$")


@dataclass
class Plugin:
    name: str
    vendor: str
    fmt: str
    add: str
    instrument: bool = False
    key: str = ""  # ключ в файлах REAPER: имя файла или «Vendor: Name»
    category: str = ""
    recent: int | None = None
    formats: list[str] = field(default_factory=list)


def cache_key(filename: str) -> str:
    """Имя файла плагина в том виде, в каком REAPER хранит его в своих списках."""
    return re.sub(r"[^A-Za-z0-9.]", "_", filename)


def _first(pattern: str, skip: str = "") -> Path | None:
    """Самый свежий из подходящих списков: на машине бывают списки от разных сборок REAPER."""
    found = [p for p in paths.REAPER.glob(pattern) if not skip or skip not in p.name]
    return max(found, key=lambda p: p.stat().st_mtime, default=None)


def _ini(path: Path) -> configparser.ConfigParser:
    ini = configparser.ConfigParser(interpolation=None, strict=False, delimiters=("=",), comment_prefixes=())
    ini.optionxform = str
    ini.read_string(path.read_text(errors="replace"))
    return ini


def _split_name(text: str) -> tuple[str, str]:
    """«Pro-C 2 (FabFilter) (8ch)» → («Pro-C 2», «FabFilter»)."""
    while _CHANNELS.search(text):
        text = _CHANNELS.sub("", text)
    if not text.endswith(")"):
        return text, ""
    # Производитель — последняя скобочная группа; внутри неё бывают свои скобки.
    depth = 0
    for i in range(len(text) - 1, -1, -1):
        depth += {")": 1, "(": -1}.get(text[i], 0)
        if depth == 0:
            return (text[:i].rstrip(), text[i + 1 : -1]) if i > 0 else (text, "")
    return text, ""


def _vst() -> list[Plugin]:
    path = _first("reaper-vstplugins*.ini")
    if not path:
        return []
    out = []
    for key, value in _ini(path)["vstcache"].items():
        parts = value.split(",", 2)
        if len(parts) < 3 or parts[2] == "<SHELL>":
            continue
        display = parts[2]
        instrument = display.endswith("!!!VSTi")
        display = display.removesuffix("!!!VSTi")
        name, vendor = _split_name(display)
        fmt = "VST3" if key.split("<")[0].endswith(".vst3") else "VST"
        pinned = f"{name} ({vendor})" if vendor else name
        out.append(Plugin(name, vendor, fmt, f"{fmt}:{pinned}", instrument, key))
    return out


def _au() -> list[Plugin]:
    path = _first("reaper-auplugins*.ini", skip="-bc")
    if not path:
        return []
    out = []
    for key, value in _ini(path)["auplugins"].items():
        vendor, _, name = key.partition(": ")
        if not name:
            name, vendor = vendor, ""
        out.append(Plugin(name, vendor, "AU", f"AU:{key}", value == "<inst>", key))
    return out


def _clap() -> list[Plugin]:
    path = _first("reaper-clap-*.ini")
    if not path:
        return []
    out = []
    ini = _ini(path)
    for section in ini.sections():
        for key, value in ini[section].items():
            if key == "_" or "|" not in value:
                continue
            flag, display = value.split("|", 1)
            name, vendor = _split_name(display)
            out.append(Plugin(name, vendor, "CLAP", f"CLAP:{name}", flag == "1", section))
    return out


def _jsfx() -> list[Plugin]:
    path = paths.REAPER / "reaper-jsfx.ini"
    if not path.is_file():
        return []
    out = []
    for line in path.read_text(errors="replace").splitlines():
        m = re.match(r'NAME (?:"([^"]+)"|(\S+)) "(?:JS: )?(.*)"$', line)
        if not m:
            continue
        file = m.group(1) or m.group(2)
        name, vendor = _split_name(m.group(3))
        out.append(Plugin(name, vendor, "JS", f"JS:{file}", False, file, category=file.split("/")[0]))
    return out


def _on_disk() -> set[str]:
    """Ключи VST- и CLAP-плагинов, файлы которых реально лежат в папках плагинов."""
    roots = [base / "Audio/Plug-Ins" / kind for base in (Path("/Library"), Path.home() / "Library") for kind in ("VST", "VST3", "CLAP")]
    found = set()

    def walk(folder: Path):
        try:
            entries = list(folder.iterdir())
        except OSError:
            return
        for entry in entries:
            if entry.suffix in (".vst", ".vst3", ".clap"):
                found.add(cache_key(entry.name))
            elif entry.is_dir():
                walk(entry)

    for root in roots:
        walk(root)
    return found


def _recent() -> dict[str, int]:
    """Недавно использованные плагины: ключ → место в списке (0 — самый свежий)."""
    path = paths.REAPER / "reaper-recentfx.ini"
    if not path.is_file():
        return {}
    out = {}
    for line in path.read_text(errors="replace").splitlines():
        m = re.match(r"Name(\d+)=(.*)", line)
        if not m:
            continue
        value = m.group(2)
        if value.startswith("/"):
            value = cache_key(Path(value.split("<")[0]).name)
        out.setdefault(value, int(m.group(1)))
    return out


def scan() -> tuple[list[Plugin], dict]:
    """Собирает каталог. Возвращает плагины (по одному на плагин, а не на формат) и сводку."""
    found = _vst() + _au() + _clap() + _jsfx()
    tags = _ini(paths.REAPER / "reaper-fxtags.ini") if (paths.REAPER / "reaper-fxtags.ini").is_file() else None
    categories = dict(tags["category"]) if tags and tags.has_section("category") else {}
    disk, recent = _on_disk(), _recent()

    present, missing = [], []
    for p in found:
        file = p.key.split("<")[0]
        builtin = file.endswith(".dylib")
        if p.fmt in ("VST3", "VST", "CLAP") and not builtin and cache_key(file) not in disk:
            missing.append(p)
            continue
        p.category = p.category or categories.get(p.key.split("<")[0], "")
        p.recent = recent.get(p.key.split("<")[0])
        present.append(p)

    # Один и тот же плагин в разных форматах — одна запись. В VST2 и AU имя часто
    # идёт с производителем в начале («FabFilter Pro-C 2»), в VST3 — без него.
    groups: dict[tuple, list[Plugin]] = {}
    for p in present:
        base = p.name
        if p.vendor and base.lower().startswith(p.vendor.lower() + " "):
            base = base[len(p.vendor) + 1 :]
        groups.setdefault((base.lower(), p.vendor.lower()), []).append(p)

    plugins = []
    for members in groups.values():
        members.sort(key=lambda p: FORMATS.index(p.fmt))
        best = members[0]
        best.formats = [p.fmt for p in members]
        best.category = next((p.category for p in members if p.category), "")
        ranks = [p.recent for p in members if p.recent is not None]
        best.recent = min(ranks) if ranks else None
        plugins.append(best)
    plugins.sort(key=lambda p: (p.category.lower() or "~", p.vendor.lower(), p.name.lower()))

    known = {cache_key(p.key.split("<")[0]) for p in found}
    summary = {
        "plugins": len(plugins),
        "missing": sorted({p.key.split("<")[0] for p in missing}),
        "unscanned": sorted(disk - known),
    }
    return plugins, summary


def save(plugins: list[Plugin], path: Path | None = None) -> None:
    path = path or paths.CATALOG
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="") as f:
        w = csv.writer(f, delimiter="\t", lineterminator="\n")
        w.writerow(COLUMNS)
        for p in plugins:
            w.writerow([
                p.category, p.name, p.vendor, "inst" if p.instrument else "fx",
                ",".join(p.formats), p.add, "" if p.recent is None else p.recent,
            ])


def load(path: Path | None = None) -> list[dict]:
    with open(path or paths.CATALOG, newline="") as f:
        return list(csv.DictReader(f, delimiter="\t"))


def find(rows: list[dict], terms: list[str], instruments: bool = False) -> list[dict]:
    """Строки каталога, где каждое слово запроса встречается в имени, производителе или категории."""
    out = []
    for row in rows:
        if row["kind"] == "inst" and not instruments:
            continue
        text = f"{row['name']} {row['vendor']} {row['category']}".lower()
        if all(t.lower() in text for t in terms):
            out.append(row)
    # Недавно использованные — первыми: это то, чем пользователь реально сводит.
    out.sort(key=lambda row: int(row["recent"]) if row["recent"] != "" else 10**6)
    return out


def line(row: dict) -> str:
    mark = "*" if row["recent"] != "" else " "
    vendor = f" ({row['vendor']})" if row["vendor"] else ""
    return f"{mark} {row['category'] or '-'} | {row['name']}{vendor} | {row['formats']} | {row['add']}"
