"""Снимки состояния в папке трека: .reamix/chunks (состояния треков по хешу) и
.reamix/snapshots (что входило в снимок). Одинаковые состояния хранятся один раз."""

import hashlib
import json
import shutil
import time
from pathlib import Path

from . import fmt, paths


def data_dir(project_path: str) -> Path:
    return Path(project_path).parent / paths.TRACK_DATA


def staging(data: Path) -> Path:
    return data / "staging" / time.strftime("%Y%m%d-%H%M%S")


def ingest(result: dict, data: Path, kind: str, label: str) -> dict:
    """Переносит снимок, который мост сложил в папку, в хранилище. Возвращает запись снимка."""
    src = Path(result["dir"])
    chunks = data / "chunks"
    chunks.mkdir(parents=True, exist_ok=True)
    tracks = []
    for t in result["tracks"]:
        file = src / t["file"]
        sha = hashlib.sha1(file.read_bytes()).hexdigest()
        target = chunks / f"{sha}.rtrack"
        if target.exists():
            file.unlink()
        else:
            file.rename(target)
        tracks.append({"n": t["n"], "guid": t["guid"], "name": t["name"], "sha": sha, "bytes": t["bytes"]})
    out = data / "snapshots"
    out.mkdir(exist_ok=True)
    # Порядковый номер разводит снимки «до» и «после», снятые в одну секунду.
    name = f"{time.strftime('%Y%m%d-%H%M%S')}-{len(snapshots(data)) + 1:05d}-{kind}"
    record = {
        "id": name,
        "kind": kind,
        "label": label,
        "rev": result["rev"],
        "tracks": tracks,
        "skeleton": json.loads((src / "skeleton.json").read_text(errors="replace")),
    }
    (out / f"{name}.json").write_text(json.dumps(record, ensure_ascii=False))
    shutil.rmtree(src)
    return record


def cleanup_staging(data: Path) -> None:
    root = data / "staging"
    if root.is_dir() and not any(p.is_file() for p in root.rglob("*")):
        shutil.rmtree(root)


def snapshots(data: Path) -> list[Path]:
    return sorted((data / "snapshots").glob("*.json"))


def load(path: Path) -> dict:
    return json.loads(path.read_text())


def latest(data: Path) -> dict | None:
    files = snapshots(data)
    return load(files[-1]) if files else None


def log_edit(data: Path, label: str, before: dict | None, after: dict | None) -> None:
    """Строка журнала правок: когда, что, снимки до и после (если снимались)."""
    data.mkdir(exist_ok=True)
    stamp = after["id"] if after else time.strftime("%Y%m%d-%H%M%S")
    line = "\t".join([stamp, label, before["id"] if before else "", str(after["rev"]) if after else ""])
    with open(data / "edits.tsv", "a") as f:
        f.write(line + "\n")


# ── Сравнение снимков ────────────────────────────────────────────────────────


def _track_changes(old: dict, new: dict) -> list[str]:
    out = []
    if old["name"] != new["name"]:
        out.append(f"имя «{old['name']}» → «{new['name']}»")
    if abs(old["vol"] - new["vol"]) > 1e-6:
        out.append(f"vol {fmt.db(old['vol'])} → {fmt.db(new['vol'])}dB")
    if abs(old["pan"] - new["pan"]) > 1e-6:
        out.append(f"pan {fmt.pan(old['pan'])} → {fmt.pan(new['pan'])}")
    for key in ("mute", "solo", "phase", "main_send", "channels", "width"):
        if old[key] != new[key]:
            out.append(f"{key} {old[key]} → {new[key]}")

    old_fx = {fx["guid"]: fx for fx in old["fx"]}
    new_fx = {fx["guid"]: fx for fx in new["fx"]}
    for guid, fx in new_fx.items():
        if guid not in old_fx:
            out.append(f"+fx {fx['i']} {fmt.fx_short(fx['name'])}")
        else:
            was = old_fx[guid]
            if fmt.fx_label(was) != fmt.fx_label(fx):
                out.append(f"fx {fmt.fx_label(was)} → {fmt.fx_label(fx)}")
            if was.get("preset") != fx.get("preset"):
                out.append(f"fx {fmt.fx_short(fx['name'])}: пресет «{fx.get('preset') or ''}»")
    for guid, fx in old_fx.items():
        if guid not in new_fx:
            out.append(f"-fx {fmt.fx_short(fx['name'])}")
    kept_old = [g for g in old_fx if g in new_fx]
    kept_new = [g for g in new_fx if g in old_fx]
    if kept_old != kept_new:
        out.append("порядок плагинов изменён")

    def send_key(s):
        # Номер получателя меняется при перестановке треков, GUID — нет.
        return s.get("dest_guid") or s.get("dest", s["dest_name"]), s.get("dst_chan", 0)

    old_sends = {send_key(s): s for s in old["sends"]}
    new_sends = {send_key(s): s for s in new["sends"]}
    for key, s in new_sends.items():
        was = old_sends.get(key)
        if was is None:
            out.append(f"+send -> {s['dest_name']} {fmt.db(s['vol'])}dB")
        elif abs(was["vol"] - s["vol"]) > 1e-6 or was["mute"] != s["mute"] or was["mode"] != s["mode"]:
            out.append(f"send -> {s['dest_name']} {fmt.db(was['vol'])} → {fmt.db(s['vol'])}dB" + (" mute" if s["mute"] else ""))
    for key, s in old_sends.items():
        if key not in new_sends:
            out.append(f"-send -> {s['dest_name']}")

    old_env = {e["name"]: e for e in old["envelopes"]}
    new_env = {e["name"]: e for e in new["envelopes"]}
    for name, e in new_env.items():
        was = old_env.get(name)
        if was is None:
            out.append(f"+env {name}({e['points']})")
        elif was["points"] != e["points"] or was["active"] != e["active"]:
            out.append(f"env {name} {was['points']} → {e['points']} точек")
    for name in old_env:
        if name not in new_env:
            out.append(f"-env {name}")

    def extent(t):
        return t["items"], t.get("items_start"), t.get("items_end")

    if extent(old) != extent(new):
        out.append(f"items {old['items']} → {new['items']}")
    return out


def diff(old: dict, new: dict) -> list[str]:
    """Что изменилось между двумя снимками, по строке на трек."""
    lines = []
    op, np_ = old["skeleton"]["project"], new["skeleton"]["project"]
    if op["bpm"] != np_["bpm"]:
        lines.append(f"темп {op['bpm']:g} → {np_['bpm']:g}")
    if old["skeleton"]["markers"] != new["skeleton"]["markers"]:
        lines.append("маркеры или регионы изменены")

    def tracks_of(snap):
        sk = snap["skeleton"]
        sha = {t["guid"]: t["sha"] for t in snap["tracks"]}
        return {t["guid"]: (t, sha.get(t["guid"])) for t in [sk["master"], *sk["tracks"]]}

    old_tracks, new_tracks = tracks_of(old), tracks_of(new)
    for guid, (t, sha) in new_tracks.items():
        title = "MASTER" if t["n"] == 0 else f"{t['n']} {t['name']}"
        if guid not in old_tracks:
            lines.append(f"+трек {title}")
            continue
        was, was_sha = old_tracks[guid]
        changes = _track_changes(was, t)
        if not changes and sha != was_sha:
            changes = ["внутреннее состояние (параметры плагинов, огибающие или айтемы)"]
        if changes:
            lines.append(f"{title}: " + "; ".join(changes))
    for guid, (t, _) in old_tracks.items():
        if guid not in new_tracks:
            lines.append(f"-трек {t['n']} {t['name']}")
    order_old = [g for g in old_tracks if g in new_tracks]
    order_new = [g for g in new_tracks if g in old_tracks]
    if order_old != order_new:
        lines.append("порядок треков изменён")
    return lines
