"""Constants for the Roborock custom integration."""

from homeassistant.const import Platform

DOMAIN = "roborock_custom"

CONF_USER_DATA = "user_data"
CONF_BASE_URL = "base_url"
CONF_CODE = "code"
CONF_SCAN_INTERVAL = "scan_interval"
CONF_DEVICE_IDENTIFIER = "device_identifier"

DEFAULT_SCAN_INTERVAL = 30
MIN_SCAN_INTERVAL = 10
MAX_SCAN_INTERVAL = 300

PLATFORMS: list[Platform] = [Platform.VACUUM, Platform.SENSOR, Platform.SELECT, Platform.IMAGE]
