"""Перенос проекта Ableton: разбор .als, пересчёт в айтемы и огибающие, запись .rpp, блоки AU."""

import base64
import gzip
import json
import os
import struct
from pathlib import Path

import pytest
from lupa import LuaRuntime

from reamix import ableton, cli, client, importer, paths, rpp
from reamix.ableton import BEFORE_START, Clip, Point
from reamix.importer import TempoMap

REPO = Path(__file__).resolve().parents[1]


# ── Темп ─────────────────────────────────────────────────────────────────────


def test_constant_tempo_and_jump():
    assert TempoMap(120).time(4) == pytest.approx(2.0)
    tempo = TempoMap(120, [Point(BEFORE_START, 120), Point(16, 120), Point(16, 60)])
    assert tempo.beats == [0.0, 16.0] and tempo.bpms == [120, 60]
    assert tempo.time(18) == pytest.approx(8.0 + 2.0)
    assert tempo.boundaries(8, 24) == [16.0] and tempo.boundaries(16, 24) == []


def test_tempo_ramp_becomes_steps_that_keep_total_time():
    tempo = TempoMap(120, [Point(BEFORE_START, 120), Point(4, 120), Point(8, 60)])
    # Точное время плавного замедления со 120 до 60 за четыре доли: 4·ln 2 секунды.
    assert tempo.time(8) == pytest.approx(2.0 + 2.772589, abs=0.005)
    assert len(tempo.beats) == 1 + 16 + 1  # ровный участок, ступени не мельче четверти доли, ровный участок
    assert all(b > a for a, b in zip(tempo.times, tempo.times[1:]))


def test_automation_without_starting_point_holds_first_value():
    assert TempoMap(100, [Point(8, 140), Point(12, 140)]).bpms == [140]


# ── Автоматизация ────────────────────────────────────────────────────────────


def test_envelope_keeps_jumps_in_order():
    points = [Point(BEFORE_START, 1.0), Point(4, 1.0), Point(4, 0.5), Point(8, 0.25)]
    out = importer.envelope(points, TempoMap(120))
    assert [(round(t, 3), v) for t, v, _ in out] == [(0, 1.0), (2, 1.0), (2, 0.5), (4, 0.25)]
    assert out[2][0] > out[1][0]  # скачок: вторая точка строго позже первой


def test_envelope_bends_where_tempo_changes():
    tempo = TempoMap(120, [Point(BEFORE_START, 120), Point(4, 120), Point(4, 60)])
    out = importer.envelope([Point(0, 0.0), Point(8, 1.0)], tempo)
    # Прямая в долях: к смене темпа пройдена половина пути, а по времени — только треть.
    assert [(round(t, 6), round(v, 6)) for t, v, _ in out] == [(0, 0), (2, 0.5), (6, 1)]


def test_curve_is_replaced_by_short_lines():
    out = importer.envelope([Point(0, 0.0, (0.5, 0.0, 1.0, 0.5)), Point(4, 1.0)], TempoMap(120))
    assert len(out) == 1 + (importer.CURVE_STEPS - 1) + 1
    values = [v for _, v, _ in out]
    assert values == sorted(values) and values[len(values) // 2] < 0.5  # выпуклость вниз сохранена


def test_switch_automation_is_steps():
    out = importer.envelope([Point(BEFORE_START, 1), Point(4, 0), Point(8, 1)], TempoMap(120), step=True, value=lambda v: 1 - v)
    assert out == [(0.0, 0, 1), (2.0, 1, 1), (4.0, 0, 1)]


# ── Клипы ────────────────────────────────────────────────────────────────────

MARKERS = [(0.0, 0.0), (2.0, 4.0)]  # файл записан в 120 уд/мин: полсекунды на долю


def test_warped_clip_at_constant_tempo_is_plain_playrate():
    clip = Clip(4, 8, "loop", loop_start=1, loop_end=5, warped=True, markers=MARKERS, fade_in=1, fade_out=2)
    (item,), notes = importer.audio_items(clip, TempoMap(60), "a.wav")
    assert (item.position, item.length, item.offset, item.rate) == pytest.approx((4, 4, 0.5, 0.5))
    assert item.stretch == [] and notes == []
    assert (item.fade_in, item.fade_out) == pytest.approx((1, 2))  # доли → секунды при 60 уд/мин


def test_warped_clip_across_tempo_change_gets_stretch_markers():
    tempo = TempoMap(60, [Point(BEFORE_START, 60), Point(6, 60), Point(6, 120)])
    clip = Clip(4, 8, loop_start=1, loop_end=5, warped=True, markers=MARKERS)
    (item,), _ = importer.audio_items(clip, tempo, "a.wav")
    assert (item.offset, item.rate) == (0, 1)
    assert item.stretch == pytest.approx([(0, 0.5), (2, 1.5), (3, 2.5)])


def test_inner_warp_marker_becomes_stretch_marker():
    clip = Clip(0, 8, loop_start=0, loop_end=8, warped=True, markers=[(0.0, 0.0), (2.0, 4.0), (3.0, 8.0)])
    (item,), _ = importer.audio_items(clip, TempoMap(60), "a.wav")
    assert item.stretch == pytest.approx([(0, 0), (4, 2), (8, 3)])


def test_unwarped_clip_plays_at_its_own_speed():
    clip = Clip(4, 8, loop_start=1.5, loop_end=3.5, fade_out=0.25)  # без варпа начало и фейды — в секундах
    (item,), _ = importer.audio_items(clip, TempoMap(120), "a.wav")
    assert (item.position, item.length, item.offset, item.rate, item.fade_out) == pytest.approx((2, 2, 1.5, 1, 0.25))


def test_looped_clip_is_unrolled_into_passes():
    clip = Clip(0, 5, loop_start=0, loop_end=2, start_rel=1, loop_on=True, warped=True, markers=MARKERS, fade_in=0.5, fade_out=0.5)
    items, _ = importer.audio_items(clip, TempoMap(60), "a.wav")
    assert [(i.position, i.length, i.offset) for i in items] == pytest.approx([(0, 1, 0.5), (1, 2, 0), (3, 2, 0)])
    assert [i.fade_in for i in items] == [0.5, 0, 0] and [i.fade_out for i in items] == [0, 0, 0.5]


def test_clip_starting_before_the_file_skips_the_silence():
    clip = Clip(0, 4, loop_start=-2, loop_end=2, warped=True, markers=MARKERS)
    (item,), _ = importer.audio_items(clip, TempoMap(60), "a.wav")
    assert (item.position, item.length, item.offset) == pytest.approx((2, 2, 0))


def test_midi_clip_loops_and_cuts_notes_at_the_pass_end():
    clip = Clip(4, 8, loop_start=0, loop_end=2, loop_on=True, notes=[(60, 0.0, 1.0, 100, True), (64, 1.5, 4.0, 90, False)])
    item = importer.midi_item(clip, TempoMap(120))
    assert (item.position, item.length, item.ticks) == (2.0, 2.0, 4 * rpp.PPQ)
    assert sorted(item.events) == sorted([
        (0, 0x90, 60, 100, False), (960, 0x80, 60, 0, False), (1440, 0x90, 64, 90, True), (1920, 0x80, 64, 0, True),
        (1920, 0x90, 60, 100, False), (2880, 0x80, 60, 0, False), (3360, 0x90, 64, 90, True), (3840, 0x80, 64, 0, True),
    ])
    lines = rpp.render(rpp.Project(tracks=[rpp.Track("m", items=[item])])).splitlines()
    midi = [line.strip() for line in lines if line.strip().startswith(("E ", "Em "))]
    # В одном тике снятие ноты идёт раньше взятия; последняя строка доводит источник до длины клипа.
    assert midi[:5] == ["E 0 90 3c 64", "E 960 80 3c 00", "Em 480 90 40 5a", "Em 480 80 40 00", "E 0 90 3c 64"]
    assert midi[-1] == "E 0 b0 7b 00"


def test_clip_controller_envelopes_become_midi_messages():
    pedal = [Point(BEFORE_START, 0), Point(1, 0), Point(1, 127), Point(3, 127), Point(3, 0)]  # цель 66 — контроллер 64
    bend = [Point(0, 0), Point(1, 8191)]  # цель 0 — pitch bend, плавно вверх за долю
    clip = Clip(0, 4, loop_start=0, loop_end=4, notes=[(60, 1.0, 1.0, 100, True)], controllers={66: pedal, 0: bend})
    item = importer.midi_item(clip, TempoMap(120))
    assert [(t, d2) for t, status, d1, d2, _ in item.events if (status, d1) == (0xB0, 64)] == [(0, 0), (960, 127), (2880, 0)]
    bends = [(t, d1 | d2 << 7) for t, status, d1, d2, _ in item.events if status == 0xE0]
    assert bends[0] == (0, 8192) and bends[-1] == (960, 16383) and len(bends) == importer.CONTROLLER_STEPS + 1
    lines = [line.strip() for line in rpp.render(rpp.Project(tracks=[rpp.Track("m", items=[item])])).splitlines()]
    pedal_down = next(i for i, line in enumerate(lines) if line.endswith("b0 40 7f"))
    note_on = next(i for i, line in enumerate(lines) if line.endswith("90 3c 64"))
    # Педаль нажата раньше ноты, хотя обе стоят в одном тике.
    assert pedal_down < note_on and all(line.startswith("E 0 ") for line in lines[pedal_down + 1 : note_on + 1])


# ── Запись .rpp ──────────────────────────────────────────────────────────────


def test_rpp_is_balanced_and_quotes_names():
    project = rpp.Project(90, (3, 4), [(0.0, 90.0), (4.0, 100.0)], [(1.5, 'Drop "A"')], tracks=[
        rpp.Track('Lead "dry"', (255, 128, 0), 0.5, -0.3, True, (1, 1), envelopes=[rpp.Envelope("VOLENV2", [(0, 1, 0), (2, 0.5, 0)])]),
        rpp.Track("it's `x` \"y\"", folder=(2, -1), fxchain="<FXCHAIN\nSHOW 0\n<VST \"VST: A\" a.vst 0 \"\" 1\nQUJD\n>\n>"),
    ])
    text = rpp.render(project)
    lines = [line.strip() for line in text.splitlines()]
    assert sum(line.startswith("<") for line in lines) == lines.count(">")
    assert "TEMPO 90 3 4" in lines and "PT 4 100 1" in lines and """MARKER 1 1.5 'Drop "A"' 0""" in lines
    assert """NAME 'Lead "dry"'""" in lines and """NAME `it's 'x' "y"`""" in lines
    assert "PEAKCOL 16810239" in lines  # как REAPER на macOS пишет цвет (255, 128, 0)
    assert "VOLPAN 0.5 -0.3 1.4142135623731 -1 1" in lines and "MUTESOLO 1 0 0" in lines
    assert lines[lines.index("<FXCHAIN") + 2] == '<VST "VST: A" a.vst 0 "" 1'
    assert text.splitlines()[lines.index("QUJD")].startswith("        Q")  # вложенный блок выровнен по глубине


# ── Поиск плагинов в REAPER ──────────────────────────────────────────────────


def test_resolver_matches_by_id_and_knows_ara(tmp_path, monkeypatch):
    reaper = tmp_path / "REAPER"
    reaper.mkdir()
    (reaper / "reaper-vstplugins_arm64.ini").write_text(
        "[vstcache]\n"
        "Comp.vst3=00,111{ABCDEF01000000020000000300000004,Comp (Maker)\n"
        "Shell.vst3=00,0,<SHELL>\n"
        "Shell.vst3<222=00,222{AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA,Inside Shell (Maker) (8ch)\n"
        "Tune.vst3=00,333{BBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBB,Tune (Maker)!!!VSTi\n"
        "Gone.vst3=00,444{CCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCC,Gone (Maker)\n"
        "Old.vst=00,1400130657,Old (Maker)\n"
    )
    (reaper / "reaper-auplugins_arm64.ini").write_text("[auplugins]\nMaker: Squash=<!inst>\n")
    (reaper / "reaper-auplugins_arm64-bc.ini").write_text('AU "Waves: Sib Stereo" "Waves: Sib Stereo" 1635083896 8 1635083896 1397310035 1802721110\n')
    for name, marker in (("Comp", b"plain"), ("Tune", b"...ARA Main Factory Class...")):
        binary = tmp_path / "VST3" / f"{name}.vst3" / "Contents" / "MacOS" / name
        binary.parent.mkdir(parents=True)
        binary.write_bytes(marker)
    monkeypatch.setattr(paths, "REAPER", reaper)
    monkeypatch.setattr(importer.plugins, "_on_disk", lambda: {"Comp.vst3", "Shell.vst3", "Tune.vst3", "Old.vst"})
    monkeypatch.setattr(importer.plugins, "scan", lambda: ([importer.plugins.Plugin("Squash", "Maker", "AU", "AU:Maker: Squash")], {}))
    monkeypatch.setattr(importer, "_vst3_bundles", lambda: {f"{n}.vst3": [tmp_path / "VST3" / f"{n}.vst3"] for n in ("Comp", "Tune")})
    resolver = importer.Resolver()
    device = lambda kind, name, uid="", vendor="": ableton.Device(kind, name, uid=uid, vendor=vendor)  # noqa: E731

    assert resolver.find(device("VST3", "Comp", "ABCDEF01000000020000000300000004")) == ("VST3:Comp (Maker)", True)
    assert resolver.find(device("VST3", "whatever", "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA")) == ("VST3:Inside Shell (Maker)", True)
    assert resolver.find(device("VST", "Old", "1400130657")) == ("VST:Old (Maker)", True)
    assert resolver.find(device("AU", "Squash", "1 2 3", "Maker")) == ("AU:Maker: Squash", True)
    # У Waves имя в REAPER другое — находится по кодам компонента.
    assert resolver.find(device("AU", "Sib (s)", "1635083896 1397310035 1802721110", "Waves")) == ("AU:Waves: Sib Stereo", True)
    # VST3 в REAPER нет, но плагин есть в AU: имя найдено, формат другой.
    assert resolver.find(device("VST3", "Squash", "DDDDDDDDDDDDDDDDDDDDDDDDDDDDDDDD")) == ("AU:Maker: Squash", False)
    # REAPER помнит плагин, а файла на диске нет.
    assert resolver.find(device("VST3", "Gone", "CCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCC")) == (None, False)
    assert resolver.may_be_ara("VST3:Tune (Maker)") and not resolver.may_be_ara("VST3:Comp (Maker)") and not resolver.may_be_ara("AU:Maker: Squash")


# ── Проект целиком ───────────────────────────────────────────────────────────


def target(tid: int) -> str:
    return f'<AutomationTarget Id="{tid}"><LockEnvelope Value="0"/></AutomationTarget>'


def mixer(tid: int, vol=1.0, pan=0.0, on=True, sends=(), tempo=None) -> str:
    holders = "".join(
        f'<TrackSendHolder Id="{i}"><Send><Manual Value="{gain}"/>{target(tid + 10 + i)}</Send><EnabledByUser Value="true"/></TrackSendHolder>'
        for i, gain in enumerate(sends)
    )
    extra = f'<Tempo><Manual Value="{tempo}"/>{target(tid + 5)}</Tempo><TimeSignature><Manual Value="201"/>{target(tid + 6)}</TimeSignature>' if tempo else ""
    return (
        f'<Mixer><Sends>{holders}</Sends><Speaker><Manual Value="{str(on).lower()}"/>{target(tid)}</Speaker>'
        f'<Pan><Manual Value="{pan}"/>{target(tid + 1)}</Pan><Volume><Manual Value="{vol}"/>{target(tid + 2)}</Volume>{extra}</Mixer>'
    )


def automation(*envelopes) -> str:
    body = ""
    for tid, events in envelopes:
        points = "".join(f'<{kind} Id="{i}" Time="{t}" Value="{v}"/>' for i, (kind, t, v) in enumerate(events))
        body += f'<AutomationEnvelope Id="{tid}"><EnvelopeTarget><PointeeId Value="{tid}"/></EnvelopeTarget><Automation><Events>{points}</Events></Automation></AutomationEnvelope>'
    return f"<AutomationEnvelopes><Envelopes>{body}</Envelopes></AutomationEnvelopes>"


def vst3(name: str, fields, tid: int, state=b"PROC", controller=b"CTL", param=None, on=True) -> str:
    uid = "".join(f'<Fields.{i} Value="{f}"/>' for i, f in enumerate(fields))
    params = ""
    if param:
        params = (
            f'<PluginFloatParameter Id="0"><ParameterName Value="{param}"/><ParameterId Value="7"/>'
            f'<ParameterValue><Manual Value="5"/>{target(tid + 1)}</ParameterValue>'
            '<LastUserRange><First Value="0"/><Last Value="10"/></LastUserRange></PluginFloatParameter>'
        )
    return (
        f'<PluginDevice Id="0"><On><Manual Value="{str(on).lower()}"/>{target(tid)}</On><PluginDesc><Vst3PluginInfo Id="0">'
        f'<Preset><Vst3Preset Id="1"><ProcessorState>{state.hex().upper()}</ProcessorState><ControllerState>{controller.hex().upper()}</ControllerState></Vst3Preset></Preset>'
        f'<Name Value="{name}"/><Uid>{uid}</Uid><DeviceType Value="2"/></Vst3PluginInfo></PluginDesc>'
        f'<SideChain><RoutedInput><Routable><Target Value="AudioIn/None"/></Routable></RoutedInput></SideChain>'
        f"<ParameterList>{params}</ParameterList></PluginDevice>"
    )


def au(name: str, vendor: str, codes, tid: int, state=b"<plist/>", sidechain="AudioIn/None") -> str:
    return (
        f'<AuPluginDevice Id="0"><On><Manual Value="true"/>{target(tid)}</On><PluginDesc><AuPluginInfo Id="0">'
        f'<ComponentType Value="{codes[0]}"/><ComponentSubType Value="{codes[1]}"/><ComponentManufacturer Value="{codes[2]}"/>'
        f'<Name Value="{name}"/><Manufacturer Value="{vendor}"/><Preset><AuPreset Id="1"><Buffer>{state.hex().upper()}</Buffer></AuPreset></Preset>'
        f'</AuPluginInfo></PluginDesc><SideChain><RoutedInput><Routable><Target Value="{sidechain}"/></Routable></RoutedInput></SideChain>'
        "<ParameterList/></AuPluginDevice>"
    )


def audio_clip(start, end, file, warped=False, loop=(0, 0)) -> str:
    markers = '<WarpMarker Id="0" SecTime="0" BeatTime="0"/><WarpMarker Id="1" SecTime="2" BeatTime="4"/>'
    return (
        f'<AudioClip Id="0" Time="{start}"><CurrentStart Value="{start}"/><CurrentEnd Value="{end}"/>'
        f'<Loop><LoopStart Value="{loop[0]}"/><LoopEnd Value="{loop[1]}"/><StartRelative Value="0"/><LoopOn Value="false"/></Loop>'
        f'<Name Value="{Path(file).stem}"/><Disabled Value="false"/><IsWarped Value="{str(warped).lower()}"/>'
        f'<SampleRef><FileRef><RelativePathType Value="3"/><RelativePath Value="{file}"/><Path Value="/nowhere/{file}"/></FileRef></SampleRef>'
        f'<WarpMode Value="4"/><Fades><FadeInLength Value="0"/><FadeOutLength Value="0"/></Fades><PitchCoarse Value="0"/><PitchFine Value="0"/>'
        f'<SampleVolume Value="0.5"/><WarpMarkers>{markers}</WarpMarkers></AudioClip>'
    )


def midi_clip(start, end) -> str:
    note = '<MidiNoteEvent Time="0" Duration="1" Velocity="100" OffVelocity="64" NoteId="1"/>'
    envelopes = "".join(
        f'<ClipEnvelope Id="{i}"><EnvelopeTarget><PointeeId Value="{tid}"/></EnvelopeTarget>'
        '<Automation><Events><FloatEvent Id="0" Time="-63072000" Value="0"/><FloatEvent Id="1" Time="2" Value="127"/></Events></Automation></ClipEnvelope>'
        for i, tid in enumerate((480, 999))
    )
    return (
        f'<MidiClip Id="0" Time="{start}"><CurrentStart Value="{start}"/><CurrentEnd Value="{end}"/>'
        f'<Loop><LoopStart Value="0"/><LoopEnd Value="{end - start}"/><StartRelative Value="0"/><LoopOn Value="false"/></Loop>'
        f'<Name Value="riff"/><Disabled Value="false"/><Envelopes><Envelopes>{envelopes}</Envelopes></Envelopes><Notes><KeyTracks><KeyTrack Id="0"><Notes>{note}</Notes><MidiKey Value="36"/></KeyTrack></KeyTracks></Notes></MidiClip>'
    )


def track(tag, tid, name, mix, group=-1, color=0, devices="", clips="", envelopes="", output="AudioOut/Main", frozen=False, freeze="") -> str:
    lane = "Sample" if tag == "AudioTrack" else "ClipTimeable"
    controllers = '<MidiControllers><ControllerTargets.66 Id="480"><LockEnvelope Value="0"/></ControllerTargets.66></MidiControllers>'
    sequencer = f"<MainSequencer><{lane}><ArrangerAutomation><Events>{clips}</Events></ArrangerAutomation></{lane}>{controllers}</MainSequencer>" if tag in ("AudioTrack", "MidiTrack") else ""
    freezer = f"<FreezeSequencer><Sample><ArrangerAutomation><Events>{freeze}</Events></ArrangerAutomation></Sample></FreezeSequencer>"
    return (
        f'<{tag} Id="{tid}"><Name><EffectiveName Value="{name}"/></Name><Color Value="{color}"/>{envelopes}<TrackGroupId Value="{group}"/>'
        f'<Freeze Value="{str(frozen).lower()}"/><DeviceChain><AudioOutputRouting><Target Value="{output}"/></AudioOutputRouting>{mix}'
        f"{sequencer}{freezer}<DeviceChain><Devices>{devices}</Devices></DeviceChain></DeviceChain></{tag}>"
    )


class FakeResolver:
    def may_be_ara(self, add):
        return add in ("VST3:Tune", "VST3:Comp")  # признак в файле есть у обоих, ARA — только Tune

    def find(self, device):
        if device.name == "Gone":
            return None, False
        if device.name == "OnlyAU":
            return "AU:Maker: OnlyAU", False
        return f"{device.kind}:{device.name}", True


@pytest.fixture
def song(tmp_path):
    """Проект: группа с бочкой, бас с сайдчейном от бочки, замороженный синт, возврат, мастер со сменой темпа."""
    folder = tmp_path / "Song Project"
    (folder / "Samples").mkdir(parents=True)
    for name in ("kick.wav", "bass.wav", "frozen.wav"):
        (folder / "Samples" / name).write_bytes(b"RIFF")
    eq = f'<Eq8 Id="1"><On><Manual Value="true"/>{target(900)}</On><Bands.0><Freq><Manual Value="80"/>{target(901)}</Freq></Bands.0></Eq8>'
    rack = (
        f'<AudioEffectGroupDevice Id="2"><On><Manual Value="true"/>{target(910)}</On><UserName Value="Par"/><Branches>'
        f'<AudioEffectBranch Id="0"><Name><EffectiveName Value="wet"/></Name><DeviceChain><AudioToAudioDeviceChain><Devices>{vst3("Verb", (1, 2, 3, 4), 920)}</Devices></AudioToAudioDeviceChain></DeviceChain>'
        '<MixerDevice><Volume><Manual Value="0.5"/></Volume><Speaker><Manual Value="true"/></Speaker></MixerDevice></AudioEffectBranch>'
        '<AudioEffectBranch Id="1"><Name><EffectiveName Value="dry"/></Name><DeviceChain><AudioToAudioDeviceChain><Devices/></AudioToAudioDeviceChain></DeviceChain></AudioEffectBranch>'
        "</Branches></AudioEffectGroupDevice>"
    )
    tracks = [
        track("GroupTrack", 10, "Drums", mixer(100, vol=0.5), color=14),
        track(
            "AudioTrack", 11, "Kick", mixer(200, sends=(0.5,)), group=10,
            devices=vst3("Comp", (-1412567295, 2, 3, 4), 250, param="Gain") + eq + vst3("Gone", (9, 9, 9, 9), 260) + vst3("OnlyAU", (8, 8, 8, 8), 270),
            clips=audio_clip(0, 4, "Samples/kick.wav", warped=True, loop=(0, 4)),
            envelopes=automation(
                (202, [("FloatEvent", -63072000, 1), ("FloatEvent", 4, 1), ("FloatEvent", 8, 0.5)]),
                (200, [("BoolEvent", -63072000, "true"), ("BoolEvent", 4, "false")]),
                (251, [("FloatEvent", -63072000, 5), ("FloatEvent", 8, 10)]),
                (250, [("BoolEvent", -63072000, "true"), ("BoolEvent", 8, "false")]),
                (901, [("FloatEvent", -63072000, 80), ("FloatEvent", 8, 200)]),
            ),
            output="AudioOut/GroupTrack",
        ),
        track(
            "AudioTrack", 12, "Bass", mixer(300, pan=-0.5, sends=(0.0003162277571,)),
            devices=au("Squash", "Maker", (1, 2, 3), 350, sidechain="AudioIn/Track.11/PostFxOut"),
            clips=audio_clip(2, 6, "Samples/bass.wav", loop=(1.0, 3.0)) + audio_clip(8, 9, "Samples/lost.wav"),
        ),
        track(
            "MidiTrack", 13, "Synth", mixer(400), devices=vst3("Poly", (5, 5, 5, 5), 450), clips=midi_clip(0, 4),
            frozen=True, freeze=audio_clip(0, 4, "Samples/frozen.wav", warped=True, loop=(0, 4)),
        ),
        track("ReturnTrack", 20, "Verb bus", mixer(500, vol=0.8), devices=rack),
    ]
    master = track(
        "MainTrack", 0, "Main", mixer(600, vol=0.9, tempo=120), devices=au("Limiter", "Maker", (4, 5, 6), 650) + vst3("Tune", (7, 7, 7, 7), 660),
        envelopes=automation((605, [("FloatEvent", -63072000, 120), ("FloatEvent", 4, 120), ("FloatEvent", 4, 60)]), (602, [("FloatEvent", -63072000, 0.9), ("FloatEvent", 8, 0.45)])),
    )
    xml = (
        '<?xml version="1.0" encoding="UTF-8"?><Ableton MajorVersion="5" MinorVersion="12.0_12402" Creator="Ableton Live 12.1"><LiveSet>'
        f'<Tracks>{"".join(tracks)}</Tracks>{master}<SendsPre><SendPreBool Id="0" Value="true"/></SendsPre>'
        '<Locators><Locators><Locator Id="0"><Time Value="8"/><Name Value="Drop"/></Locator></Locators></Locators></LiveSet></Ableton>'
    )
    path = folder / "song.als"
    path.write_bytes(gzip.compress(xml.encode()))
    return path


def build(song, **options):
    project = ableton.load(song)
    out_dir = song.parent / "reaper"
    data = out_dir / ".reamix" / "import" / "song"
    out, plan, report, tempo = importer.build(project, out_dir, data, FakeResolver(), importer.Options(**options))
    return out, plan, report, data


def test_als_is_read_into_model(song):
    project = ableton.load(song)
    assert [(t.kind, t.name, t.group) for t in project.tracks] == [("group", "Drums", "-1"), ("audio", "Kick", "10"), ("audio", "Bass", "-1"), ("midi", "Synth", "-1")]
    assert project.live_major == 12 and project.tempo == 120 and project.timesig == (4, 4) and project.sends_pre == [True]
    kick = project.tracks[1]
    comp, eq, *_ = kick.devices
    assert (comp.kind, comp.name, comp.uid, comp.state, comp.controller) == ("VST3", "Comp", "ABCDEF01000000020000000300000004", b"PROC", b"CTL")
    assert (eq.kind, eq.name, eq.settings) == ("native", "EQ Eight", {"Bands.0/Freq": "80"})
    assert kick.targets["202"] == ("volume",) and kick.targets["251"][2].name == "Gain" and kick.targets["901"] == ("native", eq, "Bands.0/Freq")
    assert kick.clips[0].file == song.parent / "Samples/kick.wav"  # путь внутри папки проекта важнее записанного полного
    assert kick.clips[0].markers == [(0.0, 0.0), (2.0, 4.0)] and kick.clips[0].gain == 0.5
    assert project.tracks[2].devices[0].sidechain == "AudioIn/Track.11/PostFxOut" and project.tracks[2].devices[0].uid == "1 2 3"
    assert project.tracks[3].frozen and len(project.tracks[3].freeze_clips) == 1 and project.tracks[3].clips[0].notes == [(36, 0.0, 1.0, 100, True)]
    # Огибающая педали (цель 66 → контроллер 64) читается; огибающая с неизвестной целью только считается.
    riff = project.tracks[3].clips[0]
    assert list(riff.controllers) == [66] and [(p.beat, p.value) for p in riff.controllers[66]] == [(BEFORE_START, 0.0), (2.0, 127.0)] and riff.envelopes == 1
    assert len(project.returns[0].devices[0].branches) == 2
    assert project.locators == [(8.0, "Drop")]


def test_not_an_ableton_file_is_refused(tmp_path):
    bad = tmp_path / "x.als"
    bad.write_text("не gzip")
    with pytest.raises(ableton.AlsError, match="не читается"):
        ableton.load(bad)


def test_project_structure_routing_and_items(song):
    out, plan, report, _ = build(song)
    drums, kick, bass, synth, verb = out.tracks
    assert [t.folder for t in out.tracks] == [(1, 1), (2, -1), (0, 0), (0, 0), (0, 0)]
    assert drums.vol == 0.5 and drums.color == (0xFF, 0x36, 0x36)
    # Темп: 120 до четвёртой доли, дальше 60. Маркер на восьмой доле — на шестой секунде.
    assert out.tempo == 120 and out.tempo_points == [(0.0, 120), (2.0, 60)] and out.markers == [(6.0, "Drop")]
    # Громкость и mute автоматизированы: огибающие абсолютные, фейдер и кнопка — в нейтрали.
    assert kick.vol == 1.0 and not kick.mute
    volume, mute = kick.envelopes
    assert (volume.tag, [(round(t, 3), v) for t, v, _ in volume.points]) == ("VOLENV2", [(0, 1.0), (2, 1.0), (6, 0.5)])
    assert (mute.tag, mute.points) == ("MUTEENV", [(0.0, 1.0, 1), (2.0, 0.0, 1)])
    # Посыл до фейдера (так стоит у возврата), закрытый посыл баса не создаётся.
    assert [(r.src, r.mode, r.vol) for r in verb.receives] == [(1, 3, 0.5)]
    # Сайдчейн баса от бочки: посыл после плагинов на каналы 3/4.
    assert [(r.src, r.mode, r.dst_chan) for r in bass.receives] == [(1, 3, 2)] and bass.channels == 4 and bass.pan == -0.5
    (item,) = kick.items
    assert (item.file, item.offset, item.rate, item.gain) == ("../Samples/kick.wav", 0, pytest.approx(1.0), 0.5)
    assert [(i.position, i.length, i.offset, i.rate) for i in bass.items] == [(1.0, pytest.approx(3.0), 1.0, 1.0)]
    assert report["media"]["missing"] == ["/nowhere/Samples/lost.wav"] and report["media"]["inside"] == 3
    # Замороженная дорожка: звук заморозки вместо MIDI, плагин на месте, но выключен.
    assert [i.file for i in synth.items] == ["../Samples/frozen.wav"] and report["frozen"] == [{"track": "4 Synth", "used": True}]
    assert out.master_vol == 1.0 and [e.tag for e in out.master_envelopes] == ["VOLENV2"]
    assert report["items"] == 3 and report["midi_items"] == 0 and report["folders"] == 1


def test_frozen_track_can_keep_its_clips(song):
    out, plan, report, _ = build(song, use_freeze=False)
    synth = out.tracks[3]
    assert synth.items[0].events is not None and report["midi_items"] == 1
    assert next(e for e in plan if e["track"] == 4)["fx"][0]["on"] is True


def test_plugin_plan_states_and_report(song):
    out, plan, report, data = build(song)
    by_track = {e["track"]: e for e in plan}
    assert sorted(by_track) == [0, 2, 3, 4, 5]
    comp, only_au = by_track[2]["fx"]
    assert comp["add"] == ["VST3:Comp"] and comp["on"] and Path(comp["state"]).name == "002-00.b64"
    raw = base64.b64decode(Path(comp["state"]).read_text())
    assert raw == struct.pack("<II", 4, 1) + b"PROC" + struct.pack("<II", 3, 0) + b"CTL"
    # Параметр записан в диапазоне 0..10 — в огибающую идёт доля диапазона (с изломом на смене темпа);
    # выключатель — в bypass.
    assert Path(comp["envelopes"][0]["file"]).read_text().splitlines()[1:] == ["0.0\t0.5\t0", "2.0\t0.75\t0", "6.0\t1.0\t0"]
    assert comp["envelopes"][0]["param"] == "Gain"
    assert Path(comp["bypass"]).read_text().splitlines()[1:] == ["0.0\t0.0\t1", "6.0\t1.0\t1"]
    # Плагин есть только в другом формате: ставится выключенным и без состояния.
    assert only_au["add"] == ["AU:Maker: OnlyAU"] and not only_au["on"] and "state" not in only_au
    assert Path(by_track[3]["fx"][0]["state"]).read_bytes() == b"<plist/>" and by_track[3]["channels"] == 4
    assert by_track[4]["fx"][0]["on"] is False  # дорожка заморожена
    assert by_track[5]["fx"][0]["on"] is False and "параллельная" in by_track[5]["fx"][0]["notes"][0]
    assert by_track[0]["fx"][0]["add"] == ["AU:Limiter"]

    plugins = report["plugins"]
    assert plugins["total"] == 8 and [m["plugin"] for m in plugins["missing"]] == ["Gone (VST3)"]
    # Возможные ARA-плагины только помечены: решает REAPER, когда собирает цепочки.
    assert [fx["label"] for e in plan for fx in e["fx"] if fx.get("maybe_ara")] == ["Comp (VST3)", "Tune (VST3)"] and plugins["ara"] == []
    assert [o["plugin"] for o in plugins["other_format"]] == ["OnlyAU (VST3)"]
    (native,) = report["native"]
    assert (native["track"], native["device"], native["automated"]) == ("2 Kick", "EQ Eight", ["Bands.0/Freq"])
    assert report["automation"]["mixer"] == 3 and report["automation"]["plugin_envelopes"] == 2
    assert [s["what"] for s in report["automation"]["skipped"]] == ["EQ Eight: Bands.0/Freq"]
    assert any("сайдчейн для Squash" in line for line in report["routing"])


def test_media_outside_project_is_linked_or_copied(song, tmp_path):
    outside = tmp_path / "elsewhere" / "bass.wav"
    outside.parent.mkdir()
    outside.write_bytes(b"RIFFRIFF")
    (song.parent / "Samples" / "bass.wav").unlink()
    xml = gzip.decompress(song.read_bytes()).decode().replace("/nowhere/Samples/bass.wav", str(outside))
    song.write_bytes(gzip.compress(xml.encode()))

    out, _, report, _ = build(song)
    assert out.tracks[2].items[0].file == str(outside) and report["media"]["outside"] == [str(outside)]
    out, _, report, _ = build(song, copy_media=True)
    assert out.tracks[2].items[0].file == "media/bass.wav" and (song.parent / "reaper/media/bass.wav").read_bytes() == b"RIFFRIFF"


def test_cli_writes_project_and_refuses_to_overwrite(song, monkeypatch, capsys):
    monkeypatch.setattr(importer, "Resolver", FakeResolver)
    monkeypatch.setattr(paths, "IPC", song.parent / ".ipc")  # моста нет
    assert cli.main(["import-als", str(song)]) == 1
    assert "мост не запущен" in capsys.readouterr().err

    assert cli.main(["import-als", str(song), "--no-plugins"]) == 0
    text = capsys.readouterr().out
    assert "дорожек 5 (папок 1, возвратов 1) · айтемов 3 (MIDI 0) · маркеров 1 · темп 60–120, изменений 1" in text
    assert "плагинов нет в REAPER (1): Gone (VST3)" in text and "родные устройства Ableton (1): EQ Eight" in text
    assert "файлов не найдено (1), их клипы пропущены: lost.wav" in text
    rpp_path = song.parent / "reaper" / "song.rpp"
    project = rpp_path.read_text()
    assert project.count("<TRACK") == 5 and project.count("<ITEM") == 3 and "<FXCHAIN" not in project
    assert "AUXRECV 1 3 0.5 0 0 0 0 0 0 -1:U 0 -1 ''" in project and "AUXRECV 1 3 1 0 0 0 0 0 2 -1:U 0 -1 ''" in project
    assert "<MASTERVOLENV2" in project
    data = song.parent / "reaper/.reamix/import/song"
    assert json.loads((data / "native.json").read_text())[0]["settings"] == {"Bands.0/Freq": "80"}
    assert "settings" not in json.loads((data / "report.json").read_text())["native"][0]

    rpp_path.write_text("мой микс")
    assert cli.main(["import-als", str(song), "--no-plugins"]) == 1
    assert "проект уже есть" in capsys.readouterr().err and rpp_path.read_text() == "мой микс"
    assert cli.main(["import-als", str(song), "--no-plugins", "--force"]) == 0 and rpp_path.read_text() == project


class FakeReaper:
    """Мост подменён: отвечает на запросы import.lua так, как отвечал бы REAPER."""

    def __init__(self, song, projects=(), active=""):
        self.song, self.calls = song, []
        self.host = {"projects": list(projects), "active": active, "active_tracks": 0 if not active else 8, "version": "7"}

    def __call__(self, code, **kwargs):
        name = code.split("return imp.")[1].split("(")[0]
        self.calls.append(name)
        if name == "host":
            return self.host
        if name == "probe_ara":
            assert code.split("probe_ara(")[1].split(", ", 1)[1] == '{"VST3:Comp", "VST3:Tune"})'
            return {"VST3:Comp": False, "VST3:Tune": True}
        if name == "build_chain":
            out = Path(code.split('}, "')[-1].split('"')[0])
            entries = code.count('["add"]')
            if out.stem == "003":
                return {"fx": [{"error": "REAPER не смог добавить плагин"}], "written": False}
            out.write_text(f'<FXCHAIN\nSHOW 0\n<VST "{out.stem}"\nQUJD\n>\n>')
            return {"fx": [{"name": "x", "state": i == 0, "envelopes": 2 if i == 0 and out.stem == "002" else 0} for i in range(entries)], "written": True}
        if name == "open":
            assert (self.song.parent / "reaper" / "song.rpp").is_file()  # открывается уже записанный проект
            return {"project": "song.rpp", "tracks": 5, "items": 3, "fx": 5, "offline": {}, "times": [62.0]}
        return True


def test_chains_from_reaper_go_into_the_project(song, monkeypatch, capsys):
    monkeypatch.setattr(importer, "Resolver", FakeResolver)
    monkeypatch.setattr(client, "alive", lambda: {"project": "", "rev": 1, "age": 0})
    reaper = FakeReaper(song)
    monkeypatch.setattr(client, "call", reaper)
    assert cli.main(["import-als", str(song)]) == 0
    text = capsys.readouterr().out
    assert "плагинов в Ableton 8 → в REAPER с настройками 4" in text
    assert "ARA-плагины не поставлены" in text and "(1): Tune (VST3)" in text
    assert "параметры плагинов 2 из 2" in text and "REAPER не смог добавить (1): Squash (AU)" in text
    assert "проект открыт в REAPER: дорожек 5, айтемов 3, плагинов 5" in text
    # Темп: REAPER поставил контрольную долю на 62-ю секунду, по расчёту — 66-я (2 с + 64 доли по секунде).
    assert "темповая карта REAPER расходится с расчётом на 4000.0 мс" in text
    assert reaper.calls == ["host", "workbench", "probe_ara"] + ["build_chain"] * 5 + ["open"]
    project = (song.parent / "reaper" / "song.rpp").read_text()
    assert project.count("<FXCHAIN") == 3 and project.count("<MASTERFXLIST") == 1
    kick = project[project.index('NAME "Kick"') : project.index('NAME "Bass"')]
    assert '<VST "002"' in kick and kick.index("<FXCHAIN") < kick.index("<ITEM")


def test_unsaved_work_in_any_reaper_tab_stops_the_import(song, monkeypatch, capsys):
    monkeypatch.setattr(importer, "Resolver", FakeResolver)
    monkeypatch.setattr(client, "alive", lambda: {"project": "", "rev": 1, "age": 0})
    tabs = [{"path": "/music/mix.rpp", "dirty": True, "tracks": 12}, {"path": "", "dirty": False, "tracks": 0}]
    reaper = FakeReaper(song, tabs)
    monkeypatch.setattr(client, "call", reaper)
    assert cli.main(["import-als", str(song)]) == 1
    assert "несохранённые изменения (/music/mix.rpp)" in capsys.readouterr().err
    assert reaper.calls == ["host"] and not (song.parent / "reaper").exists()
    assert cli.main(["import-als", str(song), "--unsaved-ok", "--no-open"]) == 0
    assert reaper.calls[-1] == "cleanup"


def test_busy_tab_needs_an_empty_one(song, monkeypatch, capsys):
    monkeypatch.setattr(importer, "Resolver", FakeResolver)
    monkeypatch.setattr(client, "alive", lambda: {"project": "/music/mix.rpp", "rev": 1, "age": 0})
    # В активной вкладке чужой проект: плагины в него не ставятся, вкладку открывает человек.
    reaper = FakeReaper(song, [{"path": "/music/mix.rpp", "dirty": False, "tracks": 8}], active="/music/mix.rpp")
    monkeypatch.setattr(client, "call", reaper)
    assert cli.main(["import-als", str(song)]) == 1
    assert "нужна пустая вкладка в REAPER" in capsys.readouterr().err and reaper.calls == ["host"]
    assert not (song.parent / "reaper").exists()


# ── Lua: base64 и блок AU ────────────────────────────────────────────────────


@pytest.fixture
def imp():
    lua = LuaRuntime(unpack_returned_tuples=True)
    module = lua.execute(f'return loadfile("{REPO}/bridge/import.lua")({{}}, function(t) return t end)')
    return lua, module


def test_base64_matches_python(imp):
    lua, module = imp
    for n in range(0, 9):
        text = "reamix!!"[:n]
        assert module.b64_encode(text) == base64.b64encode(text.encode()).decode()
        assert module.b64_decode(base64.b64encode(text.encode()).decode()) == text
    lua.globals().imp = module
    for n in (1, 2, 3, 95, 96, 97, 4000):
        encoded = base64.b64encode(os.urandom(n)).decode()
        same, size = lua.eval(f'(function(t) local raw = imp.b64_decode(t) return imp.b64_encode(raw) == t, #raw end)("{encoded}")')
        assert same and size == n


def au_block(plist: bytes, n_in=2, n_out=2) -> bytes:
    pins = lambda n: struct.pack("<I", n) + b"".join(struct.pack("<Q", 1 << i) for i in range(n))  # noqa: E731
    return struct.pack("<II", 1001, 0) + pins(n_in) + pins(n_out) + struct.pack("<I", len(plist)) + plist + b"\x01\x00\x00\x00\x00\x00\x00\x00\x00\x00"


def lines128(raw: bytes) -> str:
    text = base64.b64encode(raw).decode()
    return "\n".join(text[i : i + 128] for i in range(0, len(text), 128))


def test_au_state_is_replaced_inside_track_chunk(imp):
    lua, module = imp
    lua.globals().imp = module
    old = b"<?xml old?>" + b"o" * 300
    chunk = "\n".join([
        "<TRACK", "NAME x", "<FXCHAIN", "SHOW 0", "BYPASS 0 0 0",
        '<VST "VST: A" a.vst 0 "" 1', "QUJD", ">", "FXID {1}",
        '<AU "AU: First" "M: First" "" 1 2 3', lines128(au_block(old, n_in=4)), ">", "FXID {2}",
        "<PARMENV 1 0 1 0.5", "PT 0 1 0", ">",
        '<AU "AU: Second" "M: Second" "" 1 2 4', lines128(au_block(old)), ">", "FXID {3}", ">",
        "<ITEM", "POSITION 0", ">", ">",
    ])
    lua.globals().chunk = chunk
    # Второй AU — третий плагин цепочки (номер 2): огибающая между ними плагином не считается.
    patched = lua.eval('imp.patch_au(chunk, {[2] = "<?xml new?>"})')
    assert patched.split('<AU "AU: Second"')[0] == chunk.split('<AU "AU: Second"')[0]
    body = patched.split('<AU "AU: Second" "M: Second" "" 1 2 4\n')[1].split("\n>")[0]
    assert base64.b64decode(body) == au_block(b"<?xml new?>")
    assert patched.endswith("FXID {3}\n>\n<ITEM\nPOSITION 0\n>\n>")
    lua.globals().patched = patched
    assert lua.eval("imp.chain_of(patched)").startswith("<FXCHAIN\nSHOW 0") and lua.eval("imp.chain_of(patched)").endswith("FXID {3}\n>")
    assert lua.eval('imp.chain_of("<TRACK\\nNAME x\\n>")') is None


def test_ara_plugin_is_recognised_by_chunk_header(imp):
    _, module = imp
    assert module.is_ara_header('<VST "VST3: Vovious (Vovious)" Vovious.vst3 0 "" 236467053{ABCDEF019182FAEB444F5049564F5649} com.vovious.vovious.aradocumentarchive.version1')
    assert not module.is_ara_header('<VST "VST3: Pro-Q 4 (FabFilter)" "FabFilter Pro-Q 4.vst3" 0 "" 934538646{ED57BD725C60467EA64DD2F400758B6F} ""')
    assert not module.is_ara_header('<VST "VST: ReaComp (Cockos)" reacomp.vst.dylib 0 "" 1919247213<5653547265636D726561636F6D700000> ""')
