"""Constants for the Neat ADF scanner integration."""
from __future__ import annotations

DOMAIN = "neat_scanner"

CONF_HOST = "host"
CONF_PORT = "port"
CONF_BUSID = "busid"
CONF_SAVE_DIR = "save_dir"
CONF_DPI = "dpi"
CONF_COLOR = "color"
CONF_FORMAT = "format"
CONF_SCAN_INTERVAL = "scan_interval"
CONF_SHEETS = "sheets"
CONF_AUTO_CROP = "auto_crop"
CONF_SCAN_ON_BUTTON = "scan_on_button"
CONF_MONITOR = "monitor"
CONF_DUPLEX = "duplex"
CONF_DESKEW = "deskew"
CONF_AUTO_ROTATE = "auto_rotate"

DEFAULT_PORT = 3240
DEFAULT_SAVE_DIR = "/config/scans"
DEFAULT_DPI = 300
DEFAULT_COLOR = "color"
DEFAULT_FORMAT = "png"
DEFAULT_SCAN_INTERVAL = 30
DEFAULT_SHEETS = 50
DEFAULT_AUTO_CROP = True
DEFAULT_SCAN_ON_BUTTON = True
DEFAULT_MONITOR = True
DEFAULT_DUPLEX = False
DEFAULT_DESKEW = True
DEFAULT_AUTO_ROTATE = False

NEAT_VID = 0x1F44
NEAT_PID = 0x0050

DPI_OPTIONS = [75, 100, 150, 200, 300, 400, 600]
DPI_MIN = 75
DPI_MAX = 600
COLOR_OPTIONS = ["color", "grayscale", "bw"]
FORMAT_OPTIONS = ["png", "jpeg", "pdf"]

SERVICE_SCAN = "scan"
SERVICE_CALIBRATE = "calibrate"
STORAGE_VERSION = 1
EVENT_SCAN_COMPLETE = f"{DOMAIN}_scan_complete"
EVENT_BUTTON = f"{DOMAIN}_button"
