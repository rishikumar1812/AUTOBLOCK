import os
import re
import logging
import tempfile
import configparser
from config_loader import get_config

# Use the SAME logger name ("Process") that main_pc_popup.py
# configures with a DailyFileHandler writing to
# Process_YYYY-MM-DD.log. Previously this used a private
# "ini_editor" logger name with no handler ever attached to it
# anywhere, so these Data.ini uncheck/save messages were silently
# discarded — they never reached any log file.
logger = logging.getLogger("Process")


class IniEditError(Exception):
    """
    Raised for GENUINE Data.ini failures — file not found (and no
    backup to recover from), section missing, key missing, content
    that fails validation, a write that could not be confirmed on
    disk, or any other unexpected read/write error.

    Deliberately NOT raised for "already at the target value, no
    change needed" — that's a normal, harmless outcome, still
    signaled by returning False like before.

    This distinction matters: without it, "Data.ini not found" and
    "already correct" both just return False, and the caller
    (inline_automation.run_stop_sequence) would treat them
    identically — logging a warning and CONTINUING the automation
    anyway, straight on to clicking SETUP/START and restarting the
    line. That means a missing/misconfigured/corrupt Data.ini
    silently fails to disable the blocked site, but the line
    restarts with that site still active. Callers need to catch
    IniEditError specifically and ABORT rather than proceed when
    it's raised — see run_stop_sequence()'s handling of this
    exception. Every failure mode below (missing file, corruption,
    failed validation, failed atomic write, failed post-write
    confirmation) raises this SAME exception type, so the existing
    abort-on-IniEditError control flow in inline_automation.py needs
    NO changes to pick up all of this reliability hardening.
    """
    pass


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
# Reliability layer — paths, validation, backup, atomic write,
# TARGETED raw-text editing (no whole-file ConfigParser rewrite)
#
# Two separate problems, two separate fixes:
#
# PROBLEM 1 — plain open(path, "w") truncates the file to zero bytes
# BEFORE the new content is written. If the process is killed, the
# machine loses power, or an exception fires between the truncate and
# the write completing (antivirus lock, disk full, USB/network drive
# hiccup, etc.), Data.ini is left EMPTY or half-written on disk.
#   FIX: every write goes to a temp file in the same directory first,
#   is fsync'd, validated, and only then atomically swapped onto
#   Data.ini via os.replace() (atomic on the same volume on both
#   Windows and POSIX). No reader — including InLine_Pro itself,
#   which may be polling the file — ever observes a truncated file.
#
# PROBLEM 2 — ConfigParser.write() re-serializes the ENTIRE file from
# its in-memory model. That silently rewrites every section (not just
# RACK1/RACK2), reformats spacing, and can misrepresent or drop
# anything ConfigParser doesn't model exactly as InLine_Pro wrote it
# (comment lines, key/value spacing style, keys outside the sections
# we care about, stray top-level values, etc.) — a real Data.ini may
# contain e.g. TempSensor/JIG/OtherValue lines that must survive an
# edit completely untouched, byte-for-byte.
#   FIX: Data.ini is read and kept as RAW TEXT throughout. Editing a
#   single key is done with a targeted line-by-line text replacement
#   (_replace_value_in_text below) that walks the raw lines, tracks
#   which [section] it's currently inside, and rewrites ONLY the one
#   matching "key = value" line it was asked to change — preserving
#   the exact original text of every other line (including its own
#   whitespace/formatting) verbatim. ConfigParser is still used, but
#   ONLY for read-only structural validation (does it parse? does it
#   have RACK1/RACK2? does the target key exist? what's its current
#   value?) — it is never used to reconstruct the file for writing.
#
# A validated Data.ini.bak is refreshed (from the CURRENT on-disk
# Data.ini, only if that current file itself still validates) before
# every write — so .bak always holds the last known-good RAW text,
# never a state we haven't confirmed sane. read_ini()/_load_ini_text()
# detect a missing/empty/corrupt Data.ini and automatically restore
# that validated backup's raw text instead of handing callers an
# empty/garbage file. If there is no valid backup to restore from,
# an IniEditError is raised instead of silently proceeding on an
# empty file — which is what would previously have let a caller
# "successfully" uncheck a key in a blank file and go on to
# SETUP/START.
# =========================================================

# Sections every real Data.ini must contain. This mirrors what every
# DL/FT mapping function above and in inline_automation.py actually
# reads/writes (RACK1/RACK2) — it's a deliberately minimal schema
# check (not enumerating every BUILDERn/FUNCTIONn key), so it stays
# correct even if the real file's key list evolves, and it says
# nothing about — so never touches — any other section or stray
# key/value line elsewhere in the file (TempSensor, JIG, etc.).
_REQUIRED_SECTIONS = ("RACK1", "RACK2")

# Matches a section header line: "[RACK1]", "[ RACK1 ]", etc.
_SECTION_RE = re.compile(r'^\s*\[\s*([^\]]+?)\s*\]\s*$')

# Matches a "key = value" (or "key=value") line and captures the
# pieces needed to rewrite ONLY the value while preserving the
# original key spelling and the original spacing around "=" and
# around the value, so the rewritten line looks exactly like the
# original file's own style. Lines starting with ';' or '#' (after
# leading whitespace) are comments in configparser's default full-line
# comment prefixes and are deliberately excluded from matching here
# ([^=;#\s] as the key's first character) so a commented-out key can
# never be mistaken for a live one.
_KV_RE = re.compile(r'^(\s*)([^=;#\s][^=]*?)(\s*=\s*)(.*?)(\s*)$')


def _ini_path() -> str:
    return get_config()["paths"]["data_ini"]


def _backup_path(ini_path: str) -> str:
    return ini_path + ".bak"


# ---------------------------------------------------------
# Raw text I/O
# ---------------------------------------------------------
def _read_text_file(path: str):
    """
    Returns the full raw text of `path`, or None if it doesn't exist
    or can't be read. Never raises.
    """
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            return f.read()
    except OSError as e:
        logger.error(f"[ini_editor] Failed to read {path}: {e}")
        return None


def _parse_text(text: str):
    """
    Parse raw ini `text` (read-only structural check — the result is
    NEVER written back out; see the module docstring above for why).
    Returns a RawConfigParser on success, or None if it fails to
    parse. Never raises.
    """
    if text is None:
        return None
    try:
        parser = configparser.RawConfigParser()
        parser.optionxform = str  # preserve key case
        parser.read_string(text)
        return parser
    except (configparser.Error, UnicodeDecodeError) as e:
        logger.error(f"[ini_editor][validate] Failed to parse ini text: {e}")
        return None


def _validate_parser(parser, source_label: str):
    """
    Minimal sanity check that `parser` looks like a real Data.ini,
    not an empty/corrupt/truncated one.

    Returns (True, "") if valid, or (False, reason) if not. Does not
    raise — callers decide what to do (attempt backup restore, abort,
    etc.) and are responsible for logging using the returned reason.
    """
    if parser is None:
        return False, f"{source_label} could not be parsed"

    sections = parser.sections()
    if not sections:
        return False, f"{source_label} has no sections at all (empty/corrupt file)"

    missing = [s for s in _REQUIRED_SECTIONS if s not in sections]
    if missing:
        return False, f"{source_label} is missing required section(s): {missing}"

    for section in _REQUIRED_SECTIONS:
        if not parser.options(section):
            return False, f"{source_label} section [{section}] has no keys at all"

    return True, ""


def _atomic_write_text(path: str, content: str) -> None:
    """
    Write `content` to `path` atomically: write to a temp file in the
    SAME directory (so os.replace() stays on one volume), fsync it to
    force the bytes to disk, then os.replace() it onto `path` in one
    atomic step. `path` is NEVER seen by any other reader in a
    truncated or partially-written state — either the old content is
    there, or the complete new content is there.
    """
    directory = os.path.dirname(path) or "."
    os.makedirs(directory, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(prefix=".dataini_", suffix=".tmp", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(content)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, path)  # atomic on Windows (NTFS) and POSIX
    except Exception:
        try:
            os.remove(tmp_path)
        except OSError:
            pass
        raise


# ---------------------------------------------------------
# Targeted single-line replacement in raw text
# ---------------------------------------------------------
def _replace_value_in_text(text: str, section: str, key: str, new_value: str):
    """
    Walk `text` line by line, track which [section] we're currently
    inside, and rewrite ONLY the one line "key = value" inside
    `section` that matches `key` exactly — replacing just its value,
    preserving that line's own original spacing style. Every other
    line (other keys, other sections, comments, blank lines, stray
    top-level values like TempSensor/JIG/OtherValue) is copied through
    completely unchanged, byte-for-byte, including original line
    endings.

    Returns (new_text, replaced: bool). replaced is False if `section`
    or `key` within it could not be found — callers should treat that
    as "nothing to edit" (this mirrors has_section()/has_option()
    checks the caller already performed beforehand).
    """
    lines = text.splitlines(keepends=True)
    in_target_section = False
    replaced = False
    out_lines = []

    for line in lines:
        # Strip only the line-ending for matching purposes; keep the
        # original ending bytes to re-attach so \n vs \r\n is preserved.
        stripped = line.splitlines()[0] if line.splitlines() else line
        line_ending = line[len(stripped):]

        section_match = _SECTION_RE.match(stripped)
        if section_match:
            in_target_section = (section_match.group(1).strip() == section)
            out_lines.append(line)
            continue

        if in_target_section and not replaced:
            kv_match = _KV_RE.match(stripped)
            if kv_match:
                leading_ws, found_key, sep, _old_val, trailing_ws = kv_match.groups()
                if found_key.strip() == key:
                    new_line = f"{leading_ws}{found_key}{sep}{new_value}{trailing_ws}{line_ending}"
                    out_lines.append(new_line)
                    replaced = True
                    continue

        out_lines.append(line)

    return "".join(out_lines), replaced


# ---------------------------------------------------------
# Backup maintenance / corruption recovery (operate on raw text)
# ---------------------------------------------------------
def _restore_from_backup(ini_path: str, reason: str) -> str:
    """
    Called when Data.ini at `ini_path` is missing, empty, or fails
    validation. Attempts to restore it from a validated Data.ini.bak.

    Returns the restored RAW TEXT on success (the backup's own
    original text, atomically written onto `ini_path` — never
    re-serialized).
    Raises IniEditError if there is no usable backup — this is the
    "never continue to SETUP/START unless validation succeeds" case:
    without a valid backup there is nothing safe to hand back to the
    caller, so the caller (via _set_key -> uncheck_dl/uncheck_ft)
    must abort rather than proceed on an empty/garbage file.
    """
    backup_path = _backup_path(ini_path)
    logger.error(
        f"[ini_editor][corruption] Data.ini unusable ({reason}) — "
        f"attempting recovery from backup: {backup_path}"
    )

    backup_text = _read_text_file(backup_path)
    backup_parser = _parse_text(backup_text)
    valid, why_invalid = _validate_parser(backup_parser, backup_path)
    if not valid:
        if backup_text is None:
            logger.critical(
                f"[ini_editor][recovery] No usable Data.ini.bak found at "
                f"{backup_path} — CANNOT auto-recover. Manual intervention "
                f"required: {ini_path} must be restored/repaired by an operator."
            )
            raise IniEditError(
                f"Data.ini is unusable ({reason}) and no valid backup exists "
                f"at {backup_path} — manual intervention required."
            )
        logger.critical(
            f"[ini_editor][recovery] Data.ini.bak at {backup_path} also "
            f"fails validation ({why_invalid}) — CANNOT auto-recover. "
            f"Manual intervention required."
        )
        raise IniEditError(
            f"Data.ini is unusable ({reason}) and Data.ini.bak is also "
            f"invalid ({why_invalid}) — manual intervention required."
        )

    try:
        _atomic_write_text(ini_path, backup_text)
    except OSError as e:
        logger.critical(
            f"[ini_editor][recovery] Failed to restore {ini_path} from "
            f"validated backup {backup_path}: {e}"
        )
        raise IniEditError(
            f"Found a valid backup but failed to restore it to {ini_path}: {e}"
        ) from e

    logger.warning(
        f"[ini_editor][recovery] Data.ini RESTORED from validated backup "
        f"{backup_path} → {ini_path}. A board/site check may be needed — "
        f"the restored file reflects the last known-good state, which may "
        f"be slightly stale versus what was about to be written."
    )

    restored_text = _read_text_file(ini_path)
    restored_parser = _parse_text(restored_text)
    valid, why_invalid = _validate_parser(restored_parser, ini_path)
    if not valid:
        # Should be unreachable (we just validated this exact text as
        # `backup_text`), but never hand back unvalidated content.
        logger.critical(
            f"[ini_editor][recovery] Post-restore re-read of {ini_path} "
            f"failed validation — aborting."
        )
        raise IniEditError(
            f"Data.ini restore from backup did not validate on re-read."
        )
    return restored_text


def _refresh_backup(ini_path: str) -> None:
    """
    Called at the START of every write, BEFORE the new content is
    written. If the CURRENT on-disk Data.ini is itself valid, copy its
    RAW TEXT (atomically) to Data.ini.bak, so .bak always holds the
    most recent known-good state verbatim. If the current file is
    missing or fails validation, the existing .bak (if any) is left
    untouched — we never let a bad state overwrite the one backup we
    can trust.
    """
    backup_path = _backup_path(ini_path)
    current_text = _read_text_file(ini_path)
    current_parser = _parse_text(current_text)
    valid, reason = _validate_parser(current_parser, ini_path)
    if not valid:
        if current_text is None:
            logger.warning(
                f"[ini_editor][backup] Current Data.ini at {ini_path} is "
                f"missing/unreadable — leaving existing {backup_path} "
                f"untouched (not refreshing backup from bad state)"
            )
        else:
            logger.warning(
                f"[ini_editor][backup] Current Data.ini failed validation "
                f"({reason}) — leaving existing {backup_path} untouched "
                f"(not refreshing backup from bad state)"
            )
        return

    try:
        _atomic_write_text(backup_path, current_text)
        logger.info(
            f"[ini_editor][backup] Data.ini.bak refreshed from current "
            f"valid Data.ini → {backup_path}"
        )
    except OSError as e:
        logger.warning(
            f"[ini_editor][backup] Failed to refresh {backup_path}: {e} "
            f"— leaving previous backup (if any) in place"
        )


def _load_ini_text() -> str:
    """
    Returns Data.ini's full RAW text, guaranteed to have passed
    structural validation (parses, has RACK1/RACK2, both non-empty) —
    auto-restoring from Data.ini.bak first if the current on-disk file
    is missing/corrupt/incomplete. Raises IniEditError if neither the
    current file nor the backup are usable.

    This is the ONLY function that decides "is Data.ini OK to edit"
    for the read side; _set_key() below builds its edit on top of the
    exact text this returns, never a ConfigParser re-serialization.
    """
    ini_path = _ini_path()
    text = _read_text_file(ini_path)
    parser = _parse_text(text)
    valid, reason = _validate_parser(parser, ini_path)
    if valid:
        return text

    if text is None:
        logger.warning(f"[ini_editor] Data.ini not found: {ini_path}")
        reason = "file missing"
    # Missing OR unreadable/corrupt/incomplete — try to recover from
    # backup. _restore_from_backup() raises IniEditError if it can't.
    return _restore_from_backup(ini_path, reason=reason)


def _write_ini_text(new_text: str) -> None:
    """
    Persist `new_text` (the FULL file content, already edited via
    targeted line replacement — see _replace_value_in_text) as the new
    Data.ini, with the reliability guarantees described in the module
    docstring: backup refresh first, validate before writing, atomic
    write, validate again after writing.
    """
    ini_path = _ini_path()

    # 1. Refresh Data.ini.bak from the CURRENT on-disk file, before
    #    touching it — so a known-good backup always exists going
    #    into this write.
    _refresh_backup(ini_path)

    # 2. Never write content we haven't validated ourselves, even
    #    though it's a byte-for-byte edit of already-validated text —
    #    this is the last line of defense against writing a malformed
    #    file (e.g. if the targeted edit somehow broke the section
    #    structure, which _replace_value_in_text is designed not to
    #    do, but we verify rather than assume).
    new_parser = _parse_text(new_text)
    valid, reason = _validate_parser(new_parser, "pending write")
    if not valid:
        logger.error(
            f"[ini_editor][validate] Refusing to write Data.ini — "
            f"edited content failed validation: {reason}"
        )
        raise IniEditError(
            f"Refusing to write invalid Data.ini content: {reason}"
        )

    # 3. Atomic write: temp file -> fsync -> os.replace(). Data.ini
    #    is never visible to any reader in a truncated state.
    try:
        _atomic_write_text(ini_path, new_text)
    except OSError as e:
        logger.error(f"[ini_editor] Atomic write to {ini_path} FAILED: {e}")
        raise IniEditError(f"Atomic write to Data.ini failed: {e}") from e

    # 4. Post-write validation — re-read what actually landed on disk
    #    (not just the in-memory copy) and confirm it still validates.
    #    Catches on-disk corruption from the write itself (disk full
    #    truncating the write differently than expected, filesystem
    #    quirks, etc.) that content-level validation above can't see.
    on_disk_text = _read_text_file(ini_path)
    on_disk_parser = _parse_text(on_disk_text)
    valid, reason = _validate_parser(on_disk_parser, ini_path)
    if not valid:
        logger.critical(
            f"[ini_editor][validate] Post-write validation of {ini_path} "
            f"FAILED: {reason} — Data.ini may now be in a bad state"
        )
        raise IniEditError(
            f"Data.ini failed validation immediately after writing: {reason}"
        )

    logger.info(f"[ini_editor] Data.ini saved & validated (targeted edit) → {ini_path}")


def read_ini() -> configparser.RawConfigParser:
    """
    Public, read-only convenience wrapper: returns a validated
    RawConfigParser view of Data.ini (auto-recovering from backup if
    needed), for callers that only want to inspect values. Internal
    edits (_set_key below) do NOT go through this — they operate on
    _load_ini_text()'s raw text directly, to avoid ever reconstructing
    the file via ConfigParser.write().
    """
    return _parse_text(_load_ini_text())


# =========================================================
# Shared set/get helper — used by both DL and FT functions
# =========================================================
def _set_key(label: str, section: str, key: str,
             new_value: str, skip_value: str) -> bool:
    """
    Set [section] key = new_value in Data.ini via a TARGETED raw-text
    line replacement (see _replace_value_in_text) — every other line
    in the file (other sections, other keys, comments, stray
    top-level values) is preserved byte-for-byte. ConfigParser is
    used only to validate structure and read the current value, never
    to reconstruct the file for writing.

    Skips (returns False) if value is already new_value — this is a
    normal, harmless outcome.

    Raises IniEditError for GENUINE failures (Data.ini not found and
    unrecoverable, corrupt with no valid backup, section missing, key
    missing, failed atomic write, failed validation, or failed
    post-write confirmation) instead of silently returning False for
    these too — a genuine failure here must stop the caller
    (run_stop_sequence) from proceeding to restart the line with the
    site still active, not be treated the same as "nothing needed to
    change."

    label: human-readable name for logging (e.g. 'DL06' or 'FT1 front')
    """
    try:
        text = _load_ini_text()
    except IniEditError:
        # _load_ini_text() already logged full detail (corruption,
        # backup attempt, why recovery failed) — just propagate.
        raise
    except Exception as e:
        logger.error(f"[ini_editor] {label} — could not read Data.ini: {e}")
        raise IniEditError(f"could not read Data.ini: {e}") from e

    try:
        parser = _parse_text(text)  # already validated by _load_ini_text()

        if not parser.has_section(section):
            msg = f"{label} — section [{section}] not found in Data.ini"
            logger.error(f"[ini_editor] {msg}")
            raise IniEditError(msg)

        if not parser.has_option(section, key):
            msg = f"{label} — key '{key}' not found in [{section}]"
            logger.error(f"[ini_editor] {msg}")
            raise IniEditError(msg)

        current = parser.get(section, key).strip()

        if current == skip_value:
            logger.info(
                f"[ini_editor] {label} — [{section}] {key} "
                f"already '{skip_value}' — no change needed"
            )
            return False

        old_value = current

        new_text, replaced = _replace_value_in_text(text, section, key, new_value)
        if not replaced:
            # has_section()/has_option() above already confirmed this
            # key exists, so this should be unreachable — but never
            # write anything if the targeted edit didn't actually
            # find the line it was supposed to change.
            msg = (
                f"{label} — targeted edit could not locate the line for "
                f"[{section}] {key} in Data.ini (unexpected — aborting "
                f"without writing)"
            )
            logger.error(f"[ini_editor] {msg}")
            raise IniEditError(msg)

        _write_ini_text(new_text)   # raises IniEditError on any reliability failure

        # Final semantic confirmation: re-read what's on disk and
        # confirm THIS specific key now holds new_value, AND that
        # nothing else in the file changed except that one line.
        # _write_ini_text() already confirms the file as a whole is
        # structurally valid (has RACK1/RACK2 etc.) — this additionally
        # confirms the actual edit we intended took effect, closing
        # the loop on "validate after every update".
        confirm_text = _read_text_file(_ini_path())
        confirm_parser = _parse_text(confirm_text)
        confirmed = None
        if confirm_parser is not None and confirm_parser.has_option(section, key):
            confirmed = confirm_parser.get(section, key).strip()

        if confirmed != new_value:
            msg = (
                f"{label} — post-write confirmation FAILED: expected "
                f"[{section}] {key} = '{new_value}', but Data.ini on disk "
                f"shows {confirmed!r}"
            )
            logger.critical(f"[ini_editor][validate] {msg}")
            raise IniEditError(msg)

        if confirm_text != new_text:
            # Belt-and-suspenders: the file that landed on disk isn't
            # byte-identical to what we computed in memory (would only
            # happen from some external process writing Data.ini
            # concurrently). The value we care about is still correct
            # (checked above), so this is a warning, not an abort.
            logger.warning(
                f"[ini_editor] {label} — Data.ini on disk differs from "
                f"the exact text this write intended (possible "
                f"concurrent writer) — the target key confirmed correct, "
                f"but review Data.ini if this recurs"
            )

        logger.info(
            f"[ini_editor] {label} — [{section}] {key}: "
            f"'{old_value}' → '{new_value}' (confirmed on disk; only this "
            f"line changed)"
        )
        return True

    except IniEditError:
        raise
    except Exception as e:
        msg = f"{label} — unexpected error writing Data.ini: {e}"
        logger.error(f"[ini_editor] {msg}")
        raise IniEditError(msg) from e


# =========================================================
# DL functions
# =========================================================
def uncheck_dl(dl_name: str) -> bool:
    """Set DL building to NOT_CHECK in Data.ini."""
    try:
        section, key = dl_to_ini_key(dl_name)
    except ValueError as e:
        logger.error(f"[ini_editor] {e}")
        raise IniEditError(str(e)) from e
    return _set_key(dl_name, section, key, "NOT_CHECK", "NOT_CHECK")


def check_dl(dl_name: str) -> bool:
    """Set DL building back to CHECK in Data.ini."""
    try:
        section, key = dl_to_ini_key(dl_name)
    except ValueError as e:
        logger.error(f"[ini_editor] {e}")
        raise IniEditError(str(e)) from e
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
        raise IniEditError(str(e)) from e
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
        raise IniEditError(str(e)) from e
    label = f"FT{ft_num} {ft_side}"
    return _set_key(label, section, key, "CHECK", "CHECK")
