import datetime
from typing import Any

from homeassistant.components.camera import Camera, CameraEntityFeature
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.helpers.entity import EntityCategory

from . import _LOGGER, DOMAIN, RTKeyCamerasApi, TOKEN_REFRESH_BUFFER


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: Any,
) -> None:
    cameras_api: RTKeyCamerasApi = hass.data[DOMAIN][config_entry.entry_id]["cameras_api"]
    cameras_info = await cameras_api.get_cameras_info()

    entities = [
        RTKeyCamera(hass, config_entry, cameras_api, camera_info)
        for camera_info in cameras_info["data"]["items"]
    ]

    # Добавляем архивные камеры событий для домофонов
    if cameras_api.event_archive_enabled:
        intercoms_info = await cameras_api.get_intercoms_info()
        for intercom_info in intercoms_info.get("data", {}).get("devices", []):
            entities.append(
                RTKeyEventCamera(hass, config_entry, cameras_api, intercom_info)
            )
    else:
        _LOGGER.info("Архивные видео событий отключены")

    async_add_entities(entities)


class RTKeyCamera(Camera):
    """Live-камера (для всех устройств — и камер, и домофонов)."""

    def __init__(
        self,
        hass: HomeAssistant,
        config_entry: ConfigEntry,
        cameras_api: RTKeyCamerasApi,
        camera_info: dict,
    ) -> None:
        super().__init__()

        self.hass = hass
        self.cameras_api = cameras_api
        self.camera_id = camera_info["id"]

        self.device_name = cameras_api.build_device_name(camera_info.get("title", "Устройство"))

        self._device_model = camera_info.get("model", "")
        self._device_vendor = camera_info.get("vendor", "")
        self._device_serial = camera_info.get("serial_number", "")
        self._device_mac = camera_info.get("mac", "")
        self._device_ip = camera_info.get("ip", "")
        self._device_status_title = camera_info.get("status_title", "Неизвестно")

        self._attr_unique_id = f"camera_{self.camera_id}"
        self._attr_name = self.device_name
        self._attr_supported_features = CameraEntityFeature.STREAM

        self._unsub_stream_refresh: Any = None

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()

        self._unsub_stream_refresh = async_track_time_interval(
            self.hass,
            self._stream_refresh,
            datetime.timedelta(seconds=TOKEN_REFRESH_BUFFER),
        )
        _LOGGER.debug(f"Таймер обновления стрима запущен для {self.device_name}")

    async def async_will_remove_from_hass(self) -> None:
        if self._unsub_stream_refresh:
            self._unsub_stream_refresh()
            self._unsub_stream_refresh = None
        await super().async_will_remove_from_hass()

    async def _stream_refresh(self, now: datetime.datetime) -> None:
        try:
            # Если токены протухли — обновляем их перед получением URL
            if self.cameras_api._tokens_invalid:
                _LOGGER.debug(f"Обнаружены протухшие токены, обновляем для {self.device_name}")
                await self.cameras_api.refresh_tokens()

            url = await self.stream_source()
            if url and self.stream and getattr(self.stream, "source", None) != url:
                _LOGGER.debug(f"Обновляем URL стрима для {self.device_name}")
                self.stream.update_source(url)
        except Exception as e:
            _LOGGER.error(f"Ошибка обновления стрима для {self.camera_id}: {e}")

    async def stream_source(self) -> str | None:
        return await self.cameras_api.get_camera_stream_url(self.camera_id)

    async def async_camera_image(
        self, width: int | None = None, height: int | None = None
    ) -> bytes | None:
        return await self.cameras_api.get_camera_image(self.camera_id)

    @property
    def available(self) -> bool:
        """Камера доступна, если она не в списке недоступных."""
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
        if self._device_mac and self._device_mac != "00:00:00:00:00:00":
            attrs["MAC"] = self._device_mac
        if self._device_ip and self._device_ip != "0.0.0.0":
            attrs["IP"] = self._device_ip
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


class RTKeyEventCamera(Camera):
    """Камера для просмотра архивного видео на момент последнего события."""

    _attr_should_poll = False
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(
        self,
        hass: HomeAssistant,
        config_entry: ConfigEntry,
        cameras_api: RTKeyCamerasApi,
        intercom_info: dict,
    ) -> None:
        super().__init__()

        self.hass = hass
        self.cameras_api = cameras_api
        self.intercom_id = str(intercom_info["id"])
        self.camera_id = intercom_info.get("camera_id")

        self.device_name = cameras_api.build_device_name(
            intercom_info.get("name_by_user")
            or intercom_info.get("description")
            or intercom_info.get("name_by_company", "Домофон")
        )

        self._attr_unique_id = f"event_camera_{self.intercom_id}"
        self._attr_name = "Архив события"
        self._attr_supported_features = CameraEntityFeature.STREAM

        self._event_description = None
        self._event_time = None
        self._archive_url = None

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

        archive_url = event_info.get("archive_url")
        if archive_url:
            self._archive_url = archive_url
            self._event_description = event_info.get("description")
            self._event_time = event_info.get("local_time")

            if self.stream:
                _LOGGER.debug(f"Обновляем URL архива для {self.device_name}")
                self.stream.update_source(archive_url)

            self.async_write_ha_state()

    async def stream_source(self) -> str | None:
        return self._archive_url

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        attrs = {}
        if self._event_description:
            attrs["Событие"] = self._event_description
        if self._event_time:
            attrs["Время"] = self._event_time
        if self._archive_url:
            attrs["URL архива"] = self._archive_url
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