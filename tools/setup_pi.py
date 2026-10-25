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
MODULES_LOAD_PATH = Path("/etc/modules-load.d/i2c-dev.conf")
MODEL_PATH = Path("/proc/device-tree/model")
SPI_BUFSIZ = 8192
DEFAULT_I2C_SPEED = 100_000
DEFAULT_I2C_DELAY_US = 2
DEFAULT_I2C_BUS = 8
CORE_CLOCK_KEYS = {"core_freq", "core_freq_min", "core_freq_fixed"}


class SetupError(RuntimeError):
    """Configuration cannot be changed safely."""


@dataclass(frozen=True)
class SetupOptions:
    i2c_mode: str = "sw"
    i2c_speed: int = DEFAULT_I2C_SPEED
    i2c_delay_us: int = DEFAULT_I2C_DELAY_US
    i2c_bus: int = DEFAULT_I2C_BUS
    keep_spi_bufsiz: bool = False
    overwrite_existing: bool = False
    fix_core_clock: bool = False


@dataclass(frozen=True)
class FileText:
    path: Path
    text: str
    lines: list[str]
    newline: str
    trailing_newline: bool
    mode: int
    existed: bool = True

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
    backups: tuple[Path | None, ...]
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
    parser.add_argument(
        "--i2c-mode",
        choices=("hw", "sw"),
        default=argparse.SUPPRESS,
        help="override the Raspberry Pi model default",
    )
    parser.add_argument(
        "--i2c-speed",
        type=_bounded_int("I2C speed", 1_000, 1_000_000),
        default=argparse.SUPPRESS,
        metavar="HZ",
        help=f"hardware I2C frequency (default: {DEFAULT_I2C_SPEED})",
    )
    parser.add_argument(
        "--i2c-delay-us",
        type=_bounded_int("I2C delay", 1, 1_000),
        default=argparse.SUPPRESS,
        metavar="N",
        help=f"software I2C bit delay in microseconds (default: {DEFAULT_I2C_DELAY_US})",
    )
    parser.add_argument(
        "--i2c-bus",
        type=_bounded_int("I2C bus", 0, 255),
        default=argparse.SUPPRESS,
        metavar="N",
        help=f"software I2C bus number (default: {DEFAULT_I2C_BUS})",
    )
    parser.add_argument(
        "--keep-spi-bufsiz",
        action="store_true",
        help="leave the spidev.bufsiz kernel parameter unchanged",
    )
    parser.add_argument(
        "--keep-core-clock",
        action="store_true",
        help="leave core clock settings unchanged (for testing)",
    )
    parser.add_argument(
        "--overwrite-existing",
        action="store_true",
        help="replace existing I2C, SPI, core clock, spidev buffer and module-load settings",
    )
    return parser


def _read_pi_model(path: Path = MODEL_PATH) -> str:
    try:
        raw = Path(path).read_bytes()
        model = raw.rstrip(b"\0\r\n").decode("utf-8").strip()
    except (OSError, UnicodeDecodeError) as exc:
        raise SetupError(f"Cannot identify Raspberry Pi model from {path}: {exc}") from exc
    if not model:
        raise SetupError(f"Raspberry Pi model in {path} is empty")
    return model


def _model_default_i2c_mode(model: str) -> str:
    if re.match(r"^Raspberry Pi 5(?:\s|$)", model, re.IGNORECASE):
        return "hw"
    if re.match(
        r"^Raspberry Pi (?:Zero(?:\s+2)?(?:\s+W|\s+WH)?|[1-4](?:\s|$)|"
        r"400(?:\s|$)|Compute Module [1-4](?:\s|$)|Compute Module(?:\s+Rev|$)|"
        r"Model\s+[AB](?:\s|$))",
        model,
        re.IGNORECASE,
    ):
        return "sw"
    raise SetupError(
        f"Unsupported or unknown Raspberry Pi model {model!r}; specify "
        "--i2c-mode explicitly"
    )


def _options_from_namespace(
    namespace: argparse.Namespace, model_path: Path = MODEL_PATH
) -> SetupOptions:
    explicit_mode = hasattr(namespace, "i2c_mode")
    try:
        model_mode = _model_default_i2c_mode(_read_pi_model(model_path))
    except SetupError:
        if not explicit_mode:
            raise
        model_mode = None
    mode = namespace.i2c_mode if explicit_mode else model_mode
    explicit_speed = hasattr(namespace, "i2c_speed")
    explicit_delay = hasattr(namespace, "i2c_delay_us")
    explicit_bus = hasattr(namespace, "i2c_bus")
    if mode == "hw" and (explicit_delay or explicit_bus):
        raise SetupError("--i2c-delay-us and --i2c-bus are valid only in software I2C mode")
    if mode == "sw" and explicit_speed:
        raise SetupError("--i2c-speed is valid only in hardware I2C mode")
    return SetupOptions(
        i2c_mode=mode,
        i2c_speed=getattr(namespace, "i2c_speed", DEFAULT_I2C_SPEED),
        i2c_delay_us=getattr(namespace, "i2c_delay_us", DEFAULT_I2C_DELAY_US),
        i2c_bus=getattr(namespace, "i2c_bus", DEFAULT_I2C_BUS),
        keep_spi_bufsiz=namespace.keep_spi_bufsiz,
        overwrite_existing=namespace.overwrite_existing,
        fix_core_clock=model_mode == "sw" and not namespace.keep_core_clock,
    )


def _validate_options(options: SetupOptions) -> None:
    if options.i2c_mode not in {"hw", "sw"}:
        raise SetupError("I2C mode must be 'hw' or 'sw'")
    if not 1_000 <= options.i2c_speed <= 1_000_000:
        raise SetupError("I2C speed must be between 1000 and 1000000 Hz")
    if not 1 <= options.i2c_delay_us <= 1_000:
        raise SetupError("I2C delay must be between 1 and 1000 microseconds")
    if not 0 <= options.i2c_bus <= 255:
        raise SetupError("I2C bus must be between 0 and 255")


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


def _read_modules_load(path: Path) -> FileText:
    if path.is_symlink():
        raise SetupError(f"Expected a regular file or missing path at {path}")
    if path.exists():
        return _read_text(path)
    if not path.parent.is_dir():
        raise SetupError(f"Expected an existing directory at {path.parent}")
    return FileText(path, "", [], "\n", True, 0o644, existed=False)


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


def _i2c_gpio_parameters(
    overlay: tuple[str, list[str], str], line_number: int
) -> tuple[tuple[str, str], ...]:
    parameters: list[tuple[str, str]] = []
    seen: set[str] = set()
    for parameter in overlay[1][1:]:
        key, separator, value = parameter.partition("=")
        key = key.strip().lower()
        if not separator or not key or key in seen:
            raise SetupError(
                f"Malformed or duplicate i2c-gpio parameter in config.txt line "
                f"{line_number}"
            )
        seen.add(key)
        parameters.append((key, value.strip().lower()))
    return tuple(parameters)


def _managed_i2c_gpio(parameters: tuple[tuple[str, str], ...]) -> bool:
    values = dict(parameters)
    return values.get("i2c_gpio_sda") == "2" and values.get("i2c_gpio_scl") == "3"


def _config_entries(lines: list[str], options: SetupOptions) -> list[ConfigEntry]:
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
        if inactive:
            continue
        clock = re.match(r"^\s*(core_freq(?:_min|_fixed)?)\s*=\s*(.*?)\s*$", code, re.IGNORECASE)
        if clock and options.fix_core_clock:
            if not active:
                raise SetupError(
                    f"Relevant core clock setting in conditional [{section}] at "
                    f"config.txt line {index + 1} is ambiguous; edit it manually"
                )
            entries.append(ConfigEntry(index, "clock", clock.group(1).lower(), clock.group(2)))
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
        relevant_overlay = False
        overlay_parameters: tuple[tuple[str, str], ...] = ()
        if overlay_name == "i2c-gpio":
            overlay_parameters = _i2c_gpio_parameters(overlay, index + 1)
            parameter_values = dict(overlay_parameters)
            sda = parameter_values.get("i2c_gpio_sda")
            scl = parameter_values.get("i2c_gpio_scl")
            if (sda in {"2", "3"} or scl in {"2", "3"}) and not (
                sda == "2" and scl == "3"
            ):
                raise SetupError(
                    f"Ambiguous i2c-gpio use of GPIO2/3 in config.txt line {index + 1}"
                )
            relevant_overlay = _managed_i2c_gpio(overlay_parameters)
            if options.i2c_mode == "sw" and not relevant_overlay:
                try:
                    overlay_bus = int(parameter_values.get("bus", ""), 10)
                except ValueError:
                    overlay_bus = None
                if overlay_bus == options.i2c_bus:
                    raise SetupError(
                        f"Software I2C bus {options.i2c_bus} is already used by an "
                        f"i2c-gpio overlay in config.txt line {index + 1}"
                    )
        ambiguous_spi_overlay = overlay_name.startswith("spi0-")
        if (relevant_tokens or relevant_overlay or ambiguous_spi_overlay) and not active:
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
            entries.append(
                ConfigEntry(index, "overlay", "i2c-gpio", None, overlay_parameters)
            )
    return entries


def _boolean(value: str | None) -> bool | None:
    if value in {None, "on", "true", "yes", "1"}:
        return True
    if value in {"off", "false", "no", "0"}:
        return False
    return None


def _config_matches(entries: list[ConfigEntry], options: SetupOptions) -> bool:
    clocks = [entry for entry in entries if entry.kind == "clock"]
    if options.fix_core_clock and (
        len(clocks) != 1 or clocks[0].key != "core_freq_fixed" or clocks[0].value != "1"
    ):
        return False
    values: dict[str, list[str | None]] = {}
    overlays: list[ConfigEntry] = []
    for entry in entries:
        if entry.kind == "clock":
            continue
        elif entry.kind == "overlay":
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


def _remove_relevant_config(lines: list[str], options: SetupOptions) -> list[str]:
    result: list[str] = []
    section = "global"
    for index, line in enumerate(lines):
        section_match = re.match(r"^\s*\[([^]]+)]\s*(?:#.*)?$", line)
        if section_match:
            section = section_match.group(1).strip().lower()
            result.append(line)
            continue
        if section not in {"global", "all"}:
            result.append(line)
            continue
        code, comment = _code_and_comment(line)
        if options.fix_core_clock and code.strip().partition("=")[0].strip().lower() in CORE_CLOCK_KEYS:
            preserved = _comment_only(line, comment)
            if preserved is not None:
                result.append(preserved)
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
        if overlay and overlay[1] and overlay[1][0].lower() == "i2c-gpio" and (
            _managed_i2c_gpio(_i2c_gpio_parameters(overlay, index + 1))
        ):
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
        result.extend(
            [
                "dtparam=i2c_arm=on",
                f"dtparam=i2c_arm_baudrate={options.i2c_speed}",
            ]
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
    if options.fix_core_clock:
        result.append("core_freq_fixed=1")
    return result


def _plan_config(file: FileText, options: SetupOptions) -> str:
    entries = _config_entries(file.lines, options)
    if entries and not options.overwrite_existing:
        details = "; ".join(
            f"{file.path}: line {line + 1}: {file.lines[line].strip()}"
            for line in sorted({entry.line for entry in entries})
        )
        raise SetupError(
            f"Existing relevant boot setting(s): {details}; use "
            "--overwrite-existing to replace them"
        )
    if entries and _config_matches(entries, options):
        return file.text
    lines = _remove_relevant_config(file.lines, options) if entries else list(file.lines)
    planned_lines = _append_config(lines, options)
    if not _config_matches(_config_entries(planned_lines, options), options):
        raise SetupError(f"Generated configuration for {file.path} failed validation")
    return file.render(planned_lines)


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
    if relevant and not options.overwrite_existing:
        raise SetupError(
            f"Existing relevant boot setting in {file.path}: line 1: "
            f"{tokens[relevant[0]]}; use "
            "--overwrite-existing to replace it"
        )
    if len(relevant) == 1 and tokens[relevant[0]].lower() == desired:
        return file.text
    tokens = [token for index, token in enumerate(tokens) if index not in relevant]
    tokens.append(desired)
    return file.render([" ".join(tokens)])


def _plan_modules_load(file: FileText, options: SetupOptions) -> str:
    active = [
        (index, line.strip())
        for index, line in enumerate(file.lines)
        if line.strip() and not line.lstrip().startswith("#")
    ]
    if [line for _, line in active] == ["i2c-dev"]:
        return file.text
    if file.existed and not options.overwrite_existing:
        detail = (
            f"line {active[0][0] + 1}: {active[0][1]}"
            if active
            else "no active module entry"
        )
        raise SetupError(
            f"Existing conflicting module-load configuration in {file.path}: "
            f"{detail}; use --overwrite-existing to replace it"
        )
    return "i2c-dev\n"


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
    modules_load_path: Path = MODULES_LOAD_PATH,
) -> SetupResult:
    _validate_options(options)
    config = _read_text(Path(config_path))
    cmdline = _read_text(Path(cmdline_path), one_line=True)
    modules_load = _read_modules_load(Path(modules_load_path))
    planned = {
        config.path: (config, _plan_config(config, options)),
        cmdline.path: (cmdline, _plan_cmdline(cmdline, options)),
        modules_load.path: (modules_load, _plan_modules_load(modules_load, options)),
    }
    changes = [path for path, (source, text) in planned.items() if text != source.text]
    backups: dict[Path, Path | None] = {}
    for path in changes:
        source, _ = planned[path]
        backups[path] = _backup(path) if source.existed else None
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
                backup = backups[path]
                if backup is None:
                    path.unlink(missing_ok=True)
                else:
                    _atomic_write(source, backup.read_bytes().decode("utf-8"))
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
    modules_load_path: Path = MODULES_LOAD_PATH,
    model_path: Path = MODEL_PATH,
) -> int:
    parser = argument_parser()
    namespace = parser.parse_args(argv)
    try:
        options = _options_from_namespace(namespace, model_path)
        result = configure_files(options, config_path, cmdline_path, modules_load_path)
    except SetupError as exc:
        parser.exit(2, f"setup_pi.py: error: {exc}\n")
    if result.changed:
        for path, backup in zip(result.changed, result.backups, strict=True):
            if backup is None:
                print(f"Created {path}")
            else:
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
    if namespace.keep_core_clock:
        print("Core clock: unchanged (--keep-core-clock)")
    else:
        print("Core clock: fixed at platform frequency" if options.fix_core_clock
              else "Core clock: unchanged (Pi 5 or unidentified platform)")
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
