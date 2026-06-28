# Проект: Кастомная интеграция Home Assistant для Ростелеком Key (rtkey)

## Описание
Интеграция для работы с камерами и домофонами Ростелеком Key (key.rt.ru).
Позволяет просматривать live-стримы, скриншоты, открывать домофоны, 
отслеживать события (открытие по ключу, через приложение и т.д.).

## API endpoints
1. **Камеры (все устройства)**: 
   GET https://keyapis.key.rt.ru/vc/api/v1/camera_video_data/list
   Возвращает: uid, токены (streamer, screenshot, user), статус, модель, IP, MAC
   
2. **Домофоны**: 
   GET https://household.key.rt.ru/api/v2/app/devices/intercom
   Возвращает: id, camera_id, serial_number, inter_codes (ключи с именами), capabilities
   
3. **Открытие домофона**: 
   POST https://household.key.rt.ru/api/v2/app/devices/{id}/open
   
4. **События**: 
   GET https://events.key.rt.ru/api/v2/events/list?device_ids=...&limit=1&sort_by=raised_at
   Возвращает: event_type, raised_at (UTC), rfid_id, request_id
   
5. **Скриншоты**:
   - Последний: https://key.rt.ru/screenshot/image/{quality}/{camera_id}/last.jpg?token=...
   - По времени: https://key.rt.ru/screenshot/image/{quality}/{camera_id}/{timestamp_sec}.jpg?token=...
   
6. **Стрим**: https://live-vdk4.camera.rt.ru/stream/{camera_id}/live.mp4?token=...
7. **Архив**: https://live-vdk4.camera.rt.ru/stream/{camera_id}/{timestamp_sec}.mp4?token=...

## Критически важные нюансы
- **Лаг времени камеры ~25 секунд**: если событие в T, то в видео/скриншоте оно отображается на T+25. 
  Для получения точного кадра нужно запрашивать timestamp = T - 25.
- **Время событий в UTC**: raised_at приходит в UTC, для отображения конвертируем в hass.config.time_zone
- **Токены привязаны к IP**: при смене IP сервера РТ токены становятся невалидными (403). 
  Нужно периодически обновлять через camera_video_data/list.
- **Токены живут долго** (exp ~через год), но привязка к IP — проблема.

## Архитектура
- `RTKeyCamerasApi` — основной класс, хранит все данные, делает запросы
- Один общий таймер опроса событий (asyncio.Task) для всех домофонов
- Callback-система для уведомления сущностей об обновлении событий
- Кэш картинок (4 сек) для предотвращения дублей запросов
- Маппинги: intercom_id → camera_id, rfid_id → имя ключа

## Сущности для каждого домофона
- `button` "Открыть" — открытие двери
- `sensor` "Последнее событие" — показывает кто открыл (с маппингом rfid_id → имя)
- `image` "Скриншот события" — JPEG на момент события
- `camera` "Архив события" — видео с момента события

## Сущности для каждой камеры
- `camera` — live-стрим
- `image` "Скриншот" — периодический скриншот (интервал настраивается, 0 = отключено)

## Настройки (Options Flow)
- camera_image_refresh_interval (слайдер 0-600, шаг 30)
- screenshot_quality (dropdown)
- log_level (dropdown)
- events_enabled, events_refresh_interval, event_time_lag (25 по умолчанию)
- pre_event_seconds, event_screenshot_enabled, event_archive_enabled и т.д.

## Что уже работает
- Live-стримы камер
- Периодические скриншоты
- Открытие домофонов через кнопку
- Отслеживание событий с маппингом ключей
- Скриншоты и архивное видео на момент события
- Правильная конвертация UTC → локальное время HA
- Настройки через Options Flow с слайдерами и dropdown

## Что можно улучшить (задачи на будущее)
- [ ] Автоматическое обновление токенов при получении 403 (сейчас только по таймеру)
- [ ] Обработка ошибок в таймерах (чтобы таймер не умирал при исключениях)
- [ ] Уведомления в HA при звонке в домофон (если появится такой тип события)
- [ ] История событий (не только последнее)

## Стиль кода
- Русские комментарии и логи
- Логирование через _LOGGER с эмодзи для важных событий (✅, 🔔, 🔄)
- Типизация (Python 3.11+)
- Использование async/await, async_add_executor_job для blocking calls