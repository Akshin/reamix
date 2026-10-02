"""Анализ рендеров: громкость, пики, спектр по полосам, стерео, конфликты между стемами.

На диск идут полные данные (report.json, series.npz), в вывод — сводка (summary).
Уровни полос — RMS в дБFS: синус с пиком 0 дБFS даёт −3 дБ в своей полосе.
"""

import json
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pyloudnorm
import soundfile
from scipy import signal

from . import fmt

# Полосы сводки: границы в Гц.
BANDS = [20, 60, 120, 250, 500, 1000, 2000, 4000, 8000, 20000]
BAND_LABELS = ["20-60", "60-120", "120-250", "250-500", "500-1k", "1-2k", "2-4k", "4-8k", "8-20k"]

# Третьоктавные полосы для спектрограммы и поиска конфликтов.
THIRD_CENTERS = 1000 * 2.0 ** (np.arange(-16, 14) / 3)  # 25 Гц … 20 кГц

FRAME = 16384  # окно БПФ: на 48 кГц — 341 мс, шаг частоты 2,9 Гц
HOP_SEC = 0.1
SILENCE_DB = -90.0


def to_db(power) -> np.ndarray:
    return 10 * np.log10(np.maximum(power, 1e-20))


@dataclass
class Audio:
    data: np.ndarray  # (сэмплы, 2)
    sr: int

    @classmethod
    def read(cls, path) -> "Audio":
        data, sr = soundfile.read(str(path), dtype="float64", always_2d=True)
        if data.shape[1] == 1:
            data = np.repeat(data, 2, axis=1)
        return cls(data[:, :2], sr)

    @property
    def seconds(self) -> float:
        return len(self.data) / self.sr


# ── Громкость ────────────────────────────────────────────────────────────────


def k_weighted(audio: Audio) -> np.ndarray:
    """Сигнал после фильтров K-взвешивания (ITU-R BS.1770)."""
    out = audio.data.copy()
    for stage in pyloudnorm.Meter(audio.sr)._filters.values():
        for ch in range(out.shape[1]):
            out[:, ch] = stage.apply_filter(out[:, ch])
    return out


def loudness_series(weighted: np.ndarray, sr: int, window: float) -> np.ndarray:
    """Громкость в LUFS скользящим окном (0,4 с — momentary, 3 с — short-term), шаг 0,1 с."""
    energy = np.concatenate([[0.0], np.cumsum((weighted**2).sum(axis=1))])
    size, hop = int(window * sr), int(HOP_SEC * sr)
    if len(weighted) < size:
        return np.array([])
    starts = np.arange(0, len(weighted) - size + 1, hop)
    return -0.691 + to_db((energy[starts + size] - energy[starts]) / size)


def integrated(audio: Audio) -> float | None:
    """Интегральная громкость с гейтом; None — тишина или слишком короткий отрывок."""
    if audio.seconds < 0.5:
        return None
    value = pyloudnorm.Meter(audio.sr).integrated_loudness(audio.data)
    return float(value) if np.isfinite(value) else None


def loudness_range(short_term: np.ndarray) -> float | None:
    """LRA по EBU Tech 3342: разброс short-term между 10-м и 95-м перцентилями после гейта."""
    st = short_term[short_term > -70]
    if len(st) < 2:
        return None
    gate = to_db(np.mean(10 ** (st / 10))) - 20
    st = st[st > gate]
    if len(st) < 2:
        return None
    return float(np.percentile(st, 95) - np.percentile(st, 10))


# ── Пики и стерео ────────────────────────────────────────────────────────────


def true_peak_db(audio: Audio) -> float:
    """Межсэмпловый пик: передискретизация ×4."""
    over = signal.resample_poly(audio.data, 4, 1, axis=0)
    return float(20 * np.log10(max(np.abs(over).max(), 1e-10)))


def correlation(left: np.ndarray, right: np.ndarray) -> float | None:
    denom = np.sqrt((left**2).sum() * (right**2).sum())
    return float((left * right).sum() / denom) if denom > 1e-12 else None


def stereo(audio: Audio) -> dict:
    left, right = audio.data[:, 0], audio.data[:, 1]
    mid, side = (left + right) / 2, (left - right) / 2
    low = signal.sosfiltfilt(signal.butter(4, 120, "lowpass", fs=audio.sr, output="sos"), audio.data, axis=0)
    return {
        "correlation": correlation(left, right),
        "correlation_low": correlation(low[:, 0], low[:, 1]),
        # Ширина — отношение боковой составляющей к центральной; моно-сигнал даёт −inf.
        "width_db": float(to_db((side**2).mean()) - to_db((mid**2).mean())) if (mid**2).mean() > 1e-12 else None,
        "balance_db": float(to_db((left**2).mean()) - to_db((right**2).mean())),
    }


# ── Спектр ───────────────────────────────────────────────────────────────────


def spectrogram(audio: Audio) -> tuple[np.ndarray, np.ndarray]:
    """Мощность по частотам и кадрам, усреднённая по каналам. Сумма по частотам кадра
    равна среднему квадрату сигнала в окне."""
    window = signal.get_window("hann", FRAME)
    freqs, _, z = signal.stft(
        audio.data.T, fs=audio.sr, window=window, nperseg=FRAME,
        noverlap=FRAME - int(HOP_SEC * audio.sr), boundary=None, padded=False, scaling="spectrum",
    )
    power = (np.abs(z) ** 2).mean(axis=0)
    power[1:-1] *= 2  # односторонний спектр
    # scaling="spectrum" нормирует на амплитуду синуса; до среднего квадрата — поправка окна.
    power *= window.sum() ** 2 / (FRAME * (window**2).sum())
    return freqs, power


def band_power(freqs: np.ndarray, power: np.ndarray, edges) -> np.ndarray:
    """Мощность в полосах (полосы × кадры)."""
    return np.array([power[(freqs >= lo) & (freqs < hi)].sum(axis=0) for lo, hi in zip(edges[:-1], edges[1:])])


def third_octave_edges() -> np.ndarray:
    return np.concatenate([THIRD_CENTERS / 2 ** (1 / 6), [THIRD_CENTERS[-1] * 2 ** (1 / 6)]])


# ── Один файл ────────────────────────────────────────────────────────────────


def measure(audio: Audio, regions: list[dict]) -> tuple[dict, dict]:
    """Метрики одного рендера и его временные ряды. regions — {name, start, end} в секундах от начала файла."""
    rms = float(to_db((audio.data**2).mean()))
    peak = float(20 * np.log10(max(np.abs(audio.data).max(), 1e-10)))
    metrics = {"seconds": audio.seconds, "rms_db": rms, "peak_db": peak, "silent": rms < SILENCE_DB}
    if metrics["silent"]:
        return metrics, {}

    weighted = k_weighted(audio)
    short_term = loudness_series(weighted, audio.sr, 3.0)
    momentary = loudness_series(weighted, audio.sr, 0.4)
    freqs, power = spectrogram(audio)
    thirds = band_power(freqs, power, third_octave_edges())

    metrics |= {
        "lufs": integrated(audio),
        "lra": loudness_range(short_term),
        "momentary_max": float(momentary.max()) if len(momentary) else None,
        "true_peak_db": true_peak_db(audio),
        "crest_db": peak - rms,
        "dc": float(np.abs(audio.data.mean(axis=0)).max()),
        "bands_db": [float(v) for v in to_db(band_power(freqs, power, BANDS).mean(axis=1))],
        "thirds_db": [float(v) for v in to_db(thirds.mean(axis=1))],
        "stereo": stereo(audio),
        "regions": {},
    }
    for region in regions:
        a, b = int(region["start"] * audio.sr), int(region["end"] * audio.sr)
        metrics["regions"][region["name"]] = integrated(Audio(audio.data[a:b], audio.sr))
    series = {"short_term": short_term, "momentary": momentary, "thirds": to_db(thirds).astype(np.float32)}
    return metrics, series


# ── Где именно ───────────────────────────────────────────────────────────────


def segments(mask: np.ndarray, step: float, gap: float = 0.5, shortest: float = 1.0, limit: int = 3) -> list[list[float]]:
    """Участки времени, где mask истинна: соседние сливаются через паузы до gap секунд,
    короче shortest отбрасываются, остаются limit самых длинных — в порядке времени."""
    found, start, last = [], None, None
    for i in np.flatnonzero(mask):
        if start is None:
            start = last = i
        elif (i - last) * step <= gap + 1e-9:
            last = i
        else:
            found.append((start, last))
            start = last = i
    if start is not None:
        found.append((start, last))
    spans = [[float(a * step), float((b + 1) * step)] for a, b in found if (b + 1 - a) * step >= shortest]
    return sorted(sorted(spans, key=lambda x: x[0] - x[1])[:limit])


def clipping(audio: Audio, limit: int = 5) -> dict:
    """Места, где сигнал выходит за 0 дБFS: сколько их и самые сильные."""
    peaks = np.abs(audio.data).max(axis=1)
    over = np.flatnonzero(peaks > 1.0)
    if not len(over):
        return {"count": 0, "events": []}
    # Превышения ближе 50 мс друг к другу — одно событие.
    breaks = np.flatnonzero(np.diff(over) > 0.05 * audio.sr)
    starts = np.concatenate([[0], breaks + 1])
    ends = np.concatenate([breaks, [len(over) - 1]])
    events = []
    for a, b in zip(starts, ends):
        chunk = peaks[over[a] : over[b] + 1]
        events.append({"t": float(over[a] / audio.sr), "peak_db": float(20 * np.log10(chunk.max()))})
    strongest = sorted(events, key=lambda e: -e["peak_db"])[:limit]
    return {"count": len(events), "events": sorted(strongest, key=lambda e: e["t"])}


# ── Конфликты между стемами ──────────────────────────────────────────────────


def conflicts(series: dict[str, dict], skip: set[frozenset], limit: int = 8) -> list[dict]:
    """Где один стем накрыт другим: в полосах, важных для первого, второй звучит не тише.

    series — имя стема → ряды из measure; skip — пары, которые сравнивать не надо
    (папка и её дорожки, посыл и его получатель). Результат направленный: victim накрыт masker.
    """
    edges = third_octave_edges()
    names = [n for n, s in series.items() if "thirds" in s]
    found = []
    for victim in names:
        lv = series[victim]["thirds"].astype(np.float64)
        mean_v = to_db((10 ** (lv / 10)).mean(axis=1))
        # Важные полосы — в пределах 12 дБ от самой громкой у этого стема.
        important = (mean_v > mean_v.max() - 12) & (mean_v > -60)
        for masker in names:
            if masker == victim or frozenset((victim, masker)) in skip:
                continue
            lm = series[masker]["thirds"].astype(np.float64)
            frames = min(lv.shape[1], lm.shape[1])
            a, b = lv[:, :frames], lm[:, :frames]
            sounding = a > np.maximum(mean_v[:, None] - 10, -70)
            masked = sounding & (b > a - 6)
            share = np.where(sounding.sum(axis=1) > 0, masked.sum(axis=1) / np.maximum(sounding.sum(axis=1), 1), 0)
            hot = important & (share > 0.4)

            k = 0
            while k < len(hot):
                if not hot[k]:
                    k += 1
                    continue
                first = k
                while k < len(hot) and hot[k]:
                    k += 1
                band = slice(first, k)
                diff = float((b[band] - a[band])[masked[band]].mean())
                overlap = float(share[band].mean())
                found.append({
                    "victim": victim, "masker": masker, "lo": float(edges[first]), "hi": float(edges[k]),
                    "overlap": overlap, "diff_db": diff,
                    # Где накрыта хотя бы половина полос диапазона — время от начала участка.
                    "where": segments(masked[band].mean(axis=0) >= 0.5, HOP_SEC),
                    # Заметнее всего: долго, с большим перевесом, в громкой полосе.
                    "score": overlap * (min(max(diff, -6), 20) + 6) * (float(mean_v[band].max()) + 80),
                })
    found.sort(key=lambda c: -c["score"])
    return found[:limit]


# ── Гейн-стейджинг ───────────────────────────────────────────────────────────

WORK_LEVEL_DB = -18.0  # рабочий уровень плагинов: 0 VU у эмуляций аналога
WORK_PEAK_DB = -6.0
TRIM_STEP_DB = 1.0  # поправка меньше этой не нужна: рабочий уровень — зона, а не точка


def level(audio: Audio) -> dict:
    """Уровень для гейн-стейджинга. avg — средний по звучащим местам, без частотного
    взвешивания, по более громкому каналу, в шкале VU: синус с пиком −18 dBFS даёт −18.
    Паузы отсекаются гейтом, как в BS.1770. None — тишина."""
    peak = float(20 * np.log10(max(np.abs(audio.data).max(), 1e-10)))
    if peak < SILENCE_DB:
        return {"avg": None, "peak": peak, "lufs": None}
    size, hop = int(0.4 * audio.sr), int(HOP_SEC * audio.sr)
    if len(audio.data) >= size:
        energy = np.concatenate([np.zeros((1, 2)), np.cumsum(audio.data**2, axis=0)])
        starts = np.arange(0, len(audio.data) - size + 1, hop)
        blocks = ((energy[starts + size] - energy[starts]) / size).max(axis=1)
    else:
        blocks = (audio.data**2).mean(axis=0).max(keepdims=True)
    blocks = 2 * blocks  # синус с пиком P: средний квадрат P²/2 → показание P
    loud = blocks[to_db(blocks) > -70]
    if not len(loud):
        return {"avg": None, "peak": peak, "lufs": None}
    loud = loud[to_db(loud) > to_db(loud.mean()) - 10]
    return {"avg": float(to_db(loud.mean())), "peak": peak, "lufs": integrated(audio)}


def trim_gain(avg: float, peak: float, target: float = WORK_LEVEL_DB, ceiling: float = WORK_PEAK_DB) -> tuple[float, bool]:
    """Поправка трима в дБ: средний уровень к target, но пики не выше ceiling.
    Второе значение — решил ли предел по пикам (ударные)."""
    by_peak = ceiling - peak < target - avg
    return round(min(target - avg, ceiling - peak), 1), by_peak


# ── Райдинг громкости ────────────────────────────────────────────────────────

MOMENTARY_WINDOW = 0.4


def active_frames(momentary: np.ndarray) -> np.ndarray:
    """Кадры, где трек звучит, а не молчит между фразами."""
    return momentary > max(momentary.max() - 30, -60)


def spread(momentary: np.ndarray) -> float:
    """Разброс громкости по звучащим кадрам, LU: насколько неровно звучит трек."""
    active = momentary[active_frames(momentary)]
    return float(active.std()) if len(active) else 0.0


def simplify(times: np.ndarray, values: np.ndarray, tolerance: float) -> list[int]:
    """Прореживание ломаной (Рамер — Дуглас — Пекер): индексы точек, без которых
    кривая отклонится больше чем на tolerance."""
    keep = [0, len(values) - 1]
    stack = [(0, len(values) - 1)]
    while stack:
        a, b = stack.pop()
        if b - a < 2:
            continue
        line = values[a] + (values[b] - values[a]) * (times[a + 1 : b] - times[a]) / (times[b] - times[a])
        errors = np.abs(values[a + 1 : b] - line)
        worst = int(errors.argmax())
        if errors[worst] > tolerance:
            mid = a + 1 + worst
            keep.append(mid)
            stack += [(a, mid), (mid, b)]
    return sorted(keep)


def ride_curve(momentary: np.ndarray, max_db: float, smooth: float = 0.6, tolerance: float = 0.25) -> list[tuple[float, float]]:
    """Поправка громкости (сек от начала участка, дБ), подтягивающая звучащие места к
    их медианному уровню. В паузах поправка нулевая — шум между фразами не поднимается."""
    active = active_frames(momentary)
    if active.sum() < 2:
        return []
    target = float(np.median(momentary[active]))
    correction = np.where(active, np.clip(target - momentary, -max_db, max_db), 0.0)
    width = max(1, int(round(smooth / HOP_SEC)))
    if width > 1:
        kernel = np.hanning(width + 2)[1:-1]
        correction = np.convolve(np.pad(correction, width // 2, mode="edge"), kernel / kernel.sum(), mode="same")[width // 2 : width // 2 + len(momentary)]
    # Кадр измеряет окно 0,4 с — поправка относится к его середине.
    times = np.arange(len(momentary)) * HOP_SEC + MOMENTARY_WINDOW / 2
    return [(float(times[i]), float(correction[i])) for i in simplify(times, correction, tolerance)]


def ride_curve_vs(track: np.ndarray, rest: np.ndarray, max_db: float, offset: float = 0.0, smooth: float = 0.6, tolerance: float = 0.25) -> list[tuple[float, float]]:
    """Поправка, удерживающая трек на постоянном расстоянии от остального микса.
    track и rest — ряды momentary; расстояние — медианное по звучащим кадрам плюс offset."""
    frames = min(len(track), len(rest))
    track, rest = track[:frames], rest[:frames]
    active = active_frames(track) & (rest > -70)
    if active.sum() < 2:
        return []
    distance = track - rest
    target = float(np.median(distance[active])) + offset
    correction = np.where(active, np.clip(target - distance, -max_db, max_db), 0.0)
    width = max(1, int(round(smooth / HOP_SEC)))
    if width > 1:
        kernel = np.hanning(width + 2)[1:-1]
        correction = np.convolve(np.pad(correction, width // 2, mode="edge"), kernel / kernel.sum(), mode="same")[width // 2 : width // 2 + frames]
    times = np.arange(frames) * HOP_SEC + MOMENTARY_WINDOW / 2
    return [(float(times[i]), float(correction[i])) for i in simplify(times, correction, tolerance)]


def distance_spread(track: np.ndarray, rest: np.ndarray) -> float:
    """Насколько гуляет расстояние между треком и остальным миксом, LU."""
    frames = min(len(track), len(rest))
    track, rest = track[:frames], rest[:frames]
    active = active_frames(track) & (rest > -70)
    return float((track - rest)[active].std()) if active.any() else 0.0


# ── Фаза между дорожками ─────────────────────────────────────────────────────


def phase(a: Audio, b: Audio, max_ms: float = 20.0) -> dict:
    """Фазовые отношения двух дорожек одного источника: корреляция как есть, лучший
    сдвиг по времени и потеря низа при сложении. Положительный сдвиг — b позже a."""
    n = min(len(a.data), len(b.data))
    sos = signal.butter(4, 2000, "lowpass", fs=a.sr, output="sos")
    x = signal.sosfiltfilt(sos, a.data[:n].mean(axis=1))
    y = signal.sosfiltfilt(sos, b.data[:n].mean(axis=1))
    norm = np.sqrt((x**2).sum() * (y**2).sum())
    if norm < 1e-12:
        return {"silent": True}
    size = 1 << (2 * n - 1).bit_length()
    cross = np.fft.irfft(np.conj(np.fft.rfft(x, size)) * np.fft.rfft(y, size), size) / norm
    reach = int(max_ms / 1000 * a.sr)
    lags = np.arange(-reach, reach + 1)
    window = cross[lags % size]
    best = int(np.abs(window).argmax())

    low = signal.butter(4, 200, "lowpass", fs=a.sr, output="sos")
    xl, yl = signal.sosfiltfilt(low, x), signal.sosfiltfilt(low, y)
    apart = (xl**2).mean() + (yl**2).mean()
    return {
        "silent": False,
        "correlation": float(window[reach]),
        "best_lag_samples": int(lags[best]),
        "best_lag_ms": float(lags[best] / a.sr * 1000),
        "best_correlation": float(window[best]),
        # 0 дБ — складываются как независимые, +3 — в фазе, минус — низ вычитается.
        "low_sum_db": float(to_db(((xl + yl) ** 2).mean()) - to_db(apart)) if apart > 1e-14 else None,
    }


def phase_verdict(p: dict) -> str:
    if p["silent"]:
        return "одна из дорожек молчит"
    corr, best, lag = p["correlation"], p["best_correlation"], p["best_lag_ms"]
    if abs(best) < 0.3:
        return "дорожки почти не связаны — фазу сравнивать не с чем"
    flip = best < 0
    aligned = abs(lag) < 0.05
    if aligned and not flip:
        return "в фазе"
    if aligned and flip:
        return "противофаза: переверни полярность одной из дорожек"
    move = f"сдвиг {abs(lag):.2f} мс ({abs(p['best_lag_samples'])} сэмпл.): вторая {'позже' if lag > 0 else 'раньше'} первой"
    if abs(best) - abs(corr) < 0.1:
        return f"почти в фазе; {move}, выигрыш от выравнивания мал"
    return move + ("; после выравнивания ещё и перевернуть полярность" if flip else "; выровнять — корреляция станет " + f"{best:+.2f}")


# ── Сравнение с референсом ───────────────────────────────────────────────────


def compare(mix: dict, ref: dict, ref_name: str) -> str:
    """Микс рядом с референсом. Полосы сравниваются относительно общего уровня каждого:
    референс обычно отмастерен и громче, важен баланс, а не абсолютные цифры."""

    def row(name, m):
        st = m["stereo"]
        return (
            f"{name}  {_num(m['lufs'])} LUFS  LRA {_num(m['lra'])}  TP {m['true_peak_db']:+.1f}  "
            f"crest {m['crest_db']:.1f}  corr {_num(st['correlation'], '{:.2f}')}  width {_width(st['width_db'])}"
        )

    def balance(m):
        return np.array(m["bands_db"]) - m["rms_db"]

    diff = balance(mix) - balance(ref)
    width = max(len("mix"), len(ref_name) + 5)
    return "\n".join([
        row("mix".ljust(width), mix),
        row(f"ref: {ref_name}".ljust(width), ref),
        "баланс полос, mix минус ref (выровнено по общему уровню), Hz: " + " | ".join(BAND_LABELS),
        " " * width + "  " + " | ".join(f"{v:+.0f}" for v in diff),
    ])


# ── Отчёт ────────────────────────────────────────────────────────────────────


def analyze(files: dict[str, Path], regions: list[dict], skip: set[frozenset], out: Path, meta: dict) -> dict:
    """Измеряет все рендеры, ищет конфликты, пишет report.json и series.npz в out."""
    out.mkdir(parents=True, exist_ok=True)
    report = {**meta, "files": {}, "band_labels": BAND_LABELS}
    all_series, arrays = {}, {}
    for name, path in files.items():
        audio = Audio.read(path)
        metrics, series = measure(audio, regions)
        if name == "mix" and not metrics["silent"]:
            metrics["clipping"] = clipping(audio)
            metrics["loudest_at"] = float(series["momentary"].argmax() * HOP_SEC) if len(series["momentary"]) else None
        report["files"][name] = metrics
        if name != "mix":
            all_series[name] = series
        for key, value in series.items():
            arrays[f"{name}/{key}"] = value
    report["conflicts"] = conflicts(all_series, skip)
    report["warnings"] = warnings(report)
    np.savez_compressed(out / "series.npz", third_centers=THIRD_CENTERS, hop_sec=HOP_SEC, **arrays)
    (out / "report.json").write_text(json.dumps(report, ensure_ascii=False))
    return report


def warnings(report: dict) -> list[str]:
    out = []
    for name, m in report["files"].items():
        if m["silent"]:
            out.append(f"{name}: тишина")
            continue
        if name == "mix" and m["true_peak_db"] > -0.1:
            clip = m.get("clipping") or {"count": 0, "events": []}
            start = report["range"][0]
            where = ", ".join(f"{fmt.clock(start + e['t'])} ({e['peak_db']:+.1f})" for e in clip["events"])
            detail = f" · мест выше 0 dBFS: {clip['count']}, сильнейшие: {where}" if clip["count"] else ""
            out.append(f"mix: true peak {m['true_peak_db']:+.1f} dBTP — клиппинг на выходе{detail}")
        low = m["stereo"]["correlation_low"]
        if low is not None and low < 0.5 and m["bands_db"][0] > -60:
            out.append(f"{name}: корреляция ниже 120 Гц {low:.2f} — низ пропадает в моно")
        corr = m["stereo"]["correlation"]
        if corr is not None and corr < 0:
            out.append(f"{name}: корреляция {corr:.2f} — противофаза")
        if m["dc"] > 0.01:
            out.append(f"{name}: постоянная составляющая {m['dc']:.3f}")
    return out


def _num(value, pattern="{:.1f}", none="-") -> str:
    return none if value is None else pattern.format(value)


def _width(value) -> str:
    return "mono" if value is None or value < -60 else f"{value:.1f}"


def _hz(value: float) -> str:
    return f"{value / 1000:.1f}k" if value >= 1000 else f"{value:.0f}"


def summary(report: dict) -> str:
    """Сводка отчёта в несколько строк — то, что читает Claude."""
    rng = report["range"]
    lines = [f"анализ {report['id']} · {fmt.clock(rng[0])}–{fmt.clock(rng[1])} · полосы Hz: " + " | ".join(report["band_labels"])]
    width = max(len(n) for n in report["files"])
    for name, m in report["files"].items():
        if m["silent"]:
            lines.append(f"{name:<{width}}  тишина")
            continue
        st = m["stereo"]
        head = (
            f"{name:<{width}}  {_num(m['lufs'])} LUFS  LRA {_num(m['lra'])}  TP {m['true_peak_db']:+.1f}  "
            f"crest {m['crest_db']:.1f}  corr {_num(st['correlation'], '{:.2f}')}  width {_width(st['width_db'])}"
        )
        lines.append(head)
        lines.append(" " * width + "  bands " + " | ".join(f"{v:.0f}" for v in m["bands_db"]))
        if len(m["regions"]) > 1:
            lines.append(" " * width + "  " + " · ".join(f"{k} {_num(v)}" for k, v in m["regions"].items()))
    if report["conflicts"]:
        lines.append("маскирование (кто накрыт кем, в полосах, важных для накрытого; dB — перевес):")
        for c in report["conflicts"]:
            where = ", ".join(f"{fmt.clock(rng[0] + a)}–{fmt.clock(rng[0] + b)}" for a, b in c.get("where", []))
            lines.append(
                f"  {c['victim']} накрыт {c['masker']}  {_hz(c['lo'])}-{_hz(c['hi'])} Hz  "
                f"{c['overlap'] * 100:.0f}% времени  {c['diff_db']:+.0f} dB" + (f"  · дольше всего: {where}" if where else "")
            )
    if report["warnings"]:
        lines.append("предупреждения:")
        lines += [f"  {w}" for w in report["warnings"]]
    lines += report.get("notes", [])
    return "\n".join(lines)
