# ==================== launcher/secrets_local.py (пример) ====================
# Скопируй этот файл в launcher/secrets_local.py (он в .gitignore, в репозиторий
# не попадёт) и впиши реальный пароль WebDAV-аккаунта для локальной сборки.
#
# Альтернатива без файла — переменная окружения перед сборкой:
#   set TESL_DAV_PASSWORD=реальный_пароль   (cmd)
#   $env:TESL_DAV_PASSWORD="реальный_пароль" (PowerShell)
#
# ENV имеет приоритет над этим файлом (см. config.py).

DAV_PASSWORD = "REPLACE_ME"
