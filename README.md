# TESL

Лаунчер для сборки Skyrim Special Edition (MO2 + моды), распространяемой через
собственное depot-хранилище на WebDAV. Компаньон-репозиторий — [TESL-Manager](https://github.com/scp-oss/TESL-Manager),
который публикует релизы в это же depot и который лаунчер потребляет.

## Что умеет сейчас

- Устанавливает/обновляет сборку MO2 из depot на WebDAV (`launcher/core/depot_client.py`,
  `launcher/core/workers.py`) — сравнение по sha256, докачка только изменившихся файлов,
  resume по `Range`, чистка лишних файлов.
- Понимает ДВА протокола депо: обычный плоский (`files/<rel_path>`, скачивание файлов
  целиком) и chunk-based (`chunks/<xx>/<id>` + `manifest.db` компаньон рядом с
  JSON-манифестом версии — `core/chunk_installer.py`/`core/chunk_manifest_db.py`).
  Выбирается автоматически: если для версии есть `manifest.db`, качает из чанков;
  если нет — старый путь без изменений (см. CLAUDE.md "Chunk-протокол на стороне
  лаунчера").
- Выбор конкретной версии из `depot.json` и откат на неё (тот же механизм скачивания,
  просто другая версия — depot хранит все файлы, ничего физически не "откатывается").
- Проверка файлов (`VerifyWorker`) без переустановки — быстрая сверка хэшей + докачка
  проблемных.
- Определяет установленный Skyrim SE (реестр Steam / стандартные пути / поиск по дискам),
  проверяет версию `1.6.1170.0` и комплект AE DLC (`core/skyrim_checker.py`).
- Патчинг/откат патча найденной копии Skyrim (`core/patcher.py`) — скачивает патч-набор
  с отдельного WebDAV-пути и накатывает поверх.
- Настраивает `ModOrganizer.ini` (подставляет реальный путь к игре) и создаёт ярлык
  MO2 на рабочем столе с нужным аргументом запуска (`moshortcut://:SKSE`).
- Диалог первого запуска — определяет, патчить найденную копию, качать готовую версию
  с сервера, или отправить в Steam (`ui/first_run_dialog.py`).
- Отправка крэш-логов/сейвов на WebDAV (`core/crash_logger.py`).

## Что в разработке (см. CLAUDE.md)

- Самообновление самого `TESL.exe`.
- Поддержка нескольких сборок (не только TESVAE) с собственным install-скриптом на
  каждую.
- Веб-панель на сервере для управления релизами/сборками/лаунчером (живёт в
  TESL-Manager либо как отдельный сервис — см. обсуждение в CLAUDE.md).

## Запуск из исходников

```
pip install -r requirements.txt
cd launcher
python main.py
```

На Windows для повседневного обновления+запуска — `update_and_run.bat` в
корне репозитория: делает `git pull`, спрашивает пароль WebDAV один раз
(сохраняет через `setx`, дальше не спрашивает) и запускает `python main.py`.

Нужен пароль WebDAV-аккаунта — либо `set TESL_DAV_PASSWORD=...` перед запуском,
либо скопируй `launcher/secrets_local.example.py` → `launcher/secrets_local.py`
и впиши пароль туда (файл в `.gitignore`, в репозиторий не попадёт).

## Сборка .exe

Локально:
```
cd launcher
"build pyw.bat"
```
(PyInstaller, `--onefile --windowed`, см. сам батник). Пароль WebDAV должен быть
виден на момент сборки (ENV или `secrets_local.py`) — он встраивается в бинарник.

Через GitHub Actions (`.github/workflows/build-release.yml`, раннер `windows-latest`) —
собирает `TESL.exe` и публикует его в [Releases](../../releases):
- автоматически при пуше тега `v*` (например `git tag v19.0.8 && git push origin v19.0.8`);
- вручную — Actions → "Build & release TESL.exe" → Run workflow, указав тег.

Требует репозиторный секрет `TESL_DAV_PASSWORD` (Settings → Secrets and variables →
Actions) — без него соберётся рабочий .exe, но без пароля WebDAV он не сможет ничего
скачать (см. предупреждение в логе сборки).
