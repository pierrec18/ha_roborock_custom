"""Constants for the Roborock custom integration."""

from homeassistant.const import Platform

DOMAIN = "roborock_custom"

CONF_USER_DATA = "user_data"
CONF_BASE_URL = "base_url"
CONF_CODE = "code"
CONF_SCAN_INTERVAL = "scan_interval"
CONF_DEVICE_IDENTIFIER = "device_identifier"
# Calibration manuelle trace->carte (B01/Q10). Dict optionnel dans entry.options:
# {unit, off_x, off_y, sign_x, sign_y}. Absent => aucun overlay robot/trajet.
CONF_MAP_CALIBRATION = "map_calibration"

DEFAULT_SCAN_INTERVAL = 30
MIN_SCAN_INTERVAL = 10
MAX_SCAN_INTERVAL = 300

PLATFORMS: list[Platform] = [
    Platform.VACUUM,
    Platform.SENSOR,
    Platform.SELECT,
    Platform.IMAGE,
    Platform.CAMERA,
]
