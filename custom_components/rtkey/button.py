from typing import Any

from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo

from . import DOMAIN, RTKeyCamerasApi, _LOGGER


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: Any,
) -> None:
    cameras_api: RTKeyCamerasApi = hass.data[DOMAIN][config_entry.entry_id]["cameras_api"]
    intercoms_info = await cameras_api.get_intercoms_info()
    
    entities = [
        RTKeyOpenButton(hass, config_entry, cameras_api, intercom_info)
        for intercom_info in intercoms_info.get("data", {}).get("devices", [])
    ]
    async_add_entities(entities)


class RTKeyOpenButton(ButtonEntity):
    """Кнопка открытия домофона."""
    
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
        
        self._attr_unique_id = f"open_button_{self.intercom_id}"
        self._attr_name = "Открыть"
        self._attr_icon = "mdi:door-open"
        
        # Сохраняем информацию из API домофонов
        self._device_serial = intercom_info.get("serial_number", "")
        self._device_group = ", ".join(intercom_info.get("device_group", []))
        self._is_active = intercom_info.get("is_active", False)
        self._capabilities = [
            cap.get("name") for cap in intercom_info.get("capabilities", []) if cap.get("setup")
        ]
        self._inter_codes_count = len(intercom_info.get("inter_codes", []))
    
    async def async_press(self) -> None:
        """Вызывается при нажатии на кнопку."""
        _LOGGER.info(f"🔘 Нажата кнопка открытия для {self.device_name}")
        await self.cameras_api.open_intercom(self.intercom_id)
    
    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Атрибуты, которые будут видны в карточке сущности в HA."""
        attrs = {
            "Статус": "Активен" if self._is_active else "Неактивен",
        }
        
        if self._device_serial:
            attrs["Серийный номер"] = self._device_serial
        if self._device_group:
            attrs["Группа"] = self._device_group
        if self._inter_codes_count > 0:
            attrs["Ключей доступа"] = self._inter_codes_count
        if self._capabilities:
            attrs["Возможности"] = ", ".join(self._capabilities[:5])
            if len(self._capabilities) > 5:
                attrs["Возможности"] += f" (и еще {len(self._capabilities) - 5})"
        
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