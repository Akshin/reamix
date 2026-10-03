"""Грув: атаки и их попадание в сетку на сигналах с известным сдвигом."""

import numpy as np
import pytest

from reamix import groove
from reamix.analysis import Audio

SR = 48000
PRE = groove.PREROLL
STEP = 0.125  # шестнадцатая на 120 уд/мин
LINES = np.arange(64) * STEP  # четыре такта 4/4
LABELS = groove.line_levels(np.arange(64) % 16, [1.0] * 64, 6)


def kick():
    t = np.arange(int(0.25 * SR)) / SR
    return np.sin(2 * np.pi * 60 * t) * np.exp(-t / 0.08)


def burst(seed=0):
    t = np.arange(int(0.06 * SR)) / SR
    return 0.5 * np.random.default_rng(seed).standard_normal(len(t)) * np.exp(-t / 0.015)


def swell():
    # Нота с нарастанием 20 мс: «начало» у неё размыто.
    t = np.arange(int(0.3 * SR)) / SR
    return np.sin(2 * np.pi * 110 * t) * np.minimum(t / 0.02, 1) * np.exp(-np.maximum(t - 0.15, 0) / 0.03)


def track(times, sound, gains=None, seconds=8.0):
    """Удары в заданные моменты участка; перед участком — заход, как в рендере команды."""
    out = np.zeros(int((PRE + seconds) * SR))
    for i, at in enumerate(times):
        a = int(round((PRE + at) * SR))
        s = sound()[: len(out) - a]
        out[a : a + len(s)] += s * (1 if gains is None else gains[i])
    return Audio(np.column_stack([out, out]), SR)


def hits_of(audio, **kw):
    hits = groove.onsets(audio, **kw)
    return hits | {"t": hits["t"] - PRE}


def test_hits_on_the_grid_read_zero():
    row = groove.against_grid(hits_of(track(np.arange(16) * 0.5, kick)), LINES, LABELS, 6)
    assert row["hits"] == 16 and list(row["levels"]) == ["доли"]
    assert row["offset"] == pytest.approx(0, abs=0.3) and row["spread"] < 0.1
    assert row["attack"] < 1 and not row["loose"] and row["swing"] is None


def test_laid_back_snare_reads_its_delay_and_jitter_reads_as_spread():
    late = np.arange(8) + 0.5 + 0.012
    row = groove.against_grid(hits_of(track(late, burst)), LINES, LABELS, 6)
    assert row["hits"] == 8 and row["offset"] == pytest.approx(12, abs=0.3) and row["spread"] < 0.1
    jitter = np.random.default_rng(3).normal(0, 0.004, 64)
    row = groove.against_grid(hits_of(track(LINES[4:] + 0.012 + jitter[4:], burst)), LINES, LABELS, 6)
    assert row["offset"] == pytest.approx(12, abs=2) and row["spread"] == pytest.approx(4, abs=1.5)


def test_swing_is_not_sloppiness():
    # Слабые шестнадцатые на 15 мс позже: пара длится 250 мс, слабая стоит на 56 %.
    swung = LINES + np.where(np.arange(64) % 2 == 1, 0.015, 0)
    row = groove.against_grid(hits_of(track(swung, burst)), LINES, LABELS, 6)
    assert {k: (v["hits"], round(v["offset"])) for k, v in row["levels"].items()} == {"доли": (16, 0), "1/8": (16, 0), "1/16": (32, 15)}
    assert row["offset"] == pytest.approx(7.5, abs=0.3) and row["spread"] < 0.2
    assert row["swing"][0] == "1/16" and row["swing"][1] == pytest.approx(56, abs=0.2)


def test_triplet_shuffle_needs_its_own_grid():
    # Шаффл: слабая восьмая на третьей триоли. Сетка 1/8 читает его как свинг 67 %,
    # триольная — как попадание в линию.
    beats = np.arange(16) * 0.5
    shuffle = np.sort(np.concatenate([beats, beats + 1 / 3]))
    hits = hits_of(track(shuffle, burst))
    eighths = np.arange(32) * 0.25
    row = groove.against_grid(hits, eighths, groove.line_levels(np.arange(32) % 8, [1.0] * 32, 12), 12)
    assert row["swing"][0] == "1/8" and row["swing"][1] == pytest.approx(66.7, abs=0.3)
    triplets = np.arange(48) / 6
    row = groove.against_grid(hits, triplets, groove.line_levels(np.arange(48) % 12, [1.0] * 48, 8), 8)
    assert row["levels"]["1/8т"]["offset"] == pytest.approx(0, abs=0.3) and row["swing"] is None


def test_quiet_bleed_between_hits_is_not_counted():
    gains = np.where(np.arange(64) % 4 == 0, 1.0, 0.05)  # между ударами — пролезание на −26 дБ
    audio = track(LINES, burst, gains)
    assert len(groove.onsets(audio)["t"]) == 16
    assert len(groove.onsets(audio, floor_db=40)["t"]) == 64


def test_slow_attack_is_measured_late_and_says_so():
    row = groove.against_grid(hits_of(track(np.arange(16) * 0.5, swell)), LINES, LABELS, 6)
    assert row["hits"] == 16 and 3 < row["offset"] < 12
    assert row["attack"] > groove.SLOW_ATTACK_MS
    assert "атака медленная" in groove.table("грув", [{"n": 4, "name": "Bass"} | row])


def test_sustained_and_silent_tracks_have_no_hits(tmp_path):
    import soundfile

    t = np.arange(8 * SR) / SR
    tone = np.sin(2 * np.pi * 80 * t)
    # Ровный звук атак не даёт — кроме края файла, если рендер начался посреди звука.
    assert list(groove.onsets(Audio(np.column_stack([tone, tone])[SR:], SR))["t"]) == pytest.approx([0], abs=0.001)
    assert len(groove.onsets(Audio(np.zeros((SR, 2)), SR))["t"]) == 0
    # Команда рендерит с заходом и край не считает.
    soundfile.write(tmp_path / "pad.wav", np.column_stack([tone, tone]), SR, subtype="FLOAT")
    hits, silent = groove.read_onsets(tmp_path / "pad.wav", 16.0 - PRE, PRE)
    assert len(hits["t"]) == 0 and not silent
    soundfile.write(tmp_path / "empty.wav", np.zeros((SR, 2)), SR, subtype="FLOAT")
    assert groove.read_onsets(tmp_path / "empty.wav", 0.0, 0.0)[1]


def test_hits_beyond_the_range_are_dropped():
    # Удар за полшага до участка и удар на его правом краю к участку не относятся.
    row = groove.against_grid(hits_of(track([-0.1, 0.0, 0.5, 1.0, 1.5, 2.0], burst)), LINES[:16], LABELS[:16], 6)
    assert row["hits"] == 4


def test_levels_follow_the_time_signature():
    assert LABELS[:8] == ["доли", "1/16", "1/8", "1/16", "доли", "1/16", "1/8", "1/16"]
    # В 6/8 доля — восьмая: слабых восьмых нет.
    assert groove.line_levels(range(4), [0.5] * 4, 6) == ["доли", "1/16", "доли", "1/16"]
    assert groove.line_levels(range(6), [1.0] * 6, 4) == ["доли", "1/16т", "1/8т", "1/8", "1/8т", "1/16т"]
    assert groove.line_levels(range(2), [2.0] * 2, 24) == ["доли", "1/4"]


def test_anchor_compares_coinciding_hits_only():
    hat = hits_of(track(LINES, burst))
    snare = hits_of(track(np.arange(8) + 0.5 + 0.007, burst))
    row = groove.against_anchor(snare, hat["t"])
    assert row["hits"] == 8 and row["matched"] == 8
    assert row["offset"] == pytest.approx(7, abs=0.3) and row["spread"] < 0.1
    # Опора играет редко: ударам между её ударами не с чем сравниться.
    rare = groove.against_anchor(hat, snare["t"])
    assert rare["hits"] == 64 and rare["matched"] == 8 and rare["offset"] == pytest.approx(-7, abs=0.3)
    assert "offset" not in groove.against_anchor(snare, hat["t"][:1])


def test_part_off_the_grid_is_flagged_and_its_places_are_not_shown():
    # Удары в случайные моменты: разброс — около трети шага сетки.
    free = np.sort(np.random.default_rng(5).uniform(0.2, 7.8, 40))
    free = free[np.concatenate([[True], np.diff(free) > 0.08])]
    row = groove.against_grid(hits_of(track(free, burst)), LINES, LABELS, 6)
    assert row["loose"] and row["spread"] > 0.2 * 125
    line = groove.table("грув", [{"n": 5, "name": "Vox"} | row]).splitlines()[2]
    assert "к сетке не привязана" in line and "свинг" not in line and line.count("—") == 3


def test_table_shows_levels_and_names_what_is_left_out():
    swung = LINES + np.where(np.arange(64) % 2 == 1, 0.015, 0)
    rows = [
        {"n": 2, "name": "Kick"} | groove.against_grid(hits_of(track(np.arange(16) * 0.5, kick)), LINES, LABELS, 6),
        {"n": 5, "name": "Hat", "shift": 3.0} | groove.against_grid(hits_of(track(swung, burst)), LINES, LABELS, 6),
        {"n": 7, "name": "Pad"} | groove.against_grid(groove.onsets(Audio(np.zeros((SR, 2)), SR)), LINES, LABELS, 6),
        {"n": 9, "name": "FX", "silent": True, "hits": 0},
    ]
    assert groove.table("грув · " + groove.tempo(LINES, 6), rows).splitlines() == [
        "грув · 120 bpm · сетка 1/16 = 125 мс",
        "           ударов  среднее  разброс     доли      1/8     1/16    атака",
        "  2 Kick       16     +0.1     ±0.0     +0.1        —        —       <1",
        "  5 Hat        64     +7.6     ±0.0     +0.1     +0.1    +15.1        1  · свинг 1/16 56% · дорожка сдвинута на +3 мс",
        "атак меньше 4 — не в таблице: 7 Pad",
        "молчат на участке: 9 FX",
    ]
