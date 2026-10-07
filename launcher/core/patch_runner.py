# ==================== launcher/core/patch_runner.py ====================
"""
Выполнение `.bat`-патчей из раздела "📄 Документы и патчи"
(`patch/`, см. TESL-Manager::`depot_sync_manager/documents_tab.py`) —
прямой запрос пользователя 2026-10-07, сразу после того, как `documents/`
(чистые данные — ini-шаблоны, без исполнения) были подключены со
стороны лаунчера (см. `core/ini_profile.py` — "фиксы ты писал под эту
сборку... мб соберём их в файл... а ланчер научим принимать патч от
панели"). Это ОТДЕЛЬНАЯ, более рискованная категория — выполнение
произвольного кода, скачанного с сервера, на машине игрока — подключена
только после явного да/нет вопроса пользователю (не решена молча), в
отличие от `documents/`.

**Намеренно без Qt-зависимости** — тот же принцип, что `core/
chunk_installer.py` (проверяемость без GUI), вызывается из
`core/workers.py::PostInstallWorker.run()` тонкой обёрткой.

**Контракт очереди применения** — буквально то, что TESL-Manager уже
задокументировала в своём докстринге documents_tab.py для лаунчера:
пропустить записи с `"enabled": false`; оставшиеся — сортировать по
`"order"` по возрастанию; записи БЕЗ поля `"order"` вообще (легаси,
до соответствующей правки, или рассинхрон файл-есть/в-манифесте-нет) —
в КОНЕЦ очереди, между собой по имени файла, никогда не между двумя
записями с явным `"order"`.

**Идемпотентность — ключевое решение, не задокументированное заранее
ни в одном из трёх репозиториев, принято здесь**: `PostInstallWorker`
вызывается на КАЖДОЙ установке/обновлении — без защиты от повтора
DLSS5-скрипт (или любой другой патч) выполнялся бы заново на каждое
обновление сборки, даже если ничего не поменялось, и нет гарантии, что
произвольный чужой скрипт идемпотентен сам по себе. Журнал уже
применённых патчей — `<local_dir>/cache/applied_patches.json`
(`{rel_path: sha256}`), та же папка `cache/`, что уже хранит
`resume_state.jsonl` у `chunk_installer.py` (переживает между
прогонами намеренно, не чистится). Патч с ТЕМ ЖЕ путём, но ДРУГИМ
sha256 (оператор залил новую версию под тем же именем) — считается
НОВЫМ, выполняется заново; патч с успешным запуском — больше никогда
(для этого install_dir), пока его sha256 не изменится.

**`patchs/` (вспомогательные файлы патчей, например DLL для DLSS5) — НЕ
скачиваются сюда заранее и не перечисляются.** У лаунчера нет
публичного (без Bearer-токена) способа ПЕРЕЧИСЛИТЬ содержимое
`patchs/` — только скачать конкретный известный путь (см. `panel_
client.py`'s докстринг: "GET /api/depot/<build_id>/<любой другой путь>
как есть"). Вместо этого патчу передаётся в окружении процесса
`TESL_PANEL_BASE_URL`/`TESL_BUILD_ID`/`TESL_LOCAL_DIR` — сам скрипт,
зная имя нужного файла (`nvngx_dlss.dll` и т.п. — то, что он сам
публикует вместе с собой), может скачать его тем же публичным
эндпоинтом (`%TESL_PANEL_BASE_URL%/api/depot/%TESL_BUILD_ID%/patchs/
<имя>`) самостоятельно, например через `powershell -Command
Invoke-WebRequest` или `curl` прямо внутри `.bat`. Если это окажется
неудобным на практике — отдельный заход должен явно добавить список
`patchs/` в `extras_manifest.json` со стороны TESL-Manager, не
придумываться здесь без подтверждения реального формата.

**Выполнение** — `cmd /c <скачанный .bat>`, `cwd=local_dir` (корень
установки — тот же уровень, что `MO2p/`/`Skyrim/`/`patch/`/`patchs/` на
сервере; "относительным путём внутри архива сборки" из докстринга
TESL-Manager буквально означает относительно этого корня), таймаут
`PATCH_TIMEOUT_SECONDS` на патч. Вывод (stdout+stderr) захватывается и
логируется целиком — в отличие от debug-трассы `chunk_installer.py`,
это не тысячи строк на установку, а единичные короткие скрипты,
троттлинг не нужен. Ошибка одного патча (ненулевой код выхода, таймаут,
сам `.bat` не скачался) — логируется, патч НЕ отмечается применённым
(повторная попытка на следующем прогоне), остальная очередь
ПРОДОЛЖАЕТСЯ — тот же best-effort принцип, что у всего post-install:
одна сломанная запись не должна блокировать остальные независимые.

**Только Windows** — `.bat`/`cmd /c` не имеют смысла нигде ещё; на
любой другой платформе (линукс-песочница разработки в т.ч.) —
немедленный no-op, без исключения наружу.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Dict, List

PATCH_TIMEOUT_SECONDS = 300


def ordered_enabled_patches(manifest: dict) -> List[dict]:
    """См. докстринг модуля "Контракт очереди применения"."""
    patches = [p for p in manifest.get("patch", []) if p.get("enabled", True) is not False]
    with_order = sorted((p for p in patches if "order" in p), key=lambda p: p["order"])
    without_order = sorted((p for p in patches if "order" not in p), key=lambda p: p.get("path", ""))
    return with_order + without_order


def _applied_state_path(local_dir: str) -> Path:
    return Path(local_dir) / "cache" / "applied_patches.json"


def _load_applied(local_dir: str) -> Dict[str, str]:
    try:
        return json.loads(_applied_state_path(local_dir).read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_applied(local_dir: str, applied: Dict[str, str]) -> None:
    try:
        p = _applied_state_path(local_dir)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(applied, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass


def run_patches(local_dir: str, build_id: str, log=print) -> None:
    """Главная точка входа — см. докстринг модуля. Best-effort: любая
    ошибка логируется, никогда не бросает исключение наружу (вызывается
    из `PostInstallWorker.run()` — не должна ронять остальной
    post-install)."""
    if sys.platform != "win32":
        return
    if not local_dir or not build_id:
        return
    client = None
    try:
        from core.panel_client import PanelDepotClient
        client = PanelDepotClient(build_id)
        manifest = client.fetch_extras_manifest()
        queue = ordered_enabled_patches(manifest)
        if not queue:
            return

        applied = _load_applied(local_dir)
        pending = [p for p in queue if applied.get(p.get("path", "")) != p.get("sha256")]
        if not pending:
            return

        log(f"🩹 Патчи сборки: {len(pending)} новых/изменённых из {len(queue)} в очереди")
        patch_dir = Path(local_dir) / "patch"
        patch_dir.mkdir(parents=True, exist_ok=True)

        env = os.environ.copy()
        env["TESL_PANEL_BASE_URL"] = client.base
        env["TESL_BUILD_ID"] = build_id
        env["TESL_LOCAL_DIR"] = str(local_dir)

        for entry in pending:
            rel_path = entry.get("path", "")
            sha = entry.get("sha256", "")
            if not rel_path:
                continue
            fname = rel_path.rsplit("/", 1)[-1]

            data = client.get_bytes(rel_path)
            if data is None:
                log(f"⚠️ Патч {fname}: не удалось скачать, пропуск")
                continue

            local_patch_path = patch_dir / fname
            try:
                local_patch_path.write_bytes(data)
            except Exception as e:
                log(f"⚠️ Патч {fname}: не удалось сохранить на диск ({e}), пропуск")
                continue

            log(f"🩹 Патч {fname}: запуск...")
            try:
                result = subprocess.run(
                    ["cmd", "/c", str(local_patch_path)],
                    cwd=local_dir, env=env, timeout=PATCH_TIMEOUT_SECONDS,
                    capture_output=True,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
            except subprocess.TimeoutExpired:
                log(f"⚠️ Патч {fname}: превышен таймаут ({PATCH_TIMEOUT_SECONDS}с), пропуск")
                continue
            except Exception as e:
                log(f"⚠️ Патч {fname}: ошибка запуска ({e}), пропуск")
                continue

            out = (result.stdout or b"").decode("utf-8", errors="replace").strip()
            err = (result.stderr or b"").decode("utf-8", errors="replace").strip()
            if out:
                log(f"  {fname} stdout:\n{out}")
            if err:
                log(f"  {fname} stderr:\n{err}")

            if result.returncode == 0:
                applied[rel_path] = sha
                _save_applied(local_dir, applied)
                log(f"✅ Патч {fname} применён")
            else:
                log(f"❌ Патч {fname}: код выхода {result.returncode} — НЕ отмечен применённым")
    except Exception as e:
        log(f"❌ Ошибка применения патчей: {e}")
    finally:
        if client is not None:
            try:
                client.close()
            except Exception:
                pass
