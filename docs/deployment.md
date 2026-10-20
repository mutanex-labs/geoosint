# Развёртывание, восстановление и клиенты

Требования: Linux Docker Engine/Compose, CPU, диск для оригиналов, желательно от
512 MiB доступной памяти плюс ресурсы ОС. Контейнер ограничен 512 MiB/1 CPU,
непривилегированным UID 10001, read_only, tmpfs 32 MiB, cap_drop=ALL,
no-new-privileges, pids_limit=64. Не нужен Docker socket или host networking.
Тестовые измерения не являются гарантией RAM для крупных дел и параллельных upload.

## Локальный стенд

`docker compose config`, `docker compose build`, `docker compose up -d --wait`.
В основном Compose сеть отсутствует; доступ через `docker compose exec`.
`/healthz` проверяет процесс; `/readyz` — SQLite и миграции. Необязательный OSM не
делает приложение неготовым. Локальные smoke scripts не требуют внешней сети.

## OAuth + существующий reverse proxy

Подготовьте HTTPS-домен отдельно. Выполните `geoosint password-hash` и сохраните
hash в защищённом окружении оператора. Точные redirect URI берутся из интерфейса
регистрации MCP-клиента; не используйте wildcard или предположительный callback.
Сначала выберите DCR confidential client с client_secret_post/basic.

Переменные для `compose.oauth.yaml`:

```text
BASE_URL=https://geo.example.org
GEO_OPERATOR_PASSWORD_HASH=<ARGON2ID_HASH_FROM_INTERACTIVE_COMMAND>
GEO_REDIRECT_URIS=["https://EXACT_CLIENT_CALLBACK"]
GEO_ORIGINS=[]
GEO_PUBLISHED_PORT=18787
GEO_OSM_ENABLED=false
```

Это placeholders, не готовые credentials. `.env` для Compose храните вне Git,
с правами 0600; Argon2 hash в Compose `.env` заключайте в одинарные кавычки, чтобы
`$` не интерполировался. Приложение получает настройки из окружения контейнера.

```bash
docker compose -f compose.yaml -f compose.oauth.yaml config --quiet
docker compose -f compose.yaml -f compose.oauth.yaml build
docker compose -f compose.yaml -f compose.oauth.yaml up -d --wait
```

Порт публикуется только на 127.0.0.1. Reverse proxy настраивает оператор по образцам
`docs/nginx.example.conf` или `docs/Caddyfile.example`; существующие конфигурации
не изменяются автоматически. BASE_URL — внешний HTTPS origin, без пути. Proxy должен
сохранять Host и поддерживать запросы `/mcp`, `/authorize`, `/token`, `/register`,
`/revoke`, `/consent`, `/.well-known/*`, `/cases/*/artifacts`.

Проверки на реальном домене: certificate chain, HTTPS discovery issuer/resource,
401 WWW-Authenticate, Origin, DCR exact redirect, S256, согласие оператора, scopes,
обновление/отзыв токенов и реальный вызов инструмента из клиента.
Локальные HTTP-тесты этого не заменяют. До ручной проверки статус регистрации —
NOT VERIFIED. Деплой на действующий VPS в рамках разработки не выполняется.

## ChatGPT

Проверено по документации OpenAI 20 октября 2026:

1. Откройте ChatGPT Plugins → `+` → `Add custom MCP server`.
2. Введите имя/описание, Connection → публичный HTTPS URL `https://YOUR_DOMAIN/mcp`.
3. Настройте OAuth. Скопируйте **точный** production redirect из управления соединением
   в GEO_REDIRECT_URIS и перезапустите только GeoOSINT. Не угадывайте callback и не
   заменяйте allowlist wildcard. Сервер использует DCR, не CIMD.
4. Ознакомьтесь с предупреждением доступа, затем `Create as a plugin`. Проверьте
   обнаруженные инструменты, установите плагин и выберите его через `@` в новом чате.
5. Проверьте consent, geo_capabilities, создание/чтение дела и отказ чужому владельцу.
   После изменения схем используйте Refresh соединения и новый разговор.

Доступность зависит от аккаунта и workspace policies; наличие Plus само по себе не
обещает доступность всех пунктов. Для этого сервера берите callback, показанный именно
интерфейсом: поддержка стабильного callback с issuer identification не заявлена.

Источники: [подключение](https://developers.openai.com/plugins/deploy/connect-chatgpt),
[OAuth и redirect](https://developers.openai.com/plugins/build/auth).

Статус **NOT VERIFIED**: публичные TLS/домен, OAuth flow в интерфейсе ChatGPT и
вызов из него не выполнялись. Локальные smoke это не заменяют. MCP-подключение
предоставляет инструменты, но не гарантирует автоматическую загрузку SKILL.md:
методику нужно отдельно подключить в поддерживающем Skills клиенте/пакете плагина.
Установка полного пакета в ChatGPT и Claude не проверена.

Фото из сообщения не считается доступным серверу. Пользователь загружает оригинал
через защищённый OAuth endpoint `/cases/{case_id}/artifacts` либо локальный CLI,
получает artifact_id, затем вызывает preflight. Исходный файл из мессенджера/чата
может уже не содержать оригинальные метаданные. Автоматическая передача вложения
из ChatGPT не подтверждена; не передавайте многомегабайтный base64 через MCP.

## Реальные источники при запуске на VPS

Для разрешённого исходящего OSM-доступа используйте OAuth Compose с bridge,
GEO_OSM_ENABLED=true и GEO_OSM_USER_AGENT с настоящим контактом оператора.
GEO_OVERPASS_ENDPOINT допускает только поддержанные адреса из настроек, по умолчанию
https://overpass-api.de/api/interpreter. При отказе источника дело продолжимо через
штатный веб-поиск клиента; отказ не означает отсутствия места.

Настоящие границы подготовьте `python scripts/fetch_boundaries.py`. Перед запуском
задайте GEO_BOUNDARIES_FILE и обеспечьте доступ к registry.json внутри контейнера:
например, операторский Compose override с read-only mount
`./data-sources/osm-boundaries/registry.json:/sources/registry.json:ro` и
GEO_BOUNDARIES_FILE=/sources/registry.json. Без этого административные названия
не получают точных границ автоматически. `compose.integration.yaml` предназначен
для отдельного тестового проекта, не для production.

Команды для оператора после подготовки окружения/HTTPS и mount границ:

```bash
docker compose -f compose.yaml -f compose.oauth.yaml -f YOUR_BOUNDARY_OVERRIDE.yaml config --quiet
docker compose -f compose.yaml -f compose.oauth.yaml -f YOUR_BOUNDARY_OVERRIDE.yaml build
docker compose -f compose.yaml -f compose.oauth.yaml -f YOUR_BOUNDARY_OVERRIDE.yaml up -d --wait
docker compose -f compose.yaml -f compose.oauth.yaml -f YOUR_BOUNDARY_OVERRIDE.yaml exec geoosint geoosint doctor
```

Это инструкция; развёртывание и изменение действующего reverse proxy не выполнялись.

## Codex и другие MCP-клиенты

Для локального HTTP: `codex mcp add geoosint --url http://127.0.0.1:8787/mcp`.
Для подготовленного OAuth-сервера:

```bash
codex mcp add geoosint --url https://YOUR_DOMAIN/mcp
codex mcp login geoosint --oauth-client-registration dcr
codex mcp list
```

Сначала внесите точный callback, показываемый клиентом, в allowlist оператора.
Поддержан только confidential DCR; если конкретная версия клиента требует public
client method none, это ограничение, а не повод ослабить сервер. Нужен совместимый
клиент/зарегистрированная confidential OAuth-конфигурация. Проверяйте `codex mcp --help`
своей версии. Личные настройки и токены Codex в репозитории не хранятся.
Claude и другие клиенты используют тот же Streamable HTTP и OAuth без смены core.

## Backup и обновление

`geoosint backup /new/path/cases-backup.sqlite` использует SQLite backup API;
обычное копирование активного WAL-файла не является backup. Команда сохраняет основную
базу дела; **оригиналы и oauth.sqlite нужно резервировать отдельно**.
Для полного согласованного snapshot остановите только сервис GeoOSINT, скопируйте
его именованный volume защищённым инструментом оператора и снова запустите сервис.
Публичный кеш можно не восстанавливать. Срок хранения приватных backup — например,
7 дней по решению оператора; механизм истечения выполняется вне приложения.

Восстановление: остановить GeoOSINT, восстановить основную БД и artifacts из одного
snapshot, сохранить UID 10001/права, восстановить OAuth-базу либо отозвать все сессии
и пройти авторизацию заново, запустить doctor и integrity, затем MCP smoke.
Нельзя обещать физическое удаление данных из старых неизменяемых backup.

Перед обновлением сделайте snapshot, соберите новый образ, поднимите только GeoOSINT
и проверьте readiness/doctor. Откат несовместимой миграции — восстановление прежнего
образа и snapshot, не автоматическое обратное выполнение SQL. Docker prune, удаление
чужих volumes и изменение действующего Nginx/Caddy не входят в эти команды.
