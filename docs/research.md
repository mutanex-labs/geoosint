# Практические решения по официальным источникам

Проверено 20 октября 2026. Выбран поддерживаемый Python MCP SDK 1.30.0, ветка v1,
а не произвольный собственный JSON-RPC. Streamable HTTP проверяется SDK-клиентом.
Четыре согласованные версии перечислены в contracts.md.

- [Спецификация MCP Authorization](https://modelcontextprotocol.io/specification/2025-11-25/basic/authorization)
  — разделение OAuth resource/authorization server, metadata и resource binding.
- [Официальный Python SDK](https://github.com/modelcontextprotocol/python-sdk)
  — транспорт, схемы, проверка PKCE, обработчики OAuth и protected resource metadata.
- [OpenAI: MCP server](https://developers.openai.com/plugins/build/mcp-server)
  — инструментальные контракты и проверка реальным клиентом перед подключением.
- [OpenAI: Authentication](https://developers.openai.com/plugins/build/auth)
  — discovery, S256, registration modes, scope/audience. В этой реализации выбран
  DCR confidential; CIMD не заявляется. mTLS клиента для публичной публикации требует
  отдельной конфигурации reverse proxy и не проверялся.
- [Codex MCP](https://learn.chatgpt.com/docs/extend/mcp?surface=cli)
  — remote URL, login и способы регистрации клиента. Локальный стенд не подтверждает
  совместимость конкретного интерфейса пользователя.
- [Nominatim policy](https://operations.osmfoundation.org/policies/nominatim/)
  — публичный сервис ограничен и не подходит как безлимитный поиск. Встроенная
  интеграция отключена полностью; общего публичного геокодера нет.
- [OSM tile policy](https://operations.osmfoundation.org/policies/tiles/)
  — массовое скачивание тайлов не реализуется. В P0 нет tile downloader.
- [Copernicus DEM registry](https://registry.opendata.aws/copernicus-dem/)
  — это DSM; GLO-30 Public имеет ограничения доступности участков, GLO-90 — другое
  разрешение. Реальная DEM-интеграция отложена; недостающие высоты не равны нулю.
- [Pillow Image](https://pillow.readthedocs.io/en/stable/reference/Image.html) и
  [форматы](https://pillow.readthedocs.io/en/stable/handbook/image-file-formats.html)
  — ленивое открытие, метаданные и защита от image bombs. Реально проверяются только
  заявленные форматы, без декодирования всех пикселей ради GPS.

Геометрия: GeographicLib WGS84. Сферическое приближение используется только как
явно консервативная оболочка bbox. Пересечения ограничены 100 км и не выдают
оценку измерительной погрешности. Солнце: Astral 3.2, геометрическое положение без
рефракции; тестовый ориентир Boulder 2003-10-17, допускается 0.3° в тестовом векторе.
Заявленный ориентир 1° не является подтверждённой глобальной метрологической гарантией.

Лицензии: собственный код MIT; OSM-ответы ODbL с атрибуцией; тестовые изображения
сгенерированы из примитивов, без чужих фотографий. Данные Copernicus не включаются
в репозиторий и при будущей загрузке требуют соблюдения лицензии поставщика.
