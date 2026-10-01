"""Проект из мультитрека: осмотр папки, темп и регионы из .als, раскладка, сборка .rpp."""

import gzip
import json

import numpy as np
import pytest
import soundfile

from reamix import cli, client, multitrack, paths, rpp

SR = 48000


def tone(freq: float, seconds: float = 4.0, start: float = 0.0, level: float = 0.25) -> np.ndarray:
    t = np.arange(int(SR * seconds)) / SR
    return level * np.sin(2 * np.pi * freq * t) * (t >= start)


def write(path, left, right=None, sr=SR):
    path.parent.mkdir(parents=True, exist_ok=True)
    soundfile.write(str(path), left if right is None else np.stack([left, right], axis=1), sr, subtype="FLOAT")


def als(folder, tempo=92.0, locators=((8, "Verse"), (24, "Chorus"))):
    """Проект Ableton с группой DRUMS (в ней KICK) и дорожкой VOX — столько, сколько читает осмотр."""

    def track(tag, tid, name, group=-1):
        return (
            f'<{tag} Id="{tid}"><Name><EffectiveName Value="{name}"/></Name><TrackGroupId Value="{group}"/>'
            f'<DeviceChain><Mixer><Speaker><Manual Value="true"/></Speaker></Mixer></DeviceChain></{tag}>'
        )

    master = (
        '<MainTrack Id="0"><Name><EffectiveName Value="Main"/></Name><DeviceChain><Mixer>'
        f'<Tempo><Manual Value="{tempo}"/></Tempo><TimeSignature><Manual Value="200"/></TimeSignature></Mixer></DeviceChain></MainTrack>'
    )
    marks = "".join(f'<Locator Id="{i}"><Time Value="{beat}"/><Name Value="{name}"/></Locator>' for i, (beat, name) in enumerate(locators))
    xml = (
        '<?xml version="1.0" encoding="UTF-8"?><Ableton Creator="Ableton Live 12.1"><LiveSet><Tracks>'
        + track("GroupTrack", 1, "DRUMS") + track("AudioTrack", 2, "KICK", 1) + track("AudioTrack", 3, "VOX") + track("AudioTrack", 4, "VOX")
        + f"</Tracks>{master}<Locators><Locators>{marks}</Locators></Locators></LiveSet></Ableton>"
    )
    (folder / "Song.als").write_bytes(gzip.compress(xml.encode()))


@pytest.fixture
def stems(tmp_path):
    folder = tmp_path / "My Song"
    write(folder / "Song KICK.wav", tone(60), tone(60))  # оба канала одинаковы
    write(folder / "Song VOX.wav", tone(440, start=2.0), tone(440, start=2.0) * 0.5)
    write(folder / "Song HAT.wav", tone(9000, level=0.1))  # моно
    write(folder / "Song muted.wav", np.zeros(SR * 4))
    write(folder / "gtr" / "Song GTR.wav", tone(330, seconds=2.0), tone(660, seconds=2.0), sr=44100)
    (folder / "reaper" / "media").mkdir(parents=True)
    write(folder / "reaper" / "media" / "render.wav", tone(100))
    write(folder / "Backup" / "old.wav", tone(100))
    write(folder / "references" / "ref.wav", tone(100))  # референсы — не мультитрек
    write(folder / "Samples" / "sample.wav", tone(100, seconds=4.0))
    return folder


def by_name(result):
    return {f["name"]: f for f in result["files"]}


def test_scan_measures_files_and_skips_own_folders(stems):
    result = multitrack.scan(stems)
    files = by_name(result)
    # Общее начало имён («Song ») убрано; свои и служебные папки не осматриваются, Samples без .als — обычная папка.
    assert sorted(files) == ["GTR", "HAT", "KICK", "VOX", "muted", "sample"]
    kick, vox, hat = files["KICK"], files["VOX"], files["HAT"]
    assert kick["dual_mono"] and not vox["dual_mono"] and hat["channels"] == 1
    assert kick["low"] > 0.99 and hat["high"] > 0.99 and vox["low"] < 0.01 and vox["high"] < 0.01
    assert (vox["start"], vox["end"]) == pytest.approx((2.0, 4.0), abs=0.11) and kick["start"] == 0
    assert kick["peak_db"] == pytest.approx(-12.04, abs=0.05) and kick["crest"] == pytest.approx(3.0, abs=0.1)
    assert files["muted"]["silent"] and files["GTR"]["sr"] == 44100 and files["GTR"]["path"] == "gtr/Song GTR.wav"
    assert result["als"] is None
    # Повторный осмотр берёт измерения из кэша: файл не перечитывается, пока не изменился.
    cached = json.loads((stems / "reaper/.reamix/multitrack/scan.json").read_text())
    cached["files"][0]["peak_db"] = 123.0
    (stems / "reaper/.reamix/multitrack/scan.json").write_text(json.dumps(cached))
    assert multitrack.scan(stems)["files"][0]["peak_db"] == 123.0


def test_als_gives_tempo_regions_and_groups(stems):
    als(stems)
    result = multitrack.scan(stems)
    info = result["als"]
    assert (info["file"], info["tempo"], info["timesig"], info["tempo_changes"]) == ("Song.als", 92.0, [3, 4], False)
    assert [(round(m["time"], 3), m["name"]) for m in info["locators"]] == [(5.217, "Verse"), (15.652, "Chorus")]
    files = by_name(result)
    assert "sample" not in files  # рядом с .als папка Samples — сэмплы проекта Ableton, а не мультитрек
    assert files["KICK"]["als"]["groups"] == ["DRUMS"]
    assert "als" not in files["VOX"]  # в проекте две дорожки VOX: по имени файла их не различить

    draft = multitrack.draft(result).splitlines()
    assert draft[2:4] == ["темп 92", "размер 3/4"]
    # Локатор — точка: регион идёт от него до следующего, последний — до конца записи.
    assert draft[4:7] == ["регион 0:00.000 0:05.217 начало", "регион 0:05.217 0:15.652 Verse", ""]
    body = draft[7:]
    assert body == ["HAT <- Song HAT.wav", "VOX <- Song VOX.wav", "gtr", "  GTR <- gtr/Song GTR.wav", "DRUMS", "  KICK <- Song KICK.wav"]


def test_report_is_compact_and_flags_problems(stems):
    result = multitrack.scan(stems)
    text = multitrack.report(result, stems / "layout.txt")
    lines = text.splitlines()
    assert lines[0].endswith("My Song · файлов 6 · 0:04.0 · 44100/48000 Гц") and ".als в папке нет" in lines[1]
    assert any("Song KICK.wav" in line and "стерео=моно" in line and "100/0" in line for line in lines)
    assert "пустые, в раскладку не вошли (1): Song muted.wav" in text
    assert "частота дискретизации разная: 44100 Гц — 1, 48000 Гц — 4" in text
    assert "короче остальных (1)" in text and "gtr/Song GTR.wav 0:02.2" in text


LAYOUT = """\
# комментарий
темп 92,5
размер 6/8
регион 0:00 0:05.217 Intro
регион 5.217 1:02 Verse 1

DRUMS
  Kick <- Song KICK.wav
  CYMBALS
    <- Song HAT.wav
Lead Vox <- Song VOX.wav
"""


def test_layout_is_parsed():
    layout = multitrack.parse_layout(LAYOUT)
    assert (layout.tempo, layout.timesig) == (92.5, (6, 8))
    assert layout.regions == [(0.0, 5.217, "Intro"), (5.217, 62.0, "Verse 1")]
    assert layout.rows == [(0, "DRUMS", None), (1, "Kick", "Song KICK.wav"), (1, "CYMBALS", None), (2, "Song HAT", "Song HAT.wav"), (0, "Lead Vox", "Song VOX.wav")]
    assert multitrack.parse_layout("темп ?\nA <- a.wav\n").tempo is None
    with pytest.raises(multitrack.MultitrackError, match="строка 2: отступ не под папкой"):
        multitrack.parse_layout("A <- a.wav\n  B <- b.wav\n")
    with pytest.raises(multitrack.MultitrackError, match="строка 1: не разобрать"):
        multitrack.parse_layout("регион начало конец\n")


def test_project_is_assembled_from_layout(stems):
    result = multitrack.scan(stems)
    project, notes = multitrack.assemble(multitrack.parse_layout(LAYOUT), result, stems / "reaper")
    assert [(t.name, t.folder) for t in project.tracks] == [("DRUMS", (1, 1)), ("Kick", (0, 0)), ("CYMBALS", (1, 1)), ("Song HAT", (2, -2)), ("Lead Vox", (0, 0))]
    drums, kick, _, hat, vox = project.tracks
    assert drums.color == kick.color == hat.color == multitrack.COLORS[0] and vox.color is None
    assert (kick.items[0].file, kick.items[0].position, kick.items[0].length) == ("../Song KICK.wav", 0.0, 4.0)
    assert kick.vol == 1.0 and kick.pan_law == -1.0  # баланс уже в файлах, закон панорамы — как в REAPER
    assert notes == ["не вошли в раскладку (2): gtr/Song GTR.wav, Samples/sample.wav"]

    text = rpp.render(project)
    assert "TEMPO 92.5 6 8" in text and 'MARKER 2 5.217 "Verse 1" 1' in text and 'MARKER 2 62 "" 1' in text

    fail = lambda body: multitrack.assemble(multitrack.parse_layout("темп 90\n" + body), result, stems / "reaper")  # noqa: E731
    with pytest.raises(multitrack.MultitrackError, match="файла из раскладки нет в папке: nope.wav"):
        fail("X <- nope.wav\n")
    with pytest.raises(multitrack.MultitrackError, match="стоит в раскладке дважды"):
        fail("A <- Song KICK.wav\nB <- Song KICK.wav\n")
    with pytest.raises(multitrack.MultitrackError, match="пустой или не читается"):
        fail("A <- Song muted.wav\n")
    with pytest.raises(multitrack.MultitrackError, match="не задан темп"):
        multitrack.assemble(multitrack.parse_layout("темп ?\nA <- Song KICK.wav\n"), result, stems / "reaper")


def test_cli_scan_then_build(stems, monkeypatch, capsys):
    monkeypatch.setattr(paths, "IPC", stems.parent / ".ipc")  # моста нет
    monkeypatch.setattr(multitrack, "reaper_running", lambda: True)
    monkeypatch.chdir(stems)
    layout = stems / "reaper/.reamix/multitrack/layout.txt"
    assert cli.main(["multitrack"]) == 0
    assert f"черновик раскладки: {layout.resolve()}" in capsys.readouterr().out and "темп ?" in layout.read_text()

    # Темпа ещё нет — сборка объясняет, что делать; раскладку, поправленную руками, осмотр не затирает.
    assert cli.main(["multitrack", "--build"]) == 1
    assert "не задан темп" in capsys.readouterr().err
    layout.write_text(LAYOUT)
    assert cli.main(["multitrack"]) == 0 and layout.read_text() == LAYOUT
    capsys.readouterr()

    assert cli.main(["multitrack", "--build"]) == 0
    out = capsys.readouterr().out
    project = stems / "reaper" / "My Song.rpp"
    assert str(project.resolve()) in out and "дорожек 3 · папок 2 · темп 92.5 · размер 6/8 · регионов 2" in out
    assert "в REAPER проект не открыт: REAPER открыт, но мост не запущен" in out
    text = project.read_text()
    assert text.count("<TRACK") == 5 and text.count("<ITEM") == 3 and 'FILE "../Song VOX.wav"' in text
    assert (stems / "references").is_dir() and f"референсы клади в {(stems / 'references').resolve()}" in out

    project.write_text("мой микс")
    assert cli.main(["multitrack", "--build"]) == 1 and project.read_text() == "мой микс"
    assert cli.main(["multitrack", "--build", "--force"]) == 0 and project.read_text() == text


def test_built_project_opens_in_blank_reaper_tab(stems, monkeypatch, capsys):
    monkeypatch.chdir(stems)
    cli.main(["multitrack"])
    (stems / "reaper/.reamix/multitrack/layout.txt").write_text(LAYOUT)
    calls = []

    def call(code, **kwargs):
        calls.append(code.split("return imp.")[1].split("(")[0])
        return {"project": "x", "tracks": 5, "items": 3, "fx": 0, "offline": {}, "times": {}}

    monkeypatch.setattr(multitrack, "reaper_running", lambda: True)
    monkeypatch.setattr(client, "alive", lambda: {"project": "", "age": 0})
    monkeypatch.setattr(client, "call", call)
    assert cli.main(["multitrack", "--build"]) == 0
    assert calls == ["workbench", "open"] and "проект открыт в REAPER и сохранён: дорожек 5, айтемов 3" in capsys.readouterr().out

    def busy(code, **kwargs):
        raise client.BridgeError("активная вкладка REAPER не пустая")

    monkeypatch.setattr(client, "call", busy)
    assert cli.main(["multitrack", "--build", "--force"]) == 0
    assert "в активной вкладке REAPER другой проект" in capsys.readouterr().out


def test_closed_reaper_is_launched_with_the_project(stems, monkeypatch, capsys):
    monkeypatch.chdir(stems)
    cli.main(["multitrack"])
    (stems / "reaper/.reamix/multitrack/layout.txt").write_text(LAYOUT)
    project = (stems / "reaper" / "My Song.rpp").resolve()
    launched, calls = [], []
    monkeypatch.setattr(multitrack, "reaper_running", lambda: False)
    monkeypatch.setattr(multitrack.subprocess, "run", lambda cmd, **kw: launched.append(cmd) or type("Done", (), {"returncode": 0})())
    # Мост стартует вместе с REAPER и сообщает, что активен именно собранный проект.
    monkeypatch.setattr(client, "alive", lambda: {"project": str(project), "age": 0} if launched else None)

    def call(code, **kwargs):
        calls.append((code.split("return imp.")[1].split("(")[0], kwargs.get("project")))
        return {"tracks": 5, "items": 3, "offline": {}, "saved": True}

    monkeypatch.setattr(client, "call", call)
    assert cli.main(["multitrack", "--build"]) == 0
    assert launched == [["open", "-a", "REAPER", str(project)]] and calls == [("settle", project.parent)]
    assert "REAPER запущен, проект открыт и сохранён: дорожек 5, айтемов 3" in capsys.readouterr().out


def test_session_in_multitrack_folder_works_with_project_in_reaper(stems, monkeypatch, capsys):
    monkeypatch.chdir(stems)
    assert cli.main(["init"]) == 1  # проекта ещё нет
    assert cli.track_dir() == stems.resolve()
    (stems / "reaper" / "My Song.rpp").write_text("<REAPER_PROJECT\n>\n")
    # Защита «проект лежит в папке, откуда вызвана команда» принимает reaper/ внутри папки трека.
    assert cli.track_dir() == (stems / "reaper").resolve()
    capsys.readouterr()
    assert cli.main(["init"]) == 0
    assert (stems / "CLAUDE.md").is_file() and (stems / "docs" / "CURRENT.md").is_file() and (stems / "reaper" / ".reamix").is_dir()
    assert not (stems / "reaper" / "CLAUDE.md").exists()


def test_reference_is_found_in_references_folder(stems, monkeypatch):
    monkeypatch.chdir(stems)
    (stems / "reaper" / "My Song.rpp").write_text("<REAPER_PROJECT\n>\n")
    refs = stems / "references"
    (refs / "ref.wav").unlink()
    with pytest.raises(client.BridgeError, match="референсов нет: положи файл в"):
        cli.reference_file(None)
    write(refs / "Daft Punk.wav", tone(100))
    assert cli.reference_file(None) == refs / "Daft Punk.wav"  # один файл — берётся без имени
    write(refs / "Other.wav", tone(100))
    with pytest.raises(client.BridgeError, match="несколько файлов, укажи какой: Daft Punk.wav, Other.wav"):
        cli.reference_file(None)
    assert cli.reference_file("daft") == refs / "Daft Punk.wav" and cli.reference_file("Other.wav") == refs / "Other.wav"
    assert cli.reference_file(str(refs / "Other.wav")) == refs / "Other.wav"
    with pytest.raises(client.BridgeError, match=r"файла нет: nope \(в references/: Daft Punk.wav, Other.wav\)"):
        cli.reference_file("nope")
