import asyncio
import json
import logging
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import aiohttp
import voluptuous as vol
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers import selector
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from yarl import URL

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
CONF_EVENT_SCREENSHOT_QUALITY = "event_screenshot_quality"
CONF_EVENT_SENSOR_ENABLED = "event_sensor_enabled"

# Настройки локального хранения
CONF_ARCHIVE_PATH = "archive_path"
CONF_ARCHIVE_COPIES = "archive_copies"
CONF_ARCHIVE_DURATION = "archive_duration"
CONF_SCREENSHOT_COPIES = "screenshot_copies"

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
            min=5, max=3600, step=5,
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
    # Настройки локального хранения
    vol.Optional(CONF_ARCHIVE_PATH, default="RTKey"): str,
    vol.Optional(CONF_ARCHIVE_COPIES, default=5): selector.NumberSelector(
        selector.NumberSelectorConfig(
            min=0, max=10, step=1,
            mode=selector.NumberSelectorMode.BOX,
        )
    ),
    vol.Optional(CONF_ARCHIVE_DURATION, default=30): selector.NumberSelector(
        selector.NumberSelectorConfig(
            min=10, max=120, step=10,
            mode=selector.NumberSelectorMode.SLIDER,
            unit_of_measurement="сек",
        )
    ),
    vol.Optional(CONF_SCREENSHOT_COPIES, default=10): selector.NumberSelector(
        selector.NumberSelectorConfig(
            min=0, max=10, step=1,
            mode=selector.NumberSelectorMode.BOX,
        )
    ),
    vol.Optional(CONF_EVENT_SENSOR_ENABLED, default=True): bool,
}


@dataclass
class CameraTokenInfo:
    title: str = "Устройство"
    category_type: str = "unknown"
    category_title: str = ""
    model: str = ""
    vendor: str = ""
    serial_number: str = ""
    mac: str = ""
    ip: str = ""
    status_type: str = "unknown"
    status_title: str = ""
    streamer_token: str | None = None
    screenshot_token: str | None = None
    user_token: str | None = None


@dataclass
class _ApiResponse:
    status: int
    body: bytes


_DEFAULT_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
    "Referer": "https://key.rt.ru/",
    "Origin": "https://key.rt.ru",
}


def _mkdir_parents(p: Path):
    """Path.mkdir с keyword args для executor."""
    p.mkdir(parents=True, exist_ok=True)


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
        self.event_screenshot_quality = entry.options.get(CONF_EVENT_SCREENSHOT_QUALITY, "large")
        self.event_sensor_enabled = entry.options.get(CONF_EVENT_SENSOR_ENABLED, True)

        # Настройки локального хранения
        self.archive_path = entry.options.get(CONF_ARCHIVE_PATH, "RTKey")
        self.archive_copies = entry.options.get(CONF_ARCHIVE_COPIES, 5)
        self.archive_duration = entry.options.get(CONF_ARCHIVE_DURATION, 30)
        self.screenshot_copies = entry.options.get(CONF_SCREENSHOT_COPIES, 10)

        log_level = entry.options.get(CONF_LOG_LEVEL, "INFO")
        _LOGGER.setLevel(getattr(logging, log_level, logging.INFO))
        _LOGGER.info(f"Уровень логирования: {log_level}")

        self.user_token: str | None = None

        # HA managed aiohttp session
        self._session: aiohttp.ClientSession = async_get_clientsession(hass)

        # Информация о камерах
        self.camera_tokens: dict[str, CameraTokenInfo] = {}
        self._cameras_cache = None
        self._cameras_cache_ts = 0

        # Кэш картинок
        self._image_cache: dict[str, tuple[bytes, float]] = {}
        self._image_cache_lock = asyncio.Lock()

        # Отслеживание недоступных камер (404)
        self._unavailable_cameras: set[str] = set()

        # Флаг протухших токенов (403)
        self._tokens_invalid = False
        self._tokens_refresh_lock = asyncio.Lock()

        # Данные событий
        self.last_events: dict[str, dict] = {}
        self._last_request_ids: dict[str, str] = {}
        self.rfid_names: dict[str, str] = {}
        self.intercom_camera_map: dict[str, str] = {}
        self.intercom_ids: list[str] = []
        self._events_polling_task: asyncio.Task | None = None
        self._event_listeners: list = []

        self._local_tz = ZoneInfo(hass.config.time_zone)
        _LOGGER.info(f"Временная зона HA: {hass.config.time_zone}")

        self.ffmpeg_available = False

    async def async_initialize(self):
        await self._check_ffmpeg()
        await self._ensure_media_dirs()

    async def _ensure_media_dirs(self):
        base = self._get_media_base_path() / self.archive_path
        dirs_to_create = [base / "archives", base / "screenshots"]
        for dir_path in dirs_to_create:
            await self.hass.async_add_executor_job(_mkdir_parents, dir_path)
            _LOGGER.info(f"Медиа-папка: {dir_path}")
        _LOGGER.info(f"Базовый путь для медиа: {base}")

    async def _ensure_intercom_dirs(self, intercom_ids: list[str]):
        for intercom_id in intercom_ids:
            for dir_path in (self._get_archive_dir(intercom_id), self._get_screenshot_dir(intercom_id)):
                await self.hass.async_add_executor_job(_mkdir_parents, dir_path)

    async def _check_ffmpeg(self):
        try:
            proc = await asyncio.create_subprocess_exec(
                "ffmpeg", "-version",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            await asyncio.wait_for(proc.communicate(), timeout=5)
            self.ffmpeg_available = True
            _LOGGER.info("FFmpeg доступен")
        except FileNotFoundError:
            self.ffmpeg_available = False
            _LOGGER.error("FFmpeg не найден! Архивное видео будет недоступно.")
        except (asyncio.TimeoutError, Exception) as e:
            self.ffmpeg_available = False
            _LOGGER.error(f"Ошибка проверки FFmpeg: {e}")

    # === Медиа-пути ===

    def _get_media_base_path(self) -> Path:
        media_dirs = self.hass.config.media_dirs
        if media_dirs:
            if "media" in media_dirs:
                return Path(media_dirs["media"])
            if "local" in media_dirs:
                return Path(media_dirs["local"])
        return Path(self.hass.config.path("media"))

    def _get_archive_dir(self, intercom_id: str) -> Path:
        return self._get_media_base_path() / self.archive_path / "archives" / intercom_id

    def _get_screenshot_dir(self, intercom_id: str) -> Path:
        return self._get_media_base_path() / self.archive_path / "screenshots" / intercom_id

    # === Скачивание и cleanup ===

    async def download_event_archive(self, camera_id: str, event_timestamp: int, intercom_id: str) -> str | None:
        if not self.ffmpeg_available:
            _LOGGER.error("FFmpeg недоступен, пропускаем скачивание архива")
            return None

        url = self.get_event_archive_url(camera_id, event_timestamp)
        if not url:
            _LOGGER.error(f"Не удалось получить URL архива для камеры {camera_id}")
            return None

        archive_dir = self._get_archive_dir(intercom_id)
        await self.hass.async_add_executor_job(_mkdir_parents, archive_dir)

        dt = datetime.fromtimestamp(event_timestamp, tz=self._local_tz)
        filename = dt.strftime("%Y-%m-%d_%H-%M-%S.mp4")
        filepath = archive_dir / filename

        duration = self.archive_duration

        _LOGGER.info(f"Скачиваем архив для {intercom_id}: {duration} сек, файл: {filename}")

        cmd = [
            "ffmpeg", "-y",
            "-i", url,
            "-t", str(duration),
            "-c", "copy",
            str(filepath),
        ]

        proc: asyncio.subprocess.Process | None = None
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(),
                timeout=duration + 30,
            )

            if proc.returncode == 0:
                size = await self.hass.async_add_executor_job(
                    lambda p: p.stat().st_size if p.exists() else 0, filepath
                )
                if size > 1000:
                    _LOGGER.info(f"Архив сохранён: {filepath} ({size} байт)")
                    await self.cleanup_old_files(
                        self._get_archive_dir(intercom_id), "*.mp4", self.archive_copies
                    )
                    return str(filepath)

            error_msg = stderr.decode()[:200] if stderr else "Неизвестная ошибка"
            _LOGGER.error(f"Ошибка FFmpeg для {intercom_id}: {error_msg}")
            return None

        except asyncio.TimeoutError:
            _LOGGER.error(f"Превышено время скачивания архива для {intercom_id}")
            if proc:
                proc.kill()
            return None
        except Exception as e:
            _LOGGER.error(f"Ошибка скачивания архива для {intercom_id}: {e}")
            return None

    async def download_event_screenshot(self, camera_id: str, event_timestamp: int, intercom_id: str) -> str | None:
        screenshot_dir = self._get_screenshot_dir(intercom_id)
        await self.hass.async_add_executor_job(_mkdir_parents, screenshot_dir)

        dt = datetime.fromtimestamp(event_timestamp, tz=self._local_tz)
        filename = dt.strftime("%Y-%m-%d_%H-%M-%S.jpg")
        filepath = screenshot_dir / filename

        screenshot_data = await self.get_event_screenshot(camera_id, event_timestamp)

        if screenshot_data:
            await self.hass.async_add_executor_job(filepath.write_bytes, screenshot_data)
            _LOGGER.info(f"Скриншот сохранён: {filepath} ({len(screenshot_data)} байт)")
            await self.cleanup_old_files(
                self._get_screenshot_dir(intercom_id), "*.jpg", self.screenshot_copies
            )
            return str(filepath)

        return None

    async def cleanup_old_files(self, directory: Path, pattern: str, max_copies: int):
        def _cleanup():
            if not directory.exists():
                return
            files = sorted(directory.glob(pattern), key=lambda f: f.stat().st_mtime)
            while len(files) > max_copies:
                oldest = files.pop(0)
                oldest.unlink()
                _LOGGER.debug(f"Удалён старый файл: {oldest.name}")

        await self.hass.async_add_executor_job(_cleanup)

    # === Утилиты ===

    def build_device_name(self, name: str) -> str:
        return name.strip() if name else "RT Key Device"

    def get_camera_name(self, camera_id: str) -> str:
        info = self.camera_tokens.get(camera_id)
        return info.title if info else "Неизвестная камера"

    def is_camera_available(self, camera_id: str) -> bool:
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

    # === HTTP API (aiohttp, HA managed session) ===

    async def _api_get(self, url: str | URL, headers: dict, timeout: int = 20) -> _ApiResponse | None:
        """GET-запрос. Читает body внутри context manager, возвращает status + bytes."""
        try:
            async with self._session.get(
                url,
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=timeout),
            ) as r:
                body = await r.read()
                return _ApiResponse(status=r.status, body=body)
        except (aiohttp.ClientError, asyncio.TimeoutError) as e:
            _LOGGER.error(f"HTTP ошибка GET {url}: {e}")
            return None

    async def _api_post(self, url: str | URL, headers: dict, json_data: dict | None = None, timeout: int = 10) -> _ApiResponse | None:
        """POST-запрос. Читает body внутри context manager."""
        try:
            async with self._session.post(
                url,
                headers=headers,
                json=json_data or {},
                timeout=aiohttp.ClientTimeout(total=timeout),
            ) as r:
                body = await r.read()
                return _ApiResponse(status=r.status, body=body)
        except (aiohttp.ClientError, asyncio.TimeoutError) as e:
            _LOGGER.error(f"HTTP ошибка POST {url}: {e}")
            return None

    # === Камеры ===

    async def _fetch_cameras(self):
        if self._cameras_cache and (time.time() - self._cameras_cache_ts < 60):
            return self._cameras_cache

        try:
            _LOGGER.debug("Запрос camera_video_data/list...")
            url = URL("https://keyapis.key.rt.ru/vc/api/v1/camera_video_data/list").with_query({
                "paging.limit": 100,
                "paging.offset": 0,
            })
            r = await self._api_get(url, {"Authorization": f"Bearer {self.token}"})
            if r is None:
                return []

            if r.status == 200:
                data = json.loads(r.body)
                devices = data.get("data", [])

                if devices:
                    self.user_token = devices[0].get("userToken")

                for device in devices:
                    uid = device.get("uid")
                    if uid:
                        self.camera_tokens[uid] = CameraTokenInfo(
                            title=device.get("title", "Устройство"),
                            category_type=device.get("category", {}).get("type", "unknown"),
                            category_title=device.get("category", {}).get("title", ""),
                            model=device.get("model", ""),
                            vendor=device.get("vendor", ""),
                            serial_number=device.get("serialNumber", ""),
                            mac=device.get("mac", ""),
                            ip=device.get("ip", ""),
                            status_type=device.get("status", {}).get("type", "unknown"),
                            status_title=device.get("status", {}).get("title", ""),
                            streamer_token=device.get("streamerToken"),
                            screenshot_token=device.get("screenshotToken"),
                            user_token=device.get("userToken"),
                        )

                self._cameras_cache = devices
                self._cameras_cache_ts = time.time()
                _LOGGER.info(f"Загружено {len(self.camera_tokens)} устройств")
                return devices
            else:
                _LOGGER.error(f"camera_video_data/list вернул {r.status}")
        except Exception as e:
            _LOGGER.error(f"Ошибка получения камер: {e}")
        return []

    async def refresh_tokens(self):
        async with self._tokens_refresh_lock:
            _LOGGER.info("Принудительное обновление токенов...")
            self._cameras_cache = None
            self._cameras_cache_ts = 0
            await self._fetch_cameras()
            self._tokens_invalid = False
            _LOGGER.info(f"Токены обновлены для {len(self.camera_tokens)} устройств")

    # === Домофоны ===

    async def _fetch_intercoms(self):
        try:
            _LOGGER.debug("Запрос домофонов из household.key.rt.ru...")
            r = await self._api_get(
                "https://household.key.rt.ru/api/v2/app/devices/intercom",
                {"Authorization": f"Bearer {self.token}"},
                timeout=15,
            )
            if r is None:
                return {"data": {"devices": []}}

            if r.status == 200:
                data = json.loads(r.body)
                self._build_intercom_mappings(data)

                devices = data.get("data", {}).get("devices", [])
                self.intercom_ids = [str(d.get("id")) for d in devices if d.get("id")]

                _LOGGER.info(f"Загружено {len(self.intercom_ids)} домофонов: {self.intercom_ids}")
                return data
            else:
                _LOGGER.error(f"intercom вернул {r.status}")
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
                "title": info.title,
                "category_type": info.category_type,
                "status_title": info.status_title,
                "model": info.model,
                "vendor": info.vendor,
                "serial_number": info.serial_number,
                "mac": info.mac,
                "ip": info.ip,
                "available": self.is_camera_available(uid),
            })

        return {"data": {"items": items}}

    async def get_intercoms_info(self):
        return await self._fetch_intercoms()

    # === Скриншоты ===

    async def get_camera_image(self, camera_id: str, retry: bool = False) -> bytes | None:
        async with self._image_cache_lock:
            if camera_id in self._image_cache:
                img, ts = self._image_cache[camera_id]
                if time.time() - ts < 4:
                    return img

        info = self.camera_tokens.get(camera_id)
        screenshot_token = info.screenshot_token if info else None
        user_token = (info.user_token or self.user_token) if info else None

        camera_name = self.get_camera_name(camera_id)

        try:
            url = URL(f"https://key.rt.ru/screenshot/image/{self.screenshot_quality}/{camera_id}/last.jpg")
            if screenshot_token:
                url = url.with_query({"token": screenshot_token, "_": str(int(time.time() * 1000))})

            _LOGGER.debug(f"Запрос скриншота для {camera_name} ({camera_id})")
            r = await self._api_get(
                url,
                {**_DEFAULT_HEADERS, "X-UTOKEN": user_token or self.token},
                timeout=15,
            )
            if r is None:
                return None

            if r.status == 403 and not retry:
                _LOGGER.warning(f"Токен для {camera_name} ({camera_id}) протух (403), запускаем обновление...")
                self._tokens_invalid = True
                await self.refresh_tokens()
                return await self.get_camera_image(camera_id, retry=True)

            if r.status == 403 and retry:
                _LOGGER.error(f"Повторный 403 для {camera_name} ({camera_id}) — проблема с авторизацией")
                return None

            if r.status == 404:
                if camera_id not in self._unavailable_cameras:
                    _LOGGER.warning(f"Камера \"{camera_name}\" ({camera_id}) недоступна (404), помечаем как offline")
                self._unavailable_cameras.add(camera_id)
                return None

            if r.status == 200:
                image_data = r.body
                if len(image_data) > 2000:
                    async with self._image_cache_lock:
                        self._image_cache[camera_id] = (image_data, time.time())

                    if camera_id in self._unavailable_cameras:
                        self._unavailable_cameras.discard(camera_id)
                        _LOGGER.info(f"Камера \"{camera_name}\" ({camera_id}) снова доступна")

                    _LOGGER.info(f"Скриншот получен {camera_name} ({camera_id}) — {len(image_data)} байт")
                    return image_data
                else:
                    _LOGGER.warning(f"Скриншот для {camera_name} ({camera_id}): размер {len(image_data)} слишком мал")
            else:
                _LOGGER.warning(f"Скриншот для {camera_name} ({camera_id}): статус {r.status}")
        except Exception as e:
            _LOGGER.error(f"Ошибка скриншота {camera_name} ({camera_id}): {e}")
        return None

    async def get_event_screenshot(self, camera_id: str, event_timestamp: int) -> bytes | None:
        info = self.camera_tokens.get(camera_id)
        screenshot_token = info.screenshot_token if info else None
        user_token = (info.user_token or self.user_token) if info else None

        adjusted_timestamp = event_timestamp - self.event_time_lag

        url = URL(f"https://key.rt.ru/screenshot/image/{self.event_screenshot_quality}/{camera_id}/{adjusted_timestamp}.jpg")
        if screenshot_token:
            url = url.with_query({"token": screenshot_token})

        try:
            _LOGGER.debug(f"Запрос скриншота события для {camera_id}")
            r = await self._api_get(
                url,
                {**_DEFAULT_HEADERS, "X-UTOKEN": user_token or self.token},
                timeout=15,
            )
            if r is None:
                return None

            if r.status == 200:
                content = r.body
                if len(content) > 1000:
                    _LOGGER.info(f"Скриншот события получен {camera_id} ({len(content)} байт)")
                    return content
                else:
                    _LOGGER.warning(f"Скриншот события {camera_id}: размер {len(content)} слишком мал")
            else:
                _LOGGER.warning(f"Скриншот события {camera_id}: статус {r.status}")
        except Exception as e:
            _LOGGER.error(f"Ошибка скриншота события {camera_id}: {e}")
        return None

    # === Стримы и архивы ===

    def get_event_archive_url(self, camera_id: str, event_timestamp: int) -> str | None:
        info = self.camera_tokens.get(camera_id)
        streamer_token = info.streamer_token if info else None

        adjusted_timestamp = event_timestamp - self.event_time_lag - self.pre_event_seconds

        url = URL(f"https://live-vdk4.camera.rt.ru/stream/{camera_id}/{adjusted_timestamp}.mp4").with_query({
            "mp4-fragment-length": "0.5",
            "mp4-use-speed": "0",
            "mp4-afiller": "1",
        })
        if streamer_token:
            url = url.update_query({"token": streamer_token})
        return str(url)

    async def get_camera_stream_url(self, camera_id: str) -> str | None:
        info = self.camera_tokens.get(camera_id)
        streamer_token = info.streamer_token if info else None

        url = URL(f"https://live-vdk4.camera.rt.ru/stream/{camera_id}/live.mp4").with_query({
            "mp4-fragment-length": "0.5",
            "mp4-use-speed": "0",
            "mp4-afiller": "1",
        })
        if streamer_token:
            url = url.update_query({"token": streamer_token})
        return str(url)

    # === Домофон: открытие ===

    async def open_intercom(self, intercom_id: str):
        try:
            r = await self._api_post(
                f"https://household.key.rt.ru/api/v2/app/devices/{intercom_id}/open",
                {"Authorization": f"Bearer {self.token}"},
            )
            if r and r.status in (200, 204):
                _LOGGER.info(f"Открыт домофон {intercom_id}")
            elif r:
                _LOGGER.error(f"Ошибка открытия домофона {intercom_id}: статус {r.status}")
        except Exception as e:
            _LOGGER.error(f"Ошибка открытия {intercom_id}: {e}")

    # === Время и события ===

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
            url = URL("https://events.key.rt.ru/api/v2/events/list").with_query({
                "begin_raised_at": "2018-01-01T00:00:00Z",
                "end_raised_at": now_iso,
                "device_ids": intercom_id,
                "limit": "1",
                "sort_by": "raised_at",
                "offset": "0",
            })

            r = await self._api_get(
                url,
                {"Authorization": f"Bearer {self.token}", "Accept": "application/json"},
                timeout=15,
            )
            if r is None:
                return None

            if r.status == 200:
                data = json.loads(r.body)
                items = data.get("data", {}).get("items", [])
                if items:
                    return items[0]
            else:
                _LOGGER.warning(f"Events API вернул {r.status} для {intercom_id}")
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

        screenshot_path = None
        screenshot_error = None
        if self.screenshot_copies > 0:
            try:
                screenshot_path = await self.download_event_screenshot(camera_id, event_timestamp, intercom_id)
            except Exception as e:
                screenshot_error = str(e)
                _LOGGER.error(f"Ошибка скачивания скриншота для {intercom_id}: {e}")

        archive_path = None
        archive_error = None
        if self.archive_copies > 0:
            try:
                archive_path = await self.download_event_archive(camera_id, event_timestamp, intercom_id)
            except Exception as e:
                archive_error = str(e)
                _LOGGER.error(f"Ошибка скачивания архива для {intercom_id}: {e}")

        self.last_events[intercom_id] = {
            "event": event,
            "camera_id": camera_id,
            "timestamp": event_timestamp,
            "local_time": self._format_local_time(event.get("raised_at", "")),
            "description": self._get_event_description(event),
            "event_type": event.get("event_type", ""),
            "screenshot_path": screenshot_path,
            "archive_path": archive_path,
            "screenshot_error": screenshot_error,
            "archive_error": archive_error,
        }

        _LOGGER.info(
            f"Новое событие для домофона {intercom_id}: "
            f"{self.last_events[intercom_id]['description']} "
            f"в {self.last_events[intercom_id]['local_time']}"
        )

        self._notify_event_listeners(intercom_id)

    async def _events_polling_loop(self):
        _LOGGER.info(f"Запущен цикл опроса событий (интервал {self.events_refresh_interval}с)")

        while True:
            try:
                tasks = [self.get_last_event(id) for id in self.intercom_ids]
                results = await asyncio.gather(*tasks, return_exceptions=True)

                for intercom_id, result in zip(self.intercom_ids, results):
                    if isinstance(result, Exception):
                        _LOGGER.error(f"Ошибка опроса событий для {intercom_id}: {result}")
                        continue
                    if not result:
                        continue

                    request_id = result.get("request_id")
                    last_request_id = self._last_request_ids.get(intercom_id)

                    if request_id and request_id != last_request_id:
                        self._last_request_ids[intercom_id] = request_id
                        await self.update_last_event(intercom_id, result)

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
        await self._ensure_intercom_dirs(self.intercom_ids)

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

    await cameras_api.async_initialize()

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
