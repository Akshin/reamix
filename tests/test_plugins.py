"""Разбор списков плагинов REAPER."""

import pytest

from reamix import paths, plugins


@pytest.mark.parametrize(
    "text, expected",
    [
        ("Pro-Q 3 (FabFilter)", ("Pro-Q 3", "FabFilter")),
        ("FabFilter Pro-C 2 (Mono) (FabFilter)", ("FabFilter Pro-C 2 (Mono)", "FabFilter")),
        ("Kontakt 8 (Native Instruments) (64 out)", ("Kontakt 8", "Native Instruments")),
        ("ISOL8 (TBProAudio) (4->10ch)", ("ISOL8", "TBProAudio")),
        ("UADx SSL E Channel Strip (Universal Audio (UADx))", ("UADx SSL E Channel Strip", "Universal Audio (UADx)")),
        ("Spectropaint Synthesis", ("Spectropaint Synthesis", "")),
    ],
)
def test_split_name(text, expected):
    assert plugins._split_name(text) == expected


def test_cache_key_matches_reaper_naming():
    assert plugins.cache_key("FabFilter Pro-C 2 (Mono).vst") == "FabFilter_Pro_C_2__Mono_.vst"
    assert plugins.cache_key("WaveShell1-VST3 17.1.vst3") == "WaveShell1_VST3_17.1.vst3"


@pytest.fixture
def reaper_dir(tmp_path, monkeypatch):
    (tmp_path / "reaper-vstplugins_arm64.ini").write_text(
        "[vstcache]\n"
        "reaeq.vst.dylib=00EE,1919247729,ReaEQ (Cockos)\n"
        "FabFilter_Pro_Q_3.vst=00F0,1179726899,FabFilter Pro-Q 3 (FabFilter)\n"
        "FabFilter_Pro_Q_3.vst3=00F0,756089518{72C4,Pro-Q 3 (FabFilter)\n"
        "Gone.vst3=00F0,1{AA,Gone (Nobody)\n"
        "Shell.vst3=00E1,0,<SHELL>\n"
        "Broken.vst=0082\n"
        "Kontakt_8.vst3=00B0,952745140{5653,Kontakt 8 (Native Instruments) (64 out)!!!VSTi\n"
    )
    (tmp_path / "reaper-auplugins_arm64.ini").write_text(
        "[auplugins]\nFabFilter: Pro-Q 3=<!inst>\nApple: AUDelay=<!inst>\n"
    )
    (tmp_path / "reaper-jsfx.ini").write_text(
        'VERSION 1\nNAME analysis/loudness_meter "JS: Loudness Meter Peak/RMS/LUFS (Cockos)"\n'
    )
    (tmp_path / "reaper-fxtags.ini").write_text(
        "[category]\nFabFilter_Pro_Q_3.vst3=EQ\nreaeq.vst.dylib=EQ\nApple: AUDelay=Delay\n[developer]\nreaeq.vst.dylib=Cockos\n"
    )
    (tmp_path / "reaper-recentfx.ini").write_text(
        "[Recent]\nNb=2\nType0=3\nName0=/Library/Audio/Plug-Ins/VST3/FabFilter Pro-Q 3.vst3<756089518{72C4\n"
        "Type1=3\nName1=/Applications/REAPER.app/Contents/Plugins/FX/reaeq.vst.dylib<1919247729\n"
    )
    monkeypatch.setattr(paths, "REAPER", tmp_path)
    monkeypatch.setattr(plugins, "_on_disk", lambda: {"FabFilter_Pro_Q_3.vst", "FabFilter_Pro_Q_3.vst3", "Kontakt_8.vst3", "New.vst3"})
    return tmp_path


def test_scan_merges_formats_and_drops_missing(reaper_dir):
    found, summary = plugins.scan()
    by_name = {p.name: p for p in found}
    assert set(by_name) == {"ReaEQ", "Pro-Q 3", "Kontakt 8", "AUDelay", "Loudness Meter Peak/RMS/LUFS"}

    proq = by_name["Pro-Q 3"]
    assert proq.formats == ["VST3", "VST", "AU"]
    assert (proq.add, proq.category, proq.recent) == ("VST3:Pro-Q 3 (FabFilter)", "EQ", 0)
    assert by_name["ReaEQ"].add == "VST:ReaEQ (Cockos)" and by_name["ReaEQ"].recent == 1
    assert by_name["Kontakt 8"].instrument
    assert by_name["Loudness Meter Peak/RMS/LUFS"].add == "JS:analysis/loudness_meter"
    assert summary == {"plugins": 5, "missing": ["Gone.vst3"], "unscanned": ["New.vst3"]}


def test_find_hides_instruments_and_puts_recent_first(reaper_dir, tmp_path):
    found, _ = plugins.scan()
    catalog = tmp_path / "catalog" / "plugins.tsv"
    plugins.save(found, catalog)
    rows = plugins.load(catalog)
    assert [r["name"] for r in plugins.find(rows, ["eq"])] == ["Pro-Q 3", "ReaEQ"]
    assert plugins.find(rows, ["kontakt"]) == []
    assert [r["name"] for r in plugins.find(rows, ["kontakt"], instruments=True)] == ["Kontakt 8"]
    assert plugins.line(rows[0]).startswith(("* ", "  "))
