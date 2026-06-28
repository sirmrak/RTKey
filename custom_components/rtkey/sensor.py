from typing import Any

from homeassistant.components.sensor import SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity import EntityCategory

from . import DOMAIN, RTKeyCamerasApi, _LOGGER


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: Any,
) -> None:
    cameras_api: RTKeyCamerasApi = hass.data[DOMAIN][config_entry.entry_id]["cameras_api"]
    
    if not cameras_api.event_sensor_enabled:
        _LOGGER.info("Сенсоры событий отключены")
        return
    
    intercoms_info = await cameras_api.get_intercoms_info()
    
    entities = [
        RTKeyEventSensor(hass, config_entry, cameras_api, intercom_info)
        for intercom_info in intercoms_info.get("data", {}).get("devices", [])
    ]
    async_add_entities(entities)


class RTKeyEventSensor(SensorEntity):
    """Сенсор последнего события домофона."""
    
    _attr_should_poll = False
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    
    def __init__(
        self,
        hass: HomeAssistant,
        config_entry: ConfigEntry,
        cameras_api: RTKeyCamerasApi,
        intercom_info: dict,
    ) -> None:
        self.hass = hass
        self.cameras_api = cameras_api
        self.intercom_id = str(intercom_info["id"])
        self.camera_id = intercom_info.get("camera_id")
        
        self.device_name = cameras_api.build_device_name(
            intercom_info.get("name_by_user") or
            intercom_info.get("description") or
            intercom_info.get("name_by_company", "Домофон")
        )
        
        self._attr_unique_id = f"event_sensor_{self.intercom_id}"
        self._attr_name = "Последнее событие"
        self._attr_icon = "mdi:bell-ring"
        
        self._attr_native_value = "Нет событий"
        self._event_data = None
    
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
        
        new_value = event_info["description"]
        
        # ✅ Обновляем только если значение изменилось
        if self._attr_native_value != new_value:
            self._event_data = event_info
            self._attr_native_value = new_value
            self.async_write_ha_state()
            _LOGGER.debug(f"Обновлён сенсор события для {self.device_name}: {self._attr_native_value}")
    
    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        if not self._event_data:
            return {}
        
        attrs = {
            "Время": self._event_data.get("local_time", ""),
            "Тип события": self._event_data.get("event_type", ""),
        }
        
        archive_url = self._event_data.get("archive_url")
        if archive_url:
            attrs["Архив видео"] = archive_url
        
        event = self._event_data.get("event", {})
        rfid = event.get("rfid")
        if rfid:
            attrs["RFID"] = rfid
        
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