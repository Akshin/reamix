"""Установка и удаление трогают только своё и не теряют чужое."""

import pytest

from reamix import cli, paths, plugins


@pytest.fixture
def machine(tmp_path, monkeypatch):
    """Чистая машина: своя папка REAPER, свой PATH, свои данные пользователя."""
    reaper = tmp_path / "REAPER"
    (reaper / "Scripts").mkdir(parents=True)
    (tmp_path / "bin").mkdir()
    for name, value in {
        "REAPER": reaper,
        "STARTUP": reaper / "Scripts" / "__startup.lua",
        "SKILL_LINK": tmp_path / ".claude" / "skills" / "reamix",
        "BIN_DIRS": [tmp_path / "bin"],
        "USER": tmp_path / ".reamix",
        "CATALOG": tmp_path / ".reamix" / "plugins.tsv",
        "PREFERENCES": tmp_path / ".reamix" / "preferences.md",
    }.items():
        monkeypatch.setattr(paths, name, value)
    monkeypatch.setenv("PATH", f"{tmp_path / 'bin'}:/usr/bin:/bin")
    monkeypatch.setattr(plugins, "scan", lambda: ([], {"plugins": 0, "missing": [], "unscanned": []}))
    return tmp_path


def test_install_then_uninstall_restores_the_machine(machine, capsys):
    own_script = 'print("мой скрипт")\n'
    paths.STARTUP.write_text(own_script)

    assert cli.main(["install"]) == 0
    startup = paths.STARTUP.read_text()
    assert startup.startswith(own_script) and str(paths.BRIDGE) in startup
    assert paths.SKILL_LINK.resolve() == paths.SKILL.resolve()
    assert (machine / "bin" / "reamix").resolve() == paths.LAUNCHER.resolve()
    assert paths.PREFERENCES.read_text().startswith("# Мои предпочтения")
    assert paths.CATALOG.is_file()

    # Повторная установка ничего не дублирует и не затирает предпочтения.
    paths.PREFERENCES.write_text("мои правки")
    assert cli.main(["install"]) == 0
    assert paths.STARTUP.read_text() == startup
    assert paths.PREFERENCES.read_text() == "мои правки"

    assert cli.main(["uninstall"]) == 0
    assert paths.STARTUP.read_text() == own_script
    assert not paths.SKILL_LINK.is_symlink()
    assert not (machine / "bin" / "reamix").is_symlink()
    assert paths.PREFERENCES.read_text() == "мои правки"


def test_uninstall_removes_startup_file_it_created_and_keeps_foreign_links(machine):
    foreign = machine / "elsewhere"
    foreign.write_text("")
    (machine / "bin" / "reamix").symlink_to(foreign)  # чужая команда с тем же именем

    assert cli.main(["install"]) == 0
    assert cli.main(["uninstall"]) == 0
    assert not paths.STARTUP.exists()
    assert (machine / "bin" / "reamix").resolve() == foreign.resolve()


def test_install_stops_without_reaper(machine, capsys):
    paths.STARTUP.parent.rmdir()
    paths.REAPER.rmdir()
    assert cli.main(["install"]) == 1
    assert "папка REAPER не найдена" in capsys.readouterr().out
    assert not paths.SKILL_LINK.is_symlink()
