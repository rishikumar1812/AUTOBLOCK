import os
import logging
import configparser
from config_loader import get_config

logger = logging.getLogger("ini_editor")


# =========================================================
# DL → ini key mapping
# DL01-DL10 → [RACK1] BUILDER1-BUILDER10
# DL11-DL20 → [RACK2] BUILDER1-BUILDER10
# =========================================================
def dl_to_ini_key(dl_name: str) -> tuple:
    try:
        dl_num = int(dl_name[2:])
    except (ValueError, IndexError):
        raise ValueError(f"Invalid DL name format: {dl_name}")

    if not (1 <= dl_num <= 20):
        raise ValueError(f"DL number out of range (1-20): {dl_name}")

    if dl_num <= 10:
        section     = "RACK1"
        builder_num = dl_num
    else:
        section     = "RACK2"
        builder_num = dl_num - 10

    return section, f"BUILDER{builder_num}"


# =========================================================
# FT → ini key mapping
# ft_side="front" → [RACK1] FUNCTION{ft_num}
# ft_side="rear"  → [RACK2] FUNCTION{ft_num}
# =========================================================
def ft_to_ini_key(ft_num: int, ft_side: str) -> tuple:
    """
    Map FT PC identity to Data.ini section + key.

    Examples:
        ft_num=1, ft_side="front" → ("RACK1", "FUNCTION1")
        ft_num=3, ft_side="rear"  → ("RACK2", "FUNCTION3")
    """
    side = ft_side.strip().lower()
    if side not in ("front", "rear"):
        raise ValueError(
            f"Invalid ft_side '{ft_side}' — must be 'front' or 'rear'"
        )
    if not (1 <= ft_num <= 8):
        raise ValueError(
            f"FT number out of range (1-8): {ft_num}"
        )

    section = "RACK1" if side == "front" else "RACK2"
    return section, f"FUNCTION{ft_num}"


# =========================================================
# Read / write Data.ini
# =========================================================
def read_ini() -> configparser.RawConfigParser:
    ini_path = get_config()["paths"]["data_ini"]
    if not os.path.exists(ini_path):
        raise FileNotFoundError(f"Data.ini not found: {ini_path}")
    parser = configparser.RawConfigParser()
    parser.optionxform = str   # preserve key case
    parser.read(ini_path, encoding="utf-8")
    return parser


def write_ini(parser: configparser.RawConfigParser) -> None:
    ini_path = get_config()["paths"]["data_ini"]
    with open(ini_path, "w", encoding="utf-8") as f:
        parser.write(f)
    logger.info(f"[ini_editor] Data.ini saved → {ini_path}")


# =========================================================
# Shared set/get helper — used by both DL and FT functions
# =========================================================
def _set_key(label: str, section: str, key: str,
             new_value: str, skip_value: str) -> bool:
    """
    Read Data.ini, set [section] key = new_value.
    Skips (returns False) if value is already new_value.
    Returns True if file was actually updated.
    label: human-readable name for logging (e.g. 'DL06' or 'FT1 front')
    """
    try:
        parser = read_ini()

        if not parser.has_section(section):
            logger.error(
                f"[ini_editor] {label} — section [{section}] "
                f"not found in Data.ini"
            )
            return False

        if not parser.has_option(section, key):
            logger.error(
                f"[ini_editor] {label} — key '{key}' not found "
                f"in [{section}]"
            )
            return False

        current = parser.get(section, key).strip()

        if current == skip_value:
            logger.info(
                f"[ini_editor] {label} — [{section}] {key} "
                f"already '{skip_value}' — no change needed"
            )
            return False

        old_value = current
        parser.set(section, key, new_value)
        write_ini(parser)
        logger.info(
            f"[ini_editor] {label} — [{section}] {key}: "
            f"'{old_value}' → '{new_value}'"
        )
        return True

    except FileNotFoundError as e:
        logger.error(f"[ini_editor] {e}")
        return False
    except ValueError as e:
        logger.error(f"[ini_editor] {e}")
        return False
    except Exception as e:
        logger.error(f"[ini_editor] {label} — unexpected error: {e}")
        return False


# =========================================================
# DL functions
# =========================================================
def uncheck_dl(dl_name: str) -> bool:
    """Set DL building to NOT_CHECK in Data.ini."""
    try:
        section, key = dl_to_ini_key(dl_name)
    except ValueError as e:
        logger.error(f"[ini_editor] {e}")
        return False
    return _set_key(dl_name, section, key, "NOT_CHECK", "NOT_CHECK")


def check_dl(dl_name: str) -> bool:
    """Set DL building back to CHECK in Data.ini."""
    try:
        section, key = dl_to_ini_key(dl_name)
    except ValueError as e:
        logger.error(f"[ini_editor] {e}")
        return False
    return _set_key(dl_name, section, key, "CHECK", "CHECK")


# =========================================================
# FT functions
# =========================================================
def uncheck_ft(ft_num: int, ft_side: str) -> bool:
    """
    Set FT function to NOT_CHECK in Data.ini.

    Called by inline_automation.run_stop_sequence() for FT tasks,
    same as uncheck_dl() is called for DL tasks.

    Example:
        uncheck_ft(1, "front")  →  [RACK1] FUNCTION1 = NOT_CHECK
        uncheck_ft(3, "rear")   →  [RACK2] FUNCTION3 = NOT_CHECK
    """
    try:
        section, key = ft_to_ini_key(ft_num, ft_side)
    except ValueError as e:
        logger.error(f"[ini_editor] {e}")
        return False
    label = f"FT{ft_num} {ft_side}"
    return _set_key(label, section, key, "NOT_CHECK", "NOT_CHECK")


def check_ft(ft_num: int, ft_side: str) -> bool:
    """
    Set FT function back to CHECK in Data.ini.

    Example:
        check_ft(1, "front")  →  [RACK1] FUNCTION1 = CHECK
        check_ft(3, "rear")   →  [RACK2] FUNCTION3 = CHECK
    """
    try:
        section, key = ft_to_ini_key(ft_num, ft_side)
    except ValueError as e:
        logger.error(f"[ini_editor] {e}")
        return False
    label = f"FT{ft_num} {ft_side}"
    return _set_key(label, section, key, "CHECK", "CHECK")
