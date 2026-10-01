"""Перенос проекта Ableton Live в проект REAPER: reamix import-als.

.als → модель → .rpp: дорожки, папки, клипы, посылы, огибающие микшера и темп пишутся
текстом, без REAPER. Блоки плагинов с состоянием и автоматизацией параметров собирает
живой REAPER в пустой вкладке-верстаке (bridge/import.lua) — они вставляются в тот же
.rpp, и готовый проект открывается на месте верстака.
Что переносится точно, что приблизительно и что нет — docs/features/ableton-import.md.
"""

import base64
import bisect
import functools
import json
import math
import mmap
import os
import re
import shutil
import struct
import uuid
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

from . import ableton, client, paths, plugins, rpp

TEMPO_STEP = 0.25  # доли: мельче этого плавное изменение темпа на ступени не дробится
TEMPO_JUMP = 0.25  # уд/мин: крупнее этого ступень не делается
MAX_RUNS = 2000  # проходов зацикленного клипа, которые разворачиваются в айтемы
CURVE_STEPS = 8  # отрезков, которыми заменяется кривая автоматизации

# Палитра цветов Live 10–12: номер цвета дорожки → RGB.
PALETTE = [int(c, 16) for c in (
    "FF94A6 FFA529 CC9927 F7F47C BFFB00 1AFF2F 25FFA8 5CFFE8 8BC5FF 5480E4 92A7FF D86CE4 E553A0 FFFFFF "
    "FF3636 F66C03 99724B FFF034 87FF67 3DC300 00BFAF 19E9FF 10A4EE 007DC0 886CE4 B677C6 FF39D4 D0D0D0 "
    "E2675A FFA374 D3AD71 EDFFAE D2E498 BAD074 9BC48D D4FDE1 CDF1F8 B9C1E3 CDBBE4 AE98E5 E5DCE1 A9A9A9 "
    "C6928B B78256 99836A BFBA69 A6BE00 7DB04D 88C2BA 9BB3C4 85A5C2 8393CC A595B5 BF9FBE BC7196 7B7B7B "
    "AF3333 A95131 724F41 DBC300 85961F 539F31 0A9C8E 236384 1A2F96 2F52A2 624BAD A34BAD CC2E6E 3C3C3C"
).split()]


class ImportFailed(client.BridgeError):
    """Ошибка импорта, которую CLI показывает одной строкой."""


# ── Темп ─────────────────────────────────────────────────────────────────────


class TempoMap:
    """Доли ↔ секунды. Плавные изменения темпа заменены ступенями: тогда наш расчёт и
    темповая карта REAPER совпадают точно, а не с точностью до формы кривой."""

    def __init__(self, bpm: float, points: list[ableton.Point] | None = None):
        segments = self._segments(bpm, points or [])
        self.beats = [b for b, _ in segments]
        self.bpms = [v for _, v in segments]
        self.times = [0.0]
        for i in range(1, len(segments)):
            self.times.append(self.times[-1] + (self.beats[i] - self.beats[i - 1]) * 60 / self.bpms[i - 1])

    @staticmethod
    def _segments(bpm: float, points: list[ableton.Point]) -> list[tuple[float, float]]:
        if not points:
            return [(0.0, bpm)]
        out: list[tuple[float, float]] = []

        def add(beat: float, value: float):
            if out and out[-1][0] == beat:
                out[-1] = (beat, value)
            elif not out or out[-1][1] != value:
                out.append((beat, value))

        for a, b in zip(points, points[1:]):
            lo = max(a.beat, 0.0)
            if b.beat <= lo:
                continue
            if a.value == b.value or a.beat <= ableton.BEFORE_START:
                add(lo, a.value)
                continue
            span = b.beat - lo
            steps = max(1, min(math.ceil(abs(b.value - a.value) / TEMPO_JUMP), math.ceil(span / TEMPO_STEP)))
            for k in range(steps):
                middle = lo + span * (k + 0.5) / steps
                add(lo + span * k / steps, a.value + (b.value - a.value) * (middle - a.beat) / (b.beat - a.beat))
        add(max(points[-1].beat, 0.0), points[-1].value)
        if out[0][0] > 0:  # до первой точки действует её значение
            if out[0][1] == points[0].value:
                out[0] = (0.0, out[0][1])
            else:
                out.insert(0, (0.0, points[0].value))
        return out

    def time(self, beat: float) -> float:
        i = max(bisect.bisect_right(self.beats, beat) - 1, 0)
        return self.times[i] + (beat - self.beats[i]) * 60 / self.bpms[i]

    def boundaries(self, b0: float, b1: float) -> list[float]:
        """Доли строго между b0 и b1, в которых меняется темп."""
        return self.beats[bisect.bisect_right(self.beats, b0) : bisect.bisect_left(self.beats, b1)]

    def summary(self) -> str:
        if len(self.bpms) == 1:
            return f"{self.bpms[0]:.4g}"
        return f"{min(self.bpms):.4g}–{max(self.bpms):.4g}, изменений {len(self.bpms) - 1}"


# ── Автоматизация ────────────────────────────────────────────────────────────


def simplify(line: list[tuple[float, float]], eps: float) -> list[tuple[float, float]]:
    """Убирает точки, лежащие на прямой между соседями (Рамер — Дуглас — Пекер)."""
    if len(line) < 3:
        return line
    keep = [False] * len(line)
    keep[0] = keep[-1] = True
    stack = [(0, len(line) - 1)]
    while stack:
        i, j = stack.pop()
        (x0, y0), (x1, y1) = line[i], line[j]
        worst, at = eps, None
        for k in range(i + 1, j):
            x, y = line[k]
            off = abs(y - (y0 + (y1 - y0) * (x - x0) / (x1 - x0))) if x1 != x0 else abs(y - y0)
            if off > worst:
                worst, at = off, k
        if at is not None:
            keep[at] = True
            stack += [(i, at), (at, j)]
    return [p for p, k in zip(line, keep) if k]


def _bezier(a: ableton.Point, b: ableton.Point) -> list[tuple[float, float]]:
    c1x, c1y, c2x, c2y = a.curve
    out = []
    for k in range(1, CURVE_STEPS):
        t = k / CURVE_STEPS
        u = 1 - t
        x = 3 * u * u * t * c1x + 3 * u * t * t * c2x + t**3
        y = 3 * u * u * t * c1y + 3 * u * t * t * c2y + t**3
        out.append((a.beat + (b.beat - a.beat) * x, a.value + (b.value - a.value) * y))
    return sorted(out)


def envelope(points: list[ableton.Point], tempo: TempoMap, *, step: bool = False, value=lambda v: v) -> list[tuple[float, float, int]]:
    """Точки автоматизации Ableton (доли) → точки огибающей REAPER (секунды, значение, форма)."""
    if not points:
        return []
    if step:
        out: list[tuple[float, float, int]] = []
        for p in points:
            t, v = tempo.time(max(p.beat, 0.0)), value(p.value)
            if out and abs(out[-1][0] - t) < 1e-9:
                out[-1] = (t, v, 1)
            elif not out or out[-1][1] != v:
                out.append((t, v, 1))
        return out

    # Ломаная в долях; повтор доли — скачок значения.
    line: list[tuple[float, float]] = []
    for a, b in zip(points, points[1:]):
        line.append((a.beat, a.value))
        if a.curve and b.beat > a.beat and b.value != a.value:
            line += _bezier(a, b)
    line.append((points[-1].beat, points[-1].value))

    # Всё, что до начала аранжировки, сводится к значению в нуле.
    before = [p for p in line if p[0] <= 0]
    after = [p for p in line if p[0] > 0]
    if before:
        b0, v0 = before[-1]
        if after and b0 > ableton.BEFORE_START and b0 < 0:
            b1, v1 = after[0]
            v0 += (v1 - v0) * (0 - b0) / (b1 - b0)
        line = [(0.0, v0)] + after

    out, shift = [], 0
    for (b0, v0), (b1, v1) in zip(line, line[1:] + [line[-1]]):
        t0 = tempo.time(b0)
        if out and t0 - out[-1][0] < 1e-9:
            shift += 1  # вторая точка в то же время — скачок: сдвигаем на микросекунду, чтобы порядок не потерялся
        else:
            shift = 0
        out.append((t0 + shift * 1e-6, value(v0), 0))
        knees = tempo.boundaries(b0, b1) if v1 != v0 else []
        if knees:
            # Линия, прямая в долях, на смене темпа в секундах ломается.
            segment = [(tempo.time(b0), value(v0))] + [(tempo.time(x), value(v0 + (v1 - v0) * (x - b0) / (b1 - b0))) for x in knees] + [(tempo.time(b1), value(v1))]
            out += [(t, v, 0) for t, v in simplify(segment, 1e-3 * abs(value(v1) - value(v0)))[1:-1]]
    return out


# ── Клипы ────────────────────────────────────────────────────────────────────


def warp(markers: list[tuple[float, float]], beat: float) -> float:
    """Место в файле (секунды) для доли клипа: между варп-маркерами — линейно, за краями — по крайнему отрезку."""
    i = min(max(bisect.bisect_right([b for _, b in markers], beat) - 1, 0), len(markers) - 2)
    (s0, b0), (s1, b1) = markers[i], markers[i + 1]
    return s0 + (s1 - s0) * (beat - b0) / (b1 - b0)


def unwarp(markers: list[tuple[float, float]], sec: float) -> float:
    i = min(max(bisect.bisect_right([s for s, _ in markers], sec) - 1, 0), len(markers) - 2)
    (s0, b0), (s1, b1) = markers[i], markers[i + 1]
    return b0 + (b1 - b0) * (sec - s0) / (s1 - s0)


def runs(clip: ableton.Clip) -> tuple[list[tuple[float, float, float]], bool]:
    """Проходы клипа: (начало в аранжировке, конец, место в клипе). Зацикленный клип — несколько проходов."""
    looped = clip.loop_on and clip.loop_end - clip.loop_start > 1e-9 and (clip.warped or clip.midi)
    if not looped:
        return [(clip.start, clip.end, clip.loop_start + clip.start_rel)], False
    out, pos, at = [], clip.start, clip.loop_start + clip.start_rel
    while pos < clip.end - 1e-9 and len(out) < MAX_RUNS:
        span = min(clip.loop_end - at, clip.end - pos)
        if span > 1e-9:
            out.append((pos, pos + span, at))
            pos += span
        at = clip.loop_start
    return out, pos < clip.end - 1e-9


def audio_items(clip: ableton.Clip, tempo: TempoMap, file: str) -> tuple[list[rpp.Item], list[str]]:
    notes = []
    t_start, t_end = tempo.time(clip.start), tempo.time(clip.end)
    if not clip.warped or len(clip.markers) < 2 or any(b1 <= b0 or s1 <= s0 for (s0, b0), (s1, b1) in zip(clip.markers, clip.markers[1:])):
        if clip.warped:
            notes.append("варп-маркеры не читаются — клип поставлен без растяжения")
        length = t_end - t_start
        return [rpp.Item(
            t_start, length, clip.name, clip.disabled, min(clip.fade_in, length), min(clip.fade_out, length),
            file, offset=clip.loop_start + clip.start_rel, gain=clip.gain,
        )], notes

    markers = clip.markers
    file_start = unwarp(markers, 0.0)
    passes, cut = runs(clip)
    if cut:
        notes.append(f"цикл клипа развёрнут только на первые {MAX_RUNS} повторов")
    items = []
    for a0, a1, pos in passes:
        if pos < file_start:  # до начала файла Ableton играет тишину
            skip = min(file_start - pos, a1 - a0)
            a0, pos = a0 + skip, pos + skip
        if a1 - a0 <= 1e-9:
            continue
        beats = {a0, a1, *tempo.boundaries(a0, a1)}
        beats |= {a0 + (b - pos) for _, b in markers if pos < b < pos + (a1 - a0)}
        t0 = tempo.time(a0)
        line = simplify([(tempo.time(b) - t0, warp(markers, pos + (b - a0))) for b in sorted(beats)], 1e-6)
        item = rpp.Item(
            t0, line[-1][0], clip.name, clip.disabled, file=file, gain=clip.gain,
            pitch=0.0 if clip.warp_mode == 3 else clip.pitch, preserve_pitch=clip.warp_mode != 3,
        )
        if len(line) == 2:
            item.offset, item.rate = line[0][1], (line[1][1] - line[0][1]) / line[1][0]
        else:
            item.stretch = line
        items.append(item)
    if items:
        if abs(items[0].position - t_start) < 1e-9:
            items[0].fade_in = min(tempo.time(clip.start + clip.fade_in) - t_start, items[0].length)
        items[-1].fade_out = min(t_end - tempo.time(clip.end - clip.fade_out), items[-1].length)
    return items, notes


CONTROLLER_STEPS = 32  # сообщений контроллера на долю при плавном изменении


def controller_message(number: int, value: float) -> tuple[int, int, int]:
    """Сообщение MIDI для цели огибающей клипа: 0 — pitch bend, 1 — давление канала, дальше — контроллер n − 2."""
    if number == 0:
        bend = min(max(round(value) + 8192, 0), 16383)
        return 0xE0, bend & 0x7F, bend >> 7
    level = min(max(round(value), 0), 127)
    return (0xD0, level, 0) if number == 1 else (0xB0, number - 2, level)


def controller_events(number: int, points: list[ableton.Point], pos: float, span: float, tick) -> list[tuple]:
    """Огибающая контроллера на одном проходе клипа [pos, pos + span) → сообщения MIDI. tick — тик для доли клипа."""
    beats = [p.beat for p in points]

    def value_at(beat: float) -> float:
        i = bisect.bisect_right(beats, beat) - 1
        if i < 0:
            return points[0].value
        if i == len(points) - 1 or points[i + 1].beat <= points[i].beat:
            return points[i].value
        a, b = points[i], points[i + 1]
        return a.value + (b.value - a.value) * (beat - a.beat) / (b.beat - a.beat)

    moments = {pos} | {b for b in beats if pos < b < pos + span}
    for a, b in zip(points, points[1:]):
        lo, hi = max(a.beat, pos), min(b.beat, pos + span)
        if hi > lo and a.value != b.value:
            steps = math.ceil((hi - lo) * CONTROLLER_STEPS)
            moments |= {lo + (hi - lo) * k / steps for k in range(1, steps)}
    out, last = [], None
    for beat in sorted(moments):
        message = controller_message(number, value_at(beat))
        if message != last:
            out.append((tick(beat), *message, False))
            last = message
    return out


def midi_item(clip: ableton.Clip, tempo: TempoMap) -> rpp.Item:
    events = []
    for a0, a1, pos in runs(clip)[0]:
        for number, points in clip.controllers.items():
            events += controller_events(number, points, pos, a1 - a0, lambda beat, a0=a0, pos=pos: round((a0 + (beat - pos) - clip.start) * rpp.PPQ))
        for key, time, duration, velocity, enabled in clip.notes:
            if pos - 1e-9 <= time < pos + (a1 - a0) - 1e-9:
                on = a0 + (time - pos)
                off = min(on + duration, a1)  # нота не переживает конец прохода
                tick = round((on - clip.start) * rpp.PPQ)
                events.append((tick, 0x90, key, velocity, not enabled))
                events.append((max(round((off - clip.start) * rpp.PPQ), tick + 1), 0x80, key, 0, not enabled))
    t0 = tempo.time(clip.start)
    return rpp.Item(t0, tempo.time(clip.end) - t0, clip.name, clip.disabled, events=events, ticks=round((clip.end - clip.start) * rpp.PPQ))


# ── Плагины ──────────────────────────────────────────────────────────────────


class Resolver:
    """Имена, под которыми REAPER добавит плагины проекта Ableton. Поиск — по точным
    идентификаторам из списков REAPER; имя годится только чтобы найти плагин в другом формате."""

    def __init__(self):
        self.vst3: dict[str, str] = {}
        self.vst2: dict[str, str] = {}
        self.au: dict[str, str] = {}
        self.au_codes: dict[str, str] = {}
        self.files: dict[str, str] = {}  # имя для добавления → файл плагина, как его записал REAPER
        self._ara: dict[str, bool] = {}
        path = plugins._first("reaper-vstplugins*.ini")
        ini = plugins._ini(path) if path else None
        disk = plugins._on_disk()
        for key, value in (ini["vstcache"].items() if ini and ini.has_section("vstcache") else ()):
            parts = value.split(",", 2)
            file = key.split("<")[0]
            if len(parts) < 3 or parts[2] == "<SHELL>":
                continue
            if not file.endswith(".dylib") and plugins.cache_key(file) not in disk:
                continue  # REAPER помнит плагин, но файла уже нет
            name, vendor = plugins._split_name(parts[2].removesuffix("!!!VSTi"))
            pinned = f"{name} ({vendor})" if vendor else name
            uid, _, guid = parts[1].partition("{")
            if guid:
                self.vst3[guid.upper()] = f"VST3:{pinned}"
                self.files[f"VST3:{pinned}"] = file
            else:
                self.vst2[uid] = f"VST:{pinned}"
        path = plugins._first("reaper-auplugins*.ini", skip="-bc")
        ini = plugins._ini(path) if path else None
        for key in (ini["auplugins"] if ini and ini.has_section("auplugins") else ()):
            self.au[key.lower()] = f"AU:{key}"
        path = plugins._first("reaper-auplugins*-bc.ini")
        for line in path.read_text(errors="replace").splitlines() if path else ():
            m = re.match(r'AU "([^"]+)" "[^"]*" \d+ \d+ (\d+) (\d+) (\d+)', line)
            if m:
                self.au_codes[" ".join(m.groups()[1:])] = f"AU:{m.group(1)}"
        self.by_name = {p.name.lower(): p.add for p in plugins.scan()[0]}

    def find(self, device: ableton.Device) -> tuple[str | None, bool]:
        """(имя для добавления, тот же ли это формат). Другой формат состояние не примет."""
        exact = {
            "VST3": lambda: self.vst3.get(device.uid),
            "VST": lambda: self.vst2.get(device.uid),
            "AU": lambda: self.au.get(f"{device.vendor}: {device.name}".lower()) or self.au_codes.get(device.uid),
        }[device.kind]()
        if exact:
            return exact, True
        return self.by_name.get(device.name.lower()), False

    def may_be_ara(self, add: str) -> bool:
        """Может ли плагин оказаться ARA-плагином (Melodyne, VocAlign, Vovious): в файле VST3 есть
        класс ARA. Признак грубый — в общей оболочке (Waves) он есть у всех плагинов сразу;
        окончательно решает REAPER (import.lua: probe_ara)."""
        file = self.files.get(add)
        if file is None:
            return False
        if file not in self._ara:
            self._ara[file] = False
            for bundle in _vst3_bundles().get(file, ()):
                for binary in (bundle / "Contents" / "MacOS").glob("*"):
                    try:
                        with open(binary, "rb") as f, mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ) as data:
                            self._ara[file] |= data.find(b"ARA Main Factory Class") >= 0
                    except (OSError, ValueError):
                        pass
        return self._ara[file]


@functools.cache
def _vst3_bundles() -> dict[str, list[Path]]:
    """Плагины VST3 на диске по имени файла в том виде, в каком его хранит REAPER."""
    found: dict[str, list[Path]] = {}

    def walk(folder: Path):
        try:
            entries = list(folder.iterdir())
        except OSError:
            return
        for entry in entries:
            if entry.suffix == ".vst3":
                found.setdefault(plugins.cache_key(entry.name), []).append(entry)
            elif entry.is_dir():
                walk(entry)

    for base in (Path("/Library"), Path.home() / "Library"):
        walk(base / "Audio/Plug-Ins/VST3")
    return found


def state_file(device: ableton.Device, path: Path) -> Path | None:
    """Состояние плагина в файл, который понимает bridge/import.lua."""
    if not device.state:
        return None
    if device.kind == "AU":
        path = path.with_suffix(".plist")
        path.write_bytes(device.state)
        return path
    raw = device.state
    if device.kind == "VST3":
        # Так REAPER хранит состояние VST3: длина и состояние процессора, длина и состояние контроллера.
        raw = struct.pack("<II", len(device.state), 1) + device.state + struct.pack("<II", len(device.controller), 0) + device.controller
    path = path.with_suffix(".b64")
    path.write_text(base64.b64encode(raw).decode())
    return path


def flatten(devices: list, forced_off: bool = False, where: str = ""):
    """Устройства цепочки подряд, стойки раскрыты: (устройство, включено ли, где стояло, заметки)."""
    for dev in devices:
        if not isinstance(dev, ableton.Rack):
            yield dev, dev.on and not forced_off, where, []
            continue
        parallel = len(dev.branches) > 1
        inside = f"стойка «{dev.name}»"
        for branch in dev.branches:
            notes = []
            if parallel:
                notes.append("параллельная цепочка стойки — добавлен выключенным")
            elif abs(branch.vol - 1) > 1e-3 or abs(branch.pan) > 1e-3:
                notes.append("громкость и панорама цепочки стойки не перенесены")
            for inner in flatten(branch.devices, forced_off or parallel or not dev.on or not branch.on, f"{where}, {inside}" if where else inside):
                yield inner[0], inner[1], inner[2], notes + inner[3]


# ── Сборка ───────────────────────────────────────────────────────────────────


@dataclass
class Options:
    copy_media: bool = False
    use_freeze: bool = True


def write_points(path: Path, points: list[tuple[float, float, int]]) -> str:
    path.write_text("t\tv\tshape\n" + "".join(f"{t!r}\t{v!r}\t{shape}\n" for t, v, shape in points))
    return str(path)


def build(project: ableton.Project, out_dir: Path, data: Path, resolver: Resolver, options: Options):
    """Модель Ableton → проект REAPER, план для моста и отчёт. Файлы состояний и точек пишет в data."""
    (data / "states").mkdir(parents=True, exist_ok=True)
    (data / "envelopes").mkdir(exist_ok=True)
    als_dir = project.path.parent.resolve()
    master = project.master
    tempo_id = next((tid for tid, t in master.targets.items() if t == ("tempo",)), None)
    tempo = TempoMap(project.tempo, master.automation.get(tempo_id, []))

    source = project.tracks + project.returns
    index = {t.id: i for i, t in enumerate(source)}
    by_id = {t.id: t for t in source}
    label = lambda i: f"{i + 1} {source[i].name}"  # noqa: E731

    report = {
        "source": str(project.path), "live": project.creator, "tempo": tempo.summary(),
        "tracks": len(source), "folders": 0, "returns": len(project.returns),
        "items": 0, "midi_items": 0, "markers": len(project.locators),
        "plugins": {"total": 0, "planned": 0, "other_format": [], "missing": [], "no_state": [], "ara": []},
        "native": [], "automation": {"mixer": 0, "plugin_envelopes": 0, "skipped": []},
        "media": {"inside": 0, "outside": [], "copied": 0, "missing": []},
        "frozen": [], "routing": [], "clips": [], "no_instrument": [],
        "skipped": {"take_lane_clips": 0, "session_clips": 0, "clip_envelopes": 0},
    }

    files: dict[Path, str | None] = {}

    def media(path: Path | None) -> str | None:
        if path is None:
            return None
        if path not in files:
            real = path.resolve()
            if not real.is_file():
                report["media"]["missing"].append(str(path))
                files[path] = None
            elif real.is_relative_to(als_dir):
                report["media"]["inside"] += 1
                # Относительный путь переживёт переезд папки проекта — если проект REAPER лежит в ней же.
                files[path] = os.path.relpath(real, out_dir) if out_dir.is_relative_to(als_dir) else str(real)
            elif options.copy_media:
                target = out_dir / "media" / real.name
                n = 1
                while target.exists() and target.stat().st_size != real.stat().st_size:
                    target = target.with_stem(f"{real.stem}-{n}")
                    n += 1
                target.parent.mkdir(exist_ok=True)
                if not target.exists():
                    shutil.copy2(real, target)
                report["media"]["copied"] += 1
                files[path] = os.path.relpath(target, out_dir)
            else:
                report["media"]["outside"].append(str(real))
                files[path] = str(real)
        return files[path]

    def depth(track: ableton.Track) -> int:
        d = 0
        while track.group in by_id:
            track, d = by_id[track.group], d + 1
        return d

    depths = [depth(t) for t in source] + [0]
    out = rpp.Project(tempo.bpms[0], project.timesig, master_vol=0.0 if master.vol <= ableton.SILENCE else master.vol, master_pan=master.pan)
    if len(tempo.beats) > 1:
        out.tempo_points = list(zip(tempo.times, tempo.bpms))
    out.markers = [(tempo.time(beat), name) for beat, name in project.locators if beat >= 0]
    for i, t in enumerate(source):
        change = depths[i + 1] - depths[i]
        rgb = PALETTE[t.color] if 0 <= t.color < len(PALETTE) else None
        out.tracks.append(rpp.Track(
            t.name, (rgb >> 16, rgb >> 8 & 255, rgb & 255) if rgb is not None else None,
            0.0 if t.vol <= ableton.SILENCE else t.vol, t.pan, not t.on, (1, 1) if change > 0 else (2, change) if change < 0 else (0, 0),
        ))
        report["folders"] += change > 0

    plan = []  # по треку с плагинами: {"track": номер с 1 (0 — мастер), "name", "channels", "fx": [...]}

    def convert(t: ableton.Track, i: int | None):
        """Автоматизация, посылы, клипы и план плагинов одного трека. i — место в проекте, None — мастер."""
        rt = out.tracks[i] if i is not None else None
        where = label(i) if i is not None else "Master"
        frozen = options.use_freeze and t.frozen and bool(t.freeze_clips)
        entry = {"track": i + 1 if i is not None else 0, "name": t.name, "fx": []}

        # Плагины
        slots: dict[int, dict] = {}  # id устройства → запись плана
        natives: dict[int, dict] = {}  # id родного устройства → запись отчёта
        has_instrument = False
        for k, (dev, on, inside, notes) in enumerate(flatten(t.devices)):
            place = f"{where}" + (f" ({inside})" if inside else "")
            if dev.kind == "native":
                if dev.tag not in ableton.SILENT_NATIVE:
                    natives[id(dev)] = {"track": where, "device": dev.name, "tag": dev.tag, "on": on, "inside": inside, "settings": dev.settings, "automated": []}
                    report["native"].append(natives[id(dev)])
                continue
            report["plugins"]["total"] += 1
            add, exact = resolver.find(dev)
            title = f"{dev.name} ({dev.kind})"
            if not add:
                report["plugins"]["missing"].append({"track": place, "plugin": title})
                continue
            fx = {"add": [add], "fmt": dev.kind, "label": title, "on": on and not frozen, "envelopes": [], "notes": list(notes)}
            if resolver.may_be_ara(add):
                fx["maybe_ara"] = True
            if exact:
                state = state_file(dev, data / "states" / f"{entry['track']:03d}-{k:02d}")
                if state:
                    fx["state"] = str(state)
                else:
                    report["plugins"]["no_state"].append({"track": place, "plugin": title})
            else:
                fx["on"] = False
                fx["notes"].append(f"в REAPER нет формата {dev.kind} — поставлен {add.split(':')[0]} с настройками по умолчанию, выключенным")
                report["plugins"]["other_format"].append({"track": place, "plugin": title, "added": add})
            has_instrument |= dev.instrument and exact
            slots[id(dev)] = fx
            entry["fx"].append(fx)
            report["plugins"]["planned"] += 1
        if t.kind == "midi" and not has_instrument and not frozen and t.clips:
            report["no_instrument"].append({"track": where, "devices": [n["device"] for n in natives.values()]})

        # Автоматизация
        for tid, points in t.automation.items():
            target = t.targets.get(tid, ("other", "неизвестная цель"))
            kind = target[0]
            if kind in ("tempo", "timesig"):
                if kind == "timesig" and len({p.value for p in points}) > 1:
                    report["automation"]["skipped"].append({"track": where, "what": "смена размера внутри трека"})
                continue
            if kind == "volume":
                points_ = envelope(points, tempo, value=lambda v: 0.0 if v <= ableton.SILENCE else v)
                if rt:
                    rt.envelopes.append(rpp.Envelope("VOLENV2", points_))
                    rt.vol = 1.0
                else:
                    out.master_envelopes.append(rpp.Envelope("VOLENV2", points_))
                    out.master_vol = 1.0
            elif kind == "pan":
                # Огибающая панорамы в REAPER перевёрнута: +1 — влево.
                points_ = envelope(points, tempo, value=lambda v: -v)
                if rt:
                    rt.envelopes.append(rpp.Envelope("PANENV2", points_))
                    rt.pan = 0.0
                else:
                    out.master_envelopes.append(rpp.Envelope("PANENV2", points_))
                    out.master_pan = 0.0
            elif kind == "mute" and rt:
                rt.envelopes.append(rpp.Envelope("MUTEENV", envelope(points, tempo, step=True)))
                rt.mute = False
            elif kind == "send" and rt:
                continue  # посылы — ниже, вместе с получателем
            elif kind == "param" and id(target[1]) in slots:
                param: ableton.Param = target[2]
                span = param.hi - param.lo
                if param.steps > 1:
                    points_ = envelope(points, tempo, step=True, value=lambda v, n=param.steps: min(max(v / (n - 1), 0.0), 1.0))
                else:
                    points_ = envelope(points, tempo, value=lambda v, lo=param.lo, s=span: min(max((v - lo) / s, 0.0), 1.0) if s else 0.0)
                fx = slots[id(target[1])]
                fx["envelopes"].append({"param": param.name, "file": write_points(data / "envelopes" / f"{entry['track']:03d}-{tid}.tsv", points_)})
                report["automation"]["plugin_envelopes"] += 1
                continue
            elif kind == "on" and id(target[1]) in slots:
                points_ = envelope(points, tempo, step=True, value=lambda v: 1.0 - v)
                slots[id(target[1])]["bypass"] = write_points(data / "envelopes" / f"{entry['track']:03d}-{tid}.tsv", points_)
                report["automation"]["plugin_envelopes"] += 1
                continue
            else:
                if kind == "native":
                    if id(target[1]) in natives:
                        natives[id(target[1])]["automated"].append(target[2])
                    what = f"{target[1].name}: {target[2]}"
                elif kind in ("on", "param"):
                    what = f"{target[1].name}: " + ("вкл/выкл" if kind == "on" else target[2].name) + (" (родное устройство)" if target[1].kind == "native" else " (плагина нет в REAPER)")
                elif kind == "rack_on":
                    what = f"вкл/выкл стойки «{target[1].name}»"
                else:
                    what = str(target[-1])
                report["automation"]["skipped"].append({"track": where, "what": what})
                continue
            report["automation"]["mixer"] += 1

        if rt is None:
            if entry["fx"]:
                plan.append(entry)
            return

        # Посылы на возвраты
        targets_by_send = {target[1]: tid for tid, target in t.targets.items() if target[0] == "send"}
        for s, send in enumerate(t.sends[: len(project.returns)]):
            points = t.automation.get(targets_by_send.get(s))
            if (send.gain <= ableton.SILENCE and not points) or len(project.tracks) + s == i:
                continue
            recv = rpp.Receive(i, 3 if s < len(project.sends_pre) and project.sends_pre[s] else 0, send.gain, not send.on)
            if points:
                recv.envelope = rpp.Envelope("AUXVOLENV", envelope(points, tempo, value=lambda v: 0.0 if v <= ableton.SILENCE else v))
                recv.vol = 1.0
                report["automation"]["mixer"] += 1
            out.tracks[len(project.tracks) + s].receives.append(recv)

        # Выход не в мастер и не в свою группу
        m = re.fullmatch(r"AudioOut/Track\.(\d+)/.*", t.output)
        if m and m.group(1) in index:
            rt.main_send = False
            out.tracks[index[m.group(1)]].receives.append(rpp.Receive(i))
            report["routing"].append(f"{where}: выход на дорожку {label(index[m.group(1)])} — сделан посылом, выход в мастер отключён")
        elif t.output == "AudioOut/None":
            rt.main_send = False
            report["routing"].append(f"{where}: выхода нет (только посылы)")
        elif t.output == "AudioOut/Main" and t.group in by_id:
            report["routing"].append(f"{where}: в Ableton идёт мимо группы прямо в мастер — в REAPER пойдёт через папку")
        elif t.output not in ("AudioOut/Main", "AudioOut/GroupTrack"):
            report["routing"].append(f"{where}: выход {t.output} не перенесён — идёт в мастер")

        # Сайдчейн: источник приходит на каналы 3/4 дорожки с плагином
        seen = set()
        for dev, _, _, _ in flatten(t.devices):
            m = re.fullmatch(r"AudioIn/Track\.(\d+)/(\w+)", getattr(dev, "sidechain", "") or "")
            if not m or m.group(1) not in index or m.groups() in seen:
                continue
            seen.add(m.groups())
            rt.receives.append(rpp.Receive(index[m.group(1)], {"PreFxOut": 1, "PostFxOut": 3}.get(m.group(2), 0), dst_chan=2))
            rt.channels = 4
            report["routing"].append(f"{where}: сайдчейн для {dev.name} от {label(index[m.group(1)])} — на каналах 3/4; внешний вход в плагине проверь")

        # Клипы
        if t.frozen and t.freeze_clips:
            report["frozen"].append({"track": where, "used": frozen})
        for clip in t.freeze_clips if frozen else t.clips:
            report["skipped"]["clip_envelopes"] += clip.envelopes
            if clip.midi:
                rt.items.append(midi_item(clip, tempo))
                report["midi_items"] += 1
                continue
            file = media(clip.file)
            if file is None:
                continue
            made, notes = audio_items(clip, tempo, file)
            rt.items += made
            report["clips"] += [f"{where}, клип «{clip.name}»: {n}" for n in notes]
        report["items"] += len(rt.items)
        report["skipped"]["take_lane_clips"] += t.take_lane_clips
        report["skipped"]["session_clips"] += t.session_clips
        if entry["fx"]:
            entry["channels"] = rt.channels
            plan.append(entry)

    for i, t in enumerate(source):
        convert(t, i)
    convert(master, None)
    return out, plan, report, tempo


# ── Мост ─────────────────────────────────────────────────────────────────────


def to_lua(value) -> str:
    if value is None:
        return "nil"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return repr(value)
    if isinstance(value, str):
        escaped = value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n").replace("\r", "\\r")
        return f'"{escaped}"'
    if isinstance(value, dict):
        return "{" + ", ".join(f"[{to_lua(k)}] = {to_lua(v)}" for k, v in value.items()) + "}"
    return "{" + ", ".join(to_lua(v) for v in value) + "}"


def _load() -> str:
    return f"local imp = loadfile({to_lua(str(paths.ROOT / 'bridge' / 'import.lua'))})(rx, A) "


def workbench(unsaved_ok: bool, timeout: float) -> str:
    """Пустая вкладка REAPER, в которой собираются плагины. Возвращает метку, по которой мост её узнаёт.

    Плагины грузятся в том REAPER, что открыт у пользователя: упадёт плагин — пропадёт
    несохранённая работа во всех вкладках, поэтому с ней импорт не начинается.
    """
    host = client.call(_load() + "return imp.host()", timeout=timeout)
    # Вкладка прерванного импорта не в счёт: в ней только временные треки.
    unsaved = [p["path"] or "несохранённый проект" for p in host["projects"] if p["dirty"] and p["tracks"] > 0 and not p.get("workbench")]
    if unsaved and not unsaved_ok:
        raise ImportFailed(
            f"в REAPER есть несохранённые изменения ({', '.join(unsaved)}). Плагины из Ableton будут грузиться в этом же "
            "REAPER: упадёт плагин — изменения пропадут. Сохрани проект и повтори (или --unsaved-ok)"
        )
    if host["active_tracks"] > 0 and not host.get("workbench"):
        raise ImportFailed(
            "нужна пустая вкладка в REAPER: открой её (File → New project tab; окно Project Settings, если появится, закрой) "
            "и повтори. В ней соберутся плагины и откроется готовый проект"
        )
    token = uuid.uuid4().hex
    client.call(_load() + f"return imp.workbench({to_lua(token)})", timeout=timeout)
    return token


def chains(plan: list[dict], out: rpp.Project, data: Path, report: dict, token: str, timeout: float) -> None:
    """Блоки плагинов: REAPER собирает цепочку каждого трека на временном треке и отдаёт её текстом."""
    (data / "chains").mkdir(exist_ok=True)
    done = {"loaded": 0, "with_state": 0, "state_failed": [], "not_added": [], "no_param": [], "envelopes": 0, "notes": []}
    report["plugins"]["result"] = done

    # ARA-плагины REAPER грузит по-своему: состояние из Ableton к ним не применяется, а второй
    # на дорожке вызывает модальное окно. Кандидатов проверяем по одному и в цепочки не ставим.
    candidates = sorted({fx["add"][0] for entry in plan for fx in entry["fx"] if fx.get("maybe_ara")})
    ara = set()
    if candidates:
        found = client.call(_load() + f"return imp.probe_ara({to_lua(token)}, {to_lua(candidates)})", timeout=max(timeout, 900))
        ara = {name for name, yes in dict(found).items() if yes}

    for entry in plan:
        where = f"{entry['track']} {entry['name']}" if entry["track"] else "Master"
        report["plugins"]["ara"] += [{"track": where, "plugin": fx["label"]} for fx in entry["fx"] if fx["add"][0] in ara]
        entry["fx"] = [fx for fx in entry["fx"] if fx["add"][0] not in ara]
        if not entry["fx"]:
            continue
        file = data / "chains" / f"{entry['track']:03d}.txt"
        try:
            code = f"return imp.build_chain({to_lua(token)}, {to_lua(entry['fx'])}, {to_lua(str(file))}, {entry.get('channels', 2)})"
            result = client.call(_load() + code, timeout=max(timeout, 900))
        except client.BridgeError as e:
            names = ", ".join(fx["label"] for fx in entry["fx"])
            raise ImportFailed(f"перенос плагинов прерван на дорожке «{where}» ({names}): {e}") from None
        if result["written"]:
            if entry["track"]:
                out.tracks[entry["track"] - 1].fxchain = file.read_text()
            else:
                out.master_fxchain = file.read_text()
        for fx, res in zip(entry["fx"], result["fx"]):
            if res.get("error"):
                done["not_added"].append({"track": where, "plugin": fx["label"]})
                continue
            done["loaded"] += 1
            done["envelopes"] += res.get("envelopes", 0)
            if "state" in fx and res.get("state"):
                done["with_state"] += 1
            elif "state" in fx:
                done["state_failed"].append({"track": where, "plugin": fx["label"]})
            for name in res.get("no_param") or ():
                done["no_param"].append({"track": where, "plugin": fx["label"], "param": name})
            for note in fx["notes"]:
                done["notes"].append({"track": where, "plugin": fx["label"], "note": note})


def open_project(rpp_path: Path, tempo: TempoMap, report: dict, token: str, timeout: float) -> None:
    """Готовый проект — на место верстака; заодно сверка: файлы открылись, темп лёг как рассчитан."""
    probe = [tempo.beats[-1] + 64.0]
    opened = client.call(_load() + f"return imp.open({to_lua(token)}, {to_lua(str(rpp_path))}, {to_lua(probe)})", timeout=max(timeout, 900))
    report["reaper"] = {"tracks": opened["tracks"], "items": opened["items"], "fx": opened["fx"], "offline": list(opened["offline"])}
    drift = abs(opened["times"][0] - tempo.time(probe[0]))
    if drift > 1e-3:
        report["reaper"]["tempo_drift"] = drift


# ── Отчёт ────────────────────────────────────────────────────────────────────


def _top(items: list[str], limit: int = 8) -> str:
    counts = Counter(items)
    shown = [f"{name} ×{n}" if n > 1 else name for name, n in counts.most_common(limit)]
    return ", ".join(shown) + (f" и ещё {len(counts) - limit}" if len(counts) > limit else "")


def summary(report: dict, rpp_path: Path, data: Path) -> str:
    p, a, m, s = report["plugins"], report["automation"], report["media"], report["skipped"]
    lines = [
        f"{Path(report['source']).name} ({report['live']}) → {rpp_path}",
        f"дорожек {report['tracks']} (папок {report['folders']}, возвратов {report['returns']}) · айтемов {report['items']} (MIDI {report['midi_items']}) · "
        f"маркеров {report['markers']} · темп {report['tempo']}",
    ]
    result = p.get("result")
    if result:
        lines.append(f"плагинов в Ableton {p['total']} → в REAPER с настройками {result['with_state']}")
        lines.append(f"автоматизация: микшер {a['mixer']} огибающих · параметры плагинов {result['envelopes']} из {a['plugin_envelopes']}")
    else:
        lines.append(f"плагинов в Ableton {p['total']} — не переносились")
        lines.append(f"автоматизация: микшер {a['mixer']} огибающих · параметры плагинов не переносились")

    problems = []
    if p["missing"]:
        problems.append(f"плагинов нет в REAPER ({len(p['missing'])}): " + _top([x["plugin"] for x in p["missing"]]))
    if p["ara"]:
        problems.append(f"ARA-плагины не поставлены — в REAPER они работают иначе и правки из Ableton не подхватят ({len(p['ara'])}): " + _top([x["plugin"] for x in p["ara"]]))
    if p["other_format"]:
        problems.append(f"плагин есть только в другом формате — добавлен выключенным, без настроек ({len(p['other_format'])}): " + _top([x["plugin"] for x in p["other_format"]]))
    if result and result["not_added"]:
        problems.append(f"REAPER не смог добавить ({len(result['not_added'])}): " + _top([x["plugin"] for x in result["not_added"]]))
    if result and result["state_failed"]:
        problems.append(f"плагин не принял настройки ({len(result['state_failed'])}): " + _top([x["plugin"] for x in result["state_failed"]]))
    if p["no_state"]:
        problems.append(f"в проекте Ableton нет настроек плагина ({len(p['no_state'])}): " + _top([x["plugin"] for x in p["no_state"]]))
    if result and result["no_param"]:
        problems.append(f"параметр для автоматизации не найден ({len(result['no_param'])}): " + _top([f"{x['plugin']}: {x['param']}" for x in result["no_param"]]))
    parallel = [x for x in (result or {}).get("notes", []) if "параллельная" in x["note"]]
    if parallel:
        problems.append(f"стойки с параллельными цепочками: их плагины добавлены выключенными ({len(parallel)})")
    if report["native"]:
        problems.append(f"родные устройства Ableton ({len(report['native'])}): " + _top([x["device"].split(" «")[0] for x in report["native"]]) + f" — параметры в {data / 'native.json'}")
    if report["no_instrument"]:
        problems.append(f"MIDI-дорожки без инструмента ({len(report['no_instrument'])}): " + _top([x["track"] for x in report["no_instrument"]]))
    if a["skipped"]:
        problems.append(f"автоматизация без цели в REAPER ({len(a['skipped'])}): " + _top([x["what"].split(":")[0] for x in a["skipped"]]))
    if report["frozen"]:
        used = [x["track"] for x in report["frozen"] if x["used"]]
        problems.append(
            f"замороженные дорожки ({len(report['frozen'])}): " + _top([x["track"] for x in report["frozen"]])
            + (" — взят звук заморозки, плагины выключены, исходные клипы не перенесены" if used else " — заморозка не использована")
        )
    counted = [(s["take_lane_clips"], "клипов на дорожках дублей"), (s["session_clips"], "клипов Session"), (s["clip_envelopes"], "огибающих внутри аудиоклипов")]
    if any(n for n, _ in counted):
        problems.append("пропущено: " + " · ".join(f"{what} {n}" for n, what in counted if n))
    problems += report["routing"][:6]
    if len(report["routing"]) > 6:
        problems.append(f"… и ещё {len(report['routing']) - 6} заметок о роутинге")
    problems += report["clips"][:4]
    if m["missing"]:
        problems.append(f"файлов не найдено ({len(m['missing'])}), их клипы пропущены: " + _top([Path(x).name for x in m["missing"]], 4))
    opened = report.get("reaper")
    if opened and opened["offline"]:
        problems.append(f"REAPER не открыл файлы ({len(opened['offline'])}): " + _top([Path(x).name for x in opened["offline"]], 4))
    if opened and opened.get("tempo_drift"):
        problems.append(f"темповая карта REAPER расходится с расчётом на {opened['tempo_drift'] * 1000:.1f} мс — позиции после смен темпа проверь")
    if problems:
        lines.append("не перенесено или требует внимания:")
        lines += ["  " + line for line in problems]
    lines.append(f"файлы: в папке проекта Ableton {m['inside']} · вне её {len(m['outside'])}" + (f" · скопировано {m['copied']}" if m["copied"] else ""))
    if m["outside"]:
        lines.append("  вне папки проекта (ссылки полным путём; --copy-media скопирует в проект): " + _top([str(Path(x).parent) for x in m["outside"]], 3))
    if opened:
        lines.append(f"проект открыт в REAPER: дорожек {opened['tracks']}, айтемов {opened['items']}, плагинов {opened['fx']}")
    lines.append(f"подробности: {data / 'report.json'}")
    return "\n".join(lines)


def run(args) -> int:
    source = Path(args.file).expanduser().resolve()
    if not source.is_file():
        raise ImportFailed(f"файла нет: {source}")
    out_dir = (Path(args.out).expanduser() if args.out else source.parent / "reaper").resolve()
    rpp_path = out_dir / f"{source.stem}.rpp"
    data = out_dir / paths.TRACK_DATA / "import" / source.stem
    if rpp_path.exists() and not args.force:
        raise ImportFailed(f"проект уже есть: {rpp_path} — в нём может быть начатый микс. Перезаписать — с --force")
    beat = client.alive()
    if beat and beat.get("project") and Path(beat["project"]).resolve() == rpp_path:
        raise ImportFailed(f"{rpp_path.name} сейчас открыт в REAPER — закрой его вкладку и повтори")
    token = None
    if not args.no_plugins:
        if beat is None:
            raise ImportFailed(
                "мост не запущен: блоки плагинов собирает живой REAPER. Открой REAPER (мост стартует сам) и повтори; "
                "либо --no-plugins — перенести только дорожки, клипы и автоматизацию микшера"
            )
    try:
        project = ableton.load(source)
    except ableton.AlsError as e:
        raise ImportFailed(str(e)) from None
    if not args.no_plugins:
        token = workbench(args.unsaved_ok, args.timeout)
    if data.exists():
        shutil.rmtree(data)
    out_dir.mkdir(parents=True, exist_ok=True)
    out, plan, report, tempo = build(project, out_dir, data, Resolver(), Options(args.copy_media, not args.ignore_freeze))
    if project.live_major and project.live_major < 11:
        report["clips"].insert(0, f"проект сохранён в Live {project.live_major}: разбор проверен на Live 12, пути к файлам могли не прочитаться")
    (data / "native.json").write_text(json.dumps(report["native"], ensure_ascii=False, indent=1))
    (data / "plan.json").write_text(json.dumps(plan, ensure_ascii=False, indent=1))

    failure = None
    if token:
        try:
            chains(plan, out, data, report, token, args.timeout)
        except client.BridgeError as e:
            # Что успело собраться — остаётся в проекте: дорожки и клипы от плагинов не зависят.
            failure = report["failure"] = str(e)
    rpp_path.write_text(rpp.render(out))
    if token and not failure:
        try:
            if args.no_open:
                client.call(_load() + f"return imp.cleanup({to_lua(token)})", timeout=max(args.timeout, 120))
            else:
                open_project(rpp_path, tempo, report, token, args.timeout)
        except client.BridgeError as e:
            failure = report["failure"] = f"проект записан, но REAPER его не открыл: {e}"
    # Параметры родных устройств лежат в native.json; в отчёте хватит перечня.
    slim = dict(report, native=[{k: v for k, v in n.items() if k != "settings"} for n in report["native"]])
    (data / "report.json").write_text(json.dumps(slim, ensure_ascii=False, indent=1))
    print(summary(report, rpp_path, data))
    if failure:
        raise ImportFailed(failure)
    return 0
