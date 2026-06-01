"""
ini_editor.py  —  Main PC
Reads and updates Data.ini for InLine_Pro.

Data.ini structure:
    [Rack1]
    Building 1 = Checked
    Building 2 = Checked
    ...
    Building 10 = Checked

    [Rack2]
    Building 1 = Checked
    ...
    Building 10 = Checked

DL to INI mapping:
    DL01 → Rack1, Building 1
    DL02 → Rack1, Building 2
    ...
    DL10 → Rack1, Building 10
    DL11 → Rack2, Building 1
    DL12 → Rack2, Building 2
    ...
    DL20 → Rack2, Building 10
"""

import os
import logging
import configparser

from config_loader import get_config

logger = logging.getLogger("ini_editor")


# =========================================================
# DL name → (section, key) mapping
# =========================================================
def dl_to_ini_key(dl_name: str) -> tuple:
    """
    Convert DL name to (section, building_key) in Data.ini.

    DL01-DL10 → Rack1, Building 1-10
    DL11-DL20 → Rack2, Building 1-10

    Returns:
        ("Rack1", "Building 1")  for DL01
        ("Rack2", "Building 5")  for DL15
    Raises:
        ValueError if dl_name format is invalid or out of range
    """
    try:
        dl_num = int(dl_name[2:])   # "DL03" → 3
    except (ValueError, IndexError):
        raise ValueError(f"Invalid DL name format: {dl_name}")

    if not (1 <= dl_num <= 20):
        raise ValueError(f"DL number out of range (1-20): {dl_name}")

    if dl_num <= 10:
        section      = "Rack1"
        building_num = dl_num
    else:
        section      = "Rack2"
        building_num = dl_num - 10

    return section, f"Building {building_num}"


# =========================================================
# Read Data.ini
# =========================================================
def read_ini() -> configparser.RawConfigParser:
    """
    Read Data.ini and return parser object.
    Uses RawConfigParser to preserve exact values (Checked/Not check).
    """
    ini_path = get_config()["paths"]["data_ini"]

    if not os.path.exists(ini_path):
        raise FileNotFoundError(f"Data.ini not found: {ini_path}")

    parser = configparser.RawConfigParser()
    # Preserve key case exactly as written in the file
    parser.optionxform = str
    parser.read(ini_path, encoding="utf-8")
    return parser


# =========================================================
# Write Data.ini
# =========================================================
def write_ini(parser: configparser.RawConfigParser) -> None:
    """Write updated parser back to Data.ini."""
    ini_path = get_config()["paths"]["data_ini"]
    with open(ini_path, "w", encoding="utf-8") as f:
        parser.write(f)
    logger.info(f"[ini_editor] Data.ini saved → {ini_path}")


# =========================================================
# Uncheck a building for a given DL
# =========================================================
def uncheck_dl(dl_name: str) -> bool:
    """
    Set the Building entry for this DL from 'Checked' to 'Not check'
    in Data.ini and save the file.

    Returns:
        True  — successfully updated
        False — already unchecked or error
    """
    try:
        section, key = dl_to_ini_key(dl_name)
        parser       = read_ini()

        if not parser.has_section(section):
            logger.error(
                f"[ini_editor] Section [{section}] not found in Data.ini"
            )
            return False

        if not parser.has_option(section, key):
            logger.error(
                f"[ini_editor] Key '{key}' not found in [{section}]"
            )
            return False

        current_value = parser.get(section, key).strip()

        if current_value == "Not check":
            logger.info(
                f"[ini_editor] {dl_name} [{section}] {key} "
                f"already 'Not check' — no change needed"
            )
            return False

        # Update value
        parser.set(section, key, "Not check")
        write_ini(parser)

        logger.info(
            f"[ini_editor] {dl_name} → [{section}] {key}: "
            f"'Checked' → 'Not check'"
        )
        return True

    except FileNotFoundError as e:
        logger.error(f"[ini_editor] {e}")
        return False
    except ValueError as e:
        logger.error(f"[ini_editor] {e}")
        return False
    except Exception as e:
        logger.error(f"[ini_editor] Unexpected error for {dl_name}: {e}")
        return False


# =========================================================
# Check a building (re-enable after restart if needed)
# =========================================================
def check_dl(dl_name: str) -> bool:
    """
    Set the Building entry for this DL from 'Not check' to 'Checked'.
    Useful if operator wants to re-enable a DL from Main PC.

    Returns:
        True  — successfully updated
        False — already checked or error
    """
    try:
        section, key = dl_to_ini_key(dl_name)
        parser       = read_ini()

        if not parser.has_section(section):
            logger.error(
                f"[ini_editor] Section [{section}] not found in Data.ini"
            )
            return False

        if not parser.has_option(section, key):
            logger.error(
                f"[ini_editor] Key '{key}' not found in [{section}]"
            )
            return False

        current_value = parser.get(section, key).strip()

        if current_value == "Checked":
            logger.info(
                f"[ini_editor] {dl_name} [{section}] {key} "
                f"already 'Checked' — no change needed"
            )
            return False

        parser.set(section, key, "Checked")
        write_ini(parser)

        logger.info(
            f"[ini_editor] {dl_name} → [{section}] {key}: "
            f"'Not check' → 'Checked'"
        )
        return True

    except FileNotFoundError as e:
        logger.error(f"[ini_editor] {e}")
        return False
    except Exception as e:
        logger.error(f"[ini_editor] Unexpected error for {dl_name}: {e}")
        return False
