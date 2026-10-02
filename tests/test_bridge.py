"""Мост и lib.lua против подделки API REAPER (tests/fake_reaper.lua)."""

import json
import shutil
import threading
import time
from pathlib import Path

import pytest
from lupa import LuaRuntime

from reamix import cli, client, fmt, paths, store

REPO = Path(__file__).resolve().parents[1]


@pytest.fixture
def lua():
    return LuaRuntime(unpack_returned_tuples=True)


@pytest.fixture
def fake(tmp_path, monkeypatch, lua):
    """Запущенный мост: подделка REAPER, чей defer-цикл крутит фоновый поток."""
    root = tmp_path / "reamix"
    shutil.copytree(REPO / "bridge", root / "bridge")
    monkeypatch.setattr(paths, "IPC", root / ".ipc")
    song = tmp_path / "song"
    song.mkdir()
    monkeypatch.chdir(song)

    lua.globals().reaper = lua.execute(f'return dofile("{REPO}/tests/fake_reaper.lua")')
    state = lua.globals().reaper._fake
    state.path = str(song / "main.rpp")
    lua.execute(f'dofile("{root}/bridge/reamix_bridge.lua")')

    stop = threading.Event()

    def pump():
        while not stop.is_set():
            state.deferred()
            time.sleep(0.002)

    thread = threading.Thread(target=pump, daemon=True)
    thread.start()
    yield state
    stop.set()
    thread.join()


def test_json_keeps_precision_and_structure(lua):
    encode = lua.execute(f'return dofile("{REPO}/bridge/json.lua").encode')
    make = lua.eval(
        'function() return {name = "Вокал \\"lead\\"\\n", gain = 0.1 + 0.2, n = 3, on = true, '
        'list = {1, 2.5, "x"}, empty = {}, nested = {a = {b = 1}}} end'
    )
    decoded = json.loads(encode(make()))
    assert decoded == {
        "name": 'Вокал "lead"\n', "gain": 0.1 + 0.2, "n": 3, "on": True,
        "list": [1, 2.5, "x"], "empty": {}, "nested": {"a": {"b": 1}},
    }


def test_eval_returns_result_and_project(fake):
    resp = client.request("return 1 + 1")
    assert resp["ok"] and resp["result"] == 2
    assert resp["project"].endswith("song/main.rpp")
    assert resp["rev"] == 100


def test_errors_are_reported(fake):
    assert "request" in client.request("return (")["error"]
    with pytest.raises(client.BridgeError, match="нет трека №99"):
        client.call("return rx.track_info(99)")


def test_heartbeat_names_project(fake):
    beat = client.alive()
    assert beat["project"].endswith("main.rpp") and beat["age"] < 5


def test_track_is_found_by_number_name_and_guid(fake):
    assert client.call('return rx.track_info("Kick").n') == 2
    assert client.call('return rx.track_info("kick").n') == 2
    assert client.call("return rx.track_info(3).name") == "Vocal Lead"
    assert client.call('return rx.track_info(rx.track_info(3).guid).name') == "Vocal Lead"
    assert client.call('return rx.track_info("master").n') == 0
    with pytest.raises(client.BridgeError, match="неоднозначен: 3 Vocal Lead, 4 Vocal Double"):
        client.call('return rx.track_info("Vocal")')


def test_skeleton_prints_only_what_differs_from_defaults(fake):
    client.call('rx.track_set("Kick", {vol_db = -3.5, pan = -0.2, mute = true}) rx.fx_enable("Kick", 2, false)')
    text = fmt.skeleton(client.call("return rx.skeleton()"))
    lines = text.splitlines()
    assert lines[0].endswith("main.rpp · 120 bpm · 48000 Hz · 3:25.5 · rev 104")
    assert lines[1] == " M  MASTER"
    assert lines[2] == " 1  Drums [folder]"
    assert lines[3] == " 2    Kick  -3.5dB  L20  mute  fx: ReaEQ, Pro-C 2(off)"
    assert lines[-1] == "R1  0:30.0–1:00.0  Chorus"


def test_param_is_set_by_displayed_value(fake):
    freq = client.call('return rx.fx_set("Kick", "ReaEQ", "Freq", {fmt = 90})')
    assert freq["fmt"] == "90.0 Hz"
    # Плагин переключает единицы (Hz/kHz) — цель и показания приводятся к одной шкале.
    side = client.call('return rx.fx_set("Kick", "Pro-C", "Sidechain", {fmt = "2.5 kHz"})')
    assert side["fmt"] == "2.50 kHz"
    assert client.call('return rx.fx_set("Kick", 2, "Ratio", {fmt = "4"})')["fmt"] == "4.0:1"
    assert client.call('return rx.fx_set("Kick", 1, 1, 0.75)')["fmt"] == "12.0 dB"


def test_text_param_cannot_be_sought_and_stays_put(fake):
    with pytest.raises(client.BridgeError, match="не читается как число"):
        client.call('return rx.fx_set("Kick", 2, "Style", {fmt = 3})')
    assert client.call('return rx.fx_params("Kick", 2, "Style").params[1].fmt') == "Clean"


def test_fx_params_filter(fake):
    info = client.call('return rx.fx_params("Kick", "Pro-C", "ratio")')
    assert info["total"] == 3 and [p["name"] for p in info["params"]] == ["Ratio"]
    assert fmt.fx_params(info).splitlines() == [
        "2 VST3: Pro-C 2 (FabFilter) · параметров 3, показано 1",
        "   0  Ratio = 4.8:1  (0.2000)",
    ]


def test_fx_lists_only_non_default_params(fake, capsys, tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "DEFAULTS", tmp_path / "defaults")
    assert cli.main(["fx", "Kick", "Pro-C"]) == 0
    assert capsys.readouterr().out.splitlines() == [
        "2 VST3: Pro-C 2 (FabFilter) · параметров 3, отличаются от умолчания 0 (все — с --all)",
    ]
    client.call('rx.fx_set("Kick", "Pro-C", "Ratio", 0.5)')
    assert cli.main(["fx", "Kick", "Pro-C"]) == 0
    assert capsys.readouterr().out.splitlines()[1:] == ["   0  Ratio = 10.5:1  (0.5000)"]
    assert cli.main(["fx", "Kick", "Pro-C", "--all"]) == 0
    assert len(capsys.readouterr().out.splitlines()) == 4
    # Значения по умолчанию сняты один раз и лежат в кэше.
    assert [p.name for p in (tmp_path / "defaults").iterdir()] == ["Pro_C_2_FabFilter_.vst_1.json"]


def test_reading_defaults_leaves_no_trace(fake):
    before = client.call("return {tracks = reaper.CountTracks(0), undo = reaper.Undo_CanUndo2(0) or false}")
    found = client.call('return rx.fx_defaults("Kick", "ReaEQ")')
    assert found["norms"] == [0.5, 0.5]
    assert client.call("return {tracks = reaper.CountTracks(0), undo = reaper.Undo_CanUndo2(0) or false}") == before


def test_failed_edit_is_rolled_back(fake):
    resp = client.request('rx.track_set("Kick", {vol_db = -6}) rx.fx_add("Kick", "Missing Plugin")', undo="сломанная правка")
    assert not resp["ok"] and resp["rolled_back"]
    assert "плагин не добавился" in resp["error"]
    assert client.call('return rx.track_info("Kick").vol') == 1


def test_project_guard_blocks_other_folder(fake, tmp_path):
    other = tmp_path / "other-song"
    resp = client.request('rx.track_set("Kick", {mute = true})', undo="x", project=other)
    assert not resp["ok"] and "а команда вызвана из" in resp["error"]
    assert client.call('return rx.track_info("Kick").mute') is False
    assert client.request("return 1", project=tmp_path / "song" / "docs")["ok"]


def test_do_snapshots_and_reports_changes(fake, capsys, tmp_path):
    code = 'rx.track_set("Kick", {vol_db = -3}) rx.fx_add("Vocal Lead", "VST: ReaComp (Cockos)") return "ok"'
    assert cli.main(["do", "тише бочку", code]) == 0
    out = capsys.readouterr().out
    assert out.splitlines() == [
        "ok",
        "изменено:",
        "  2 Kick: vol +0.0 → -3.0dB",
        "  3 Vocal Lead: +fx 1 ReaComp",
    ]
    data = tmp_path / "song" / ".reamix"
    kinds = [store.load(p)["kind"] for p in store.snapshots(data)]
    assert kinds == ["before", "after"]
    assert not (data / "staging").exists()
    assert (data / "edits.tsv").read_text().split("\t")[1] == "тише бочку"

    # Состояние не менялось — снимок «до» второй правки не снимается заново.
    assert cli.main(["do", "параметр", 'rx.fx_set("Kick", 1, 0, 0.25)']) == 0
    assert "2 Kick: внутреннее состояние" in capsys.readouterr().out
    assert [store.load(p)["kind"] for p in store.snapshots(data)] == ["before", "after", "after"]
    # Неизменившиеся треки хранятся один раз.
    assert len(list((data / "chunks").iterdir())) == 5 + 2 + 1


def test_changes_since_last_snapshot(fake, capsys):
    assert cli.main(["snapshot", "старт"]) == 0
    assert cli.main(["changes"]) == 0
    assert "без изменений" in capsys.readouterr().out.splitlines()[-1]
    client.call('rx.track_set("Vocal Lead", {pan = 0.3})')
    assert cli.main(["changes"]) == 0
    assert capsys.readouterr().out.splitlines()[-1] == "  3 Vocal Lead: pan C → R30"


def test_undo_only_reverts_own_edits(fake, capsys, tmp_path):
    assert cli.main(["undo"]) == 1
    assert "не правка reamix" in capsys.readouterr().err
    assert cli.main(["do", "mute", 'rx.track_set("Kick", {mute = true})', "--no-snapshot"]) == 0
    assert cli.main(["undo"]) == 0
    assert "откатано: reamix: mute" in capsys.readouterr().out
    assert client.call('return rx.track_info("Kick").mute') is False
    journal = (tmp_path / "song" / ".reamix" / "edits.tsv").read_text().splitlines()
    assert journal[-1].split("\t")[1] == "откат: mute"


def test_no_bridge_fails_at_once(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "IPC", tmp_path / ".ipc")
    started = time.monotonic()
    with pytest.raises(client.BridgeError, match="мост не запущен"):
        client.request("return 1")
    assert time.monotonic() - started < 1


def test_ab_switches_last_edit_and_blocks_edits_while_before(fake, capsys):
    assert cli.main(["do", "тише бочку", 'rx.track_set("Kick", {vol_db = -6})']) == 0
    capsys.readouterr()
    vol = lambda: client.call('return rx.track_info("Kick").vol')
    assert vol() == pytest.approx(0.501, abs=0.001)

    assert cli.main(["ab"]) == 0
    assert capsys.readouterr().out.splitlines()[0] == "сейчас: ДО правки «тише бочку»"
    assert vol() == 1
    assert cli.main(["ab", "before"]) == 0  # повтор ничего не ломает
    assert vol() == 1

    # Новая правка стёрла бы redo вместе с правкой, которую слушают.
    assert cli.main(["do", "другая", 'rx.track_set("Kick", {mute = true})']) == 1
    assert "сначала reamix ab after" in capsys.readouterr().err
    with pytest.raises(client.BridgeError, match="состояние «до»"):
        client.call('return rx.fx_defaults("Kick", "ReaEQ")')

    assert cli.main(["ab", "after"]) == 0
    assert capsys.readouterr().out.strip() == "сейчас: ПОСЛЕ правки «тише бочку»"
    assert vol() == pytest.approx(0.501, abs=0.001)
    assert cli.main(["do", "другая", 'rx.track_set("Kick", {mute = true})']) == 0


def test_undo_after_listening_to_before_keeps_it_undone(fake, capsys, tmp_path):
    assert cli.main(["do", "первая", 'rx.track_set("Kick", {vol_db = -6})']) == 0
    assert cli.main(["do", "вторая", 'rx.track_set("Kick", {mute = true})']) == 0
    assert cli.main(["ab", "before"]) == 0
    capsys.readouterr()
    assert cli.main(["undo"]) == 0
    assert capsys.readouterr().out.strip() == "откатано: reamix: вторая"
    info = client.call('return rx.track_info("Kick")')
    assert info["mute"] is False and info["vol"] == pytest.approx(0.501, abs=0.001)
    # Следующий откат снимает уже первую правку, а не «вторую» ещё раз.
    assert cli.main(["undo"]) == 0
    assert capsys.readouterr().out.strip() == "откатано: reamix: первая"
    journal = [line.split("\t")[1] for line in (tmp_path / "song" / ".reamix" / "edits.tsv").read_text().splitlines()]
    assert journal == ["первая", "вторая", "откат: вторая", "откат: первая"]


def test_trim_goes_first_and_is_reused(fake):
    assert client.call('return rx.trim("Kick", -4.5)')["raw"] == -4.5
    chain = [fx["name"] for fx in client.call('return rx.track_info("Kick").fx')]
    assert chain == ["JS: utility/volume", "VST: ReaEQ (Cockos)", "VST3: Pro-C 2 (FabFilter)"]
    # Второй вызов правит стоящий трим, а не ставит ещё один.
    client.call('rx.fx_enable("Kick", 1, false) rx.trim("Kick", 2)')
    info = client.call('return rx.track_info("Kick")')
    assert [fx["name"] for fx in info["fx"]] == chain and info["fx"][0]["enabled"]
    assert client.call('return rx.fx_params("Kick", 1).params[1].fmt') == "2.0"


def test_text_option_is_set_by_name(fake):
    assert [o["text"] for o in client.call('return rx.fx_options("Kick", "Pro-C", "Style")')] == ["Clean", "Punch", "Vocal"]
    assert client.call('return rx.fx_set("Kick", "Pro-C", "Style", {text = "Punch"})')["fmt"] == "Punch"
    with pytest.raises(client.BridgeError, match="вариант не найден: Opto"):
        client.call('return rx.fx_set("Kick", "Pro-C", "Style", {text = "Opto"})')


def test_unanswered_request_is_withdrawn(tmp_path, monkeypatch):
    """REAPER завис или упал: запрос не должен дождаться следующего запуска моста."""
    ipc = tmp_path / ".ipc"
    ipc.mkdir()
    (ipc / "alive.json").write_text(json.dumps({"time": time.time() - 60, "project": "", "rev": 1}))
    monkeypatch.setattr(paths, "IPC", ipc)
    with pytest.raises(client.BridgeError, match="последний сигнал 60 с назад"):
        client.request("return 1", timeout=0.2)
    assert not (ipc / "request.lua").exists()


def test_gain_ladder_and_trim_table_name_what_to_fix():
    track = {"n": 5, "name": "Vox"}
    step = lambda fx, name, avg, lufs: {"fx": fx, "name": name, "avg": avg, "peak": avg + 10, "lufs": lufs}  # noqa: E731
    chain = {
        "trim": True, "off": [{"fx": 4, "name": "VST3: Pro-Q 4 (FabFilter)"}],
        "steps": [step(0, "", -25.0, -26.0), step(1, "JS: Volume Adjustment", -18.0, -19.0), step(2, "VST: ReaComp (Cockos)", -21.0, -22.0)],
    }
    lines = fmt.gain_ladder(track, (16.0, 32.0), chain, None).splitlines()
    assert lines[2].endswith("+7.0  трим") and lines[3].endswith("-3.0 !")
    assert lines[4:] == ["выключены: 4 Pro-Q 4", "вход в плагины на рабочем уровне", "увели уровень больше чем на 1 дБ: 2 ReaComp"]
    assert "нужна поправка -6.0 дБ — reamix trim 5" in fmt.gain_ladder(track, (0, 1), chain | {"trim": False}, -6.0)

    rows = [
        {"n": 2, "name": "Kick", "avg": -24.1, "peak": -8.0, "old": None, "new": 2.0, "by_peak": True},
        {"n": 7, "name": "Pad", "avg": -18.4, "peak": -9.9, "old": -3.0, "new": -3.0, "by_peak": False},
        {"n": 3, "name": "Hats", "avg": None, "peak": -120.0, "old": None, "note": "молчит на участке"},
    ]
    assert fmt.trim_table((0.0, 60.0), rows, -18.0, -6.0, ["9 Old"]).splitlines()[1:] == [
        "  2 Kick  вход  -24.1  пик   -8.0  трим +2.0 (по пикам)",
        "  7 Pad   вход  -18.4  пик   -9.9  трим -3.0 — как было",
        "  3 Hats  молчит на участке",
        "пропущены (mute): 9 Old",
    ]
