import voluptuous as vol
from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    OptionsFlowWithConfigEntry,
)
from homeassistant.core import callback

from . import (
    CONF_NAME,
    CONF_TOKEN,
    CONF_CAMERA_IMAGE_REFRESH_INTERVAL,
    CONF_SCREENSHOT_QUALITY,
    CONF_LOG_LEVEL,
    CONF_EVENTS_ENABLED,
    CONF_EVENTS_REFRESH_INTERVAL,
    CONF_EVENT_TIME_LAG,
    CONF_PRE_EVENT_SECONDS,
    CONF_EVENT_SCREENSHOT_ENABLED,
    CONF_EVENT_SCREENSHOT_QUALITY,
    CONF_EVENT_ARCHIVE_ENABLED,
    CONF_ARCHIVE_DURATION,
    CONF_EVENT_SENSOR_ENABLED,
    DATA_SCHEMA,
    OPTIONS_SCHEMA,
    DOMAIN,
)


class RTKeyOptionsFlow(OptionsFlowWithConfigEntry):
    async def async_step_init(self, user_input=None):
        if user_input is not None:
            return self.async_create_entry(
                title=self.config_entry.data.get(CONF_NAME, "RT Key"),
                data=user_input,
            )
        
        current_options = dict(self.options)
        if CONF_TOKEN not in current_options and CONF_TOKEN in self.config_entry.data:
            current_options[CONF_TOKEN] = self.config_entry.data[CONF_TOKEN]

        return self.async_show_form(
            step_id="init",
            data_schema=self.add_suggested_values_to_schema(
                vol.Schema(OPTIONS_SCHEMA), current_options
            ),
        )


class RTKeyConfigFlow(ConfigFlow, domain=DOMAIN):
    VERSION = 1
    MINOR_VERSION = 1

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> RTKeyOptionsFlow:
        return RTKeyOptionsFlow(config_entry)

    async def async_step_user(self, user_input=None):
        if user_input is not None:
            return self.async_create_entry(
                title=user_input.get(CONF_NAME, "RT Key"),
                data=user_input,
                options=user_input,
            )

        # ✅ Добавляем add_suggested_values_to_schema для корректной работы переводов
        return self.async_show_form(
            step_id="user",
            data_schema=self.add_suggested_values_to_schema(
                vol.Schema(DATA_SCHEMA).extend(OPTIONS_SCHEMA),
                {},  # Пустые значения по умолчанию для первой настройки
            ),
        )