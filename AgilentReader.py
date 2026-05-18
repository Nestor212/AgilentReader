#!/usr/bin/env python3
"""
Read 60 thermocouple channels from an Agilent 34970A over RS-232
with three 34901A 20-channel multiplexer cards (slots 1, 2, 3).

Channels used:
- Slot 1: 101-120
- Slot 2: 201-220
- Slot 3: 301-320

Default behavior:
- Thermocouple type K
- Celsius
- Internal reference junction compensation (per MUX card)
- Poll with READ? at fixed interval
- Print and optionally log CSV

Usage examples:
- Basic run on COM3 at 9600 baud, 5 second interval:
    python AgilentReader.py --port COM3 --baud 9600 --interval 5

- Log to CSV every 2 seconds:
    python AgilentReader.py --port COM3 --interval 2 --csv temps.csv

- Capture a finite test run of 10 scans:
    python AgilentReader.py --port COM3 --interval 1 --samples 10 --csv test_run.csv

- Use Type T thermocouples and Fahrenheit:
    python AgilentReader.py --port COM3 --tc-type T --units F --interval 3
"""

import argparse
import csv
import datetime as dt
import sys
import time
from typing import List, Optional

import serial


def build_channels() -> List[int]:
    # 34970A channel numbering is scc, where:
    # - s = slot number (1..3 in this setup)
    # - cc = channel number on the card (01..20 on 34901A)
    # This creates a deterministic scan order across all three cards.
    chans = []
    chans += list(range(101, 121))
    chans += list(range(201, 221))
    chans += list(range(301, 321))
    return chans


def scpi_channel_list(channels: List[int]) -> str:
    # Return a SCPI channel list in compact range form.
    # NOTE: This script intentionally targets a fixed 3x20 card layout.
    # The `channels` argument is kept for API symmetry/readability.
    return "(@101:120,201:220,301:320)"


class Agilent34970A:
    def __init__(self, port: str, baud: int, timeout: float = 5.0):
        # Open an RS-232 session.
        # Most 34970A serial links are 8-N-1; baud must match front-panel I/O config.
        # Flow control is disabled here unless your installation requires it.
        self.ser = serial.Serial(
            port=port,
            baudrate=baud,
            bytesize=serial.EIGHTBITS,
            parity=serial.PARITY_NONE,
            stopbits=serial.STOPBITS_ONE,
            timeout=timeout,
            xonxoff=False,
            rtscts=False,
            dsrdtr=False,
        )

    def close(self):
        # Close the serial port cleanly so the COM handle is released.
        if self.ser and self.ser.is_open:
            self.ser.close()

    def write(self, cmd: str):
        # 34970A accepts LF-terminated SCPI over serial
        self.ser.write((cmd + "\n").encode("ascii"))
        self.ser.flush()

    def read_line(self) -> str:
        line = self.ser.readline()
        if not line:
            raise TimeoutError("Timed out waiting for response from instrument.")
        return line.decode("ascii", errors="replace").strip()

    def query(self, cmd: str) -> str:
        # Query helper: send command, then read exactly one response line.
        self.write(cmd)
        return self.read_line()

    def check_error(self) -> Optional[str]:
        # Query instrument error queue. Returns None when queue reports no error.
        resp = self.query("SYST:ERR?")
        # Typical no-error response: +0,"No error"
        if resp.startswith("+0") or resp.startswith("0"):
            return None
        return resp

    def configure_thermocouples(
        self,
        channels: List[int],
        tc_type: str = "K",
        units: str = "C",
    ):
        # Build SCPI channel list for commands that apply to all measurement points.
        ch = scpi_channel_list(channels)

        # Clear status/event registers and error queue.
        # We do not send *RST because it can reset user-preferred front-panel state.
        self.write("*CLS")

        # SCPI: Configure all listed channels for temperature using thermocouples.
        # Form: CONF:TEMP TC,<type>,(@chanlist)
        self.write(f"CONF:TEMP TC,{tc_type},{ch}")

        # SCPI: Set units for configured temperature channels (C/F/K).
        self.write(f"UNIT:TEMP {units},{ch}")

        # SCPI: Use the card's internal cold-junction sensor as the reference.
        # This is the typical "mux reference" mode for 34901A thermocouple scans.
        # Form: TEMP:TRAN:TC:RJUN:TYPE INT,(@chanlist)
        self.write(f"TEMP:TRAN:TC:RJUN:TYPE INT,{ch}")

        # SCPI: Define scan list and acquisition model.
        # ROUT:SCAN selects channels and order for each scan pass.
        self.write(f"ROUT:SCAN {ch}")

        # TRIG:SOUR IMM means each READ? starts measurement immediately.
        # SAMP:COUN 1 means one sample per trigger event per scan pass.
        self.write("TRIG:SOUR IMM")
        self.write("SAMP:COUN 1")

        # Validate that configuration commands were accepted.
        err = self.check_error()
        if err:
            raise RuntimeError(f"Instrument reported configuration error: {err}")

    def read_scan(self) -> List[float]:
        # READ? initiates and returns one full scan (in scan-list order)
        raw = self.query("READ?")

        # Expected response is comma-separated ASCII numeric values.
        # Example: "24.1123,24.0871,..."
        parts = [p.strip() for p in raw.split(",") if p.strip() != ""]
        vals = []
        for p in parts:
            try:
                vals.append(float(p))
            except ValueError:
                # If the instrument returns non-numeric tokens, preserve alignment
                # by inserting NaN at that channel position.
                vals.append(float("nan"))
        return vals


def parse_args():
    p = argparse.ArgumentParser(
        description="Read 60 thermocouple channels from Agilent 34970A over RS-232."
    )
    p.add_argument("--port", required=True, help="Serial COM port, e.g. COM3")
    p.add_argument("--baud", type=int, default=9600, help="Baud rate (default: 9600)")
    p.add_argument(
        "--interval",
        type=float,
        default=5.0,
        help="Seconds between scans (default: 5.0)",
    )
    p.add_argument(
        "--tc-type",
        default="K",
        choices=["B", "E", "J", "K", "N", "R", "S", "T"],
        help="Thermocouple type (default: K)",
    )
    p.add_argument(
        "--units",
        default="C",
        choices=["C", "F", "K"],
        help="Temperature units (default: C)",
    )
    p.add_argument(
        "--samples",
        type=int,
        default=0,
        help="Number of scans to acquire (0 = run forever)",
    )
    p.add_argument(
        "--csv",
        default="",
        help="Optional CSV output path, e.g. temps.csv",
    )
    return p.parse_args()


def main():
    args = parse_args()
    channels = build_channels()

    # CSV resources are opened lazily only when --csv is supplied.
    csv_file = None
    writer = None

    try:
        # Connect to instrument over serial.
        dev = Agilent34970A(args.port, args.baud, timeout=10.0)

        # Basic communications sanity check.
        idn = dev.query("*IDN?")
        print(f"Connected: {idn}")

        # Configure all selected channels for thermocouple scanning.
        dev.configure_thermocouples(
            channels=channels,
            tc_type=args.tc_type,
            units=args.units,
        )
        print(
            f"Configured {len(channels)} channels for TC-{args.tc_type}, "
            f"units={args.units}, RJUN=INT."
        )

        if args.csv:
            # Use ASCII-safe CSV output for broad compatibility.
            csv_file = open(args.csv, "w", newline="", encoding="ascii")
            writer = csv.writer(csv_file)

            # Header maps each reading column to its physical channel label.
            header = ["timestamp"] + [f"ch{ch}" for ch in channels]
            writer.writerow(header)
            csv_file.flush()
            print(f"Logging to CSV: {args.csv}")

        print(f"Starting acquisition every {args.interval:.3f} s. Ctrl+C to stop.")
        count = 0

        while True:
            # Capture loop start to enforce interval from one scan start to the next.
            t0 = time.time()
            timestamp = dt.datetime.now().isoformat(timespec="seconds")

            # One READ? returns an entire scan across ROUT:SCAN channels.
            values = dev.read_scan()
            if len(values) != len(channels):
                print(
                    f"{timestamp} WARNING: expected {len(channels)} values, got {len(values)}",
                    file=sys.stderr,
                )

            # Print one compact line with first few channels and min/max
            finite_vals = [v for v in values if v == v]  # NaN check
            if finite_vals:
                vmin = min(finite_vals)
                vmax = max(finite_vals)
                preview = ", ".join(
                    f"ch{channels[i]}={values[i]:.2f}" for i in range(min(5, len(values)))
                )
                print(f"{timestamp} {preview} ... min={vmin:.2f}, max={vmax:.2f}")
            else:
                print(f"{timestamp} all readings invalid/NaN")

            if writer:
                # Persist full row: timestamp + all channel readings in scan order.
                row = [timestamp] + values
                writer.writerow(row)
                csv_file.flush()

            count += 1
            if args.samples > 0 and count >= args.samples:
                # Optional finite-run mode for validation or scripted captures.
                break

            # Keep a stable polling cadence by compensating for scan/processing time.
            elapsed = time.time() - t0
            sleep_s = args.interval - elapsed
            if sleep_s > 0:
                time.sleep(sleep_s)

    except KeyboardInterrupt:
        print("\nStopped by user.")
    except Exception as e:
        # Bubble operational failures with a non-zero exit for automation.
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)
    finally:
        try:
            # Always attempt serial cleanup, even after errors.
            dev.close()  # type: ignore[name-defined]
        except Exception:
            pass
        if csv_file:
            # Ensure buffered CSV data is flushed to disk.
            csv_file.close()


if __name__ == "__main__":
    main()