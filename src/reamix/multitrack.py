"""Проект REAPER из папки с мультитреком: reamix multitrack.

Два шага. Осмотр: что за файлы лежат в папке (длина, каналы, уровень, где звучит, какой
спектр), темп, размер и регионы из .als, если он рядом, и черновик раскладки. Сборка: по
раскладке, которую Claude поправил вместе с человеком, пишется reaper/<имя>.rpp — дорожки
по папкам, файлы остаются на месте. Плагинов нет, REAPER нужен только чтобы открыть проект.
"""

import json
import os
import re
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from . import ableton, client, fmt, paths, rpp

AUDIO = {".wav", ".aif", ".aiff", ".flac", ".mp3", ".ogg"}
# Папки, в которых мультитрека не бывает: наш проект, служебное Ableton.
SKIP_DIRS = {"reaper", "Backup", "Ableton Project Info", "Ableton Folder Info"}
SILENT_DB = -80.0  # файл с пиком ниже — пустой: дорожка была выключена
ACTIVE_BELOW_PEAK = 50.0  # дБ: тише пика настолько — пауза
LOW_HZ, HIGH_HZ = 250.0, 4000.0

# Цвета папок верхнего уровня, по порядку.
COLORS = [(214, 87, 71), (222, 152, 61), (214, 196, 73), (116, 181, 92), (72, 172, 160), (84, 142, 212), (140, 112, 208), (198, 104, 176), (150, 150, 150)]

KEYWORDS = {"темп": "tempo", "tempo": "tempo", "размер": "timesig", "timesig": "timesig", "регион": "region", "region": "region"}


class MultitrackError(client.BridgeError):
    """Ошибка, которую CLI показывает одной строкой."""


# ── Осмотр ───────────────────────────────────────────────────────────────────


def find_files(folder: Path) -> list[Path]:
    """Аудиофайлы мультитрека, пути относительно папки. Samples/ пропускается только в папке проекта Ableton."""
    skip = set(SKIP_DIRS)
    if any(folder.glob("*.als")):
        skip.add("Samples")
    found = []

    def walk(directory: Path, top: bool):
        for entry in sorted(directory.iterdir(), key=lambda p: p.name.lower()):
            if entry.name.startswith("."):
                continue
            if entry.is_dir():
                if not (top and entry.name in skip):
                    walk(entry, False)
            elif entry.suffix.lower() in AUDIO:
                found.append(entry.relative_to(folder))

    walk(folder, True)
    return found


def measure(path: Path) -> dict:
    """Что нужно знать о файле, чтобы понять, что это за дорожка, не слушая её."""
    import numpy as np
    import soundfile
    from scipy import signal

    data, sr = soundfile.read(str(path), dtype="float32", always_2d=True)
    seconds = len(data) / sr
    info = {"seconds": seconds, "sr": sr, "channels": data.shape[1]}
    peak = float(np.max(np.abs(data))) if len(data) else 0.0
    info["peak_db"] = float(20 * np.log10(peak)) if peak > 0 else -200.0
    if info["peak_db"] < SILENT_DB:
        return info | {"silent": True}

    mono = data.mean(axis=1)
    rms = float(np.sqrt(np.mean(mono**2)))
    info["rms_db"] = float(20 * np.log10(max(rms, 1e-10)))
    info["crest"] = info["peak_db"] - info["rms_db"]  # большой — ударное, малый — ровный звук
    # Стерео, в котором оба канала одинаковы, — моно-источник.
    info["dual_mono"] = bool(data.shape[1] >= 2 and float(np.max(np.abs(data[:, 0] - data[:, 1]))) < peak * 1e-3)

    step = max(int(sr * 0.1), 1)
    frames = np.sqrt(np.mean(mono[: len(mono) // step * step].reshape(-1, step) ** 2, axis=1)) if len(mono) >= step else np.array([rms])
    active = np.nonzero(20 * np.log10(np.maximum(frames, 1e-10)) > info["peak_db"] - ACTIVE_BELOW_PEAK)[0]
    info["start"] = float(active[0] * 0.1) if len(active) else 0.0
    info["end"] = min(float((active[-1] + 1) * 0.1), seconds) if len(active) else seconds
    info["active_share"] = len(active) / max(len(frames), 1)

    freqs, power = signal.welch(mono, sr, nperseg=min(8192, len(mono)))
    total = float(power.sum()) or 1.0
    info["low"] = float(power[freqs < LOW_HZ].sum()) / total
    info["high"] = float(power[freqs >= HIGH_HZ].sum()) / total
    return info


def clean_name(stem: str, prefix: str) -> str:
    """Имя дорожки из имени файла: без общего начала (при экспорте Ableton ставит туда имя проекта)."""
    name = stem[len(prefix):] if prefix and stem.startswith(prefix) and len(stem) > len(prefix) else stem
    return name.strip(" -_") or stem


def common_prefix(stems: list[str]) -> str:
    """Начало до границы слова, общее почти для всех имён: «Song KICK», «Song SNARE» → «Song ».
    Пара посторонних файлов в папке не мешает: хватает четырёх имён из пяти."""
    counts: dict[str, int] = {}
    for stem in stems:
        for m in re.finditer(r"[ _-]+", stem):
            counts[stem[: m.end()]] = counts.get(stem[: m.end()], 0) + 1
    shared = [p for p, n in counts.items() if n >= 2 and n >= 0.8 * len(stems)]
    return max(shared, key=len, default="")


def read_als(folder: Path) -> dict | None:
    """Темп, размер, регионы и группы дорожек из проекта Ableton, если он лежит в папке."""
    files = sorted(folder.glob("*.als"), key=lambda p: p.stat().st_mtime, reverse=True)
    if not files:
        return None
    try:
        project = ableton.load(files[0])
    except ableton.AlsError as e:
        return {"file": files[0].name, "error": str(e)}
    master = project.master
    tempo_id = next((tid for tid, t in master.targets.items() if t == ("tempo",)), None)
    values = [p.value for p in master.automation.get(tempo_id, [])] or [project.tempo]
    bpm = round(values[0], 3)
    by_id = {t.id: t for t in project.tracks}

    def group_path(track: ableton.Track) -> list[str]:
        out = []
        while track.group in by_id:
            track = by_id[track.group]
            out.insert(0, track.name)
        return out

    beats = [(beat, name) for beat, name in project.locators if beat >= 0]
    return {
        "file": files[0].name, "others": [f.name for f in files[1:]], "live": project.creator,
        "tempo": bpm, "tempo_changes": len({round(v, 3) for v in values}) > 1, "timesig": list(project.timesig),
        # Локатор Ableton — точка; регион идёт от него до следующего локатора.
        "locators": [{"time": beat * 60 / bpm, "name": name} for beat, name in beats],
        "tracks": [{"name": t.name, "kind": t.kind, "groups": group_path(t), "on": t.on} for t in project.tracks + project.returns],
    }


def scan(folder: Path) -> dict:
    files = find_files(folder)
    if not files:
        raise MultitrackError(f"в папке нет аудиофайлов: {folder}")
    data = folder / "reaper" / paths.TRACK_DATA / "multitrack"
    data.mkdir(parents=True, exist_ok=True)
    cache_file = data / "scan.json"
    try:
        cache = {f["path"]: f for f in json.loads(cache_file.read_text())["files"]}
    except (OSError, ValueError, KeyError):
        cache = {}

    prefix = common_prefix([f.stem for f in files])
    out = []
    for rel in files:
        stat = (folder / rel).stat()
        stamp = [stat.st_size, int(stat.st_mtime)]
        entry = cache.get(str(rel))
        if not entry or entry.get("stamp") != stamp:
            try:
                entry = measure(folder / rel)
            except Exception as e:  # libsndfile называет причину по-своему для каждого формата
                entry = {"unreadable": str(e)[:120]}
            entry |= {"path": str(rel), "stamp": stamp}
        entry.pop("als", None)
        entry["name"] = clean_name(rel.stem, prefix)
        out.append(entry)

    als = read_als(folder)
    if als and "tracks" in als:
        names: dict[str, list[dict]] = {}
        for t in als["tracks"]:
            names.setdefault(t["name"].casefold(), []).append(t)
        for entry in out:
            found = names.get(entry["name"].casefold(), [])
            if len(found) == 1:  # одноимённые дорожки по имени файла не различить
                entry["als"] = found[0]
    result = {"folder": str(folder), "files": out, "als": als}
    cache_file.write_text(json.dumps(result, ensure_ascii=False, indent=1))
    return result


def good(files: list[dict]) -> list[dict]:
    return [f for f in files if not f.get("silent") and not f.get("unreadable")]


def _stamp(seconds: float) -> str:
    """Время для раскладки: с миллисекундами, чтобы регион встал точно на долю."""
    return f"{int(seconds // 60)}:{seconds % 60:06.3f}"


def draft(result: dict) -> str:
    """Черновик раскладки: темп и регионы из .als, дорожки — по группам Ableton или по подпапкам."""
    als = result["als"] if result["als"] and "tracks" in result["als"] else None
    files = good(result["files"])
    length = max((f["seconds"] for f in files), default=0.0)
    lines = [
        "# Раскладка проекта. Строка без «<-» — папка, отступ в два пробела — вложенность.",
        "# «Имя <- файл» — дорожка: имя можно менять, файл — нет. Строки с # не читаются.",
        f"темп {als['tempo']:g}" if als else "темп ?",
        f"размер {als['timesig'][0]}/{als['timesig'][1]}" if als else "размер 4/4",
    ]
    if als and als["locators"]:
        marks = als["locators"]
        if marks[0]["time"] > 0.5:
            marks = [{"time": 0.0, "name": "начало"}] + marks
        for mark, nxt in zip(marks, marks[1:] + [{"time": length}]):
            if nxt["time"] - mark["time"] > 0.05:
                lines.append(f"регион {_stamp(mark['time'])} {_stamp(nxt['time'])} {mark['name'] or '?'}")
    lines.append("")

    tree: dict = {}
    for f in files:
        groups = f["als"]["groups"] if f.get("als") else list(Path(f["path"]).parts[:-1])
        node = tree
        for g in groups:
            node = node.setdefault(g, {})
        node.setdefault("", []).append(f)

    def emit(node: dict, depth: int):
        for f in node.get("", []):
            lines.append("  " * depth + f"{f['name']} <- {f['path']}")
        for name, child in node.items():
            if name:
                lines.append("  " * depth + name)
                emit(child, depth + 1)

    emit(tree, 0)
    return "\n".join(lines) + "\n"


def _channels(f: dict) -> str:
    if f["channels"] == 1:
        return "моно"
    return "стерео=моно" if f.get("dual_mono") else "стерео" if f["channels"] == 2 else f"{f['channels']} кан"


def report(result: dict, layout: Path) -> str:
    files, usable = result["files"], good(result["files"])
    length = max((f["seconds"] for f in usable), default=0.0)
    rates = sorted({f["sr"] for f in usable})
    lines = [f"мультитрек: {result['folder']} · файлов {len(files)} · {fmt.clock(length)} · {'/'.join(str(r) for r in rates)} Гц"]
    als = result["als"]
    if als and "error" in als:
        lines.append(f".als: {als['file']} не прочитан — {als['error']}")
    elif als:
        lines.append(f".als: {als['file']} · темп {als['tempo']:g} · размер {als['timesig'][0]}/{als['timesig'][1]} · локаторов {len(als['locators'])}")
    else:
        lines.append(".als в папке нет: темп и размер надо спросить, регионов нет")

    width = min(max((len(f["path"]) for f in files), default=10), 46)
    lines.append(f" №  {'файл':<{width}}  длина  каналы        пик    RMS  crest  звучит        низ/верх %" + ("  в Ableton" if als and "tracks" in als else ""))
    for i, f in enumerate(files, 1):
        path = f["path"] if len(f["path"]) <= width else "…" + f["path"][-(width - 1):]
        if f.get("unreadable"):
            lines.append(f"{i:>3} {path:<{width}}  не читается: {f['unreadable']}")
            continue
        if f.get("silent"):
            lines.append(f"{i:>3} {path:<{width}}  {fmt.clock(f['seconds']):>5}  пустой")
            continue
        heard = f"{fmt.clock(f['start'])}–{fmt.clock(f['end'])}"
        origin = ("  " + " › ".join(f["als"]["groups"] + [f["als"]["kind"]]) if f.get("als") else "") if als and "tracks" in als else ""
        lines.append(
            f"{i:>3} {path:<{width}}  {fmt.clock(f['seconds']):>5}  {_channels(f):<11} {f['peak_db']:>6.1f} {f['rms_db']:>6.1f} {f['crest']:>6.1f}  "
            f"{heard:<13} {f['low'] * 100:>3.0f}/{f['high'] * 100:<3.0f}{origin}"
        )

    notes = []
    empty = [f["path"] for f in files if f.get("silent")]
    if empty:
        notes.append(f"пустые, в раскладку не вошли ({len(empty)}): " + ", ".join(empty[:8]) + (" …" if len(empty) > 8 else ""))
    broken = [f["path"] for f in files if f.get("unreadable")]
    if broken:
        notes.append(f"не читаются ({len(broken)}): " + ", ".join(broken[:8]))
    hot = [f["path"] for f in usable if f["peak_db"] > 0.05]
    if hot:
        notes.append(f"пик выше 0 дБFS ({len(hot)}): " + ", ".join(hot[:6]) + " — в формате float это не искажение, но запас по уровню нужен")
    if len(rates) > 1:
        notes.append("частота дискретизации разная: " + ", ".join(f"{r} Гц — {sum(f['sr'] == r for f in usable)}" for r in rates))
    odd = [f for f in usable if abs(f["seconds"] - length) > 1.0]
    if odd and len(odd) < len(usable):
        notes.append(f"короче остальных ({len(odd)}) — проверь, что начинаются с начала трека: " + ", ".join(f"{f['path']} {fmt.clock(f['seconds'])}" for f in odd[:6]))
    if als and als.get("tempo_changes"):
        notes.append("в .als темп меняется по ходу трека — взят начальный; сетка REAPER с записью разойдётся")
    if als and als.get("others"):
        notes.append(f"в папке несколько .als — взят самый свежий; остальные: {', '.join(als['others'])}")
    if notes:
        lines.append("внимание:")
        lines += ["  " + n for n in notes]
    lines.append(f"черновик раскладки: {layout}")
    return "\n".join(lines)


# ── Раскладка ────────────────────────────────────────────────────────────────


@dataclass
class Layout:
    tempo: float | None = None
    timesig: tuple[int, int] = (4, 4)
    regions: list[tuple[float, float, str]] = field(default_factory=list)
    rows: list[tuple[int, str, str | None]] = field(default_factory=list)  # глубина, имя, файл (None — папка)


def _seconds(text: str) -> float:
    m = re.fullmatch(r"(?:(\d+):)?(\d+(?:\.\d+)?)", text)
    if not m:
        raise ValueError(text)
    return int(m.group(1) or 0) * 60 + float(m.group(2))


def parse_layout(text: str) -> Layout:
    layout = Layout()
    for n, raw in enumerate(text.splitlines(), 1):
        line = raw.rstrip()
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        fail = lambda why: MultitrackError(f"раскладка, строка {n}: {why} — «{raw.strip()}»")  # noqa: E731
        indent = len(line) - len(line.lstrip(" \t"))
        body = line.strip()
        word, _, rest = body.partition(" ")
        key = KEYWORDS.get(word.lower()) if indent == 0 and "<-" not in body else None
        try:
            if key == "tempo":
                layout.tempo = None if rest.strip() == "?" else float(rest.replace(",", "."))
                continue
            if key == "timesig":
                num, den = rest.split("/")
                layout.timesig = (int(num), int(den))
                continue
            if key == "region":
                start, end, *name = rest.split(None, 2)
                layout.regions.append((_seconds(start), _seconds(end), name[0] if name else ""))
                continue
        except ValueError:
            raise fail("не разобрать") from None
        depth = line[:indent].count("\t") + line[:indent].count(" ") // 2
        if "<-" in body:
            name, _, file = body.partition("<-")
            file = file.strip()
            if not file:
                raise fail("после «<-» нет файла")
            layout.rows.append((depth, name.strip() or Path(file).stem, file))
        else:
            layout.rows.append((depth, body, None))
        parent = next((r for r in reversed(layout.rows[:-1]) if r[0] < depth), None)
        if depth > 0 and (parent is None or parent[0] != depth - 1 or parent[2] is not None):
            raise fail("отступ не под папкой")
    return layout


def assemble(layout: Layout, result: dict, out_dir: Path) -> tuple[rpp.Project, list[str]]:
    """Проект REAPER по раскладке. Возвращает проект и замечания."""
    folder = Path(result["folder"])
    known = {f["path"]: f for f in result["files"]}
    if layout.tempo is None:
        raise MultitrackError("в раскладке не задан темп («темп ?»): спроси у человека и впиши")
    used, notes = set(), []
    project = rpp.Project(layout.tempo, layout.timesig, regions=sorted(layout.regions))
    top, color = -1, None
    rows = [r for i, r in enumerate(layout.rows) if r[2] is not None or (i + 1 < len(layout.rows) and layout.rows[i + 1][0] > r[0])]
    if len(rows) < len(layout.rows):
        notes.append("пустые папки пропущены: " + ", ".join(r[1] for r in layout.rows if r not in rows))
    for i, (depth, name, file) in enumerate(rows):
        after = rows[i + 1][0] if i + 1 < len(rows) else 0
        if depth == 0:
            top += file is None
            color = COLORS[top % len(COLORS)] if file is None else None
        track = rpp.Track(name, color, folder=(1, 1) if after > depth else (2, after - depth) if after < depth else (0, 0))
        if file is not None:
            info = known.get(file)
            if info is None or not (folder / file).is_file():
                raise MultitrackError(f"файла из раскладки нет в папке: {file}")
            if file in used:
                raise MultitrackError(f"файл стоит в раскладке дважды: {file}")
            if info.get("silent") or info.get("unreadable"):
                raise MultitrackError(f"файл пустой или не читается, убери его из раскладки: {file}")
            used.add(file)
            track.items.append(rpp.Item(0.0, info["seconds"], name, file=os.path.relpath(folder / file, out_dir)))
        project.tracks.append(track)
    left = [f["path"] for f in good(result["files"]) if f["path"] not in used]
    if left:
        notes.append(f"не вошли в раскладку ({len(left)}): " + ", ".join(left[:8]) + (" …" if len(left) > 8 else ""))
    if not used:
        raise MultitrackError("в раскладке нет ни одной дорожки")
    return project, notes


# ── Команда ──────────────────────────────────────────────────────────────────


def open_in_reaper(rpp_path: Path, timeout: float) -> dict | None:
    """Открывает проект в REAPER, если там активна пустая вкладка; иначе его открывает человек."""
    if client.alive() is None:
        return None
    load = f'local imp = loadfile("{paths.ROOT / "bridge" / "import.lua"}")(rx, A) '
    token = uuid.uuid4().hex
    try:
        client.call(load + f'return imp.workbench("{token}")', timeout=timeout)
        path = str(rpp_path).replace("\\", "\\\\").replace('"', '\\"')
        return client.call(load + f'return imp.open("{token}", "{path}", {{}})', timeout=max(timeout, 300))
    except client.BridgeError:
        return None


def run(args) -> int:
    folder = Path(args.folder).expanduser().resolve()
    if not folder.is_dir():
        raise MultitrackError(f"папки нет: {folder}")
    out_dir = folder / "reaper"
    layout_file = Path(args.layout).expanduser() if args.layout else out_dir / paths.TRACK_DATA / "multitrack" / "layout.txt"
    result = scan(folder)

    if not args.build:
        if args.layout is None and (not layout_file.exists() or args.force):
            layout_file.write_text(draft(result))
        print(report(result, layout_file))
        return 0

    if not layout_file.is_file():
        raise MultitrackError(f"раскладки нет: {layout_file} — сначала reamix multitrack без --build")
    rpp_path = out_dir / f"{folder.name}.rpp"
    if rpp_path.exists() and not args.force:
        raise MultitrackError(f"проект уже есть: {rpp_path} — в нём может быть начатый микс. Перезаписать — с --force")
    project, notes = assemble(parse_layout(layout_file.read_text()), result, out_dir)
    rpp_path.write_text(rpp.render(project))

    tracks = sum(1 for t in project.tracks if t.items)
    print(f"{rpp_path}")
    print(
        f"дорожек {tracks} · папок {len(project.tracks) - tracks} · темп {project.tempo:g} · размер {project.timesig[0]}/{project.timesig[1]}"
        f" · регионов {len(project.regions)}"
    )
    for note in notes:
        print("  " + note)
    opened = None if args.no_open else open_in_reaper(rpp_path, args.timeout)
    if opened:
        print(f"проект открыт в REAPER: дорожек {opened['tracks']}, айтемов {opened['items']}")
        if opened["offline"]:
            print(f"  REAPER не открыл файлы ({len(list(opened['offline']))}): " + ", ".join(Path(f).name for f in list(opened["offline"])[:6]))
    else:
        print("в REAPER проект не открыт: нужна пустая активная вкладка — либо открой файл сам (File → Open project)")
    return 0
