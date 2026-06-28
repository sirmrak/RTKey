from datetime import datetime, timedelta
from typing import Any

from homeassistant.components.image import ImageEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.helpers.entity import EntityCategory
from zoneinfo import ZoneInfo

from . import CONF_CAMERA_IMAGE_REFRESH_INTERVAL, DOMAIN, RTKeyCamerasApi, _LOGGER


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: Any,
) -> None:
    cameras_api: RTKeyCamerasApi = hass.data[DOMAIN][config_entry.entry_id]["cameras_api"]
    cameras_info = await cameras_api.get_cameras_info()

    # Проверяем, включены ли обычные скриншоты
    interval = config_entry.options.get(CONF_CAMERA_IMAGE_REFRESH_INTERVAL, 60)

    entities = []

    # Обычные скриншоты (если interval > 0)
    if interval > 0:
        entities.extend([
            RTKeyCameraImageEntity(hass, config_entry, cameras_api, camera_info)
            for camera_info in cameras_info["data"]["items"]
        ])
    else:
        _LOGGER.info("Обычные скриншоты отключены (interval=0)")

    # Скриншоты событий
    if cameras_api.event_screenshot_enabled:
        intercoms_info = await cameras_api.get_intercoms_info()
        for intercom_info in intercoms_info.get("data", {}).get("devices", []):
            entities.append(
                RTKeyEventImage(hass, config_entry, cameras_api, intercom_info)
            )
    else:
        _LOGGER.info("Скриншоты событий отключены")

    async_add_entities(entities)


class RTKeyCameraImageEntity(ImageEntity):
    """Периодический скриншот камеры."""

    def __init__(
        self,
        hass: HomeAssistant,
        config_entry: ConfigEntry,
        cameras_api: RTKeyCamerasApi,
        camera_info: dict,
    ) -> None:
        super().__init__(hass)

        self.hass = hass
        self.cameras_api = cameras_api
        self.camera_id = camera_info["id"]

        self.device_name = cameras_api.build_device_name(camera_info.get("title", "Устройство"))

        self._device_model = camera_info.get("model", "")
        self._device_vendor = camera_info.get("vendor", "")
        self._device_serial = camera_info.get("serial_number", "")
        self._device_status_title = camera_info.get("status_title", "Неизвестно")

        self._attr_unique_id = f"image_{self.camera_id}"
        self._attr_name = "Скриншот"

        self.camera_image_refresh_interval = config_entry.options.get(
            CONF_CAMERA_IMAGE_REFRESH_INTERVAL, 60
        )

        self._attr_image_last_updated = datetime.now()
        self._cached_image: bytes | None = None
        self._unsub_timer: Any = None

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()

        self._unsub_timer = async_track_time_interval(
            self.hass,
            self._async_update_image,
            timedelta(seconds=self.camera_image_refresh_interval),
        )
        await self._async_update_image()

    async def async_will_remove_from_hass(self) -> None:
        if self._unsub_timer:
            self._unsub_timer()
        await super().async_will_remove_from_hass()

    async def _async_update_image(self, now: datetime | None = None) -> None:
        """Продолжаем опрашивать API даже если камера недоступна,
        чтобы узнать, когда она снова появится."""
        try:
            image = await self.cameras_api.get_camera_image(self.camera_id)
            if image:
                self._cached_image = image
                self._attr_image_last_updated = datetime.now()
                self.async_write_ha_state()
                _LOGGER.debug(f"Обновлено изображение: {self.device_name}")
        except Exception as e:
            _LOGGER.error(f"Ошибка обновления изображения {self.device_name}: {e}")

    async def async_image(self) -> bytes | None:
        return self._cached_image

    @property
    def available(self) -> bool:
        """Сущность доступна, если камера не в списке недоступных."""
        return self.cameras_api.is_camera_available(self.camera_id)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Атрибуты с динамической проверкой доступности камеры."""
        if self.cameras_api.is_camera_available(self.camera_id):
            status = self._device_status_title
        else:
            status = "❌ Нет связи"

        attrs = {"Статус": status}
        if self._device_model:
            attrs["Модель"] = self._device_model
        if self._device_serial:
            attrs["Серийный номер"] = self._device_serial
        return attrs

    @property
    def device_info(self) -> DeviceInfo:
        info = {
            "identifiers": {(DOMAIN, self.camera_id)},
            "name": self.device_name,
            "manufacturer": self._device_vendor or "RT Key",
        }
        if self._device_model:
            info["model"] = self._device_model
        if self._device_serial:
            info["serial_number"] = self._device_serial
        return info


class RTKeyEventImage(ImageEntity):
    """Скриншот на момент последнего события домофона."""

    _attr_should_poll = False
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(
        self,
        hass: HomeAssistant,
        config_entry: ConfigEntry,
        cameras_api: RTKeyCamerasApi,
        intercom_info: dict,
    ) -> None:
        super().__init__(hass)

        self.hass = hass
        self.cameras_api = cameras_api
        self.intercom_id = str(intercom_info["id"])
        self.camera_id = intercom_info.get("camera_id")

        self.device_name = cameras_api.build_device_name(
            intercom_info.get("name_by_user")
            or intercom_info.get("description")
            or intercom_info.get("name_by_company", "Домофон")
        )

        self._attr_unique_id = f"event_image_{self.intercom_id}"
        self._attr_name = "Скриншот события"

        self._attr_image_last_updated = None
        self._cached_image: bytes | None = None
        self._event_description = None
        self._event_time = None

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        self.cameras_api.register_event_listener(self._handle_event_update)

        if self.intercom_id in self.cameras_api.last_events:
            self._update_from_event()

    async def async_will_remove_from_hass(self) -> None:
        self.cameras_api.unregister_event_listener(self._handle_event_update)
        await super().async_will_remove_from_hass()

    @callback
    def _handle_event_update(self, intercom_id: str):
        if intercom_id == self.intercom_id:
            self._update_from_event()

    def _update_from_event(self):
        event_info = self.cameras_api.last_events.get(self.intercom_id)
        if not event_info:
            return

        screenshot_data = event_info.get("screenshot_data")
        if screenshot_data:
            self._cached_image = screenshot_data
            self._event_description = event_info.get("description")
            self._event_time = event_info.get("local_time")

            # ✅ Используем время события, а не текущее время
            event_timestamp = event_info.get("timestamp")
            if event_timestamp:
                self._attr_image_last_updated = datetime.fromtimestamp(
                    event_timestamp,
                    tz=ZoneInfo(self.hass.config.time_zone),
                )
            else:
                self._attr_image_last_updated = datetime.now()

            self.async_write_ha_state()
            _LOGGER.debug(f"Обновлён скриншот события для {self.device_name}")

    async def async_image(self) -> bytes | None:
        return self._cached_image

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        attrs = {}
        if self._event_description:
            attrs["Событие"] = self._event_description
        if self._event_time:
            attrs["Время"] = self._event_time
        return attrs

    @property
    def device_info(self) -> DeviceInfo:
        device_id = self.camera_id or self.intercom_id
        return {
            "identifiers": {(DOMAIN, device_id)},
            "name": self.device_name,
            "manufacturer": "RT Key",
            "model": "Домофон",
        }