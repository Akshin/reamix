"""Запись проекта REAPER (.rpp) текстом: дорожки, папки, айтемы, посылы, огибающие, темп.

Пишется только то, что отличается от умолчаний REAPER: недостающие строки он дополняет сам.
Блоки плагинов (fxchain) сюда приходят готовыми: их собирает живой REAPER
(bridge/import.lua) — заголовок блока плагина знает только он. Формат строк сверен с
чанками REAPER 7.81.
"""

import uuid
from dataclasses import dataclass, field

# Закон панорамы Ableton: 0 дБ в центре, +3 дБ на краях, синусная кривая. В REAPER это
# −3 дБ с компенсацией усиления (число больше 1) и синусная кривая (умолчание без PANLAWFLAGS).
ABLETON_PAN_LAW = 1.4142135623731

SOURCES = {
    ".wav": "WAVE", ".aif": "WAVE", ".aiff": "WAVE", ".w64": "WAVE", ".bwf": "WAVE", ".mp3": "MP3", ".flac": "FLAC",
    ".ogg": "VORBIS", ".opus": "OPUS", ".m4a": "VIDEO", ".mp4": "VIDEO", ".mov": "VIDEO", ".aac": "VIDEO",
}
PPQ = 960  # тиков MIDI на четверть


@dataclass
class Envelope:
    tag: str  # VOLENV2 | PANENV2 | MUTEENV | AUXVOLENV
    points: list[tuple[float, float, int]]  # время, значение, форма (0 — линия, 1 — ступень)


@dataclass
class Item:
    position: float
    length: float
    name: str = ""
    mute: bool = False
    fade_in: float = 0.0
    fade_out: float = 0.0
    # аудио
    file: str | None = None  # путь, как он будет записан: относительно папки проекта или полный
    offset: float = 0.0  # с какого места файла играть, секунды
    rate: float = 1.0
    preserve_pitch: bool = True
    pitch: float = 0.0  # полутоны
    gain: float = 1.0
    stretch: list[tuple[float, float]] = field(default_factory=list)  # (секунды от начала айтема, секунды файла)
    # MIDI: (тик от начала айтема, статус, байт 1, байт 2, заглушено) — ноты, контроллеры,
    # pitch bend; ticks — длина в тиках
    events: list[tuple[int, int, int, int, bool]] | None = None
    ticks: int = 0


@dataclass
class Receive:
    src: int  # номер трека-источника с 0
    mode: int = 0  # 0 — после фейдера, 1 — до плагинов, 3 — после плагинов до фейдера
    vol: float = 1.0
    mute: bool = False
    dst_chan: int = 0  # 2 — каналы 3/4 (сайдчейн)
    envelope: Envelope | None = None


@dataclass
class Track:
    name: str
    color: tuple[int, int, int] | None = None
    vol: float = 1.0
    pan: float = 0.0
    mute: bool = False
    folder: tuple[int, int] = (0, 0)  # (1, 1) — открывает папку; (2, −n) — закрывает n папок
    channels: int = 2
    main_send: bool = True
    receives: list[Receive] = field(default_factory=list)
    envelopes: list[Envelope] = field(default_factory=list)
    items: list[Item] = field(default_factory=list)
    fxchain: str | None = None  # блок <FXCHAIN ... > как его отдал REAPER


@dataclass
class Project:
    tempo: float = 120.0
    timesig: tuple[int, int] = (4, 4)
    tempo_points: list[tuple[float, float]] = field(default_factory=list)  # (время, темп) — ступенями
    markers: list[tuple[float, str]] = field(default_factory=list)
    master_vol: float = 1.0
    master_pan: float = 0.0
    master_envelopes: list[Envelope] = field(default_factory=list)
    master_fxchain: str | None = None
    tracks: list[Track] = field(default_factory=list)


def num(x: float) -> str:
    return format(x, ".15g")


def quote(text: str) -> str:
    text = text.replace("\n", " ").replace("\r", " ")
    if '"' not in text:
        return f'"{text}"'
    if "'" not in text:
        return f"'{text}'"
    return "`" + text.replace("`", "'") + "`"


def guid(seed: str) -> str:
    """GUID, который зависит только от seed: повторный импорт даёт тот же файл."""
    return "{" + str(uuid.uuid5(uuid.NAMESPACE_URL, "reamix:" + seed)).upper() + "}"


def native_color(rgb: tuple[int, int, int]) -> int:
    r, g, b = rgb
    return 0x1000000 | r | g << 8 | b << 16


def _envelope(env: Envelope, seed: str) -> list[str]:
    step = all(shape == 1 for _, _, shape in env.points)
    lines = [f"<{env.tag}", f"EGUID {guid(seed)}", "ACT 1 -1", "VIS 1 1 1", "LANEHEIGHT 0 0", "ARM 1", f"DEFSHAPE {1 if step else 0} -1 -1"]
    if "VOL" in env.tag:
        lines.append("VOLTYPE 1")  # шкала фейдера на экране; значения в файле — усиление
    lines += [f"PT {num(t)} {num(v)} {shape}" for t, v, shape in env.points]
    return lines + [">"]


def _midi_source(item: Item) -> list[str]:
    lines = ["<SOURCE MIDI", f"HASDATA 1 {PPQ} QN"]
    last = 0
    # В одном тике сначала снимаются ноты, затем идут контроллеры (педаль — до нот), затем
    # берутся ноты: иначе повтор той же ноты склеится.
    order = {0x80: 0, 0x90: 2}
    for tick, status, d1, d2, muted in sorted(item.events, key=lambda e: (e[0], order.get(e[1] & 0xF0, 1))):
        lines.append(f"{'Em' if muted else 'E'} {tick - last} {status:02x} {d1:02x} {d2:02x}")
        last = tick
    # Событие «все ноты сняты» отмечает конец источника.
    return lines + [f"E {max(item.ticks - last, 0)} b0 7b 00", "IGNTEMPO 0 120 4 4", ">"]


def _item(item: Item, seed: str) -> list[str]:
    lines = [
        "<ITEM", f"POSITION {num(item.position)}", "SNAPOFFS 0", f"LENGTH {num(item.length)}",
        "LOOP 0",  # за краями файла — тишина, а не повтор
        "ALLTAKES 0", f"FADEIN 1 {num(item.fade_in)} 0 1 0 0 0", f"FADEOUT 1 {num(item.fade_out)} 0 1 0 0 0",
        f"MUTE {int(item.mute)} 0", f"IGUID {guid(seed + '/item')}", f"NAME {quote(item.name)}",
        f"VOLPAN 1 0 {num(item.gain)} -1", f"SOFFS {num(item.offset)}",
        f"PLAYRATE {num(item.rate)} {int(item.preserve_pitch)} {num(item.pitch)} -1 0 0.0025", "CHANMODE 0", f"GUID {guid(seed + '/take')}",
    ]
    if item.events is not None:
        return lines + _midi_source(item) + [">"]
    if item.stretch:
        lines.append("SM " + " + ".join(f"{num(pos)} {num(src)}" for pos, src in item.stretch))
    suffix = item.file[item.file.rfind("."):].lower() if "." in item.file else ""
    return lines + [f"<SOURCE {SOURCES.get(suffix, 'WAVE')}", f"FILE {quote(item.file)}", ">", ">"]


def _chain(chunk: str | None, tag: str = "FXCHAIN") -> list[str]:
    if not chunk:
        return []
    lines = [line.strip() for line in chunk.splitlines() if line.strip()]
    return [f"<{tag}"] + lines[1:]


def _track(track: Track, index: int) -> list[str]:
    seed = f"track/{index}"
    lines = [f"<TRACK {guid(seed)}", f"NAME {quote(track.name)}"]
    if track.color:
        lines.append(f"PEAKCOL {native_color(track.color)}")
    lines += [
        f"VOLPAN {num(track.vol)} {num(track.pan)} {num(ABLETON_PAN_LAW)} -1 1", f"MUTESOLO {int(track.mute)} 0 0",
        f"ISBUS {track.folder[0]} {track.folder[1]}", f"NCHAN {track.channels}", f"TRACKID {guid(seed)}",
    ]
    for i, recv in enumerate(track.receives):
        lines.append(f"AUXRECV {recv.src} {recv.mode} {num(recv.vol)} 0 {int(recv.mute)} 0 0 0 {recv.dst_chan} -1:U 0 -1 ''")
        if recv.envelope:
            lines += _envelope(recv.envelope, f"{seed}/recv/{i}")
    lines.append(f"MAINSEND {int(track.main_send)} 0")
    for env in track.envelopes:
        lines += _envelope(env, f"{seed}/{env.tag}")
    lines += _chain(track.fxchain)
    for i, item in enumerate(track.items):
        lines += _item(item, f"{seed}/{i}")
    return lines + [">"]


def render(project: Project) -> str:
    num_, den = project.timesig
    lines = [
        '<REAPER_PROJECT 0.1 "7.0/reamix" 0', "RIPPLE 0 0", "PANMODE 3",
        'RECORD_PATH "media" ""',  # склейки и записи — в свою папку, а не рядом с проектом
        "DEFPITCHMODE 589824 0",  # растяжение — élastique Pro, ближе всего к Complex в Ableton
        f"TEMPO {num(project.tempo)} {num_} {den}", f"MASTER_VOLUME {num(project.master_vol)} {num(project.master_pan)} -1 -1 1",
    ]
    if project.tempo_points:
        lines += ["<TEMPOENVEX", f"EGUID {guid('tempo')}", "ACT 1 -1", "VIS 1 0 1", "LANEHEIGHT 0 0", "ARM 0", "DEFSHAPE 1 -1 -1"]
        lines += [f"PT {num(t)} {num(bpm)} 1" for t, bpm in project.tempo_points]
        lines.append(">")
    for env in project.master_envelopes:
        lines += _envelope(Envelope("MASTER" + env.tag, env.points), f"master/{env.tag}")
    lines += _chain(project.master_fxchain, "MASTERFXLIST")
    for i, (time, name) in enumerate(project.markers, 1):
        lines.append(f"MARKER {i} {num(time)} {quote(name)} 0")
    for i, track in enumerate(project.tracks):
        lines += _track(track, i)
    lines.append(">")

    out, depth = [], 0
    for line in lines:
        if line == ">":
            depth -= 1
        out.append("  " * depth + line)
        if line.startswith("<"):
            depth += 1
    return "\n".join(out) + "\n"
