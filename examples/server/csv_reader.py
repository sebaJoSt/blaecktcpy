"""
CSV Tail Reader — blaecktcpy Example
=====================================
Tails a growing CSV file and streams new rows as BlaeckTCP signals.

Signals are created dynamically from the CSV header (all columns except
the first, which is assumed to be a timestamp/index). Each time a new
row appears, the signal values are updated and sent immediately.

Usage:
    1. Start this script:       python csv_reader.py
    2. Start the data source:   python csv_generator.py
    3. Connect Loggbok to 127.0.0.1:23

    python csv_reader.py                          # defaults
    python csv_reader.py my_log.csv 9000          # custom file and port
"""

import csv
import os
import sys
import time

from blaecktcpy import BlaeckTCPy

CSV_FILE = sys.argv[1] if len(sys.argv) > 1 else "test_data.csv"
PORT = int(sys.argv[2]) if len(sys.argv) > 2 else 23

EXAMPLE_VERSION = "1.0"
POLL_INTERVAL = 0.05  # seconds between file checks


def wait_for_file(path: str) -> None:
    """Block until the CSV file exists and has at least a header row."""
    print(f"Waiting for {path} ...")
    while True:
        if os.path.exists(path) and os.path.getsize(path) > 0:
            return
        time.sleep(0.5)


def read_header(path: str) -> list[str]:
    """Read and return the CSV header row (column names)."""
    with open(path, newline="") as f:
        reader = csv.reader(f)
        return next(reader)


def main():
    wait_for_file(CSV_FILE)
    header = read_header(CSV_FILE)

    # First column is assumed to be timestamp/index — skip it for signals
    signal_names = header[1:]
    print(f"CSV columns: {header}")
    print(f"Signals:     {signal_names}")

    bltcp = BlaeckTCPy(
        "127.0.0.1", PORT, "CSV Tail Reader", "Python Script", EXAMPLE_VERSION
    )

    for name in signal_names:
        bltcp.add_signal(name, "double")

    bltcp.start()
    print("##LOGGBOK:READY##")

    # Open file and seek past existing content so we only stream new rows
    f = open(CSV_FILE, newline="")
    reader = csv.reader(f)
    next(reader)  # skip header

    # Skip rows that already exist
    for _ in reader:
        pass

    rows_sent = 0

    try:
        while True:
            # Read any new rows that appeared since last check
            new_rows = list(reader)

            if new_rows:
                for row in new_rows:
                    if len(row) < len(header):
                        continue  # skip incomplete rows
                    for i, name in enumerate(signal_names):
                        cell = row[i + 1]
                        if cell:  # skip empty cells (partial row)
                            bltcp.update(name, float(cell))
                    bltcp.write_updated_data()
                    rows_sent += 1
                    print(
                        f"\r  Rows sent: {rows_sent}  "
                        f"(latest: {', '.join(f'{n}={bltcp.signals[i].value:.2f}' for i, n in enumerate(signal_names))})",
                        end="",
                        flush=True,
                    )

            # Handle protocol commands (symbol list, device info, etc.)
            bltcp.read()

            time.sleep(POLL_INTERVAL)
    except KeyboardInterrupt:
        print(f"\nStopped. {rows_sent} rows streamed.")
    finally:
        f.close()
        bltcp.close()


if __name__ == "__main__":
    main()
