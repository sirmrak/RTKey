import asyncio
import functools
import logging
import time
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import requests
import voluptuous as vol

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers import selector

DOMAIN = "rtkey"
PLATFORMS = [Platform.CAMERA, Platform.IMAGE, Platform.BUTTON, Platform.SENSOR]

_LOGGER = logging.getLogger(__name__)

# === Константы настроек ===
CONF_NAME = "name"
CONF_TOKEN = "token"
CONF_CAMERA_IMAGE_REFRESH_INTERVAL = "camera_image_refresh_interval"
CONF_SCREENSHOT_QUALITY = "screenshot_quality"
CONF_LOG_LEVEL = "log_level"

# Настройки событий
CONF_EVENTS_ENABLED = "events_enabled"
CONF_EVENTS_REFRESH_INTERVAL = "events_refresh_interval"
CONF_EVENT_TIME_LAG = "event_time_lag"
CONF_PRE_EVENT_SECONDS = "pre_event_seconds"
CONF_EVENT_SCREENSHOT_ENABLED = "event_screenshot_enabled"
CONF_EVENT_SCREENSHOT_QUALITY = "event_screenshot_quality"
CONF_EVENT_ARCHIVE_ENABLED = "event_archive_enabled"
CONF_ARCHIVE_DURATION = "archive_duration"
CONF_EVENT_SENSOR_ENABLED = "event_sensor_enabled"

TOKEN_REFRESH_BUFFER = 300

DATA_SCHEMA = {
    vol.Required(CONF_NAME): str,
    vol.Required(CONF_TOKEN): str,
}

OPTIONS_SCHEMA = {
    vol.Optional(CONF_NAME): str,
    vol.Optional(CONF_TOKEN): str,
    
    vol.Optional(CONF_CAMERA_IMAGE_REFRESH_INTERVAL, default=60): selector.NumberSelector(
        selector.NumberSelectorConfig(
            min=0, max=600, step=30,
            mode=selector.NumberSelectorMode.SLIDER,
            unit_of_measurement="сек",
        )
    ),
    
    vol.Optional(CONF_SCREENSHOT_QUALITY, default="medium"): selector.SelectSelector(
        selector.SelectSelectorConfig(
            options=[
                selector.SelectOptionDict(value="small", label="Маленькое (быстрее)"),
                selector.SelectOptionDict(value="medium", label="Среднее"),
                selector.SelectOptionDict(value="large", label="Большое"),
                selector.SelectOptionDict(value="precise", label="Точное (медленнее)"),
            ],
            mode=selector.SelectSelectorMode.DROPDOWN,
        )
    ),
    
    vol.Optional(CONF_LOG_LEVEL, default="INFO"): selector.SelectSelector(
        selector.SelectSelectorConfig(
            options=[
                selector.SelectOptionDict(value="DEBUG", label="DEBUG (подробно)"),
                selector.SelectOptionDict(value="INFO", label="INFO (обычный)"),
                selector.SelectOptionDict(value="WARNING", label="WARNING (только предупреждения)"),
                selector.SelectOptionDict(value="ERROR", label="ERROR (только ошибки)"),
            ],
            mode=selector.SelectSelectorMode.DROPDOWN,
        )
    ),
    
    vol.Optional(CONF_EVENTS_ENABLED, default=True): bool,
    
    vol.Optional(CONF_EVENTS_REFRESH_INTERVAL, default=15): selector.NumberSelector(
        selector.NumberSelectorConfig(
            min=5, max=60, step=5,
            mode=selector.NumberSelectorMode.SLIDER,
            unit_of_measurement="сек",
        )
    ),
    
    vol.Optional(CONF_EVENT_TIME_LAG, default=25): selector.NumberSelector(
        selector.NumberSelectorConfig(
            min=0, max=60, step=5,
            mode=selector.NumberSelectorMode.SLIDER,
            unit_of_measurement="сек",
        )
    ),
    
    vol.Optional(CONF_PRE_EVENT_SECONDS, default=10): selector.NumberSelector(
        selector.NumberSelectorConfig(
            min=0, max=30, step=5,
            mode=selector.NumberSelectorMode.SLIDER,
            unit_of_measurement="сек",
        )
    ),
    
    vol.Optional(CONF_EVENT_SCREENSHOT_ENABLED, default=True): bool,
    
    vol.Optional(CONF_EVENT_SCREENSHOT_QUALITY, default="large"): selector.SelectSelector(
        selector.SelectSelectorConfig(
            options=[
                selector.SelectOptionDict(value="small", label="Маленькое"),
                selector.SelectOptionDict(value="medium", label="Среднее"),
                selector.SelectOptionDict(value="large", label="Большое"),
                selector.SelectOptionDict(value="precise", label="Точное"),
            ],
            mode=selector.SelectSelectorMode.DROPDOWN,
        )
    ),
    
    vol.Optional(CONF_EVENT_ARCHIVE_ENABLED, default=True): bool,
    
    vol.Optional(CONF_ARCHIVE_DURATION, default=30): selector.NumberSelector(
        selector.NumberSelectorConfig(
            min=10, max=120, step=10,
            mode=selector.NumberSelectorMode.SLIDER,
            unit_of_measurement="сек",
        )
    ),
    
    vol.Optional(CONF_EVENT_SENSOR_ENABLED, default=True): bool,
}


class RTKeyCamerasApi:
    def __init__(self, hass: HomeAssistant, entry: ConfigEntry):
        self.hass = hass
        self.entry = entry
        self.token = entry.options.get(CONF_TOKEN) or entry.data.get(CONF_TOKEN, "")
        self.screenshot_quality = entry.options.get(CONF_SCREENSHOT_QUALITY, "medium")
        
        # Настройки событий
        self.events_enabled = entry.options.get(CONF_EVENTS_ENABLED, True)
        self.events_refresh_interval = entry.options.get(CONF_EVENTS_REFRESH_INTERVAL, 15)
        self.event_time_lag = entry.options.get(CONF_EVENT_TIME_LAG, 25)
        self.pre_event_seconds = entry.options.get(CONF_PRE_EVENT_SECONDS, 10)
        self.event_screenshot_enabled = entry.options.get(CONF_EVENT_SCREENSHOT_ENABLED, True)
        self.event_screenshot_quality = entry.options.get(CONF_EVENT_SCREENSHOT_QUALITY, "large")
        self.event_archive_enabled = entry.options.get(CONF_EVENT_ARCHIVE_ENABLED, True)
        self.archive_duration = entry.options.get(CONF_ARCHIVE_DURATION, 30)
        self.event_sensor_enabled = entry.options.get(CONF_EVENT_SENSOR_ENABLED, True)

        log_level = entry.options.get(CONF_LOG_LEVEL, "INFO")
        _LOGGER.setLevel(getattr(logging, log_level, logging.INFO))
        _LOGGER.info(f"📊 Уровень логирования: {log_level}")

        self.user_token = None
        self.lock = asyncio.Lock()

        # Информация о камерах
        self.camera_tokens = {}
        self._cameras_cache = None
        self._cameras_cache_ts = 0

        # Кэш картинок
        self._image_cache = {}
        self._image_cache_lock = asyncio.Lock()
        
        # ✅ НОВОЕ: Отслеживание недоступных камер (404)
        self._unavailable_cameras: set[str] = set()
        
        # ✅ НОВОЕ: Флаг протухших токенов (403)
        self._tokens_invalid = False
        self._tokens_refresh_lock = asyncio.Lock()
        
        # Данные событий
        self.last_events = {}
        self._last_request_ids = {}
        self.rfid_names = {}
        self.intercom_camera_map = {}
        self.intercom_ids = []
        self._events_polling_task: asyncio.Task | None = None
        self._event_listeners = []
        
        self._local_tz = ZoneInfo(hass.config.time_zone)
        _LOGGER.info(f"🕐 Временная зона HA: {hass.config.time_zone}")

    def build_device_name(self, name: str) -> str:
        return name.strip() if name else "RT Key Device"
        
    def get_camera_name(self, camera_id: str) -> str:
        """Получаем название камеры по её ID."""
        token_info = self.camera_tokens.get(camera_id)
        if token_info:
            return token_info.get("title", "Неизвестная камера")
        return "Неизвестная камера"
        
    def is_camera_available(self, camera_id: str) -> bool:
        """Проверяем, доступна ли камера (не в списке недоступных)."""
        return camera_id not in self._unavailable_cameras

    def register_event_listener(self, callback):
        if callback not in self._event_listeners:
            self._event_listeners.append(callback)
    
    def unregister_event_listener(self, callback):
        if callback in self._event_listeners:
            self._event_listeners.remove(callback)
    
    def _notify_event_listeners(self, intercom_id: str):
        for callback in self._event_listeners:
            try:
                callback(intercom_id)
            except Exception as e:
                _LOGGER.error(f"Ошибка в event listener: {e}")

    async def _fetch_cameras(self):
        """Получаем информацию о камерах (токены для стрима и скриншотов)."""
        if self._cameras_cache and (time.time() - self._cameras_cache_ts < 60):
            return self._cameras_cache

        try:
            _LOGGER.debug("Запрос camera_video_data/list...")
            r = await self.hass.async_add_executor_job(
                functools.partial(
                    requests.get,
                    "https://keyapis.key.rt.ru/vc/api/v1/camera_video_data/list?paging.limit=100&paging.offset=0",
                    headers={"Authorization": f"Bearer {self.token}"},
                    timeout=20,
                )
            )
            if r.status_code == 200:
                data = r.json()
                devices = data.get("data", [])

                if devices:
                    self.user_token = devices[0].get("userToken")

                for device in devices:
                    uid = device.get("uid")
                    if uid:
                        self.camera_tokens[uid] = {
                            "title": device.get("title", "Устройство"),
                            "category_type": device.get("category", {}).get("type", "unknown"),
                            "category_title": device.get("category", {}).get("title", ""),
                            "model": device.get("model", ""),
                            "vendor": device.get("vendor", ""),
                            "serial_number": device.get("serialNumber", ""),
                            "mac": device.get("mac", ""),
                            "ip": device.get("ip", ""),
                            "status_type": device.get("status", {}).get("type", "unknown"),
                            "status_title": device.get("status", {}).get("title", ""),
                            "streamer_token": device.get("streamerToken"),
                            "screenshot_token": device.get("screenshotToken"),
                            "user_token": device.get("userToken"),
                        }

                self._cameras_cache = devices
                self._cameras_cache_ts = time.time()
                _LOGGER.info(f"✅ Загружено {len(self.camera_tokens)} устройств")
                return devices
            else:
                _LOGGER.error(f"camera_video_data/list вернул {r.status_code}")
        except Exception as e:
            _LOGGER.error(f"Ошибка получения камер: {e}")
        return []

    async def refresh_tokens(self):
        """🔄 Принудительно обновляем все токены (сбрасываем кэш)."""
        async with self._tokens_refresh_lock:
            _LOGGER.info("🔄 Принудительное обновление токенов...")
            self._cameras_cache = None
            self._cameras_cache_ts = 0
            await self._fetch_cameras()
            self._tokens_invalid = False
            _LOGGER.info(f"✅ Токены обновлены для {len(self.camera_tokens)} устройств")

    async def _fetch_intercoms(self):
        """Получаем информацию о домофонах из отдельного API."""
        try:
            _LOGGER.debug("Запрос домофонов из household.key.rt.ru...")
            r = await self.hass.async_add_executor_job(
                functools.partial(
                    requests.get,
                    "https://household.key.rt.ru/api/v2/app/devices/intercom",
                    headers={"Authorization": f"Bearer {self.token}"},
                    timeout=15,
                )
            )
            if r.status_code == 200:
                data = r.json()
                
                self._build_intercom_mappings(data)
                
                devices = data.get("data", {}).get("devices", [])
                self.intercom_ids = [str(d.get("id")) for d in devices if d.get("id")]
                
                _LOGGER.info(f"✅ Загружено {len(self.intercom_ids)} домофонов: {self.intercom_ids}")
                return data
            else:
                _LOGGER.error(f"intercom вернул {r.status_code}")
        except Exception as e:
            _LOGGER.error(f"Ошибка получения домофонов: {e}")
        return {"data": {"devices": []}}
    
    def _build_intercom_mappings(self, data: dict):
        devices = data.get("data", {}).get("devices", [])
        for device in devices:
            intercom_id = device.get("id")
            camera_id = device.get("camera_id")
            if intercom_id and camera_id:
                self.intercom_camera_map[str(intercom_id)] = camera_id
            
            for code in device.get("inter_codes", []):
                rfid_id = code.get("id")
                name = code.get("name_by_user", "")
                if rfid_id and name:
                    self.rfid_names[rfid_id] = name
        
        _LOGGER.debug(
            f"Построены маппинги: {len(self.intercom_camera_map)} камер домофонов, "
            f"{len(self.rfid_names)} ключей"
        )

    async def get_cameras_info(self):
        await self._fetch_cameras()

        items = []
        for uid, info in self.camera_tokens.items():
            items.append({
                "id": uid,
                "title": info["title"],
                "category_type": info["category_type"],
                "status_title": info["status_title"],
                "model": info["model"],
                "vendor": info["vendor"],
                "serial_number": info["serial_number"],
                "mac": info["mac"],
                "ip": info["ip"],
                # ✅ Добавляем флаг доступности
                "available": self.is_camera_available(uid),
            })

        return {"data": {"items": items}}

    async def get_intercoms_info(self):
        if not self.intercom_ids:
            return await self._fetch_intercoms()
        return await self._fetch_intercoms()


    async def get_camera_image(self, camera_id: str, retry: bool = False) -> bytes | None:
        """Получаем скриншот камеры с обработкой ошибок 403 и 404."""
        async with self._image_cache_lock:
            if camera_id in self._image_cache:
                img, ts = self._image_cache[camera_id]
                if time.time() - ts < 4:
                    return img

        token_info = self.camera_tokens.get(camera_id)
        screenshot_token = token_info.get("screenshot_token") if token_info else None
        user_token = (token_info.get("user_token") or self.user_token) if token_info else None
        
        # ✅ Получаем название камеры для логов
        camera_name = self.get_camera_name(camera_id)

        try:
            url = f"https://key.rt.ru/screenshot/image/{self.screenshot_quality}/{camera_id}/last.jpg"
            if screenshot_token:
                url += f"?token={screenshot_token}&_={int(time.time() * 1000)}"

            _LOGGER.debug(f"Запрос скриншота для {camera_name} ({camera_id}): {url}")
            r = await self.hass.async_add_executor_job(
                functools.partial(
                    requests.get,
                    url,
                    headers={
                        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                        "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
                        "Referer": "https://key.rt.ru/",
                        "Origin": "https://key.rt.ru",
                        "X-UTOKEN": user_token or self.token,
                    },
                    timeout=15,
                )
            )

            # ✅ Обработка 403 — токен протух
            if r.status_code == 403 and not retry:
                _LOGGER.warning(
                    f"⚠️ Токен для {camera_name} ({camera_id}) протух (403), запускаем обновление..."
                )
                self._tokens_invalid = True
                await self.refresh_tokens()
                return await self.get_camera_image(camera_id, retry=True)
            
            if r.status_code == 403 and retry:
                _LOGGER.error(
                    f"❌ Повторный 403 для {camera_name} ({camera_id}) — проблема с авторизацией"
                )
                return None

            # ✅ Обработка 404 — камера недоступна
            if r.status_code == 404:
                if camera_id not in self._unavailable_cameras:
                    _LOGGER.warning(
                        f"📷 Камера \"{camera_name}\" ({camera_id}) недоступна (404), помечаем как offline"
                    )
                self._unavailable_cameras.add(camera_id)
                return None

            if r.status_code == 200 and len(r.content) > 2000:
                image_data = r.content
                async with self._image_cache_lock:
                    self._image_cache[camera_id] = (image_data, time.time())
                
                # ✅ Если камера была недоступна — помечаем как восстановленную
                if camera_id in self._unavailable_cameras:
                    self._unavailable_cameras.discard(camera_id)
                    _LOGGER.info(f"✅ Камера \"{camera_name}\" ({camera_id}) снова доступна")
                
                _LOGGER.info(f"✅ Скриншот получен {camera_name} ({camera_id}) — {len(image_data)} байт")
                return image_data
            else:
                _LOGGER.warning(
                    f"Скриншот для {camera_name} ({camera_id}): статус {r.status_code}, размер {len(r.content)}"
                )
        except Exception as e:
            _LOGGER.error(f"Ошибка скриншота {camera_name} ({camera_id}): {e}")
        return None

   
    async def get_event_screenshot(self, camera_id: str, event_timestamp: int) -> bytes | None:
        """Получаем скриншот на момент события (с учётом лага)."""
        token_info = self.camera_tokens.get(camera_id)
        screenshot_token = token_info.get("screenshot_token") if token_info else None
        user_token = (token_info.get("user_token") or self.user_token) if token_info else None
        
        adjusted_timestamp = event_timestamp - self.event_time_lag
        
        url = f"https://key.rt.ru/screenshot/image/{self.event_screenshot_quality}/{camera_id}/{adjusted_timestamp}.jpg"
        if screenshot_token:
            url += f"?token={screenshot_token}"
        
        try:
            _LOGGER.debug(f"Запрос скриншота события для {camera_id}: {url}")
            r = await self.hass.async_add_executor_job(
                functools.partial(
                    requests.get,
                    url,
                    headers={
                        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                        "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
                        "Referer": "https://key.rt.ru/",
                        "Origin": "https://key.rt.ru",
                        "X-UTOKEN": user_token or self.token,
                    },
                    timeout=15,
                )
            )
            
            if r.status_code == 200 and len(r.content) > 1000:
                _LOGGER.info(f"✅ Скриншот события получен {camera_id} ({len(r.content)} байт)")
                return r.content
            else:
                _LOGGER.warning(f"Скриншот события {camera_id}: статус {r.status_code}")
        except Exception as e:
            _LOGGER.error(f"Ошибка скриншота события {camera_id}: {e}")
        return None
    
    def get_event_archive_url(self, camera_id: str, event_timestamp: int) -> str | None:
        token_info = self.camera_tokens.get(camera_id)
        streamer_token = token_info.get("streamer_token") if token_info else None
        
        adjusted_timestamp = event_timestamp - self.event_time_lag - self.pre_event_seconds
        
        base = f"https://live-vdk4.camera.rt.ru/stream/{camera_id}/{adjusted_timestamp}.mp4"
        params = "mp4-fragment-length=0.5&mp4-use-speed=0&mp4-afiller=1"
        
        if streamer_token:
            return f"{base}?{params}&token={streamer_token}"
        return f"{base}?{params}"

    async def get_camera_stream_url(self, camera_id: str) -> str | None:
        token_info = self.camera_tokens.get(camera_id)
        streamer_token = token_info.get("streamer_token") if token_info else None

        base = f"https://live-vdk4.camera.rt.ru/stream/{camera_id}/live.mp4"
        params = "mp4-fragment-length=0.5&mp4-use-speed=0&mp4-afiller=1"
        if streamer_token:
            return f"{base}?{params}&token={streamer_token}"
        return f"{base}?{params}"

    async def open_intercom(self, intercom_id: str):
        try:
            await self.hass.async_add_executor_job(
                functools.partial(
                    requests.post,
                    f"https://household.key.rt.ru/api/v2/app/devices/{intercom_id}/open",
                    headers={"Authorization": f"Bearer {self.token}"},
                    json={},
                    timeout=10,
                )
            )
            _LOGGER.info(f"✅ Открыт домофон {intercom_id}")
        except Exception as e:
            _LOGGER.error(f"Ошибка открытия {intercom_id}: {e}")
    
    def _parse_iso_to_timestamp(self, iso_str: str) -> int:
        try:
            dt = datetime.fromisoformat(iso_str.replace("Z", "+00:00"))
            return int(dt.timestamp())
        except Exception as e:
            _LOGGER.error(f"Ошибка парсинга времени {iso_str}: {e}")
            return 0
    
    def _format_local_time(self, iso_str: str) -> str:
        try:
            dt_utc = datetime.fromisoformat(iso_str.replace("Z", "+00:00"))
            dt_local = dt_utc.astimezone(self._local_tz)
            return dt_local.strftime("%Y-%m-%d %H:%M:%S")
        except Exception as e:
            _LOGGER.error(f"Ошибка форматирования времени {iso_str}: {e}")
            return iso_str
    
    def _get_event_description(self, event: dict) -> str:
        event_type = event.get("event_type", "")
        rfid_id = event.get("rfid_id")
        
        if rfid_id and rfid_id in self.rfid_names:
            return self.rfid_names[rfid_id]
        
        descriptions = {
            "api_open_remote": "Открытие через приложение",
            "face_open_remote": "Открытие по лицу",
            "pin_code_open_remote": "Открытие по пин-коду",
            "code_open_local": "Открытие по коду",
            "rfid_open_local": "Открытие по ключу",
            "dtmf_open_local": "Открытие через телефон",
        }
        
        if event.get("rfid") and event_type == "rfid_open_local":
            return f"Ключ {event.get('rfid')}"
        
        return descriptions.get(event_type, event_type)
    
    async def get_last_event(self, intercom_id: str) -> dict | None:
        try:
            now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            url = (
                f"https://events.key.rt.ru/api/v2/events/list?"
                f"begin_raised_at=2018-01-01T00:00:00Z&"
                f"end_raised_at={now_iso}&"
                f"device_ids={intercom_id}&"
                f"limit=1&"
                f"sort_by=raised_at&"
                f"offset=0"
            )
            
            r = await self.hass.async_add_executor_job(
                functools.partial(
                    requests.get,
                    url,
                    headers={
                        "Authorization": f"Bearer {self.token}",
                        "Accept": "application/json",
                    },
                    timeout=15,
                )
            )
            
            if r.status_code == 200:
                data = r.json()
                items = data.get("data", {}).get("items", [])
                if items:
                    return items[0]
            else:
                _LOGGER.warning(f"Events API вернул {r.status_code} для {intercom_id}")
        except Exception as e:
            _LOGGER.error(f"Ошибка получения событий для {intercom_id}: {e}")
        return None
    
    async def update_last_event(self, intercom_id: str, event: dict):
        camera_id = self.intercom_camera_map.get(str(intercom_id))
        if not camera_id:
            _LOGGER.warning(f"Не найден camera_id для домофона {intercom_id}")
            return
        
        event_timestamp = self._parse_iso_to_timestamp(event.get("raised_at", ""))
        if not event_timestamp:
            return
        
        screenshot_data = None
        if self.event_screenshot_enabled:
            screenshot_data = await self.get_event_screenshot(camera_id, event_timestamp)
        
        archive_url = None
        if self.event_archive_enabled:
            archive_url = self.get_event_archive_url(camera_id, event_timestamp)
        
        self.last_events[intercom_id] = {
            "event": event,
            "camera_id": camera_id,
            "timestamp": event_timestamp,
            "local_time": self._format_local_time(event.get("raised_at", "")),
            "description": self._get_event_description(event),
            "event_type": event.get("event_type", ""),
            "screenshot_data": screenshot_data,
            "archive_url": archive_url,
        }
        
        _LOGGER.info(
            f"🔔 Новое событие для домофона {intercom_id}: "
            f"{self.last_events[intercom_id]['description']} "
            f"в {self.last_events[intercom_id]['local_time']}"
        )
        
        self._notify_event_listeners(intercom_id)
    
    async def _events_polling_loop(self):
        _LOGGER.info(f"🔄 Запущен цикл опроса событий (интервал {self.events_refresh_interval}с)")
        
        while True:
            try:
                for intercom_id in self.intercom_ids:
                    last_event = await self.get_last_event(intercom_id)
                    if not last_event:
                        continue
                    
                    request_id = last_event.get("request_id")
                    last_request_id = self._last_request_ids.get(intercom_id)
                    
                    if request_id and request_id != last_request_id:
                        self._last_request_ids[intercom_id] = request_id
                        await self.update_last_event(intercom_id, last_event)
                
            except Exception as e:
                _LOGGER.error(f"Ошибка в цикле опроса событий: {e}")
            
            await asyncio.sleep(self.events_refresh_interval)
    
    async def start_events_polling(self):
        if not self.events_enabled:
            _LOGGER.info("Отслеживание событий отключено")
            return
        
        if self._events_polling_task and not self._events_polling_task.done():
            return
        
        await self._fetch_intercoms()
        
        self._events_polling_task = asyncio.create_task(self._events_polling_loop())
    
    async def stop_events_polling(self):
        if self._events_polling_task and not self._events_polling_task.done():
            self._events_polling_task.cancel()
            try:
                await self._events_polling_task
            except asyncio.CancelledError:
                pass
            self._events_polling_task = None


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    cameras_api = RTKeyCamerasApi(hass, entry)
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = {
        "cameras_api": cameras_api
    }

    entry.add_update_listener(update_listener)
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    
    await cameras_api.start_events_polling()
    
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    cameras_api = hass.data[DOMAIN][entry.entry_id]["cameras_api"]
    await cameras_api.stop_events_polling()
    
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


async def update_listener(hass: HomeAssistant, entry: ConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)