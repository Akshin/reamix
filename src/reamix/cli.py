"""Командная строка reamix. Справка по командам для Claude — skill/SKILL.md."""

import argparse
import hashlib
import json
import re
import os
import shutil
import sys
import time
from pathlib import Path

from . import client, fmt, paths, plugins, store

def lua(value) -> str:
    """Литерал Lua: число как есть, остальное — строка в кавычках."""
    if isinstance(value, (int, float)):
        return repr(value)
    escaped = value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n").replace("\r", "\\r")
    return f'"{escaped}"'


def ref(text: str):
    """Ссылка на трек или плагин из командной строки: номер или имя."""
    return int(text) if text.isdigit() else text


def read_code(arg: str) -> str:
    if arg == "-":
        return sys.stdin.read()
    if arg.startswith("@"):
        return Path(arg[1:]).read_text()
    return arg


def track_dir() -> Path:
    """Папка с проектом REAPER этого трека: текущая, а у трека из мультитрека — reaper/ в ней."""
    cwd = Path.cwd().resolve()
    nested = cwd / "reaper"
    has_project = lambda folder: folder.is_dir() and any(p.suffix.lower() == ".rpp" for p in folder.iterdir())  # noqa: E731
    return nested if not has_project(cwd) and has_project(nested) else cwd


def guard(args) -> Path | None:
    """Папка, в которой обязан лежать активный проект REAPER."""
    return None if args.any_project else track_dir()


def show(value) -> None:
    if value is not None:
        print(value if isinstance(value, str) else json.dumps(value, ensure_ascii=False))


def project_data(args) -> Path:
    path = client.call("return rx.project().path", project=guard(args), timeout=args.timeout)
    if not path:
        raise client.BridgeError("проект в REAPER не сохранён — снимки класть некуда")
    return store.data_dir(path)


# ── Команды ──────────────────────────────────────────────────────────────────


def cmd_status(args):
    beat = client.alive()
    if beat is None:
        print("мост не запущен")
        return 1
    try:
        client.call("return true", timeout=3)
        state = "отвечает"
    except client.BridgeError:
        state = f"не отвечает, последний сигнал {beat['age']:.0f} с назад"
    print(f"мост {state} · {beat['project'] or '(проект не сохранён)'} · rev {beat['rev']}")


def cmd_reload(args):
    """Перезапускает мост из самого моста: новый экземпляр вытесняет старый."""
    client.call(f"dofile({lua(str(paths.BRIDGE))})", timeout=args.timeout)
    print("мост перезапущен")


def cmd_state(args):
    sk = client.call("return rx.skeleton()", project=guard(args), timeout=args.timeout)
    print(json.dumps(sk, ensure_ascii=False) if args.json else fmt.skeleton(sk))


def cmd_fx(args):
    if args.fx is None:
        info = client.call(f"return rx.track_info({lua(ref(args.track))})", project=guard(args), timeout=args.timeout)
        for fx in info["fx"]:
            print(f"{fx['i']:>2}  {fmt.fx_label(fx)}  · {fx['name']} · параметров {fx['params']}" + (f" · пресет «{fx['preset']}»" if fx.get("preset") else ""))
        return
    match = lua(args.match) if args.match else "nil"
    target = f"{lua(ref(args.track))}, {lua(ref(args.fx))}"
    info = client.call(f"return rx.fx_params({target}, {match})", project=guard(args), timeout=args.timeout)
    if args.all or args.match:
        print(fmt.fx_params(info))
        return
    # По умолчанию — только то, что кто-то крутил: у плагина бывают сотни параметров.
    defaults = fx_defaults(args, target, info)
    if defaults is None or len(defaults) != info["total"]:
        print(fmt.fx_params(info, " (значения по умолчанию неизвестны — показаны все)"))
        return
    info["params"] = [p for p in info["params"] if abs(p["norm"] - defaults[p["p"]]) > 1e-6]
    print(fmt.fx_params(info, f", отличаются от умолчания {len(info['params'])} (все — с --all)"))


def fx_defaults(args, target: str, info: dict) -> list[float] | None:
    """Значения параметров по умолчанию; один раз на плагин, дальше из catalog/defaults."""
    name = re.sub(r"[^A-Za-z0-9.]+", "_", info["ident"].rsplit("/", 1)[-1]) or "unknown"
    cache = paths.DEFAULTS / f"{name}.json"
    if cache.is_file():
        return json.loads(cache.read_text())["norms"]
    try:
        found = client.call(f"return rx.fx_defaults({target})", project=guard(args), timeout=max(args.timeout, 60))
    except client.BridgeError:
        return None
    paths.DEFAULTS.mkdir(parents=True, exist_ok=True)
    cache.write_text(json.dumps(found, ensure_ascii=False))
    return found["norms"]


def cmd_eval(args):
    show(client.call(read_code(args.code), project=guard(args), timeout=args.timeout))


def apply_edit(args, data: Path, label: str, code: str, snapshot: bool = True):
    """Правка одним undo-блоком со снимками до и после. Возвращает результат и список изменений."""
    snap = store.staging(data) if snapshot else None
    resp = client.request(code, undo=label, project=guard(args), snap=snap, timeout=args.timeout)
    before = store.latest(data) if snap else None
    if resp.get("snap_before"):
        before = store.ingest(resp["snap_before"], data, "before", label)
    if not resp.get("ok"):
        if snap:
            store.cleanup_staging(data)
        note = " (правка откатана)" if resp.get("rolled_back") else ""
        raise client.BridgeError(str(resp.get("error")) + note)
    changes, after = None, None
    if snap:
        after = store.ingest(resp["snap_after"], data, "after", label)
        store.cleanup_staging(data)
        changes = store.diff(before, after) if before else []
    store.log_edit(data, label, before, after)
    return resp.get("result"), changes


def cmd_do(args):
    result, changes = apply_edit(args, project_data(args), args.label, read_code(args.code), not args.no_snapshot)
    show(result)
    if changes is not None:
        print("изменено:\n  " + "\n  ".join(changes) if changes else "изменений в состоянии нет")


def cmd_undo(args):
    data = project_data(args)
    label = client.call("return rx.undo_edit()", project=guard(args), timeout=args.timeout)
    # Журнал только дополняется: откат — отдельная запись, а не удаление правки.
    store.log_edit(data, f"откат: {label.removeprefix('reamix: ')}", None, None)
    print(f"откатано: {label}")


def play_code(sk: dict, args) -> str:
    if not (args.region or args.range):
        return "rx.play()"
    t0, t1 = pick_range(sk, args)
    return f"rx.play({{{t0!r}, {t1!r}}}, {'false' if getattr(args, 'once', False) else 'true'})"


def cmd_ab(args):
    """Переключает последнюю правку между «до» и «после»; с участком — сразу запускает его по кругу."""
    want = lua(args.state) if args.state else "nil"
    code = f"local res = rx.ab({want})"
    if args.region or args.range:
        sk = client.call("return rx.skeleton()", project=guard(args), timeout=args.timeout)
        code += " " + play_code(sk, args)
    res = client.call(code + " return res", project=guard(args), timeout=args.timeout)
    name = res["label"].removeprefix("reamix: ")
    print(f"сейчас: {'ДО правки' if res['state'] == 'before' else 'ПОСЛЕ правки'} «{name}»")
    if res["state"] == "before":
        print("пока включено «до», правки заблокированы: reamix ab after — оставить, reamix undo — откатить")


def cmd_play(args):
    sk = client.call("return rx.skeleton()", project=guard(args), timeout=args.timeout) if (args.region or args.range) else {}
    client.call(play_code(sk, args), project=guard(args), timeout=args.timeout)
    print("играет" + (f" по кругу {args.region or args.range}" if (args.region or args.range) and not args.once else ""))


def cmd_stop(args):
    client.call("rx.stop()", project=guard(args), timeout=args.timeout)
    print("остановлено")


def take_snapshot(args, kind: str, label: str) -> tuple[Path, dict]:
    data = project_data(args)
    result = client.call(f"return rx.snapshot({lua(str(store.staging(data) / kind))})", project=guard(args), timeout=args.timeout)
    record = store.ingest(result, data, kind, label)
    store.cleanup_staging(data)
    return data, record


def cmd_snapshot(args):
    _, record = take_snapshot(args, "manual", args.label)
    size = sum(t["bytes"] for t in record["tracks"])
    print(f"снимок {record['id']} · треков {len(record['tracks'])} · {size / 1e3:.0f} КБ")


def cmd_changes(args):
    data = project_data(args)
    previous = store.latest(data)
    if previous is None:
        print("снимков ещё нет — сравнивать не с чем")
        return
    if client.call("return rx.project().rev", project=guard(args), timeout=args.timeout) == previous["rev"]:
        print(f"без изменений с {previous['id']}")
        return
    _, now = take_snapshot(args, "check", "")
    changes = store.diff(previous, now)
    print(f"с {previous['id']}:\n  " + "\n  ".join(changes) if changes else "изменений в состоянии треков нет")


def pick_tracks(sk: dict, spec: str | None) -> list[dict]:
    """Треки для стемов: по умолчанию верхнего уровня, «all» — все, иначе список номеров и имён."""
    tracks = sk["tracks"]
    if spec is None:
        return [t for t in tracks if t["depth"] == 0]
    if spec == "all":
        return tracks
    chosen = []
    for item in (part.strip() for part in spec.split(",")):
        if item.isdigit():
            found = [t for t in tracks if t["n"] == int(item)]
        else:
            found = [t for t in tracks if t["name"] == item] or [t for t in tracks if item.lower() in t["name"].lower()]
        if len(found) != 1:
            raise client.BridgeError(f"трек «{item}»: найдено {len(found)}")
        chosen += found
    return chosen


def pick_range(sk: dict, args) -> tuple[float, float]:
    if args.range:
        m = re.fullmatch(r"([\d.]+)-([\d.]+)", args.range)
        if not m:
            raise client.BridgeError("--range задаётся как начало-конец в секундах: 16-32")
        return float(m.group(1)), float(m.group(2))
    if args.region:
        regions = [m for m in sk["markers"] if m["region"]]
        found = [m for m in regions if str(m["n"]) == args.region] or [m for m in regions if args.region.lower() in m["name"].lower()]
        if not found:
            raise client.BridgeError(f"региона «{args.region}» нет")
        return found[0]["pos"], found[0]["end"]
    return 0.0, sk["project"]["length"]


def unrelated_pairs(sk: dict, label: dict) -> set[frozenset]:
    """Пары треков, чьё перекрытие — не конфликт: папка и её дорожки, посыл и получатель."""
    skip, parents = set(), []
    for t in sk["tracks"]:
        del parents[t["depth"]:]
        skip |= {frozenset((label[p], label[t["n"]])) for p in parents if p in label and t["n"] in label}
        parents.append(t["n"])
        for send in t["sends"]:
            if send.get("dest") in label and t["n"] in label:
                skip.add(frozenset((label[t["n"]], label[send["dest"]])))
    return skip


def cmd_analyze(args):
    """Рендерит микс и стемы, считает метрики, печатает сводку; полные данные — в .reamix/analysis."""
    from . import analysis  # numpy и scipy нужны только здесь

    data = project_data(args)
    sk = client.call("return rx.skeleton()", project=guard(args), timeout=args.timeout)
    t0, t1 = pick_range(sk, args)
    if t1 - t0 < 0.5:
        raise client.BridgeError("участок короче 0,5 с — анализировать нечего")
    wanted = pick_tracks(sk, args.tracks)
    tracks = [t for t in wanted if not t["mute"]]
    notes = []
    if len(tracks) < len(wanted):
        notes.append("пропущены (mute): " + ", ".join(f"{t['n']} {t['name']}" for t in wanted if t["mute"]))
    if any(t["solo"] for t in sk["tracks"]):
        notes.append("в проекте включён solo — стемы остальных треков будут пустыми")

    # Тот же участок тех же треков в том же состоянии уже считали — берём готовый отчёт.
    def state_key() -> tuple[str, dict]:
        _, snap = take_snapshot(args, "analysis", "")
        state = sorted(t["sha"] for t in snap["tracks"])
        return hashlib.sha1(json.dumps([state, [t["n"] for t in tracks], t0, t1]).encode()).hexdigest(), snap

    key, snap = state_key()
    for path in sorted((data / "analysis").glob("*/report.json")):
        report = json.loads(path.read_text())
        if key in report.get("keys", []) and not args.fresh:
            print(analysis.summary(report) + "\n(из кэша: состояние не менялось)")
            return

    run = f"{time.strftime('%Y%m%d-%H%M%S')}-rev{sk['project']['rev']}"
    render_dir = data / "renders" / run
    numbers = ", ".join(str(t["n"]) for t in tracks)
    result = client.call(
        f"return rx.render({{dir = {lua(str(render_dir))}, mix = true, tracks = {{{numbers}}}, range = {{{t0!r}, {t1!r}}}}})",
        project=guard(args), timeout=max(args.timeout, 1800),
    )
    if result["offline"]:
        notes.append("источники не открылись (в рендере их нет): " + ", ".join(sorted(set(result["offline"]))))
    label = {t["n"]: f"{t['n']} {t['name']}" for t in tracks}
    files = {}
    for path in map(Path, result["files"]):
        if not path.is_file():
            raise client.BridgeError(f"REAPER не записал {path.name} — рендер отменён?")
        m = re.fullmatch(r"stem-(\d+)", path.stem)
        files[label[int(m.group(1))] if m else "mix"] = path

    regions, seen = [], {}
    for m in (m for m in sk["markers"] if m["region"]):
        start, end = max(m["pos"], t0) - t0, min(m["end"], t1) - t0
        if end - start >= 0.5:
            seen[m["name"]] = seen.get(m["name"], 0) + 1
            name = m["name"] if seen[m["name"]] == 1 else f"{m['name']} {seen[m['name']]}"
            regions.append({"name": name, "start": start, "end": end})

    # Рендер сам меняет внутреннее состояние части плагинов (измерители, задержки):
    # состояние «после» — то же самое для кэша, что и «до».
    meta = {"id": run, "keys": [key, state_key()[0]], "range": [t0, t1], "snapshot": snap["id"], "notes": notes}
    report = analysis.analyze(files, regions, unrelated_pairs(sk, label), data / "analysis" / run, meta)
    print(analysis.summary(report))
    prune_renders(data)


def render_stems(args, data: Path, tracks: list[dict], t0: float, t1: float, tag: str) -> dict[int, Path]:
    """Стемы треков за участок: номер трека → файл."""
    render_dir = data / "renders" / f"{time.strftime('%Y%m%d-%H%M%S')}-{tag}"
    numbers = ", ".join(str(t["n"]) for t in tracks)
    result = client.call(
        f"return rx.render({{dir = {lua(str(render_dir))}, tracks = {{{numbers}}}, range = {{{t0!r}, {t1!r}}}}})",
        project=guard(args), timeout=max(args.timeout, 1800),
    )
    files = {}
    for path in map(Path, result["files"]):
        if not path.is_file():
            raise client.BridgeError(f"REAPER не записал {path.name} — рендер отменён?")
        files[int(re.fullmatch(r"stem-(\d+)", path.stem).group(1))] = path
    return files


def render_stem(args, data: Path, track: dict, t0: float, t1: float, tag: str) -> Path:
    return render_stems(args, data, [track], t0, t1, tag)[track["n"]]


def render_rest(args, data: Path, track: dict, t0: float, t1: float, tag: str) -> Path:
    """Микс без одного трека: трек глушится на время рендера, след стирается."""
    render_dir = data / "renders" / f"{time.strftime('%Y%m%d-%H%M%S')}-{tag}"
    code = (
        "return rx.scratch(function() "
        f"rx.track_set({track['n']}, {{mute = true}}) "
        f"return rx.render({{dir = {lua(str(render_dir))}, mix = true, range = {{{t0!r}, {t1!r}}}}}) end)"
    )
    result = client.call(code, project=guard(args), timeout=max(args.timeout, 1800))
    path = Path(result["files"][0])
    if not path.is_file():
        raise client.BridgeError("REAPER не записал микс — рендер отменён?")
    return path


def prune_renders(data: Path, keep: int = 2) -> None:
    """Рендеры — кэш: остаются последние наборы, метрики лежат отдельно."""
    for old in sorted((data / "renders").iterdir())[:-keep]:
        shutil.rmtree(old)


def cmd_ride(args):
    """Выравнивает громкость трека огибающей: рендер → кривая поправки → запись точек."""
    from . import analysis

    data = project_data(args)
    sk = client.call("return rx.skeleton()", project=guard(args), timeout=args.timeout)
    track = pick_tracks(sk, args.track)[0]
    t0, t1 = pick_range(sk, args)
    title = f"{track['n']} {track['name']}"

    def momentary(path):
        audio = analysis.Audio.read(path)
        return analysis.loudness_series(analysis.k_weighted(audio), audio.sr, analysis.MOMENTARY_WINDOW), analysis.integrated(audio)

    before, lufs_before = momentary(render_stem(args, data, track, t0, t1, "ride-before"))
    rest = None
    if args.vs_mix:
        # Остальной микс от правки трека не меняется — рендерим его один раз.
        rest, _ = momentary(render_rest(args, data, track, t0, t1, "ride-rest"))
        curve = analysis.ride_curve_vs(before, rest, args.max, args.offset, args.smooth) if len(before) else []
    else:
        curve = analysis.ride_curve(before, args.max, args.smooth) if len(before) else []
    if not curve or max(abs(v) for _, v in curve) < 0.5:
        print(f"райдинг {title}: поправка меньше 0,5 дБ — трек и так ровный, ничего не записано")
        prune_renders(data)
        return
    # Края участка — без поправки, чтобы кривая за его пределами не сдвинулась.
    points = [(t0, 0.0)] + [(t0 + t, v) for t, v in curve if 0 < t < t1 - t0] + [(t1, 0.0)]
    rides = data / "rides"
    rides.mkdir(exist_ok=True)
    table = rides / f"{time.strftime('%Y%m%d-%H%M%S')}-{track['n']}.tsv"
    table.write_text("t\tv\n" + "".join(f"{t!r}\t{v!r}\n" for t, v in points))

    lo, hi = min(v for _, v in points), max(v for _, v in points)
    kind = "райдинг относительно микса" if args.vs_mix else "райдинг громкости"
    label = f"{kind}: {track['name']} {lo:+.1f}…{hi:+.1f} дБ"
    code = f"return rx.env_write({track['n']}, 'volume', {lua(str(table))}, {{db = true, add = true, clear = {{{t0!r}, {t1!r}}}}})"
    result, _ = apply_edit(args, data, label, code)
    print(f"{kind} {title} · {fmt.clock(t0)}–{fmt.clock(t1)} · точек {len(points)} (в огибающей {result['points']}) · поправка {lo:+.1f}…{hi:+.1f} дБ")
    if not args.no_verify:
        after, lufs_after = momentary(render_stem(args, data, track, t0, t1, "ride-after"))
        if args.vs_mix:
            print(
                f"расстояние до остального микса гуляет на {analysis.distance_spread(before, rest):.1f} → "
                f"{analysis.distance_spread(after, rest):.1f} LU · LUFS трека {analysis._num(lufs_before)} → {analysis._num(lufs_after)}"
            )
        else:
            print(
                f"разброс громкости {analysis.spread(before):.1f} → {analysis.spread(after):.1f} LU · "
                f"LUFS {analysis._num(lufs_before)} → {analysis._num(lufs_after)}"
            )
    prune_renders(data)


def cmd_phase(args):
    """Фаза между дорожками одного источника: корреляция, сдвиг по времени, потеря низа."""
    from . import analysis

    data = project_data(args)
    sk = client.call("return rx.skeleton()", project=guard(args), timeout=args.timeout)
    tracks = pick_tracks(sk, ",".join(args.tracks))
    if len(tracks) < 2:
        raise client.BridgeError("нужно минимум две дорожки")
    t0, t1 = pick_range(sk, args)
    files = render_stems(args, data, tracks, t0, t1, "phase")
    audio = {t["n"]: analysis.Audio.read(files[t["n"]]) for t in tracks}
    print(f"фаза · {fmt.clock(t0)}–{fmt.clock(t1)}")
    for i, a in enumerate(tracks):
        for b in tracks[i + 1 :]:
            p = analysis.phase(audio[a["n"]], audio[b["n"]])
            head = f"{a['n']} {a['name']} <> {b['n']} {b['name']}"
            if p["silent"]:
                print(f"{head}: {analysis.phase_verdict(p)}")
                continue
            print(
                f"{head}: корреляция {p['correlation']:+.2f} · лучший сдвиг {p['best_lag_ms']:+.2f} мс → {p['best_correlation']:+.2f} · "
                f"низ в сумме {analysis._num(p['low_sum_db'], '{:+.1f}')} dB · {analysis.phase_verdict(p)}"
            )
    prune_renders(data)


def chain_levels(args, data: Path, track: dict, t0: float, t1: float) -> dict:
    """Уровень трека до фейдера на каждой ступени цепочки плагинов."""
    from . import analysis

    render_dir = data / "renders" / f"{time.strftime('%Y%m%d-%H%M%S')}-gain"
    result = client.call(
        f"return rx.chain_render({track['n']}, {lua(str(render_dir))}, {{{t0!r}, {t1!r}}})",
        project=guard(args), timeout=max(args.timeout, 1800),
    )
    for step in result["steps"]:
        path = Path(step.pop("file"))
        if not path.is_file():
            raise client.BridgeError(f"REAPER не записал {path.name} — рендер отменён?")
        step |= analysis.level(analysis.Audio.read(path))
    return result


def cmd_gain(args):
    """Гейн-стейджинг трека: уровень на входе цепочки и после каждого плагина."""
    from . import analysis

    data = project_data(args)
    sk = client.call("return rx.skeleton()", project=guard(args), timeout=args.timeout)
    track = pick_tracks(sk, args.track)[0]
    t0, t1 = pick_range(sk, args)
    chain = chain_levels(args, data, track, t0, t1)
    prune_renders(data)
    steps = chain["steps"]
    entry = steps[1] if chain["trim"] and len(steps) > 1 else steps[0]
    need = None
    if entry["avg"] is not None:
        gain, _ = analysis.trim_gain(entry["avg"], entry["peak"])
        need = gain if abs(gain) >= analysis.TRIM_STEP_DB else None
    print(fmt.gain_ladder(track, (t0, t1), chain, need))


def input_levels(args, data: Path, tracks: list[dict], t0: float, t1: float) -> dict[int, dict]:
    """Уровень источника (до плагинов и фейдера) по трекам: номер → уровень, стоящий трим, прочие плагины."""
    from . import analysis

    # Источники не влияют на вход друг друга — их меряем одним рендером. Шина получает
    # звук других треков, уже обработанный их плагинами: каждую — отдельным проходом.
    buses = [t for t in tracks if t["folder"] or t["receives"]]
    passes = [[t for t in tracks if t not in buses]] + [[t] for t in buses]
    out = {}
    for i, group in enumerate(p for p in passes if p):
        render_dir = data / "renders" / f"{time.strftime('%Y%m%d-%H%M%S')}-trim-{i}"
        numbers = ", ".join(str(t["n"]) for t in group)
        result = client.call(
            f"return rx.input_render({{{numbers}}}, {lua(str(render_dir))}, {{{t0!r}, {t1!r}}})",
            project=guard(args), timeout=max(args.timeout, 1800),
        )
        info = {t["n"]: t for t in result["tracks"]}
        for path in map(Path, result["files"]):
            if not path.is_file():
                raise client.BridgeError(f"REAPER не записал {path.name} — рендер отменён?")
            n = int(re.fullmatch(r"stem-(\d+)", path.stem).group(1))
            out[n] = info[n] | analysis.level(analysis.Audio.read(path))
        shutil.rmtree(render_dir, ignore_errors=True)  # стемы всех дорожек за весь трек — гигабайты
    return out


def cmd_trim(args):
    """Ставит трим первым плагином: источник приводится к рабочему уровню плагинов."""
    from . import analysis

    data = project_data(args)
    sk = client.call("return rx.skeleton()", project=guard(args), timeout=args.timeout)
    if args.tracks:
        wanted = pick_tracks(sk, ",".join(args.tracks))
    else:
        wanted = [t for t in sk["tracks"] if t["items"] and not t["folder"]]
    tracks = [t for t in wanted if not t["mute"]]
    if not tracks:
        raise client.BridgeError("дорожек для трима нет")
    t0, t1 = pick_range(sk, args)
    levels = input_levels(args, data, tracks, t0, t1)

    rows, edits = [], []
    for t in tracks:
        m = levels[t["n"]]
        row = {"n": t["n"], "name": t["name"], "avg": m["avg"], "peak": m["peak"], "old": m.get("trim")}
        rows.append(row)
        if m["avg"] is None:
            row["note"] = "молчит на участке"
            continue
        gain, row["by_peak"] = analysis.trim_gain(m["avg"], m["peak"], args.target, args.peak)
        if row["old"] is not None and abs(gain - row["old"]) < analysis.TRIM_STEP_DB:
            row["new"] = row["old"]
            continue
        row["new"] = 0.0 if abs(gain) < analysis.TRIM_STEP_DB else gain
        if m["others"] and not args.with_fx:
            row["note"] = "не тронут — после трима стоят плагины, он изменит их работу: --with-fx"
        else:
            edits.append(row)

    muted = [f"{t['n']} {t['name']}" for t in wanted if t["mute"]]
    print(fmt.trim_table((t0, t1), rows, args.target, args.peak, muted))
    if args.dry_run or not edits:
        return
    code = "\n".join(f"rx.trim({row['n']}, {row['new']!r})" for row in edits)
    _, changes = apply_edit(args, data, f"трим: дорожек {len(edits)} к {args.target:g} dBFS", code)
    print("изменено:\n  " + "\n  ".join(changes) if changes else "изменений в состоянии нет")


def reference_file(name: str | None) -> Path:
    """Референс: путь как есть, имя файла из references/ в папке трека, а без имени — единственный файл там."""
    if name and Path(name).expanduser().is_file():
        return Path(name).expanduser()
    project = track_dir()
    folder = (project.parent if project.name == "reaper" else project) / "references"
    found = sorted(p for p in folder.iterdir() if p.is_file() and not p.name.startswith(".")) if folder.is_dir() else []
    if name:
        match = [p for p in found if p.name == name or p.stem == name] or [p for p in found if name.lower() in p.name.lower()]
        if len(match) == 1:
            return match[0]
        raise client.BridgeError(f"файла нет: {name}" + (f" (в references/: {', '.join(p.name for p in found)})" if found else ""))
    if len(found) == 1:
        return found[0]
    if not found:
        raise client.BridgeError(f"референсов нет: положи файл в {folder} или укажи путь")
    raise client.BridgeError("в references/ несколько файлов, укажи какой: " + ", ".join(p.name for p in found))


def cmd_reference(args):
    """Сравнивает последний анализ микса с референсным файлом."""
    import subprocess
    import tempfile

    from . import analysis

    data = project_data(args)
    reports = sorted((data / "analysis").glob("*/report.json"))
    mixes = [r for r in (json.loads(p.read_text()) for p in reports) if "mix" in r["files"] and not r["files"]["mix"]["silent"]]
    if not mixes:
        raise client.BridgeError("анализа микса ещё нет — сначала reamix analyze")
    source = reference_file(args.file)
    cache = data / "reference" / f"{source.stem}.json"
    if cache.is_file() and not args.fresh:
        ref = json.loads(cache.read_text())
    else:
        # ffmpeg читает любой формат (mp3, m4a, flac) и приводит к float WAV.
        with tempfile.TemporaryDirectory() as tmp:
            wav = Path(tmp) / "ref.wav"
            done = subprocess.run(
                ["ffmpeg", "-v", "error", "-y", "-i", str(source), "-ac", "2", "-c:a", "pcm_f32le", str(wav)],
                capture_output=True, text=True,
            )
            if done.returncode != 0:
                raise client.BridgeError(f"ffmpeg не прочитал файл: {done.stderr.strip()[:200]}")
            ref, _ = analysis.measure(analysis.Audio.read(wav), [])
        cache.parent.mkdir(exist_ok=True)
        cache.write_text(json.dumps(ref, ensure_ascii=False))
    if ref["silent"]:
        raise client.BridgeError("референс — тишина")
    latest = mixes[-1]
    print(f"анализ {latest['id']} · {fmt.clock(latest['range'][0])}–{fmt.clock(latest['range'][1])} против {source.name} ({fmt.clock(ref['seconds'])})")
    print(analysis.compare(latest["files"]["mix"], ref, source.stem[:24]))


def cmd_plugins(args):
    if args.action == "scan":
        print(scan_plugins())
        return
    if not paths.CATALOG.is_file():
        print("каталога нет — сначала reamix plugins scan")
        return 1
    rows = plugins.find(plugins.load(), args.terms, args.inst)
    for row in rows:
        print(plugins.line(row))
    print(f"найдено {len(rows)}" + (" · * — использовался недавно" if rows else ""))


def cmd_import_als(args):
    """Переносит проект Ableton Live в проект REAPER; плагины ставит мост."""
    from . import importer

    return importer.run(args)


def cmd_multitrack(args):
    """Проект REAPER из папки с мультитреком: осмотр файлов, затем сборка по раскладке."""
    from . import multitrack

    return multitrack.run(args)


def cmd_init(args):
    """Заводит в папке трека документацию для Claude и служебную папку."""
    cwd, project = Path.cwd().resolve(), track_dir()
    if not any(p.suffix.lower() == ".rpp" for p in project.iterdir()):
        print("в этой папке нет проекта REAPER (.rpp) — ни здесь, ни в reaper/")
        return 1
    for src in sorted((paths.TEMPLATES / "track").rglob("*")):
        dst = cwd / src.relative_to(paths.TEMPLATES / "track")
        if src.is_dir():
            dst.mkdir(exist_ok=True)
        elif dst.exists():
            print(f"есть, не трогаю: {dst.relative_to(cwd)}")
        else:
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy(src, dst)
            print(f"создан: {dst.relative_to(cwd)}")
    (project / paths.TRACK_DATA).mkdir(exist_ok=True)


def scan_plugins() -> str:
    found, summary = plugins.scan()
    plugins.save(found)
    lines = [f"плагинов {summary['plugins']} → {paths.CATALOG}"]
    if summary["missing"]:
        lines.append(f"в списках REAPER, но файла нет ({len(summary['missing'])}): " + ", ".join(summary["missing"]))
    if summary["unscanned"]:
        lines.append(f"лежат на диске, но REAPER их не видит ({len(summary['unscanned'])}): " + ", ".join(summary["unscanned"]))
    return "\n".join(lines)


def startup_line() -> str:
    return (
        f'do local ok, err = pcall(dofile, "{paths.BRIDGE}") '
        'if not ok then reaper.ShowConsoleMsg("reamix: " .. tostring(err) .. "\\n") end end'
    )


def bin_link() -> Path | None:
    """Где лежит (или может лежать) ссылка на команду reamix."""
    on_path = os.environ.get("PATH", "").split(os.pathsep)
    for folder in paths.BIN_DIRS:
        if (folder / "reamix").is_symlink():
            return folder / "reamix"
    for folder in paths.BIN_DIRS:
        if str(folder) in on_path and os.access(folder, os.W_OK):
            return folder / "reamix"
    return None


def skills() -> list[tuple[Path, Path]]:
    """Скиллы для Claude Code: ссылка в ~/.claude/skills → папка в репозитории."""
    return [(paths.SKILL_LINK, paths.SKILL), (paths.SKILL_ABLETON_LINK, paths.SKILL_ABLETON)]


def cmd_install(args):
    """Автозапуск моста в REAPER, скилл для Claude Code, команда в PATH, данные пользователя."""
    if not paths.REAPER.is_dir():
        print(f"папка REAPER не найдена: {paths.REAPER}\nесли REAPER стоит в другом месте, задай REAPER_RESOURCE_PATH")
        return 1
    text = paths.STARTUP.read_text() if paths.STARTUP.exists() else ""
    if str(paths.BRIDGE) in text:
        print(f"автозапуск уже есть: {paths.STARTUP}")
    else:
        paths.STARTUP.parent.mkdir(parents=True, exist_ok=True)
        tail = "" if not text or text.endswith("\n") else "\n"
        paths.STARTUP.write_text(text + tail + "-- reamix: мост для сведения с Claude\n" + startup_line() + "\n")
        print(f"автозапуск добавлен: {paths.STARTUP}")

    for link, folder in skills():
        if link.is_symlink() or link.exists():
            print(f"скилл уже есть: {link}")
        else:
            link.parent.mkdir(parents=True, exist_ok=True)
            link.symlink_to(folder)
            print(f"скилл подключён: {link} → {folder}")

    link = bin_link()
    if link is None:
        print(f"в PATH нет папки с правом записи — вызывай {paths.LAUNCHER} по полному пути")
    elif link.is_symlink() or link.exists():
        print(f"команда уже есть: {link}")
    else:
        link.symlink_to(paths.LAUNCHER)
        print(f"команда в PATH: {link} → {paths.LAUNCHER}")

    paths.USER.mkdir(parents=True, exist_ok=True)
    if not paths.PREFERENCES.exists():
        shutil.copy(paths.TEMPLATES / "preferences.md", paths.PREFERENCES)
        print(f"предпочтения: {paths.PREFERENCES} (пока пустой шаблон)")
    print(scan_plugins())
    if not shutil.which("ffmpeg"):
        print("ffmpeg не найден — нужен только для reamix reference (brew install ffmpeg)")
    print("\nПерезапусти REAPER: мост стартует вместе с ним. Проверка — reamix doctor")


def cmd_uninstall(args):
    """Убирает автозапуск, скилл и команду. Данные пользователя и папки .reamix в треках остаются."""
    if paths.STARTUP.exists():
        lines = paths.STARTUP.read_text().splitlines()
        kept = [l for l in lines if str(paths.BRIDGE) not in l and l.strip() != "-- reamix: мост для сведения с Claude"]
        if len(kept) != len(lines):
            if any(l.strip() for l in kept):
                paths.STARTUP.write_text("\n".join(kept) + "\n")
            else:
                paths.STARTUP.unlink()
            print(f"автозапуск убран: {paths.STARTUP}")
    for link, folder in skills():
        if link.is_symlink() and link.resolve() == folder.resolve():
            link.unlink()
            print(f"скилл отключён: {link}")
    for folder in paths.BIN_DIRS:
        link = folder / "reamix"
        if link.is_symlink() and link.resolve() == paths.LAUNCHER.resolve():
            link.unlink()
            print(f"команда убрана: {link}")
    print(f"мост остановится при следующем запуске REAPER; твои данные остались в {paths.USER}")


def cmd_doctor(args):
    """Проверка окружения: что установлено, что запущено, чего не хватает."""
    problems = 0

    def check(ok: bool, good: str, bad: str):
        nonlocal problems
        problems += not ok
        print(("  ок   " if ok else "  НЕТ  ") + (good if ok else bad))

    check(paths.REAPER.is_dir(), f"папка REAPER: {paths.REAPER}", f"папка REAPER не найдена: {paths.REAPER}")
    startup = paths.STARTUP.exists() and str(paths.BRIDGE) in paths.STARTUP.read_text()
    check(startup, "автозапуск моста прописан", "автозапуска нет — reamix install")
    for link, _ in skills():
        check(link.exists(), f"скилл подключён: {link}", f"скилл {link.name} не подключён — reamix install")
    check(shutil.which("reamix") is not None, "команда reamix в PATH", f"команды нет в PATH — вызывай {paths.LAUNCHER}")
    check(shutil.which("ffmpeg") is not None, "ffmpeg найден", "ffmpeg не найден — нужен для reamix reference")
    check(paths.CATALOG.is_file(), f"каталог плагинов: {paths.CATALOG}", "каталога плагинов нет — reamix plugins scan")

    beat = client.alive()
    if beat is None:
        check(False, "", "мост не запущен — открой REAPER (или перезапусти после установки)")
    else:
        try:
            info = client.call(
                "return {version = reaper.GetAppVersion(), offline = ({reaper.get_config_var_string('offlineinact')})[2]}",
                timeout=3,
            )
            version = info["version"]
            check(True, f"мост отвечает · REAPER {version} · {beat['project'] or 'проект не сохранён'}", "")
            major = int(re.match(r"\d+", version).group())
            check(major >= 7, "версия REAPER подходит", f"REAPER {version}: проверено только на 7 и новее")
            if info.get("offline") == "1":
                print("  инфо REAPER отключает медиа, когда неактивен — рендер сам включает их обратно")
        except client.BridgeError as e:
            check(False, "", f"мост запущен, но не отвечает: {e}")
    return 1 if problems else 0


def cmd_prefs(args):
    """Предпочтения пользователя, общие для всех треков."""
    if not paths.PREFERENCES.exists():
        paths.USER.mkdir(parents=True, exist_ok=True)
        shutil.copy(paths.TEMPLATES / "preferences.md", paths.PREFERENCES)
    print(paths.PREFERENCES if args.path else f"{paths.PREFERENCES}\n\n{paths.PREFERENCES.read_text().rstrip()}")


def main(argv=None) -> int:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--any-project", action="store_true", help="не сверять активный проект REAPER с текущей папкой")
    common.add_argument("--timeout", type=float, default=15.0, help="сколько секунд ждать ответа моста")

    parser = argparse.ArgumentParser(prog="reamix", description="Сведение в REAPER руками Claude")
    sub = parser.add_subparsers(dest="command", required=True)

    def add(name, func, help):
        p = sub.add_parser(name, help=help, parents=[common])
        p.set_defaults(func=func)
        return p

    add("status", cmd_status, "жив ли мост и какой проект открыт")
    add("reload", cmd_reload, "перезапустить мост после правки reamix_bridge.lua")
    p = add("state", cmd_state, "скелет проекта: треки, плагины, посылы, огибающие, маркеры")
    p.add_argument("--json", action="store_true", help="полные данные вместо компактного вида")
    p = add("fx", cmd_fx, "цепочка плагинов трека или параметры одного плагина")
    p.add_argument("track")
    p.add_argument("fx", nargs="?")
    p.add_argument("--match", help="только параметры с этой подстрокой в имени")
    p.add_argument("--all", action="store_true", help="все параметры, а не только отличающиеся от умолчания")
    p = add("eval", cmd_eval, "выполнить Lua в REAPER без undo-блока (чтение)")
    p.add_argument("code", help="код, @файл или - (stdin)")
    p = add("do", cmd_do, "правка: Lua одним undo-блоком, со снимками до и после")
    p.add_argument("label", help="описание правки — попадёт в историю undo и журнал")
    p.add_argument("code", help="код, @файл или - (stdin)")
    p.add_argument("--no-snapshot", action="store_true")
    add("undo", cmd_undo, "откатить последнюю правку, если она сделана через reamix")
    p = add("ab", cmd_ab, "переключить последнюю правку: до / после (для сравнения на слух)")
    p.add_argument("state", nargs="?", choices=["before", "after"], help="без аргумента — в другое состояние")
    p.add_argument("--region", help="запустить этот регион по кругу")
    p.add_argument("--range", help="запустить участок по кругу: 16-32")
    p = add("play", cmd_play, "воспроизведение; с участком — по кругу")
    p.add_argument("--region")
    p.add_argument("--range")
    p.add_argument("--once", action="store_true", help="участок один раз, без повтора")
    add("stop", cmd_stop, "остановить воспроизведение")
    p = add("snapshot", cmd_snapshot, "сохранить полное состояние треков")
    p.add_argument("label", nargs="?", default="")
    add("changes", cmd_changes, "что изменилось с последнего снимка")
    p = add("analyze", cmd_analyze, "рендер микса и стемов + метрики: громкость, пики, спектр, стерео, конфликты")
    p.add_argument("--tracks", help="номера и имена через запятую или all; по умолчанию треки верхнего уровня")
    p.add_argument("--region", help="имя или номер региона")
    p.add_argument("--range", help="участок в секундах: 16-32")
    p.add_argument("--fresh", action="store_true", help="не брать отчёт из кэша")
    p = add("ride", cmd_ride, "выровнять громкость трека огибающей по его же кривой громкости")
    p.add_argument("track", help="номер или имя трека")
    p.add_argument("--region", help="имя или номер региона")
    p.add_argument("--range", help="участок в секундах: 16-32")
    p.add_argument("--max", type=float, default=4.0, help="предел поправки, дБ (по умолчанию 4)")
    p.add_argument("--smooth", type=float, default=0.6, help="сглаживание, с (по умолчанию 0,6)")
    p.add_argument("--no-verify", action="store_true", help="не рендерить повторно для проверки")
    p.add_argument("--vs-mix", action="store_true", help="держать трек на постоянном расстоянии от остального микса")
    p.add_argument("--offset", type=float, default=0.0, help="с --vs-mix: сдвинуть расстояние, дБ (плюс — трек громче)")
    p = add("phase", cmd_phase, "фаза между дорожками одного источника")
    p.add_argument("tracks", nargs="+", help="две или больше дорожек: номера или имена")
    p.add_argument("--region", help="имя или номер региона")
    p.add_argument("--range", help="участок в секундах: 16-32")
    p = add("gain", cmd_gain, "уровень трека на входе цепочки и после каждого плагина")
    p.add_argument("track", help="номер или имя трека")
    p.add_argument("--region", help="имя или номер региона")
    p.add_argument("--range", help="участок в секундах: 16-32")
    p = add("trim", cmd_trim, "трим первым плагином: привести источник к рабочему уровню плагинов")
    p.add_argument("tracks", nargs="*", help="номера или имена; без них — все дорожки со звуком, кроме папок")
    p.add_argument("--region", help="имя или номер региона")
    p.add_argument("--range", help="участок в секундах: 16-32")
    p.add_argument("--target", type=float, default=-18.0, help="средний уровень, dBFS (по умолчанию −18)")
    p.add_argument("--peak", type=float, default=-6.0, help="предел пиков, dBFS (по умолчанию −6)")
    p.add_argument("--with-fx", action="store_true", help="ставить и там, где после трима уже стоят плагины")
    p.add_argument("--dry-run", action="store_true", help="только показать поправки")
    p = add("reference", cmd_reference, "сравнить последний анализ микса с референсным файлом")
    p.add_argument("file", nargs="?", help="путь или имя файла из references/; без него — единственный файл в references/")
    p.add_argument("--fresh", action="store_true", help="пересчитать референс заново")
    p = add("plugins", cmd_plugins, "каталог установленных плагинов")
    p.add_argument("action", choices=["scan", "find"])
    p.add_argument("terms", nargs="*")
    p.add_argument("--inst", action="store_true", help="включая инструменты")
    p = add("multitrack", cmd_multitrack, "проект REAPER из папки с мультитреком: осмотр, с --build — сборка по раскладке")
    p.add_argument("folder", nargs="?", default=".", help="папка с мультитреком (по умолчанию текущая)")
    p.add_argument("--build", action="store_true", help="собрать проект по раскладке")
    p.add_argument("--layout", help="файл раскладки (по умолчанию reaper/.reamix/multitrack/layout.txt)")
    p.add_argument("--force", action="store_true", help="осмотр: пересоздать черновик раскладки; сборка: перезаписать .rpp")
    p.add_argument("--no-open", action="store_true", help="не открывать готовый проект в REAPER")
    p = add("import-als", cmd_import_als, "перенести проект Ableton Live (.als) в проект REAPER")
    p.add_argument("file", help="путь к .als")
    p.add_argument("--out", help="папка для проекта REAPER (по умолчанию reaper/ рядом с .als)")
    p.add_argument("--force", action="store_true", help="перезаписать уже существующий .rpp")
    p.add_argument("--no-plugins", action="store_true", help="только дорожки, клипы и автоматизация микшера — REAPER не нужен")
    p.add_argument("--unsaved-ok", action="store_true", help="грузить плагины, даже если в REAPER есть несохранённые изменения")
    p.add_argument("--no-open", action="store_true", help="не открывать готовый проект в REAPER")
    p.add_argument("--copy-media", action="store_true", help="скопировать в проект файлы, лежащие вне папки проекта Ableton")
    p.add_argument("--ignore-freeze", action="store_true", help="у замороженных дорожек брать исходные клипы и живые плагины, а не звук заморозки")
    add("init", cmd_init, "завести в папке трека документацию и служебную папку")
    add("install", cmd_install, "автозапуск моста, скилл, команда в PATH, каталог плагинов")
    add("uninstall", cmd_uninstall, "убрать автозапуск, скилл и команду (данные остаются)")
    add("doctor", cmd_doctor, "проверить окружение и связь с REAPER")
    p = add("prefs", cmd_prefs, "предпочтения пользователя, общие для всех треков")
    p.add_argument("--path", action="store_true", help="только путь к файлу")

    args = parser.parse_args(argv)
    try:
        return args.func(args) or 0
    except client.BridgeError as e:
        print(f"ошибка: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
