"""Обмен с мостом в REAPER через файлы в .ipc/ (протокол — docs/architecture.md)."""

import fcntl
import json
import os
import time
import uuid
from pathlib import Path

from . import paths


class BridgeError(Exception):
    pass


def alive() -> dict | None:
    """Последний сигнал моста или None, если мост не запущен."""
    try:
        data = json.loads((paths.IPC / "alive.json").read_text(errors="replace"))
    except (OSError, ValueError):
        return None
    data["age"] = time.time() - data.get("time", 0)
    return data


def request(
    code: str,
    *,
    undo: str | None = None,
    project: Path | None = None,
    snap: Path | None = None,
    timeout: float = 15.0,
) -> dict:
    """Выполняет Lua-код в REAPER и возвращает ответ моста целиком.

    undo — описание правки: код выполняется одним undo-блоком и откатывается при ошибке.
    project — папка, в которой должен лежать активный проект (иначе мост откажет).
    snap — папка, куда мост сложит снимки состояния до и после правки.
    """
    if alive() is None:
        raise BridgeError("мост не запущен: открой REAPER (мост стартует сам) или выполни в нём reamix_bridge.lua")
    req, resp = paths.IPC / "request.lua", paths.IPC / "response.json"
    rid = uuid.uuid4().hex[:12]
    headers = [f"--@id {rid}"]
    for name, value in (("undo", undo), ("project", project), ("snap", snap)):
        if value is not None:
            headers.append(f"--@{name} {str(value).replace(chr(10), ' ')}")

    with open(paths.IPC / "lock", "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        resp.unlink(missing_ok=True)
        tmp = req.with_suffix(".tmp")
        tmp.write_text("\n".join(headers) + "\n" + code)
        os.replace(tmp, req)

        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                data = json.loads(resp.read_text(errors="replace"))
            except (OSError, ValueError):
                time.sleep(0.01)
                continue
            resp.unlink(missing_ok=True)
            if data.get("id") == rid:
                return data

        # Запрос, который мост так и не забрал, нельзя оставлять: его выполнит
        # следующий запуск моста, когда правка уже никому не нужна.
        try:
            req.unlink()
            picked_up = False
        except FileNotFoundError:
            picked_up = True

    if picked_up:
        raise BridgeError(
            f"мост принял запрос, но не ответил за {timeout:g} с — REAPER занят "
            "(рендер, открытый диалог); результат неизвестен, проверь состояние"
        )
    beat = alive()
    ago = f"последний сигнал {beat['age']:.0f} с назад" if beat else "сигнала нет"
    raise BridgeError(f"мост не отвечает ({ago}) — REAPER занят или закрыт")


def call(code: str, **kwargs):
    """Как request, но возвращает только результат и бросает ошибку моста."""
    data = request(code, **kwargs)
    if not data.get("ok"):
        note = " (правка откатана)" if data.get("rolled_back") else ""
        raise BridgeError(str(data.get("error")) + note)
    return data.get("result")
