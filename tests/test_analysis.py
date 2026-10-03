"""Точность метрик анализа на сигналах с известным ответом."""

import numpy as np
import pytest

from reamix import analysis
from reamix.analysis import Audio

SR = 48000


def sine(freq, amp, seconds=6.0, phase=0.0):
    t = np.arange(int(seconds * SR)) / SR
    return amp * np.sin(2 * np.pi * freq * t + phase)


def stereo(left, right=None):
    return Audio(np.column_stack([left, left if right is None else right]), SR)


def db(amp):
    return 20 * np.log10(amp)


def test_loudness_matches_bs1770_reference():
    # Опорный сигнал EBU: стереосинус 1 кГц с пиком −23 дБFS даёт −23 LUFS.
    audio = stereo(sine(1000, 10 ** (-23 / 20)))
    m, series = analysis.measure(audio, [{"name": "all", "start": 0, "end": 6}])
    assert m["lufs"] == pytest.approx(-23.0, abs=0.1)
    assert m["regions"]["all"] == pytest.approx(-23.0, abs=0.1)
    assert series["short_term"] == pytest.approx(-23.0, abs=0.1)
    assert series["momentary"] == pytest.approx(-23.0, abs=0.1)
    assert m["lra"] == pytest.approx(0.0, abs=0.1)
    assert len(series["momentary"]) == 57  # (6 − 0,4) / 0,1 + 1


def test_band_levels_are_rms_dbfs():
    m, _ = analysis.measure(stereo(sine(100, 0.5)), [])
    bands = dict(zip(analysis.BAND_LABELS, m["bands_db"]))
    assert bands["60-120"] == pytest.approx(db(0.5) - 3.01, abs=0.1)
    assert all(v < -70 for k, v in bands.items() if k != "60-120")
    assert m["rms_db"] == pytest.approx(db(0.5) - 3.01, abs=0.05)
    assert m["peak_db"] == pytest.approx(db(0.5), abs=0.05)
    assert m["crest_db"] == pytest.approx(3.01, abs=0.05)


def test_true_peak_sees_between_samples():
    # Синус на четверти частоты дискретизации со сдвигом 45°: сэмплы попадают на ±0,707 пика.
    audio = stereo(sine(SR / 4, 1.0, phase=np.pi / 4))
    assert db(np.abs(audio.data).max()) == pytest.approx(-3.01, abs=0.01)
    assert analysis.true_peak_db(audio) == pytest.approx(0.0, abs=0.2)


def test_stereo_metrics():
    tone = sine(80, 0.5)
    assert analysis.stereo(stereo(tone))["correlation"] == pytest.approx(1.0)
    assert analysis.stereo(stereo(tone))["width_db"] < -100
    flipped = analysis.stereo(stereo(tone, -tone))
    assert flipped["correlation"] == pytest.approx(-1.0)
    assert flipped["correlation_low"] == pytest.approx(-1.0, abs=0.01)
    assert analysis.stereo(stereo(tone, tone / 2))["balance_db"] == pytest.approx(6.02, abs=0.01)
    wide = analysis.stereo(stereo(sine(300, 0.5), sine(300, 0.5, phase=np.pi / 2)))
    assert wide["correlation"] == pytest.approx(0.0, abs=0.01)
    assert wide["width_db"] == pytest.approx(0.0, abs=0.1)


def test_silence_is_flagged():
    m, series = analysis.measure(stereo(np.zeros(SR * 2)), [])
    assert m["silent"] and series == {}


def series_of(*signals):
    return {name: analysis.measure(stereo(sig), [])[1] for name, sig in signals}


def test_masking_is_directional():
    found = analysis.conflicts(series_of(("kick", sine(55, 0.1)), ("bass", sine(55, 0.8)), ("hat", sine(8000, 0.3))), set())
    assert [(c["victim"], c["masker"]) for c in found] == [("kick", "bass")]
    hit = found[0]
    assert hit["lo"] < 55 < hit["hi"]
    assert hit["diff_db"] == pytest.approx(db(0.8 / 0.1), abs=0.5)
    assert hit["overlap"] == pytest.approx(1.0)


def test_equal_levels_mask_each_other_and_skip_pairs_are_ignored():
    signals = (("a", sine(200, 0.3)), ("b", sine(200, 0.3)))
    found = analysis.conflicts(series_of(*signals), set())
    assert {(c["victim"], c["masker"]) for c in found} == {("a", "b"), ("b", "a")}
    assert analysis.conflicts(series_of(*signals), {frozenset(("a", "b"))}) == []


def test_bus_fader_after_the_stem_counts_in_masking():
    # Стемы сняты до фейдеров шин: дорожка, чья шина убрана на 20 дБ, в миксе никого не накрывает.
    signals = (("a", sine(200, 0.3)), ("b", sine(200, 0.3)))
    found = analysis.conflicts(series_of(*signals), set(), {"b": -20.0})
    assert [(c["victim"], c["masker"]) for c in found] == [("b", "a")]
    assert found[0]["diff_db"] == pytest.approx(20, abs=0.5)


def test_segments_merge_short_gaps_and_keep_the_longest():
    mask = np.zeros(300, dtype=bool)
    mask[10:40] = True      # 1,0–4,0 с
    mask[43:60] = True      # пауза 0,3 с — тот же участок, до 6,0 с
    mask[100:105] = True    # 0,5 с — слишком коротко
    mask[150:200] = True    # 15–20 с
    mask[220:232] = True    # 22,0–23,2 с
    mask[260:290] = True    # 26–29 с
    assert analysis.segments(mask, 0.1) == [[1.0, 6.0], [15.0, 20.0], [26.0, 29.0]]
    assert analysis.segments(np.zeros(50, dtype=bool), 0.1) == []


def test_clipping_reports_where_and_how_much():
    signal = sine(100, 0.5, seconds=4.0)
    signal[SR : SR + 200] = 1.5          # событие на 1,0 с
    signal[2 * SR : 2 * SR + 10] = -2.0  # событие на 2,0 с, сильнее
    clip = analysis.clipping(stereo(signal))
    assert clip["count"] == 2
    assert [round(e["t"], 2) for e in clip["events"]] == [1.0, 2.0]
    assert clip["events"][1]["peak_db"] == pytest.approx(6.02, abs=0.01)
    assert analysis.clipping(stereo(sine(100, 0.5)))["count"] == 0


def noise(seconds=4.0, seed=1):
    return np.random.default_rng(seed).standard_normal(int(seconds * SR)) * 0.1


def test_phase_finds_delay_and_polarity():
    source = noise()
    same = analysis.phase(stereo(source), stereo(source))
    assert same["correlation"] == pytest.approx(1.0, abs=0.01) and same["best_lag_samples"] == 0
    assert same["low_sum_db"] == pytest.approx(3.01, abs=0.05)
    assert analysis.phase_verdict(same) == "в фазе"

    flipped = analysis.phase(stereo(source), stereo(-source))
    assert flipped["correlation"] == pytest.approx(-1.0, abs=0.01)
    assert flipped["low_sum_db"] < -40
    assert analysis.phase_verdict(flipped).startswith("противофаза")

    late = analysis.phase(stereo(source), stereo(np.roll(source, 96)))  # вторая позже на 2 мс
    assert late["best_lag_samples"] == 96 and late["best_lag_ms"] == pytest.approx(2.0)
    assert late["best_correlation"] == pytest.approx(1.0, abs=0.02)
    assert abs(late["correlation"]) < 0.5
    assert analysis.phase_verdict(late).startswith("сдвиг 2.00 мс (96 сэмпл.): вторая позже первой")

    unrelated = analysis.phase(stereo(source), stereo(noise(seed=2)))
    assert "почти не связаны" in analysis.phase_verdict(unrelated)
    assert analysis.phase_verdict(analysis.phase(stereo(source), stereo(np.zeros_like(source)))) == "одна из дорожек молчит"


def test_level_reads_like_a_vu_meter_and_skips_pauses():
    tone = sine(1000, 10 ** (-18 / 20))
    assert analysis.level(stereo(tone))["avg"] == pytest.approx(-18.0, abs=0.05)
    # Звук только в одном канале и низкая частота показывают то же: взвешивания нет.
    assert analysis.level(stereo(tone, np.zeros_like(tone)))["avg"] == pytest.approx(-18.0, abs=0.05)
    low = analysis.level(stereo(sine(40, 10 ** (-18 / 20))))
    assert low["avg"] == pytest.approx(-18.0, abs=0.05) and low["lufs"] < -22
    # Паузы между фразами средний уровень не занижают.
    sparse = np.concatenate([tone, np.zeros(len(tone) * 3)])
    assert analysis.level(stereo(sparse))["avg"] == pytest.approx(-18.0, abs=0.3)
    assert analysis.level(stereo(np.zeros(SR)))["avg"] is None


def test_trim_brings_average_to_working_level_unless_peaks_object():
    assert analysis.trim_gain(-12.0, -3.0) == (-6.0, False)
    assert analysis.trim_gain(-25.0, -15.0) == (7.0, False)
    # Ударное: по среднему можно поднять на 12 дБ, но пики тогда уйдут выше −6.
    assert analysis.trim_gain(-30.0, -8.0) == (2.0, True)


def test_ride_vs_mix_follows_the_backing():
    # Вокал ровный (−20), а остальной микс в припеве громче на 6 LU: вокал там проседает относительно него.
    vocal = np.full(100, -20.0)
    rest = np.concatenate([np.full(50, -22.0), np.full(50, -16.0)])
    assert analysis.distance_spread(vocal, rest) == pytest.approx(3.0, abs=0.01)
    curve = analysis.ride_curve_vs(vocal, rest, max_db=4.0, smooth=0.3)
    at = lambda t: np.interp(t, [p[0] for p in curve], [p[1] for p in curve])
    assert at(2.5) == pytest.approx(-3.0, abs=0.1)   # в куплете вокал слишком впереди
    assert at(8.0) == pytest.approx(+3.0, abs=0.1)   # в припеве подтягиваем за миксом
    corrected = vocal + np.array([at(i * 0.1 + 0.2) for i in range(100)])
    assert analysis.distance_spread(corrected, rest) < 0.7
    # Сдвиг расстояния: вокал на 2 дБ громче относительно микса везде.
    shifted = analysis.ride_curve_vs(vocal, rest, max_db=6.0, offset=2.0, smooth=0.3)
    assert np.interp(2.5, [p[0] for p in shifted], [p[1] for p in shifted]) == pytest.approx(-1.0, abs=0.1)


def test_simplify_keeps_only_corners():
    times = np.arange(11.0)
    line = times * 2
    assert analysis.simplify(times, line, 0.1) == [0, 10]
    bent = np.where(times < 5, 0.0, (times - 5) * 3)
    assert analysis.simplify(times, bent, 0.1) == [0, 5, 10]


def test_ride_levels_phrases_and_leaves_pauses_alone():
    # Громкая фраза, пауза, тихая фраза — по 5 с, шаг ряда 0,1 с.
    momentary = np.concatenate([np.full(50, -18.0), np.full(20, -90.0), np.full(50, -24.0)])
    curve = analysis.ride_curve(momentary, max_db=4.0, smooth=0.3)
    at = lambda t: np.interp(t, [p[0] for p in curve], [p[1] for p in curve])
    # Цель — медиана звучащих кадров (−21): громкую фразу вниз, тихую вверх.
    assert at(2.5) == pytest.approx(-3.0, abs=0.1)
    assert at(9.5) == pytest.approx(+3.0, abs=0.1)
    assert at(6.2) == pytest.approx(0.0, abs=0.1)
    assert len(curve) < 20
    assert analysis.spread(momentary) == pytest.approx(3.0, abs=0.01)


def test_ride_respects_limit():
    momentary = np.concatenate([np.full(50, -10.0), np.full(50, -30.0)])
    curve = analysis.ride_curve(momentary, max_db=4.0)
    assert max(abs(v) for _, v in curve) == pytest.approx(4.0, abs=0.01)


def test_compare_aligns_balance_by_overall_level():
    quiet, _ = analysis.measure(stereo(sine(100, 0.1) + sine(3000, 0.1)), [])
    loud, _ = analysis.measure(stereo(sine(100, 0.5) + sine(3000, 0.5)), [])
    bright, _ = analysis.measure(stereo(sine(100, 0.1) + sine(3000, 0.4)), [])
    same = analysis.compare(quiet, loud, "ref").splitlines()[-1].split("  ")[-1]
    assert same.split(" | ")[1] == "+0" and same.split(" | ")[6] == "+0"
    tilt = analysis.compare(quiet, bright, "ref").splitlines()[-1].split("  ")[-1].split(" | ")
    assert tilt[1] == "+9" and tilt[6] == "-3"


def test_report_and_summary(tmp_path):
    import soundfile

    files = {}
    for name, sig in (("mix", sine(50, 0.9) + sine(3000, 0.2)), ("2 Kick", sine(50, 0.1)), ("4 Bass", sine(50, 0.8))):
        files[name] = tmp_path / f"{name}.wav"
        soundfile.write(files[name], np.column_stack([sig, sig]), SR, subtype="FLOAT")
    regions = [{"name": "Verse", "start": 0, "end": 3}, {"name": "Chorus", "start": 3, "end": 6}]
    meta = {"id": "test", "keys": ["k"], "range": [16.0, 22.0], "snapshot": "s", "notes": []}
    report = analysis.analyze(files, regions, set(), tmp_path / "out", meta)

    assert (tmp_path / "out" / "report.json").is_file()
    stored = np.load(tmp_path / "out" / "series.npz")
    assert stored["4 Bass/thirds"].shape[0] == len(analysis.THIRD_CENTERS)
    # Синус клиппует каждые 10 мс — это одно непрерывное место, а не триста.
    assert report["warnings"] == [
        "mix: true peak +0.8 dBTP — клиппинг на выходе · мест выше 0 dBFS: 1, сильнейшие: 0:16.0 (+0.8)"
    ]

    text = analysis.summary(report).splitlines()
    assert text[0].startswith("анализ test · 0:16.0–0:22.0 · полосы Hz: 20-60 | 60-120")
    assert text[1].startswith("mix     ") and "width mono" in text[1]
    assert "Verse" in text[3] and "Chorus" in text[3]
    assert text[-3] == "  2 Kick накрыт 4 Bass  44-56 Hz  100% времени  +18 dB  · дольше всего: 0:16.0–0:21.7"
