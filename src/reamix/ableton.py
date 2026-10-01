"""Чтение проекта Ableton Live (.als) в модель, из которой собирается проект REAPER.

.als — gzip с XML. Время в аранжировке — в долях (четвертях), значения микшера — линейное
усиление, состояние плагина — байты в том виде, в каком их отдал сам плагин.
Разбор проверен на проектах Live 12; что здесь не читается — docs/features/ableton-import.md.
"""

import gzip
import struct
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path

# Первая точка любой автоматизации стоит в этом «минус бесконечном» времени: значение до начала.
BEFORE_START = -1e6
# Ниже этого усиления Ableton считает посыл и фейдер закрытыми (−70 дБ).
SILENCE = 0.00032

RACKS = ("AudioEffectGroupDevice", "InstrumentGroupDevice", "DrumGroupDevice", "MidiEffectGroupDevice")

# Названия родных устройств, как их видит пользователь; остальные показываются тегом из файла.
NATIVE_NAMES = {
    "Eq8": "EQ Eight", "FilterEQ3": "EQ Three", "ChannelEq": "Channel EQ", "StereoGain": "Utility",
    "Compressor2": "Compressor", "GlueCompressor": "Glue Compressor", "MultibandDynamics": "Multiband Dynamics",
    "AutoFilter": "Auto Filter", "AutoFilter2": "Auto Filter", "AutoPan": "Auto Pan", "AutoPan2": "Auto Pan",
    "AutoShift": "Auto Shift", "DrumBuss": "Drum Buss", "OriginalSimpler": "Simpler", "MultiSampler": "Sampler",
    "InstrumentVector": "Wavetable", "UltraAnalog": "Analog", "StringStudio": "Tension", "LoungeLizard": "Electric",
    "InstrumentImpulse": "Impulse", "Chorus2": "Chorus-Ensemble", "PhaserNew": "Phaser-Flanger", "Redux2": "Redux",
    "Hybrid": "Hybrid Reverb", "Transmute": "Spectral Resonator", "Spectral": "Spectral Time", "Vinyl": "Vinyl Distortion",
    "BeatRepeat": "Beat Repeat", "GrainDelay": "Grain Delay", "FilterDelay": "Filter Delay", "FrequencyShifter": "Frequency Shifter",
    "MxDeviceAudioEffect": "Max for Live (эффект)", "MxDeviceInstrument": "Max for Live (инструмент)",
    "MxDeviceMidiEffect": "Max for Live (MIDI)", "MidiArpeggiator": "Arpeggiator", "MidiPitcher": "Pitch",
    "MidiChord": "Chord", "MidiScale": "Scale", "MidiVelocity": "Velocity", "MidiNoteLength": "Note Length",
    "MidiRandom": "Random",
}
# Устройства, которые звук не меняют: в отчёте о потерях им делать нечего.
SILENT_NATIVE = {"Tuner", "SpectrumAnalyzer"}


class AlsError(Exception):
    pass


@dataclass
class Point:
    """Точка автоматизации. curve — опорные точки кривой Безье до следующей точки (доли отрезка)."""

    beat: float
    value: float
    curve: tuple[float, float, float, float] | None = None


@dataclass
class Param:
    """Параметр плагина, как его записал Ableton: значение лежит в диапазоне lo..hi."""

    name: str
    pid: int
    lo: float = 0.0
    hi: float = 1.0
    steps: int = 0  # для переключателя — число вариантов


@dataclass
class Device:
    kind: str  # 'VST3' | 'VST' | 'AU' | 'native'
    name: str
    on: bool = True
    vendor: str = ""
    uid: str = ""  # VST3 — 32 шестнадцатеричных знака; VST — число; AU — «тип подтип производитель»
    state: bytes = b""
    controller: bytes = b""  # вторая половина состояния VST3
    instrument: bool = False
    sidechain: str = ""  # откуда плагин берёт сигнал сайдчейна (цель роутинга Ableton)
    tag: str = ""  # для родного устройства — имя элемента в файле
    settings: dict[str, str] = field(default_factory=dict)  # родное устройство: путь → значение


@dataclass
class Branch:
    name: str
    devices: list
    vol: float = 1.0
    pan: float = 0.0
    on: bool = True


@dataclass
class Rack:
    tag: str
    name: str
    on: bool
    branches: list[Branch]


@dataclass
class Clip:
    start: float  # доли аранжировки
    end: float
    name: str = ""
    disabled: bool = False
    loop_start: float = 0.0  # для клипа с варпом и MIDI — доли клипа, без варпа — секунды файла
    loop_end: float = 0.0
    start_rel: float = 0.0
    loop_on: bool = False
    envelopes: int = 0  # огибающие внутри клипа, которые не переносятся (модуляция громкости и т. п.)
    # Огибающие MIDI-контроллеров клипа: номер цели → точки в долях клипа.
    # Цель 0 — pitch bend (−8192…8191), 1 — давление канала, n ≥ 2 — контроллер n − 2 (0…127).
    controllers: dict[int, list[Point]] = field(default_factory=dict)
    # аудио
    file: Path | None = None
    warped: bool = False
    warp_mode: int = 0
    markers: list[tuple[float, float]] = field(default_factory=list)  # (секунды файла, доли клипа)
    gain: float = 1.0
    pitch: float = 0.0  # полутоны
    fade_in: float = 0.0  # с варпом — доли, без — секунды
    fade_out: float = 0.0
    # MIDI: (нота, время в долях клипа, длительность, сила, включена)
    notes: list[tuple[int, float, float, int, bool]] | None = None

    @property
    def midi(self) -> bool:
        return self.notes is not None


@dataclass
class Send:
    gain: float
    on: bool


@dataclass
class Track:
    id: str
    kind: str  # 'audio' | 'midi' | 'group' | 'return' | 'master'
    name: str
    color: int = -1
    group: str = "-1"
    vol: float = 1.0
    pan: float = 0.0
    on: bool = True
    sends: list[Send] = field(default_factory=list)
    output: str = "AudioOut/Main"
    devices: list = field(default_factory=list)
    clips: list[Clip] = field(default_factory=list)
    frozen: bool = False
    freeze_clips: list[Clip] = field(default_factory=list)
    # Цели автоматизации трека: id → ('volume',) | ('pan',) | ('mute',) | ('send', i) |
    # ('tempo',) | ('timesig',) | ('on', Device) | ('param', Device, Param) | ('native', Device, путь) | ('other', что)
    targets: dict[str, tuple] = field(default_factory=dict)
    automation: dict[str, list[Point]] = field(default_factory=dict)  # id цели → точки
    take_lane_clips: int = 0
    session_clips: int = 0


@dataclass
class Project:
    path: Path
    creator: str
    tracks: list[Track]
    returns: list[Track]
    master: Track
    tempo: float = 120.0
    timesig: tuple[int, int] = (4, 4)
    locators: list[tuple[float, str]] = field(default_factory=list)
    sends_pre: list[bool] = field(default_factory=list)  # по возвратам: посыл до фейдера

    @property
    def live_major(self) -> int:
        digits = "".join(c if c.isdigit() else " " for c in self.creator).split()
        return int(digits[0]) if digits else 0


def _val(el, path: str, default=None):
    found = el.find(path) if el is not None else None
    return found.get("Value", default) if found is not None else default


def _num(el, path: str, default: float = 0.0) -> float:
    try:
        return float(_val(el, path, default))
    except (TypeError, ValueError):
        return default


def _flag(el, path: str, default: bool = False) -> bool:
    text = _val(el, path)
    return default if text is None else text == "true"


def _blob(el) -> bytes:
    if el is None or not el.text:
        return b""
    return bytes.fromhex("".join(el.text.split()))


def _target(el, path: str) -> str | None:
    found = el.find(path + "/AutomationTarget") if el is not None else None
    return found.get("Id") if found is not None else None


def _file(ref, base: Path) -> Path | None:
    """Файл сэмпла. Путь относительно папки проекта надёжнее: проект могли перенести."""
    if ref is None:
        return None
    relative, absolute = _val(ref, "RelativePath"), _val(ref, "Path")
    if relative and _val(ref, "RelativePathType") == "3" and (base / relative).is_file():
        return base / relative
    if absolute:
        return Path(absolute)
    return base / relative if relative else None


def _params(dev, device: Device, targets: dict) -> None:
    for p in dev.findall("ParameterList/*"):
        name = _val(p, "ParameterName", "")
        tid = _target(p, "ParameterValue")
        if not name or tid is None:
            continue
        steps = int(_num(p, "LastItemCount")) if p.tag == "PluginEnumParameter" else 0
        param = Param(name, int(_num(p, "ParameterId", -1)), _num(p, "LastUserRange/First", 0.0), _num(p, "LastUserRange/Last", 1.0), steps)
        targets[tid] = ("param", device, param)


def _plugin(dev, targets: dict) -> Device:
    on = _flag(dev, "On/Manual", True)
    side = _val(dev, "SideChain/RoutedInput/Routable/Target", "")
    side = "" if side.endswith("/None") else side
    if dev.tag == "AuPluginDevice":
        info = dev.find("PluginDesc/AuPluginInfo")
        codes = [str(int(_num(info, f"Component{k}"))) for k in ("Type", "SubType", "Manufacturer")]
        device = Device(
            "AU", _val(info, "Name", "?"), on, _val(info, "Manufacturer", ""), " ".join(codes),
            _blob(info.find("Preset/AuPreset/Buffer")), instrument=codes[0] == str(int.from_bytes(b"aumu")), sidechain=side,
        )
    else:
        info = dev.find("PluginDesc/Vst3PluginInfo")
        if info is not None:
            fields = [int(_num(info, f"Uid/Fields.{i}")) for i in range(4)]
            preset = info.find("Preset/Vst3Preset")
            device = Device(
                "VST3", _val(info, "Name", "?"), on, uid="".join(struct.pack(">i", f).hex().upper() for f in fields),
                state=_blob(preset.find("ProcessorState") if preset is not None else None),
                controller=_blob(preset.find("ControllerState") if preset is not None else None),
                instrument=_val(info, "DeviceType") == "1", sidechain=side,
            )
        else:
            info = dev.find("PluginDesc/VstPluginInfo")
            device = Device(
                "VST", _val(info, "PlugName", "?"), on, uid=str(int(_num(info, "UniqueId"))),
                state=_blob(info.find("Preset/VstPreset/Buffer") if info is not None else None),
                instrument=_val(info, "Category") == "2", sidechain=side,
            )
    tid = _target(dev, "On")
    if tid:
        targets[tid] = ("on", device)
    _params(dev, device, targets)
    return device


def _native(dev, targets: dict) -> Device:
    device = Device("native", NATIVE_NAMES.get(dev.tag, dev.tag), _flag(dev, "On/Manual", True), tag=dev.tag)
    user = _val(dev, "UserName", "")
    if user:
        device.name += f" «{user}»"
    tid = _target(dev, "On")
    if tid:
        targets[tid] = ("on", device)

    def walk(el, prefix: str):
        for child in el:
            if child.tag in ("Branches", "On", "SourceContext", "LastPresetRef"):
                continue
            path = f"{prefix}/{child.tag}" if prefix else child.tag
            manual = child.find("Manual")
            if manual is not None:
                device.settings[path] = manual.get("Value", "")
                target = child.find("AutomationTarget")
                if target is not None:
                    targets[target.get("Id")] = ("native", device, path)
            elif len(child):
                walk(child, path)

    walk(dev, "")
    return device


def _devices(parent, targets: dict) -> list:
    out = []
    for dev in parent if parent is not None else ():
        if dev.tag in ("PluginDevice", "AuPluginDevice"):
            out.append(_plugin(dev, targets))
        elif dev.tag in RACKS:
            branches = []
            for br in dev.findall("Branches/*"):
                chain = next((c.find("Devices") for c in br.findall("DeviceChain/*") if c.find("Devices") is not None), None)
                mixer = br.find("MixerDevice")
                branches.append(Branch(
                    _val(br, "Name/EffectiveName", ""), _devices(chain, targets),
                    _num(mixer, "Volume/Manual", 1.0), _num(mixer, "Panorama/Manual", 0.0), _flag(mixer, "Speaker/Manual", True),
                ))
            rack = Rack(dev.tag, _val(dev, "UserName", "") or NATIVE_NAMES.get(dev.tag, dev.tag), _flag(dev, "On/Manual", True), branches)
            tid = _target(dev, "On")
            if tid:
                targets[tid] = ("rack_on", rack)
            out.append(rack)
        else:
            out.append(_native(dev, targets))
    return out


def _clip(el, base: Path, controllers: dict[str, int]) -> Clip:
    clip = Clip(
        _num(el, "CurrentStart"), _num(el, "CurrentEnd"), _val(el, "Name", ""), _flag(el, "Disabled"),
        _num(el, "Loop/LoopStart"), _num(el, "Loop/LoopEnd"), _num(el, "Loop/StartRelative"), _flag(el, "Loop/LoopOn"),
    )
    for env in el.findall("Envelopes/Envelopes/ClipEnvelope"):
        number = controllers.get(_val(env, "EnvelopeTarget/PointeeId"))
        points = _points(env)
        if number is not None and points and el.tag == "MidiClip":
            clip.controllers[number] = points
        else:
            clip.envelopes += 1
    if el.tag == "MidiClip":
        clip.notes = []
        for key in el.findall("Notes/KeyTracks/KeyTrack"):
            pitch = int(_num(key, "MidiKey", 60))
            for note in key.findall("Notes/MidiNoteEvent"):
                clip.notes.append((
                    pitch, float(note.get("Time", 0)), float(note.get("Duration", 0)),
                    max(1, min(127, round(float(note.get("Velocity", 100))))), note.get("IsEnabled", "true") == "true",
                ))
        return clip
    clip.file = _file(el.find("SampleRef/FileRef"), base)
    clip.warped = _flag(el, "IsWarped")
    clip.warp_mode = int(_num(el, "WarpMode"))
    clip.markers = sorted((float(m.get("SecTime")), float(m.get("BeatTime"))) for m in el.findall("WarpMarkers/WarpMarker"))
    clip.gain = _num(el, "SampleVolume", 1.0)
    clip.pitch = _num(el, "PitchCoarse") + _num(el, "PitchFine") / 100
    clip.fade_in, clip.fade_out = _num(el, "Fades/FadeInLength"), _num(el, "Fades/FadeOutLength")
    return clip


def _clips(parent, base: Path, controllers: dict[str, int] | None = None) -> list[Clip]:
    if parent is None:
        return []
    return sorted((_clip(el, base, controllers or {}) for el in parent if el.tag in ("AudioClip", "MidiClip")), key=lambda c: c.start)


def _points(env) -> list[Point]:
    out = []
    for ev in env.findall("Automation/Events/*"):
        raw = ev.get("Value", "0")
        value = {"true": 1.0, "false": 0.0}.get(raw)
        if value is None:
            try:
                value = float(raw)
            except ValueError:
                continue
        curve = None
        if "CurveControl1X" in ev.attrib:
            curve = tuple(float(ev.get(k, 0)) for k in ("CurveControl1X", "CurveControl1Y", "CurveControl2X", "CurveControl2Y"))
        out.append(Point(max(float(ev.get("Time", 0)), BEFORE_START), value, curve))
    return out


def _track(el, kind: str, base: Path) -> Track:
    chain = el.find("DeviceChain")
    mixer = chain.find("Mixer") if chain is not None else None
    track = Track(
        el.get("Id", ""), kind, _val(el, "Name/EffectiveName", ""), int(_num(el, "Color", -1)), _val(el, "TrackGroupId", "-1"),
        _num(mixer, "Volume/Manual", 1.0), _num(mixer, "Pan/Manual", 0.0), _flag(mixer, "Speaker/Manual", True),
        output=_val(chain, "AudioOutputRouting/Target", "AudioOut/Main"), frozen=_flag(el, "Freeze"),
    )
    for name, target in (("Volume", ("volume",)), ("Pan", ("pan",)), ("Speaker", ("mute",)), ("Tempo", ("tempo",)), ("TimeSignature", ("timesig",))):
        tid = _target(mixer, name)
        if tid:
            track.targets[tid] = target
    for name in ("SplitStereoPanL", "SplitStereoPanR", "CrossFadeState"):
        tid = _target(mixer, name)
        if tid:
            track.targets[tid] = ("other", name)
    for i, holder in enumerate(mixer.findall("Sends/TrackSendHolder") if mixer is not None else ()):
        track.sends.append(Send(_num(holder, "Send/Manual"), _flag(holder, "EnabledByUser", True)))
        tid = _target(holder, "Send")
        if tid:
            track.targets[tid] = ("send", i)

    if chain is not None:
        track.devices = _devices(chain.find("DeviceChain/Devices"), track.targets)
        sequencer = chain.find("MainSequencer")
        if sequencer is not None:
            lane = "Sample" if kind == "audio" else "ClipTimeable"
            # Цели огибающих контроллеров: элемент ControllerTargets.N с номером цели N.
            controllers = {
                c.get("Id"): int(c.tag.rsplit(".", 1)[1]) for c in sequencer.findall("MidiControllers/*")
                if c.tag.startswith("ControllerTargets.") and c.get("Id")
            }
            track.clips = _clips(sequencer.find(f"{lane}/ArrangerAutomation/Events"), base, controllers)
            track.session_clips = sum(1 for slot in sequencer.findall("ClipSlotList/ClipSlot/ClipSlot/Value") if len(slot))
        track.freeze_clips = _clips(chain.find("FreezeSequencer/Sample/ArrangerAutomation/Events"), base)
    track.take_lane_clips = len(el.findall("TakeLanes/TakeLanes/TakeLane/ClipAutomation/Events/*"))

    for env in el.findall("AutomationEnvelopes/Envelopes/AutomationEnvelope"):
        tid, points = _val(env, "EnvelopeTarget/PointeeId"), _points(env)
        if tid and points:
            track.automation[tid] = points
    return track


def load(path: Path) -> Project:
    path = Path(path)
    try:
        with gzip.open(path, "rb") as f:
            root = ET.parse(f).getroot()
    except (OSError, EOFError, ET.ParseError) as e:
        raise AlsError(f"не читается как проект Ableton: {path} ({e})") from None
    live = root.find("LiveSet")
    if root.tag != "Ableton" or live is None:
        raise AlsError(f"в файле нет проекта Ableton: {path}")
    base = path.parent

    kinds = {"AudioTrack": "audio", "MidiTrack": "midi", "GroupTrack": "group", "ReturnTrack": "return"}
    tracks, returns = [], []
    for el in live.findall("Tracks/*"):
        if el.tag in kinds:
            (returns if el.tag == "ReturnTrack" else tracks).append(_track(el, kinds[el.tag], base))
    master_el = live.find("MainTrack") if live.find("MainTrack") is not None else live.find("MasterTrack")
    if master_el is None:
        raise AlsError("в проекте нет мастер-дорожки — формат не опознан")
    master = _track(master_el, "master", base)
    master.name = "Master"

    mixer = master_el.find("DeviceChain/Mixer")
    # Размер записан одним числом: числитель − 1 плюс 99 на каждую степень двойки знаменателя.
    code = int(_num(mixer, "TimeSignature/Manual", 201))
    return Project(
        path, root.get("Creator", ""), tracks, returns, master,
        tempo=_num(mixer, "Tempo/Manual", 120.0), timesig=(code % 99 + 1, 2 ** (code // 99)),
        locators=sorted((_num(loc, "Time"), _val(loc, "Name", "")) for loc in live.findall("Locators/Locators/Locator")),
        sends_pre=[s.get("Value") == "true" for s in live.findall("SendsPre/SendPreBool")],
    )
