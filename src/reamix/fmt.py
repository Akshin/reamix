"""Компактные текстовые представления состояния проекта — то, что читает Claude.

Полные данные лежат в снимках; здесь печатаются только отличия от значений по умолчанию.
"""

import math
import re

_FX_PREFIX = re.compile(r"^(VST3?i?|AUi?|JS|CLAPi?|LV2i?|DX): ")
_FX_VENDOR = re.compile(r" \([^()]*\)$")


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
