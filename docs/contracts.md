# Контракты MCP

Endpoint `/mcp`, официальный `mcp==1.30.0`, stateless Streamable HTTP с JSON-ответами.
HTTP-тесты согласуют 2024-11-05, 2025-03-26, 2025-06-18 и 2025-11-25.
Нет собственного JSON-RPC транспорта или универсального execute/fetch инструмента.

Общий ответ: `status`, `data`, `warnings`, `errors`, `source_refs`, `request_id`.
Для дела добавлены `budget_used`, `budget_remaining`, `scope_checked`,
`expansion_reasons`. Не относящиеся к вызову поля могут быть null.
Ошибки прикладного сервиса возвращаются структурированно; неверные параметры
SDK могут вернуть стандартный MCP `isError`. Точные схемы доступны в `tools/list`.
Лишние аргументы запрещены как схемой, так и серверной валидацией.

| Инструмент | Назначение |
|---|---|
| geo_capabilities | Реальные возможности и ограничения |
| geo_case_create | title, idempotency_key, optional privacy_sensitive/search_scope |
| geo_case_get | Сводка и текущая revision |
| geo_observation_add | Неподтверждённое наблюдение и dependency_group |
| geo_candidates_add | Пакет 1–50 гипотез с областью и optional point |
| geo_candidates_rank | Рейтинг -100…100, история, без удаления |
| geo_geometry | inverse/direct/intersection/bounds/angle |
| geo_sun_position | Координаты и captured_at с offset либо null |
| geo_places_search | Ограниченный встроенный справочник, не публичный геокодер |
| geo_osm_features | bbox, разрешённая категория, offset/limit |
| geo_evidence_add | Реальный operation_id, candidate_id, зависимые наблюдения |
| geo_spatial_check | Проверка расстояния/азимута по наблюдению между сохранёнными OSM-объектами |
| geo_case_next | План с учётом scope; может журналировать расширение PREFERRED |
| geo_case_scope_set | Новый search_scope с expected_revision |
| geo_case_scope_expand | Основание, ссылки на проверки, контроль STRICT/бюджета |
| geo_metadata_inspect | case_id/artifact_id, EXIF/XMP и доказательство происхождения |
| geo_case_preflight | Тот же безопасный metadata pipeline и следующий шаг |
| geo_artifact_delete | Удаление оригинала и приватных производных |
| geo_case_transition | Проверяемый переход состояния |
| geo_case_finalize | UNRESOLVED либо assessment + candidate_id + level; без ложного VERIFIED |
| geo_case_export | JSON geoosint-report/1 или Markdown |
| geo_case_delete | Удаление дела владельцем с ревизией |

Изменения требуют `expected_revision` и `idempotency_key`, кроме удаления всего дела,
которое требует ревизию. Повтор должен содержать исходную ревизию и идентичные параметры.
Смена параметров под тем же ключом — REVISION_CONFLICT. Ключи изолированы владельцем.
`geo_case_next` может увеличить revision при автоматическом расширении; используйте
возвращённую `case_revision` или перечитайте дело.

Расстояния — метры, углы — градусы; координаты как именованные latitude/longitude.
GeoJSON использует longitude/latitude. Время — ISO 8601; локальное EXIF-время без
смещения явно помечается OFFSET_MISSING и не выдаётся за UTC.

Коды ошибок включают INVALID_INPUT, UNAUTHORIZED, FORBIDDEN, NOT_FOUND,
REVISION_CONFLICT, RATE_LIMITED, UPSTREAM_TIMEOUT, UPSTREAM_UNAVAILABLE,
COVERAGE_MISSING, EVIDENCE_INSUFFICIENT, INTERNAL_ERROR; расширения P0:
SCOPE_AMBIGUOUS, SCOPE_VIOLATION, BUDGET_EXHAUSTED, BUDGET_RESERVED,
INVALID_FILE, UNSUPPORTED_OR_INVALID_FILE, METADATA_TIMEOUT, ARTIFACT_CHANGED.

Ресурс `geoosint://guide` содержит компактную методику. Основной навык поставляется
в `skills/geoosint`, экспериментальное распространение Skills через MCP не требуется.

## Загрузка оригинала

Только OAuth-режим: `POST /cases/{case_id}/artifacts` с бинарным телом, Bearer-токеном,
`Content-Type: application/octet-stream`, `If-Match: REVISION`, `Idempotency-Key: KEY`.
Ответ содержит artifact_id, новую revision и preflight с извлечёнными координатами.
Имя/путь файла клиент не передаёт.
Затем geo_case_preflight или geo_metadata_inspect с этим artifact_id и новой ревизией.
Нет universal URL download и автоматического чтения файлов хоста.

## Исследовательские расширения

Всего 22 инструмента. geo_osm_features дополнен optional name_query и категориями
intersections/tunnels/landmarks/names. В ответе геометрия, OSM ID, теги, даты, покрытие,
лицензия. Ways — линии; relation — линии членов, без выдуманного восстановления полигонов.
Общие узлы ways могут быть разбиением дороги, а не настоящим перекрёстком.

CandidateInput принимает optional source_operation_id/source_feature_id. geo_spatial_check
использует эти OSM-точки и spatial_constraint наблюдения. Ожидание сообщает клиент;
сервер проверяет расчёт, а не происхождение визуального ожидания. Центр bbox не равен
ближайшей точке объекта или положению камеры.

ObservationInput дополнен MODEL_VISUAL_COMPARISON, compared_image_refs и research:
класс результата, инструмент, вызовы/запросы клиента, URL, время, доступность, зависимости.
Сервер не получает браузер и не удостоверяет заявленный клиентом просмотр.

Отчёт содержит top_candidates (до 3, без заполнителей), качественную уверенность,
основания, противоречия, пробелы, request_traces и отдельный client_research.
FACT_SUPPORTED — условная согласованность географических отношений, scene_verified=false,
дело REVIEW_REQUIRED. Проценты не вычисляются. METADATA_LOCATION остаётся отдельным.
diagnostics OSM связывает provider_attempts по request_id: стадия, длительность, HTTP-код,
ошибка, размер ответа, cache_hit. Трассы без координат/запросов/оригиналов, максимум 10 000 на дело.
# Deep Investigation: совместимые дополнения

GeoPrior: observation_type дополнен MODEL_GEO_PRIOR. Он сохраняется UNVERIFIED,
не принимает spatial_constraint и не повышает статус координат. В оценке кандидата
model_geo_prior_ids/geoprior_reasons выводят происхождение предположения.
Фактические результаты геоданных остаются operations.kind=osm (GEODATA_RESULT),
вычисления — geometry/spatial_check (GEOMETRY_CHECK). VERIFIED_EVIDENCE не является
принимаемым от клиента флагом: критерии Evidence проверяет сервер. Старые имена
GEO_SOURCE и поля ResearchRecord сохранены для совместимости.

geo_observation_add принимает необязательные scene_graph, research_actions и
action_outcomes. geo_candidates_add принимает необязательные observation_ids.
Полные ограничения вложенных типов публикуются в MCP JSON Schema; старые параметры
сохранены. geo_case_next добавляет research_plan и возвращает RESEARCH при доступной
предложенной проверке. Без предложений сохраняется прежний выбор действия.
geo_case_export добавляет evidence_graph и hypothesis_ledger. Эти структуры не являются
подтверждением сцены. Подробности: [архитектура](investigation-architecture.md).
