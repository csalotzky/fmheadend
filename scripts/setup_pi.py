#!/usr/bin/env python3
"""Configure Raspberry Pi OS I2C and SPI interfaces for FMheadend."""

from __future__ import annotations

import argparse
import os
import re
import shutil
import stat
import tempfile
from dataclasses import dataclass
from pathlib import Path


CONFIG_PATH = Path("/boot/firmware/config.txt")
CMDLINE_PATH = Path("/boot/firmware/cmdline.txt")
SPI_BUFSIZ = 8192


class SetupError(RuntimeError):
    """Configuration cannot be changed safely."""


@dataclass(frozen=True)
class SetupOptions:
    i2c_mode: str = "hw"
    i2c_speed: int = 25_000
    i2c_delay_us: int = 2
    i2c_bus: int | None = None
    keep_spi_bufsiz: bool = False
    overwrite_existing: bool = False


@dataclass(frozen=True)
class FileText:
    path: Path
    text: str
    lines: list[str]
    newline: str
    trailing_newline: bool
    mode: int

    def render(self, lines: list[str]) -> str:
        result = self.newline.join(lines)
        if self.trailing_newline:
            result += self.newline
        return result


@dataclass(frozen=True)
class ConfigEntry:
    line: int
    kind: str
    key: str
    value: str | None
    parameters: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True)
class SetupResult:
    changed: tuple[Path, ...]
    backups: tuple[Path, ...]
    i2c_device: str

    @property
    def reboot_required(self) -> bool:
        return bool(self.changed)


def _bounded_int(name: str, minimum: int, maximum: int):
    def parse(value: str) -> int:
        try:
            parsed = int(value, 10)
        except ValueError as exc:
            raise argparse.ArgumentTypeError(f"{name} must be an integer") from exc
        if not minimum <= parsed <= maximum:
            raise argparse.ArgumentTypeError(
                f"{name} must be between {minimum} and {maximum}"
            )
        return parsed

    return parse


def argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Configure Raspberry Pi OS I2C and SPI for FMheadend"
    )
    parser.add_argument("--i2c-mode", choices=("hw", "sw"), default="hw")
    parser.add_argument(
        "--i2c-speed",
        type=_bounded_int("I2C speed", 1_000, 1_000_000),
        default=25_000,
        metavar="HZ",
        help="hardware I2C frequency (experimental default: %(default)s)",
    )
    parser.add_argument(
        "--i2c-delay-us",
        type=_bounded_int("I2C delay", 1, 1_000),
        default=2,
        metavar="N",
        help="software I2C bit delay in microseconds (default: %(default)s)",
    )
    parser.add_argument(
        "--i2c-bus",
        type=_bounded_int("I2C bus", 0, 255),
        metavar="N",
        help="software I2C bus number (required with --i2c-mode sw)",
    )
    parser.add_argument(
        "--keep-spi-bufsiz",
        action="store_true",
        help="leave the spidev.bufsiz kernel parameter unchanged",
    )
    parser.add_argument(
        "--overwrite-existing",
        action="store_true",
        help="replace existing I2C, SPI and spidev buffer settings",
    )
    return parser


def _validate_options(options: SetupOptions) -> None:
    if options.i2c_mode not in {"hw", "sw"}:
        raise SetupError("I2C mode must be 'hw' or 'sw'")
    if not 1_000 <= options.i2c_speed <= 1_000_000:
        raise SetupError("I2C speed must be between 1000 and 1000000 Hz")
    if not 1 <= options.i2c_delay_us <= 1_000:
        raise SetupError("I2C delay must be between 1 and 1000 microseconds")
    if options.i2c_bus is not None and not 0 <= options.i2c_bus <= 255:
        raise SetupError("I2C bus must be between 0 and 255")
    if options.i2c_mode == "sw" and options.i2c_bus is None:
        raise SetupError("--i2c-bus is required with --i2c-mode sw")
    if options.i2c_mode == "hw" and options.i2c_bus is not None:
        raise SetupError("--i2c-bus is valid only with --i2c-mode sw")


def _read_text(path: Path, *, one_line: bool = False) -> FileText:
    if path.is_symlink() or not path.is_file():
        raise SetupError(f"Expected a regular file at {path}")
    try:
        data = path.read_bytes()
        text = data.decode("utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise SetupError(f"Cannot read UTF-8 configuration file {path}: {exc}") from exc
    if "\0" in text:
        raise SetupError(f"NUL byte in {path}")
    without_crlf = text.replace("\r\n", "")
    if "\r" in without_crlf or ("\r\n" in text and "\n" in without_crlf):
        raise SetupError(f"Mixed or unsupported line endings in {path}")
    newline = "\r\n" if "\r\n" in text else "\n"
    trailing = text.endswith(newline)
    body = text[: -len(newline)] if trailing else text
    lines = body.split(newline) if body else []
    if one_line and (len(lines) != 1 or not lines[0].strip()):
        raise SetupError(f"{path} must contain exactly one non-empty line")
    mode = stat.S_IMODE(path.stat().st_mode)
    return FileText(path, text, lines, newline, trailing, mode)


def _code_and_comment(line: str) -> tuple[str, str]:
    if "#" not in line:
        return line, ""
    code, comment = line.split("#", 1)
    return code, "#" + comment


def _relevant_dtparam(key: str) -> bool:
    return key in {
        "i2c",
        "i2c_arm",
        "i2c_arm_baudrate",
        "i2c_gpio_sda",
        "i2c_gpio_scl",
        "i2c_gpio_delay_us",
        "spi",
    }


def _dtparam_parts(line: str) -> tuple[str, list[str], str, str] | None:
    code, comment = _code_and_comment(line)
    match = re.match(r"^(\s*dtparam\s*=\s*)(.*?)(\s*)$", code, re.IGNORECASE)
    if not match:
        return None
    return match.group(1), match.group(2).split(","), match.group(3), comment


def _overlay_parts(line: str) -> tuple[str, list[str], str] | None:
    code, comment = _code_and_comment(line)
    match = re.match(r"^\s*dtoverlay\s*=\s*(.*?)\s*$", code, re.IGNORECASE)
    if not match:
        return None
    return match.group(1), [part.strip() for part in match.group(1).split(",")], comment


def _config_entries(lines: list[str]) -> list[ConfigEntry]:
    entries: list[ConfigEntry] = []
    section = "global"
    for index, line in enumerate(lines):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        section_match = re.match(r"^\s*\[([^]]+)]\s*(?:#.*)?$", line)
        if section_match:
            section = section_match.group(1).strip().lower()
            continue
        if stripped.startswith("["):
            raise SetupError(f"Malformed conditional section in config.txt line {index + 1}")
        active = section in {"global", "all"}
        inactive = section == "none"
        code, _ = _code_and_comment(line)
        if not inactive and re.match(r"^\s*include\s+", code, re.IGNORECASE):
            raise SetupError(
                f"Include in config.txt line {index + 1} may contain relevant settings; "
                "edit it manually"
            )
        dtparam = _dtparam_parts(line)
        relevant_tokens: list[tuple[str, str | None]] = []
        if dtparam:
            for token in dtparam[1]:
                key, separator, value = token.strip().partition("=")
                key = key.lower()
                if _relevant_dtparam(key):
                    relevant_tokens.append((key, value.strip().lower() if separator else None))
        overlay = _overlay_parts(line)
        overlay_name = overlay[1][0].lower() if overlay and overlay[1] else ""
        relevant_overlay = overlay_name == "i2c-gpio"
        ambiguous_spi_overlay = overlay_name.startswith("spi0-")
        if (relevant_tokens or relevant_overlay or ambiguous_spi_overlay) and not active:
            if inactive:
                continue
            raise SetupError(
                f"Relevant setting in conditional [{section}] at config.txt line "
                f"{index + 1} is ambiguous"
            )
        if ambiguous_spi_overlay:
            raise SetupError(
                f"SPI0 overlay in config.txt line {index + 1} is ambiguous; edit it manually"
            )
        for key, value in relevant_tokens:
            entries.append(ConfigEntry(index, "dtparam", key, value))
        if relevant_overlay:
            parameters: list[tuple[str, str]] = []
            for parameter in overlay[1][1:]:
                key, separator, value = parameter.partition("=")
                if not separator or not key.strip():
                    raise SetupError(f"Malformed i2c-gpio overlay in config.txt line {index + 1}")
                parameters.append((key.strip().lower(), value.strip().lower()))
            entries.append(
                ConfigEntry(index, "overlay", "i2c-gpio", None, tuple(parameters))
            )
    return entries


def _boolean(value: str | None) -> bool | None:
    if value in {None, "on", "true", "yes", "1"}:
        return True
    if value in {"off", "false", "no", "0"}:
        return False
    return None


def _config_matches(entries: list[ConfigEntry], options: SetupOptions) -> bool:
    values: dict[str, list[str | None]] = {}
    overlays: list[ConfigEntry] = []
    for entry in entries:
        if entry.kind == "overlay":
            overlays.append(entry)
        else:
            values.setdefault(entry.key, []).append(entry.value)
    if any(len(items) != 1 for items in values.values()):
        return False
    if set(values) - {"i2c_arm", "i2c_arm_baudrate", "spi"}:
        return False
    if len(values.get("spi", [])) != 1 or _boolean(values["spi"][0]) is not True:
        return False
    if options.i2c_mode == "hw":
        return (
            not overlays
            and set(values) == {"i2c_arm", "i2c_arm_baudrate", "spi"}
            and _boolean(values["i2c_arm"][0]) is True
            and values["i2c_arm_baudrate"][0] == str(options.i2c_speed)
        )
    if set(values) != {"i2c_arm", "spi"} or _boolean(values["i2c_arm"][0]) is not False:
        return False
    if len(overlays) != 1:
        return False
    params = dict(overlays[0].parameters)
    return len(overlays[0].parameters) == 4 and params == {
        "bus": str(options.i2c_bus),
        "i2c_gpio_sda": "2",
        "i2c_gpio_scl": "3",
        "i2c_gpio_delay_us": str(options.i2c_delay_us),
    }


def _comment_only(line: str, comment: str) -> str | None:
    if not comment:
        return None
    indent = line[: len(line) - len(line.lstrip())]
    return indent + comment


def _remove_relevant_config(lines: list[str]) -> list[str]:
    result: list[str] = []
    section = "global"
    for line in lines:
        section_match = re.match(r"^\s*\[([^]]+)]\s*(?:#.*)?$", line)
        if section_match:
            section = section_match.group(1).strip().lower()
            result.append(line)
            continue
        if section not in {"global", "all"}:
            result.append(line)
            continue
        dtparam = _dtparam_parts(line)
        if dtparam:
            prefix, tokens, trailing, comment = dtparam
            kept = []
            for token in tokens:
                key = token.strip().partition("=")[0].lower()
                if not _relevant_dtparam(key):
                    kept.append(token)
            if len(kept) != len(tokens):
                if kept:
                    result.append(prefix + ",".join(kept) + trailing + comment)
                else:
                    preserved = _comment_only(line, comment)
                    if preserved is not None:
                        result.append(preserved)
                continue
        overlay = _overlay_parts(line)
        if overlay and overlay[1] and overlay[1][0].lower() == "i2c-gpio":
            preserved = _comment_only(line, overlay[2])
            if preserved is not None:
                result.append(preserved)
            continue
        result.append(line)
    return result


def _last_section(lines: list[str]) -> str:
    section = "global"
    for line in lines:
        match = re.match(r"^\s*\[([^]]+)]\s*(?:#.*)?$", line)
        if match:
            section = match.group(1).strip().lower()
    return section


def _append_config(lines: list[str], options: SetupOptions) -> list[str]:
    result = list(lines)
    if result and result[-1].strip():
        result.append("")
    if _last_section(result) not in {"global", "all"}:
        result.extend(["[all]", ""])
    if options.i2c_mode == "hw":
        result.append(
            f"dtparam=i2c_arm=on,i2c_arm_baudrate={options.i2c_speed}"
        )
    else:
        result.extend(
            [
                "dtparam=i2c_arm=off",
                "dtoverlay=i2c-gpio,"
                f"bus={options.i2c_bus},i2c_gpio_sda=2,i2c_gpio_scl=3,"
                f"i2c_gpio_delay_us={options.i2c_delay_us}",
            ]
        )
    result.append("dtparam=spi=on")
    return result


def _plan_config(file: FileText, options: SetupOptions) -> str:
    entries = _config_entries(file.lines)
    if entries and _config_matches(entries, options):
        return file.text
    if entries and not options.overwrite_existing:
        locations = ", ".join(
            str(line) for line in sorted({entry.line + 1 for entry in entries})
        )
        raise SetupError(
            "Existing I2C/SPI settings in config.txt line(s) "
            f"{locations}; use --overwrite-existing to replace them"
        )
    lines = _remove_relevant_config(file.lines) if entries else list(file.lines)
    return file.render(_append_config(lines, options))


def _plan_cmdline(file: FileText, options: SetupOptions) -> str:
    if options.keep_spi_bufsiz:
        return file.text
    tokens = file.lines[0].split()
    relevant = [
        index
        for index, token in enumerate(tokens)
        if re.match(r"^spidev\.bufsiz(?:=|$)", token, re.IGNORECASE)
    ]
    desired = f"spidev.bufsiz={SPI_BUFSIZ}"
    if len(relevant) == 1 and tokens[relevant[0]].lower() == desired:
        return file.text
    if relevant and not options.overwrite_existing:
        raise SetupError(
            "Existing spidev.bufsiz setting in cmdline.txt; use "
            "--overwrite-existing to replace it"
        )
    tokens = [token for index, token in enumerate(tokens) if index not in relevant]
    tokens.append(desired)
    return file.render([" ".join(tokens)])


def _backup(path: Path) -> Path:
    candidate = path.with_name(path.name + ".fmheadend.bak")
    number = 1
    while candidate.exists():
        candidate = path.with_name(path.name + f".fmheadend.bak.{number}")
        number += 1
    try:
        shutil.copy2(path, candidate)
    except OSError as exc:
        raise SetupError(f"Cannot back up {path}: {exc}") from exc
    return candidate


def _atomic_write(file: FileText, text: str) -> None:
    temporary: str | None = None
    try:
        descriptor, temporary = tempfile.mkstemp(
            prefix=f".{file.path.name}.", dir=file.path.parent
        )
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(text.encode("utf-8"))
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, file.mode)
        os.replace(temporary, file.path)
    except OSError as exc:
        if temporary:
            try:
                os.unlink(temporary)
            except OSError:
                pass
        raise SetupError(f"Cannot safely write {file.path}: {exc}") from exc


def configure_files(
    options: SetupOptions,
    config_path: Path = CONFIG_PATH,
    cmdline_path: Path = CMDLINE_PATH,
) -> SetupResult:
    _validate_options(options)
    config = _read_text(Path(config_path))
    cmdline = _read_text(Path(cmdline_path), one_line=True)
    planned = {
        config.path: (config, _plan_config(config, options)),
        cmdline.path: (cmdline, _plan_cmdline(cmdline, options)),
    }
    changes = [path for path, (source, text) in planned.items() if text != source.text]
    backups: dict[Path, Path] = {}
    for path in changes:
        backups[path] = _backup(path)
    written: list[Path] = []
    try:
        for path in changes:
            source, text = planned[path]
            _atomic_write(source, text)
            written.append(path)
    except SetupError as exc:
        rollback_errors = []
        for path in reversed(written):
            source, _ = planned[path]
            try:
                _atomic_write(source, backups[path].read_bytes().decode("utf-8"))
            except (OSError, SetupError) as rollback_exc:
                rollback_errors.append(f"{path}: {rollback_exc}")
        if rollback_errors:
            raise SetupError(
                f"{exc}; rollback also failed: {'; '.join(rollback_errors)}"
            ) from exc
        raise
    device = "/dev/i2c-1" if options.i2c_mode == "hw" else f"/dev/i2c-{options.i2c_bus}"
    return SetupResult(tuple(changes), tuple(backups[path] for path in changes), device)


def main(
    argv: list[str] | None = None,
    config_path: Path = CONFIG_PATH,
    cmdline_path: Path = CMDLINE_PATH,
) -> int:
    parser = argument_parser()
    namespace = parser.parse_args(argv)
    options = SetupOptions(
        i2c_mode=namespace.i2c_mode,
        i2c_speed=namespace.i2c_speed,
        i2c_delay_us=namespace.i2c_delay_us,
        i2c_bus=namespace.i2c_bus,
        keep_spi_bufsiz=namespace.keep_spi_bufsiz,
        overwrite_existing=namespace.overwrite_existing,
    )
    try:
        result = configure_files(options, config_path, cmdline_path)
    except SetupError as exc:
        parser.exit(2, f"setup_pi.py: error: {exc}\n")
    if result.changed:
        for path, backup in zip(result.changed, result.backups, strict=True):
            print(f"Updated {path} (backup: {backup})")
    else:
        print("Configuration already matches the requested settings; no files changed")
    if options.i2c_mode == "hw":
        print(f"I2C configuration: hardware GPIO2/3 at {options.i2c_speed} Hz")
    else:
        print(
            "I2C configuration: software GPIO2/3, "
            f"bus {options.i2c_bus}, delay {options.i2c_delay_us} us"
        )
    print("SPI0: enabled")
    print(
        "spidev.bufsiz: unchanged"
        if options.keep_spi_bufsiz
        else f"spidev.bufsiz: {SPI_BUFSIZ}"
    )
    print(f"I2C device: {result.i2c_device}")
    print(f"Reboot required: {'yes' if result.reboot_required else 'no'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
