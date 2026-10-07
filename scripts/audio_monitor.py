#!/usr/bin/env python3
"""
Live console monitor for fmheadend audio telemetry.

Shows only stages that expose useful runtime telemetry, in DSP-chain order.
No third-party dependencies.
"""

from __future__ import annotations

import argparse
import json
import math
import socket
import sys
import time
from collections import deque
from typing import Any

DEFAULT_SOCKET = "/run/fmheadend/audio-telemetry.sock"

# LSP Autogain UI display ranges.
AGC_LOUDNESS_MIN = -84.0
AGC_LOUDNESS_MAX = 24.0
AGC_GAIN_MIN = -84.0
AGC_GAIN_MAX = 84.0

# Display-only scale for dBFS meters (not a plugin-native range).
LEVEL_MIN_DBFS = -60.0
LEVEL_MAX_DBFS = 0.0

BAR_WIDTH = 28
AGC_BAR_WIDTH = 28
STEREO_BAR_WIDTH = 18
BAND_BAR_WIDTH = 8


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Live fmheadend DSP telemetry monitor")
    p.add_argument("--socket", default=DEFAULT_SOCKET)
    sel = p.add_mutually_exclusive_group()
    sel.add_argument("--tx", type=int, help="Follow the Source currently consumed by this TX")
    sel.add_argument("--source", type=int, help="Follow this Source ID")
    p.add_argument("--refresh-hz", type=float, default=20.0,
                   help="Maximum terminal redraw rate (default: 20 Hz)")
    return p.parse_args()


def lin_db(value: Any) -> float | None:
    if not isinstance(value, (int, float)) or not math.isfinite(value):
        return None
    if value <= 0:
        return float("-inf")
    return 20.0 * math.log10(value)


def finite(value: Any) -> float | None:
    if isinstance(value, (int, float)) and math.isfinite(value):
        return float(value)
    return None


def fmt_num(value: float | None, decimals: int = 2, width: int = 7) -> str:
    if value is None:
        return "n/a".rjust(width)
    if value == float("inf"):
        return "inf".rjust(width)
    if value == float("-inf"):
        return "-inf".rjust(width)
    if round(value, decimals) == 0:
        value = 0.0  # avoid "-0.00"
    return f"{value:{width}.{decimals}f}"


def fmt_db(value: float | None, unit: str = "dB") -> str:
    return f"{fmt_num(value, 2, 7)} {unit}"


def level_bar(value_db: float | None, lo: float, hi: float, width: int = BAR_WIDTH) -> str:
    if value_db is None or value_db == float("-inf") or math.isnan(value_db):
        filled = 0
    else:
        x = min(max(value_db, lo), hi)
        filled = round((x - lo) / (hi - lo) * width)
    return "[" + "#" * filled + "-" * (width - filled) + "]"


def centered_bar(value_db: float | None, lo: float, hi: float, width: int = BAR_WIDTH) -> str:
    left_width = width // 2
    right_width = width - left_width - 1
    left = ["-"] * left_width
    right = ["-"] * right_width

    if value_db is not None and value_db != float("-inf"):
        if value_db < 0 and lo < 0:
            n = round(min(abs(value_db) / abs(lo), 1.0) * left_width)
            for i in range(left_width - n, left_width):
                left[i] = "#"
        elif value_db > 0 and hi > 0:
            n = round(min(value_db / hi, 1.0) * right_width)
            for i in range(n):
                right[i] = "#"

    return "[" + "".join(left) + "|" + "".join(right) + "]"


def reduction_db(raw: Any) -> float | None:
    """Return positive dB of attenuation for a linear reduction multiplier."""
    db = lin_db(raw)
    if db is None:
        return None
    if db == float("-inf"):
        return float("inf")
    return -db


def stereo_row(label: str, left: float | None, right: float | None, lo: float, hi: float,
               unit: str = "dBFS", bar: int = STEREO_BAR_WIDTH, decimals: int = 2,
               width: int = 7, label_width: int = 6) -> str:
    """One line: label, L value+bar, R value+bar. Fixed columns."""
    u = f" {unit:<4}" if unit else ""

    def side(name: str, x: float | None) -> str:
        return f"{name} {fmt_num(x, decimals, width)}{u} {level_bar(x, lo, hi, bar)}"

    return f"{label:<{label_width}}{side('L', left)}  {side('R', right)}"


def level_row(label: str, raw_l: Any, raw_r: Any) -> str:
    """Stereo row for linear meter values, shown as dBFS."""
    return stereo_row(label, lin_db(raw_l), lin_db(raw_r), LEVEL_MIN_DBFS, LEVEL_MAX_DBFS)


def reduction_row(label: str, raw_l: Any, raw_r: Any, hi: float) -> str:
    return stereo_row(label, reduction_db(raw_l), reduction_db(raw_r), 0.0, hi, "dB")


def stage_values(source: dict[str, Any], name: str):
    stage = (source.get("stages") or {}).get(name)
    if not isinstance(stage, dict):
        return None, None
    values = stage.get("values")
    return stage, values if isinstance(values, dict) else {}


def section(title: str) -> str:
    return f"---- {title} ----"


def render_endpoint(source: dict[str, Any], key: str, title: str) -> list[str]:
    ep = source.get(key)
    ep = ep if isinstance(ep, dict) else {}
    # Endpoint peaks are already dBFS; do not convert again.
    left = finite(ep.get("peak_l_dbfs"))
    right = finite(ep.get("peak_r_dbfs"))
    return [section(title), stereo_row("peak", left, right, LEVEL_MIN_DBFS, LEVEL_MAX_DBFS)]


def render_input(source: dict[str, Any]) -> list[str]:
    return render_endpoint(source, "input", "INPUT")


def render_output(source: dict[str, Any]) -> list[str]:
    return render_endpoint(source, "output", "OUTPUT")


def render_gate(source: dict[str, Any]) -> list[str]:
    stage, v = stage_values(source, "gate")
    if stage is None:
        return []

    impl = stage.get("implementation", "GATE/EXPANDER")
    label = "MULTIBAND GATE" if impl == "LSP_MULTIBAND_GATE" else "MULTIBAND EXPANDER"
    return multiband_rows(v, label)


def render_agc(source: dict[str, Any]) -> list[str]:
    stage, v = stage_values(source, "agc")
    if stage is None:
        return []

    g = lin_db(v.get("g-g"))
    ins = lin_db(v.get("g-in-s"))
    inl = lin_db(v.get("g-in-l"))
    outs = lin_db(v.get("g-out-s"))
    outl = lin_db(v.get("g-out-l"))
    w = AGC_BAR_WIDTH

    return [
        section("AGC"),
        f"gain corr.   {fmt_db(g):>11}  {centered_bar(g, AGC_GAIN_MIN, AGC_GAIN_MAX, w)}",
        f"input short  {fmt_db(ins, 'LUFS'):>11}  {level_bar(ins, AGC_LOUDNESS_MIN, AGC_LOUDNESS_MAX, w)}",
        f"input long   {fmt_db(inl, 'LUFS'):>11}  {level_bar(inl, AGC_LOUDNESS_MIN, AGC_LOUDNESS_MAX, w)}",
        f"output short {fmt_db(outs, 'LUFS'):>11}  {level_bar(outs, AGC_LOUDNESS_MIN, AGC_LOUDNESS_MAX, w)}",
        f"output long  {fmt_db(outl, 'LUFS'):>11}  {level_bar(outl, AGC_LOUDNESS_MIN, AGC_LOUDNESS_MAX, w)}",
    ]


def render_eq(source: dict[str, Any]) -> list[str]:
    stage, v = stage_values(source, "eq")
    if stage is None:
        return []

    return [
        section("EQ"),
        level_row("in", v.get("iml"), v.get("imr")),
        level_row("out", v.get("sml"), v.get("smr")),
    ]


def render_wideband(source: dict[str, Any]) -> list[str]:
    stage, v = stage_values(source, "wideband")
    if stage is None:
        return []

    return [
        section("WIDEBAND COMP"),
        level_row("in", v.get("ilm-l"), v.get("ilm-r")),
        level_row("out", v.get("olm-l"), v.get("olm-r")),
        level_row("side", v.get("slm-l"), v.get("slm-r")),
        level_row("env", v.get("elm-l"), v.get("elm-r")),
        reduction_row("red", v.get("rlm-l"), v.get("rlm-r"), 24.0),
    ]


def render_multiband(source: dict[str, Any]) -> list[str]:
    stage, v = stage_values(source, "multiband")
    if stage is None:
        return []
    return multiband_rows(v, "MULTIBAND COMP")


def multiband_rows(v: dict[str, Any], label: str) -> list[str]:
    lines = [
        section(label),
        level_row("in", v.get("ilm-l"), v.get("ilm-r")),
        level_row("out", v.get("olm-l"), v.get("olm-r")),
    ]

    # Telemetry emits only the active bands. Discover them rather than assuming 3.
    bands = []
    for key in v:
        if key.startswith("elm-") and key.endswith("l"):
            middle = key[4:-1]
            if middle.isdigit():
                bands.append(int(middle))

    for band in sorted(set(bands)):
        env = stereo_row(
            f"B{band + 1} env",
            lin_db(v.get(f"elm-{band}l")), lin_db(v.get(f"elm-{band}r")),
            LEVEL_MIN_DBFS, LEVEL_MAX_DBFS, unit="", bar=BAND_BAR_WIDTH,
            decimals=1, width=6, label_width=7,
        )
        red = stereo_row(
            "red",
            reduction_db(v.get(f"rlm-{band}l")), reduction_db(v.get(f"rlm-{band}r")),
            0.0, 24.0, unit="", bar=BAND_BAR_WIDTH, decimals=1, width=5, label_width=4,
        )
        lines.append(f"{env}  {red}")

    return lines


class LimiterPeakHold:
    """Monitor-only maximum of received samples; one instance per connection."""

    def __init__(self):
        self.identity = None
        self.samples: deque[tuple[float, float]] = deque()

    def update(self, source: dict[str, Any] | None, now: float) -> float | None:
        identity = (source.get("source_id"), source.get("generation")) if source else None
        if identity != self.identity:
            self.samples.clear()
            self.identity = identity
        while self.samples and self.samples[0][0] < now - 1.0:
            self.samples.popleft()
        if not source or not source.get("telemetry_available"):
            self.samples.clear()
            return None
        stage, values = stage_values(source, "limiter")
        if stage is None or stage.get("implementation") != "FM_HEADEND_LIMITER":
            self.samples.clear()
            return None
        reduction = finite(values.get("gain-reduction-db"))
        if reduction is None:
            return None
        while self.samples and self.samples[-1][1] <= reduction:
            self.samples.pop()
        self.samples.append((now, reduction))
        return self.samples[0][1]


def render_limiter(source: dict[str, Any], max_1s: float | None = None) -> list[str]:
    stage, v = stage_values(source, "limiter")
    if stage is None:
        return []

    impl = stage.get("implementation")

    if impl == "FM_HEADEND_LIMITER":
        peak = finite(v.get("detector-peak-dbfs"))
        gr = finite(v.get("gain-reduction-db"))
        return [
            section("FM LIMITER"),
            f"detector  {fmt_db(peak, 'dBFS')} {level_bar(peak, LEVEL_MIN_DBFS, LEVEL_MAX_DBFS, AGC_BAR_WIDTH)}",
            f"reduction {fmt_db(gr, 'dB  ')} {level_bar(gr, 0.0, 24.0, AGC_BAR_WIDTH)}",
            f"max 1s    {fmt_db(max_1s)}",
        ]

    if impl == "LSP_LIMITER":
        return [
            section("LSP LIMITER"),
            level_row("in", v.get("ilm-l"), v.get("ilm-r")),
            level_row("out", v.get("olm-l"), v.get("olm-r")),
            level_row("side", v.get("sclm-l"), v.get("sclm-r")),
            reduction_row("red", v.get("grlm-l"), v.get("grlm-r"), 24.0),
        ]

    return [section(f"LIMITER ({impl or '?'})")]


def render_clipper(source: dict[str, Any]) -> list[str]:
    stage, v = stage_values(source, "clipper")
    if stage is None:
        return []

    peak = finite(v.get("detector-peak-dbfs"))
    max_clip = finite(v.get("max-clipping-db"))
    max_10s = finite(v.get("max-clipping-db-10s"))
    count = v.get("clip-count")

    max_text = f"{max_clip:.2f} dB" if max_clip is not None else "n/a"
    max_10s_text = f"{max_10s:.2f} dB" if max_10s is not None else "n/a"
    count_text = str(count) if isinstance(count, int) and not isinstance(count, bool) else "n/a"

    return [
        section("FM CLIPPER"),
        f"detector  {fmt_db(peak, 'dBFS')} {level_bar(peak, LEVEL_MIN_DBFS, LEVEL_MAX_DBFS, AGC_BAR_WIDTH)}",
        f"clips     {count_text:>7}",
        f"max 10s   {max_10s_text}",
        f"max ever  {max_text}",
    ]

def select_source(record: dict[str, Any], tx: int | None, source_id: int | None):
    sources = record.get("sources")
    if not isinstance(sources, list):
        return None, "Malformed record: missing sources[]"

    if source_id is not None:
        for source in sources:
            if source.get("source_id") == source_id:
                return source, None
        return None, f"Source {source_id} is not active"

    if tx is not None:
        for source in sources:
            if tx in source.get("consumer_tx_ids", []):
                return source, None
        return None, f"TX {tx} currently has no telemetry Source"

    active = [s for s in sources if s.get("telemetry_available")]
    if len(active) == 1:
        return active[0], None
    if not active:
        return None, "No active telemetry Source"
    ids = ", ".join(str(s.get("source_id")) for s in active)
    return None, f"Multiple active Sources ({ids}); use --tx or --source"


def render(source: dict[str, Any], sample_interval_ms: float | None, limiter_max_1s: float | None = None) -> str:
    sid = source.get("source_id", "?")
    name = source.get("source_name", "?")
    txs = source.get("consumer_tx_ids", [])
    generation = source.get("generation", "?")

    if not source.get("telemetry_available"):
        return "\n".join([
            "fmheadend DSP monitor",
            f"Source {sid}: {name}   TX: {txs}   generation: {generation}",
            "",
            "Telemetry unavailable",
        ])

    collect_ns = source.get("collect_duration_ns")
    collect_ms = collect_ns / 1_000_000 if isinstance(collect_ns, (int, float)) else None
    sample_text = f"{sample_interval_ms:5.2f} ms" if sample_interval_ms is not None else " n/a"
    collect_text = f"{collect_ms:5.3f} ms" if collect_ms is not None else " n/a"

    lines = [
        "fmheadend DSP monitor",
        f"Source {sid}: {name}   TX: {txs}   generation: {generation}",
        f"sample delta: {sample_text}   collect: {collect_text}",
        "",
    ]

    renderers = (
        render_input,
        render_gate,
        render_agc,
        render_eq,
        render_wideband,
        render_multiband,
        render_limiter,
        render_clipper,
        render_output,
    )

    first = True
    for fn in renderers:
        part = fn(source, limiter_max_1s) if fn is render_limiter else fn(source)
        if not part:
            continue
        if not first:
            lines.append("")
        lines.extend(part)
        first = False

    lines.extend(["", "Ctrl-C: exit"])
    return "\n".join(lines)


def render_message(message: str) -> str:
    return "\n".join(["fmheadend DSP monitor", "", message, "", "Ctrl-C: exit"])


def draw(text: str) -> None:
    sys.stdout.write("\x1b[H\x1b[J")
    sys.stdout.write(text)
    sys.stdout.write("\n")
    sys.stdout.flush()


def run(args: argparse.Namespace) -> None:
    if not math.isfinite(args.refresh_hz) or args.refresh_hz <= 0:
        raise SystemExit("--refresh-hz must be a positive finite number")

    redraw_period = 1.0 / args.refresh_hz
    last_redraw = 0.0
    last_sample_ns: int | None = None

    while True:
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
                sock.connect(args.socket)
                limiter_hold = LimiterPeakHold()

                with sock.makefile("r", encoding="utf-8") as stream:
                    for line in stream:
                        try:
                            record = json.loads(line)
                        except json.JSONDecodeError as exc:
                            now = time.monotonic()
                            if now - last_redraw >= redraw_period:
                                draw(render_message(f"Bad NDJSON record: {exc}"))
                                last_redraw = now
                            continue

                        source, error = select_source(record, args.tx, args.source)
                        now = time.monotonic()
                        # Collect every received record, including ones skipped by
                        # the terminal redraw throttle. Do not alter telemetry data.
                        limiter_max_1s = limiter_hold.update(source, now)

                        if error is not None:
                            if now - last_redraw >= redraw_period:
                                draw(render_message(error))
                                last_redraw = now
                            continue

                        sample_ns = source.get("sample_monotonic_ns")
                        sample_interval_ms = None
                        if isinstance(sample_ns, int):
                            if last_sample_ns is not None and sample_ns != last_sample_ns:
                                sample_interval_ms = (sample_ns - last_sample_ns) / 1_000_000
                            if sample_ns != last_sample_ns:
                                last_sample_ns = sample_ns

                        if now - last_redraw >= redraw_period:
                            draw(render(source, sample_interval_ms, limiter_max_1s))
                            last_redraw = now

            draw(render_message("Telemetry socket closed; reconnecting..."))
            time.sleep(0.5)

        except FileNotFoundError:
            draw(render_message(f"Socket not found: {args.socket}\nRetrying..."))
            time.sleep(1.0)
        except ConnectionRefusedError:
            draw(render_message(f"Connection refused: {args.socket}\nRetrying..."))
            time.sleep(1.0)
        except OSError as exc:
            draw(render_message(f"Socket error: {exc}\nRetrying..."))
            time.sleep(1.0)


def main() -> int:
    args = parse_args()

    if sys.stdout.isatty():
        sys.stdout.write("\x1b[?25l")
        sys.stdout.flush()

    try:
        run(args)
    except KeyboardInterrupt:
        return 0
    finally:
        if sys.stdout.isatty():
            sys.stdout.write("\x1b[?25h")
            sys.stdout.flush()


if __name__ == "__main__":
    raise SystemExit(main())
