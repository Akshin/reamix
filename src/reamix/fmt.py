"""Компактные текстовые представления состояния проекта — то, что читает Claude.

Полные данные лежат в снимках; здесь печатаются только отличия от значений по умолчанию.
"""

import math
import re

_FX_PREFIX = re.compile(r"^(VST3?i?|AUi?|JS|CLAPi?|LV2i?|DX): ")
_FX_VENDOR = re.compile(r" \((?:[^()]|\([^()]*\))*\)$")  # и «(Universal Audio (UADx))»


def db(gain: float) -> str:
    if gain <= 0:
        return "-inf"
    return f"{20 * math.log10(gain):+.1f}"


def pan(value: float) -> str:
    if abs(value) < 0.005:
        return "C"
    return f"{'L' if value < 0 else 'R'}{abs(value) * 100:.0f}"


def clock(seconds: float) -> str:
    minutes, rest = divmod(seconds, 60)
    return f"{int(minutes)}:{rest:04.1f}"


def fx_short(name: str) -> str:
    """«VST3: Pro-Q 3 (FabFilter)» → «Pro-Q 3»."""
    return _FX_VENDOR.sub("", _FX_PREFIX.sub("", name))


def fx_label(fx: dict) -> str:
    label = fx_short(fx["name"])
    if fx["offline"]:
        return label + "(offline)"
    return label if fx["enabled"] else label + "(off)"


def track_line(t: dict, master: bool = False) -> str:
    head = " M  MASTER" if master else f"{t['n']:>2}  {'  ' * t['depth']}{t['name'] or '(без имени)'}"
    parts = [head + (" [folder]" if t["folder"] else "")]
    if abs(t["vol"] - 1) > 1e-4:
        parts.append(db(t["vol"]) + "dB")
    if abs(t["pan"]) >= 0.005:
        parts.append(pan(t["pan"]))
    for flag in ("mute", "solo", "phase"):
        if t[flag]:
            parts.append(flag)
    if abs(t.get("offset_ms", 0)) >= 0.05:
        parts.append(f"{t['offset_ms']:+g}ms")
    if not master and not t["main_send"]:
        parts.append("no-master")
    if t["channels"] != 2:
        parts.append(f"{t['channels']}ch")
    if t["fx"]:
        parts.append("fx: " + ", ".join(fx_label(fx) for fx in t["fx"]))
    for s in t["sends"]:
        dest = f"{s['dest']} {s['dest_name']}" if "dest" in s else s["dest_name"]
        # Посыл на каналы 3/4 и выше — сайдчейн, а не звук в микс получателя.
        kind = " sidechain" if s.get("dst_chan", 0) >= 2 else ""
        parts.append(f"-> {dest} {db(s['vol'])}{kind}" + (" mute" if s["mute"] else ""))
    envs = [f"{e['name']}({e['points']})" for e in t["envelopes"] if e["active"]]
    if envs:
        parts.append("env: " + ", ".join(envs))
    if t["items"]:
        parts.append(f"items {t['items']}")
    return "  ".join(parts)


def skeleton(sk: dict) -> str:
    p = sk["project"]
    head = [
        p["path"] or "(проект не сохранён)",
        f"{p['bpm']:g} bpm",
        f"{p['srate']:g} Hz",
        clock(p["length"]),
        f"rev {p['rev']}",
    ]
    if p["dirty"]:
        head.append("не сохранён")
    lines = [" · ".join(head), track_line(sk["master"], master=True)]
    lines += [track_line(t) for t in sk["tracks"]]
    for m in sk["markers"]:
        span = f"{clock(m['pos'])}–{clock(m['end'])}" if m["region"] else clock(m["pos"])
        lines.append(f"{'R' if m['region'] else 'M'}{m['n']}  {span}  {m['name']}")
    return "\n".join(lines)


def fx_params(info: dict, note: str = "") -> str:
    shown = info["params"]
    note = note or (f", показано {len(shown)}" if len(shown) != info["total"] else "")
    lines = [f"{info['fx']} {info['name']} · параметров {info['total']}{note}"]
    for p in shown:
        lines.append(f"{p['p']:>4}  {p['name']} = {p['fmt']}  ({p['norm']:.4f})")
    return "\n".join(lines)


def _level(value) -> str:
    return "тишина" if value is None else f"{value:6.1f}"


def gain_ladder(track: dict, span: tuple[float, float], chain: dict, need: float | None) -> str:
    """Уровень трека по ступеням цепочки плагинов. need — поправка, которой не хватает на входе в плагины."""
    steps = chain["steps"]
    names = ["вход"] + [f"{s['fx']} {fx_short(s['name'])}" for s in steps[1:]]
    width = max(len(n) for n in names)
    lines = [
        f"уровни по цепочке · {track['n']} {track['name']} · {clock(span[0])}–{clock(span[1])} · "
        "до фейдера, dBFS: средний / пик / LUFS и его сдвиг на плагине"
    ]
    moved = []
    for i, (name, s) in enumerate(zip(names, steps)):
        line = f"  {name:<{width}}  {_level(s['avg'])}  пик {s['peak']:6.1f}"
        if s["lufs"] is not None:
            line += f"  {s['lufs']:6.1f} LUFS"
        is_trim = chain["trim"] and s["fx"] == 1
        if i and s["lufs"] is not None and steps[i - 1]["lufs"] is not None:
            delta = s["lufs"] - steps[i - 1]["lufs"]
            line += f"  {delta:+5.1f}"
            if abs(delta) >= 1 and not is_trim:
                line += " !"
                moved.append(name)
        lines.append(line + ("  трим" if is_trim else ""))
    if chain["off"]:
        lines.append("выключены: " + ", ".join(f"{fx['fx']} {fx_short(fx['name'])}" for fx in chain["off"]))
    entry = steps[1] if chain["trim"] and len(steps) > 1 else steps[0]
    if entry["avg"] is None:
        lines.append("вход молчит на участке")
    elif need:
        lines.append(f"вход в плагины не на рабочем уровне: нужна поправка {need:+.1f} дБ — reamix trim {track['n']}")
    else:
        lines.append("вход в плагины на рабочем уровне" + ("" if chain["trim"] else " (трима нет)"))
    if moved:
        lines.append("увели уровень больше чем на 1 дБ: " + ", ".join(moved))
    return "\n".join(lines)


def trim_table(span: tuple[float, float], rows: list[dict], target: float, ceiling: float, muted: list[str]) -> str:
    names = [f"{r['n']} {r['name']}" for r in rows]
    width = max(len(n) for n in names)
    lines = [f"трим · {clock(span[0])}–{clock(span[1])} · средний уровень к {target:g} dBFS, пики не выше {ceiling:g}"]
    for name, r in zip(names, rows):
        if r["avg"] is None:
            lines.append(f"  {name:<{width}}  {r['note']}")
            continue
        line = f"  {name:<{width}}  вход {r['avg']:6.1f}  пик {r['peak']:6.1f}  трим {r['new']:+.1f}"
        if r["old"] is not None:
            line += " — как было" if r["new"] == r["old"] else f", было {r['old']:+.1f}"
        if r["by_peak"]:
            line += " (по пикам)"
        lines.append(line + (f" · {r['note']}" if r.get("note") else ""))
    if muted:
        lines.append("пропущены (mute): " + ", ".join(muted))
    return "\n".join(lines)
