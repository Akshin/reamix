"""Грув: где у дорожки атаки и как они ложатся на сетку проекта или на другую дорожку.

Атака ставится там, где огибающая транзиента прошла треть пути от уровня перед ударом до
его пика. У ударных это доли миллисекунды от начала звука; у звуков с медленной атакой
«начало» — вопрос определения, и цифрам там верить с допуском.
"""

import numpy as np
from scipy import ndimage, signal

from . import fmt
from .analysis import SILENCE_DB, Audio

HOLD = 0.015  # с: огибающая держит пик — полупериод низкой ноты не читается как удар
LOOKBACK = 0.03  # с: отрезок, за который огибающая должна вырасти
RISE = 2.0  # во сколько раз вырасти (6 дБ)
NOISE_DB = 50.0  # ниже самого сильного удара на столько — уже не уровень, а шум
GAP = 0.05  # с: атаки ближе этого — один удар (флэм, отскок)
PEAK_WINDOW = 0.05  # с: пик удара ищется не дальше этого от начала роста
ONSET_AT = 0.3  # доля пути от уровня перед ударом до пика, где ставится атака
FLOOR_DB = 15.0  # удары тише сильных на столько не в счёт: пролезание, призвуки
MIN_HITS = 4  # меньше — не ритмическая партия, статистике верить нельзя
PREROLL = 0.25  # с: рендер начинается раньше участка, чтобы первый удар был виден целиком
SLOW_ATTACK_MS = 10.0
LOOSE = 0.2  # разброс больше этой доли шага сетки — партия к сетке не привязана
ANCHOR_WINDOW = 0.06  # с: удар дальше этого от удара опоры с ним не сравнивается

# Шаг сетки в 1/24 четверти: в этих единицах целые и прямые доли, и триоли.
GRIDS = {"4": 24, "8": 12, "16": 6, "32": 3, "8t": 8, "16t": 4}
BEATS = "доли"
LEVELS = [("1/4", 24), ("1/8", 12), ("1/8т", 8), ("1/16", 6), ("1/16т", 4), ("1/32", 3)]
ORDER = [BEATS] + [label for label, _ in LEVELS]


def grid_name(key: str) -> str:
    return "1/" + key.replace("t", "т")


# ── Атаки ────────────────────────────────────────────────────────────────────


def onsets(audio: Audio, floor_db: float = FLOOR_DB) -> dict[str, np.ndarray]:
    """Атаки в записи: t — секунды от начала файла, level — сила удара относительно
    сильных ударов дорожки (дБ), attack — время нарастания от 10 до 90 % (мс)."""
    empty = {key: np.array([]) for key in ("t", "level", "attack")}
    sr = audio.sr
    back = int(LOOKBACK * sr)
    if len(audio.data) <= back:
        return empty
    # Фильтр первого порядка, в одну сторону: подчёркивает щелчок и не размазывает его назад.
    sos = signal.butter(1, 1000, "highpass", fs=sr, output="sos")
    sharp = np.abs(signal.sosfilt(sos, audio.data, axis=0)).max(axis=1)
    top = float(sharp.max())
    if 20 * np.log10(max(top, 1e-10)) < SILENCE_DB:
        return empty
    # Максимум за последние HOLD секунд: фронт остаётся на месте, спад запаздывает.
    half = int(HOLD * sr / 2)
    env = ndimage.maximum_filter1d(np.concatenate([np.zeros(half), sharp]), 2 * half + 1, mode="constant")[: len(sharp)]
    before = np.full(len(env), np.inf)  # в первые LOOKBACK секунд сравнивать не с чем
    before[back:] = np.maximum(env[:-back], top * 10 ** (-NOISE_DB / 20))
    rising = env >= RISE * before
    starts = []
    for s in np.flatnonzero(rising[1:] & ~rising[:-1]) + 1:
        if not starts or s - starts[-1] >= GAP * sr:
            starts.append(int(s))

    window = int(PEAK_WINDOW * sr)
    times, peaks, attacks = [], [], []
    for i, s in enumerate(starts):
        end = min(s + window, starts[i + 1] if i + 1 < len(starts) else len(env))
        p = s + int(env[s:end].argmax())
        lo, base, peak = s - back, before[s], env[p]
        front = env[lo : p + 1]
        t10, at, t90 = (lo + int(np.argmax(front >= base + share * (peak - base))) for share in (0.1, ONSET_AT, 0.9))
        times.append(at / sr)
        peaks.append(peak)
        attacks.append((t90 - t10) / sr * 1000)
    if not times:
        return empty
    level = 20 * np.log10(np.array(peaks))
    level -= np.percentile(level, 95)
    keep = level >= -floor_db
    return {"t": np.array(times)[keep], "level": level[keep], "attack": np.array(attacks)[keep]}


def read_onsets(path, start: float, preroll: float, floor_db: float = FLOOR_DB) -> tuple[dict[str, np.ndarray], bool]:
    """Атаки рендера во времени проекта и молчит ли он. start — время проекта в начале
    файла, preroll — сколько секунд файла записано до участка."""
    audio = Audio.read(path)
    silent = not len(audio.data) or bool(20 * np.log10(max(np.abs(audio.data).max(), 1e-10)) < SILENCE_DB)
    hits = onsets(audio, floor_db)
    hits["t"] = hits["t"] + start
    # Рендер, начатый посреди звука, читает свой край как атаку: в начале захода ей не верим.
    keep = hits["t"] >= start + preroll / 2
    return {key: value[keep] for key, value in hits.items()}, silent


# ── Сетка ────────────────────────────────────────────────────────────────────


def tempo(lines: np.ndarray, step: int) -> str:
    """Темп и шаг сетки для заголовка: «120 bpm · сетка 1/16 = 125 мс»."""
    gaps = np.diff(lines) * 1000
    lo, hi = float(gaps.min()), float(gaps.max())
    name = next(grid_name(key) for key, units in GRIDS.items() if units == step)
    if hi - lo < 0.005 * hi:
        return f"{60000 * step / 24 / np.median(gaps):.4g} bpm · сетка {name} = {np.median(gaps):.4g} мс"
    return f"{60000 * step / 24 / hi:.4g}–{60000 * step / 24 / lo:.4g} bpm · сетка {name} = {lo:.4g}–{hi:.4g} мс"



def line_levels(k, beat, step: int) -> list[str]:
    """Место каждой линии сетки в такте: «доли» или самая крупная длительность, на которую
    она попадает. k — номер шага от начала такта, beat — длина доли в четвертях."""
    out = []
    for i, b in zip(k, beat):
        pos = int(i) * step
        if pos % round(b * 24) == 0:
            out.append(BEATS)
        else:
            out.append(next(label for label, units in LEVELS if units % step == 0 and pos % units == 0))
    return out


def nearest(t: np.ndarray, marks: np.ndarray) -> np.ndarray:
    """Номер ближайшей отметки для каждого времени; отметок — не меньше двух."""
    i = np.clip(np.searchsorted(marks, t), 1, len(marks) - 1)
    return np.where(t - marks[i - 1] <= marks[i] - t, i - 1, i)


def spread(values: np.ndarray) -> float:
    """Половина интервала, в который ложатся две трети значений: выбросы его не раздувают."""
    lo, hi = np.percentile(values, [16, 84])
    return float(hi - lo) / 2


def swing(levels: dict, total: int, step: int, quarter_ms: float) -> tuple[str, float] | None:
    """Свинг самой мелкой прямой длительности: где в паре «сильная — слабая» стоит слабая,
    в процентах длины пары. 50 — ровно, 67 — триольный шаффл."""
    if step not in (12, 6, 3):
        return None
    found = [(label, units) for label, units in LEVELS if units in (12, 6, 3) and label in levels]
    if not found:
        return None
    label, units = found[-1]
    weak = levels[label]
    if weak["hits"] < max(MIN_HITS, 0.15 * total):
        return None
    strong = [levels[name] for name in ORDER[: ORDER.index(label)] if name in levels and "т" not in name]
    hits = sum(s["hits"] for s in strong)
    on = sum(s["hits"] * s["offset"] for s in strong) / hits if hits else 0.0
    return label, 50 + 100 * (weak["offset"] - on) / (2 * units / 24 * quarter_ms)


def against_grid(hits: dict, lines: np.ndarray, labels: list[str], step: int) -> dict:
    """Попадание атак в сетку. Смещения — в мс, плюс — позже линии. Среднее собрано из
    медиан по местам в такте, разброс считается вокруг них: свинг — не неровность."""
    row = {"hits": 0, "at": []}
    if not len(hits["t"]):
        return row
    j = nearest(hits["t"], lines)
    offset = (hits["t"] - lines[j]) * 1000
    gaps = np.diff(lines)
    room = np.minimum(np.concatenate([[np.inf], gaps]), np.concatenate([gaps, [np.inf]]))[j] * 500
    inside = np.abs(offset) <= room  # остальные — удары за краями участка
    j, offset = j[inside], offset[inside]
    place = np.array(labels)[j]
    row |= {
        "hits": len(offset),
        "at": [[float(a), float(b), str(c), float(d), float(e)] for a, b, c, d, e in zip(hits["t"][inside], offset, place, hits["level"][inside], hits["attack"][inside])],
    }
    if len(offset) < MIN_HITS:
        return row
    levels, rest = {}, np.empty_like(offset)
    for label in ORDER:
        here = place == label
        if here.any():
            levels[label] = {"hits": int(here.sum()), "offset": float(np.median(offset[here]))}
            rest[here] = offset[here] - levels[label]["offset"]
    quarter_ms = float(np.median(gaps)) * 1000 * 24 / step
    row |= {
        "offset": sum(v["hits"] * v["offset"] for v in levels.values()) / len(offset),
        "spread": spread(rest),
        "levels": levels,
        "attack": float(np.median(hits["attack"][inside])),
        "swing": swing(levels, len(offset), step, quarter_ms),
    }
    row["loose"] = bool(row["spread"] > LOOSE * quarter_ms * step / 24)
    return row


def against_anchor(hits: dict, anchor: np.ndarray) -> dict:
    """Атаки дорожки относительно ближайших атак опорной: для записи, где сетке верить нельзя."""
    row = {"hits": len(hits["t"]), "matched": 0, "at": []}
    if not len(hits["t"]) or len(anchor) < 2:
        return row
    window = min(ANCHOR_WINDOW, float(np.median(np.diff(anchor))) / 2) * 1000
    j = nearest(hits["t"], anchor)
    offset = (hits["t"] - anchor[j]) * 1000
    near = np.abs(offset) <= window
    row |= {
        "matched": int(near.sum()),
        "at": [[float(a), float(b), "", float(c), float(d)] for a, b, c, d in zip(hits["t"][near], offset[near], hits["level"][near], hits["attack"][near])],
    }
    if row["matched"] >= MIN_HITS:
        row |= {"offset": float(np.median(offset[near])), "spread": spread(offset[near]), "attack": float(np.median(hits["attack"][near]))}
    return row


# ── Таблица ──────────────────────────────────────────────────────────────────


def _remarks(row: dict) -> list[str]:
    out = []
    if row.get("swing") and abs(row["swing"][1] - 50) >= 2 and not row.get("loose"):
        out.append(f"свинг {row['swing'][0]} {row['swing'][1]:.0f}%")
    if row["attack"] >= SLOW_ATTACK_MS:
        out.append(f"атака медленная: точность ±{row['attack'] / 2:.0f} мс")
    if row.get("loose"):
        out.append("к сетке не привязана")
    if row.get("shift"):
        out.append(f"дорожка сдвинута на {row['shift']:+g} мс")
    return out


def table(head: str, rows: list[dict], anchor: bool = False) -> str:
    """Таблица грува — то, что читает Claude. rows — дорожки с полями n, name и замером
    against_grid или against_anchor; дорожки без цифр уходят в строку под таблицей."""
    measured = [r for r in rows if "offset" in r]
    levels = [label for label in ORDER if any(label in r["levels"] for r in measured)] if not anchor else []
    columns = ["ударов"] + (["с опорой", "сдвиг", "разброс"] if anchor else ["среднее", "разброс", *levels]) + ["атака"]
    widths = [max(7, len(c)) for c in columns]
    names = [f"{r['n']} {r['name']}" for r in measured]
    indent = max(map(len, names), default=0)
    lines = [head]
    if measured:
        lines.append("  " + " " * indent + "".join(f"  {c:>{w}}" for c, w in zip(columns, widths)))
    for name, r in zip(names, measured):
        cells = [str(r["hits"])]
        if anchor:
            cells += [str(r["matched"]), f"{r['offset']:+.1f}", f"±{r['spread']:.1f}"]
        else:
            cells += [f"{r['offset']:+.1f}", f"±{r['spread']:.1f}"]
            # Пара ударов на месте, где партия обычно не играет, — сбивка, а не грув.
            # У партии, не привязанной к сетке, места в такте — случайность.
            shown = float("inf") if r["loose"] else max(MIN_HITS, 0.05 * r["hits"])
            cells += [f"{r['levels'][l]['offset']:+.1f}" if l in r["levels"] and r["levels"][l]["hits"] >= shown else "—" for l in levels]
        cells.append("<1" if r["attack"] < 1 else f"{r['attack']:.0f}")
        remarks = _remarks(r)
        lines.append("  " + name.ljust(indent) + "".join(f"  {c:>{w}}" for c, w in zip(cells, widths)) + ("  · " + " · ".join(remarks) if remarks else ""))
    rest = [f"{r['n']} {r['name']}" for r in rows if "offset" not in r and not r.get("silent")]
    if rest:
        lines.append((f"с опорой совпало меньше {MIN_HITS} ударов: " if anchor else f"атак меньше {MIN_HITS} — не в таблице: ") + ", ".join(rest))
    silent = [f"{r['n']} {r['name']}" for r in rows if r.get("silent")]
    if silent:
        lines.append("молчат на участке: " + ", ".join(silent))
    if len(measured) > 1 and sum(bool(r.get("loose")) for r in measured) > len(measured) / 2:
        lines.append("большинство дорожек к сетке не привязаны: запись без клика или темп проекта не тот — мерь относительно дорожки (--anchor)")
    return "\n".join(lines)
