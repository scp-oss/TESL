#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
fix_mo2_paths.py — чинит gamePath=/customExecutables в ModOrganizer.ini
после переноса/копирования установленной сборки на другой компьютер.

Живой симптом, который это лечит: MO2 отказывается открывать "Portable"
инстанс с ошибкой вида
    Cannot open instance 'Portable', the game directory
    'P:\\Games\\skyrim\\Skyrim' doesn't exist or the game plugin
    'Skyrim Special Edition' doesn't recognize it.
— потому что ModOrganizer.ini внутри всё ещё хранит абсолютный путь с
ТОЙ машины/папки, где сборка стояла раньше (или где её изначально
собирали), а не с текущего компьютера.

САМОСТОЯТЕЛЬНЫЙ СКРИПТ — не зависит от launcher/core/*, работает только
стандартной библиотекой Python, можно запускать отдельно от самого
лаунчера (прямой запрос: "прикрепим его к сборке").

Как использовать:
  1. Положить этот файл в КОРЕНЬ папки установки сборки — туда же, где
     лежат папки MO2p/ и Skyrim/ (рядом с ними, не внутри одной из них).
  2. Запустить: двойным кликом (если .py ассоциирован с Python) или
     `python fix_mo2_paths.py` из консоли в этой же папке.

Что делает:
  1. Находит <эта_папка>/MO2p/ModOrganizer.ini
  2. Правильный путь к игре — <эта_папка>/Skyrim (так эта сборка
     раскладывает компоненты: MO2p/ и Skyrim/ — соседние папки в корне
     установки); если такой папки нет, можно ввести путь вручную.
  3. Читает ТЕКУЩЕЕ значение gamePath= из ModOrganizer.ini — это и есть
     старый, сломанный путь с другого компьютера/папки.
  4. Заменяет этот старый путь на правильный ВЕЗДЕ в файле — не только
     в самой строке gamePath=, но и во всех customExecutables (SKSE,
     Creation Kit, Launcher, сторонние инструменты вроде Explorer++),
     которые тоже хранят тот же абсолютный путь — если починить только
     gamePath=, MO2 откроется, но каждый из этих ярлыков останется
     указывать в никуда.
  5. Перед записью сохраняет резервную копию ModOrganizer.ini.bak.
"""
import os
import re
import sys

_GAME_PATH_LINE_RE = re.compile(r"^gamePath\s*=\s*(.*)$", re.MULTILINE)
_SETTINGS_SECTION_RE = re.compile(r"^\[Settings\]\s*$", re.MULTILINE)


def _normalize(path: str) -> str:
    return path.replace("\\", "/")


def _extract_game_path(content: str):
    """Текущее значение gamePath= из [General] — без обёртки
    @ByteArray(...) и с одиночными обратными слэшами (ini-формат
    хранит их задвоенными)."""
    m = _GAME_PATH_LINE_RE.search(content)
    if not m:
        return None
    value = m.group(1).strip()
    if value.startswith("@ByteArray(") and value.endswith(")"):
        value = value[len("@ByteArray("):-1]
    return value.replace("\\\\", "\\")


def rewrite_everywhere(content: str, new_game_folder: str):
    """Возвращает (новый_текст, изменилось_ли, старый_путь).
    Заменяет оба текстовых представления старого пути — forward-slash
    (customExecutables binary=/workingDirectory=) и экранированный
    двойной обратный слэш (gamePath=@ByteArray(...), arguments="...")
    — простой заменой подстроки, поэтому подпути вроде .../Skyrim/data
    чинятся автоматически вместе с базовым путём."""
    old_path = _extract_game_path(content)
    if not old_path:
        return content, False, None

    old_fwd = _normalize(old_path)
    new_fwd = _normalize(new_game_folder)
    if old_fwd == new_fwd:
        return content, False, old_fwd

    old_bs_escaped = old_fwd.replace("/", "\\").replace("\\", "\\\\")
    new_bs_escaped = new_fwd.replace("/", "\\").replace("\\", "\\\\")

    new_content = content.replace(old_bs_escaped, new_bs_escaped)
    new_content = new_content.replace(old_fwd, new_fwd)
    return new_content, new_content != content, old_fwd


def set_plain_ini_key(content: str, key: str, value: str, section_header_re):
    """Переписывает голую (без @ByteArray) строку `key=...` на `value`
    везде, где бы она ни стояла; если строки нет вообще, вставляет её
    сразу после заголовка секции `section_header_re`. Возвращает
    (новый_текст, изменилось_ли)."""
    key_re = re.compile(rf"^{re.escape(key)}\s*=.*$", re.MULTILINE)
    new_line = f"{key}={value}"
    if key_re.search(content):
        new_content = key_re.sub(new_line, content, count=1)
        return new_content, new_content != content

    m = section_header_re.search(content)
    if not m:
        return content, False
    insert_at = m.end()
    new_content = content[:insert_at] + "\n" + new_line + content[insert_at:]
    return new_content, True


def main() -> int:
    root = os.path.dirname(os.path.abspath(__file__))
    ini_path = os.path.join(root, "MO2p", "ModOrganizer.ini")
    skyrim_dir = os.path.join(root, "Skyrim")

    print(f"Папка сборки:              {root}")
    print(f"Ожидаемый ModOrganizer.ini: {ini_path}")
    print(f"Ожидаемая папка игры:      {skyrim_dir}")
    print()

    if not os.path.isfile(ini_path):
        print(f"❌ Не найден {ini_path}")
        print("   Убедитесь, что этот файл лежит в корне папки сборки (рядом с MO2p и Skyrim).")
        input("\nНажмите Enter для выхода...")
        return 1

    if not os.path.isdir(skyrim_dir):
        print(f"⚠️ Папка {skyrim_dir} не найдена.")
        manual = input(
            "Введите реальный путь к папке с SkyrimSE.exe (или просто Enter, чтобы отменить): "
        ).strip()
        if not manual:
            return 1
        skyrim_dir = manual

    with open(ini_path, "r", encoding="utf-8") as f:
        content = f.read()

    new_content, path_changed, old_path = rewrite_everywhere(content, skyrim_dir)

    if old_path is None:
        print("❌ Не удалось найти строку gamePath= в ModOrganizer.ini — файл повреждён или имеет неожиданный формат.")
        input("\nНажмите Enter для выхода...")
        return 1

    # "Диалог подтверждения смены игры" — MO2 показывает его именно
    # потому, что gamePath= реально изменился (та же причина, что и у
    # исходной ошибки) — выключаем заодно, прямой запрос пользователя.
    new_content, dialog_changed = set_plain_ini_key(
        new_content, "show_change_game_confirmation", "false", _SETTINGS_SECTION_RE,
    )

    if not path_changed and not dialog_changed:
        print(f"✅ gamePath= уже указывает на правильный путь ({_normalize(skyrim_dir)}), диалог подтверждения уже выключен — ничего менять не нужно.")
        input("\nНажмите Enter для выхода...")
        return 0

    backup_path = ini_path + ".bak"
    with open(backup_path, "w", encoding="utf-8") as f:
        f.write(content)
    with open(ini_path, "w", encoding="utf-8") as f:
        f.write(new_content)

    if path_changed:
        print(f"Старый путь в файле:      {old_path}")
        print(f"Новый (реальный) путь:    {_normalize(skyrim_dir)}")
    if dialog_changed:
        print("Диалог подтверждения смены игры — выключен (show_change_game_confirmation=false).")
    print(f"Резервная копия сохранена: {backup_path}")
    print("✅ ModOrganizer.ini исправлен.")
    input("\nНажмите Enter для выхода...")
    return 0


if __name__ == "__main__":
    sys.exit(main())
