# pyright: reportUnusedCallResult=false
"""
Random CSV Data Generator
=========================
Appends rows to a CSV file at random intervals (0.2–2.0 s).
Each row has a timestamp and three sensor-like channels.
Some rows are partial — one or two channels may be left empty
to simulate sensors that don't all update at the same rate.

Run this alongside csv_reader.py to test the blaecktcpy CSV tail example.

Usage:
    python csv_generator.py              # writes to test_data.csv
    python csv_generator.py my_log.csv   # custom filename
"""

import csv
import math
import os
import random
import sys
import tempfile
import time

CSV_FILE = sys.argv[1] if len(sys.argv) > 1 else os.path.join(tempfile.gettempdir(), "test_data.csv")
COLUMNS = ["timestamp", "temperature", "pressure", "humidity"]


def generate_row(elapsed: float) -> list[float | str]:
    """Produce one row of random but plausible sensor data.

    *elapsed* drives the sine waves; the timestamp column uses wall-clock time.
    ~30% of rows are partial: one or two channels are left empty.
    """
    temperature = 20.0 + 5.0 * math.sin(elapsed * 0.1) + random.gauss(0, 0.3)
    pressure = 1013.25 + 2.0 * math.sin(elapsed * 0.05) + random.gauss(0, 0.5)
    humidity = 55.0 + 10.0 * math.sin(elapsed * 0.08) + random.gauss(0, 1.0)
    values: list[float | str] = [round(temperature, 2), round(pressure, 2), round(humidity, 1)]

    # ~30% chance to make this a partial row
    if random.random() < 0.3:
        # Blank 1 or 2 random channels
        blanks = random.sample(range(3), k=random.randint(1, 2))
        for idx in blanks:
            values[idx] = ""

    return [round(time.time(), 6)] + values


def main():
    file_exists = os.path.exists(CSV_FILE)

    with open(CSV_FILE, "a", newline="") as f:
        writer = csv.writer(f)
        if not file_exists or os.path.getsize(CSV_FILE) == 0:
            writer.writerow(COLUMNS)
            f.flush()
            print(f"Created {CSV_FILE} with header: {COLUMNS}")

        start = time.time()
        row_count = 0
        print("Generating data... (Ctrl+C to stop)")
        print("##LOGGBOK:READY##")  # Sentinel for Loggbok's process launcher — safe to remove
        try:
            while True:
                elapsed = time.time() - start
                row = generate_row(elapsed)
                writer.writerow(row)
                f.flush()
                row_count += 1
                print(
                    f"\r  Rows written: {row_count}  (t={elapsed:.1f}s)",
                    end="",
                    flush=True,
                )

                # Random delay between 0.2 and 2.0 seconds
                time.sleep(random.uniform(0.2, 2.0))
        except KeyboardInterrupt:
            print(f"\nStopped. {row_count} rows written to {CSV_FILE}")


if __name__ == "__main__":
    main()
