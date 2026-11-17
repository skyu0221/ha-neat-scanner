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

DEFAULT_PORT = 3240
DEFAULT_SAVE_DIR = "/config/scans"
DEFAULT_DPI = 300
DEFAULT_COLOR = "color"
DEFAULT_FORMAT = "png"
DEFAULT_SCAN_INTERVAL = 30

NEAT_VID = 0x1F44
NEAT_PID = 0x0050

DPI_OPTIONS = [150, 200, 300, 600]
COLOR_OPTIONS = ["color", "grayscale", "bw"]
FORMAT_OPTIONS = ["png", "jpeg", "pdf"]

SERVICE_SCAN = "scan"
EVENT_SCAN_COMPLETE = f"{DOMAIN}_scan_complete"
