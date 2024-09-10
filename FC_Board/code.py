# This is where the magic happens!
# This file is executed on every boot (including wake-boot from deepsleep)
# Created By: Michael Pham

"""
Built for the PySquared FC Board
Version: 1.0.1 (Beta)
Published: July 26, 2024
"""

import time

print("=" * 70)
print("Hello World!")
print("PySquared FC Board Circuit Python Software Version: 1.0.1 (Beta)")
print("Published: July 26, 2024")
print("=" * 70)

try:
    for i in range(10):
        print(f"Code Starting in {10-i} seconds")
        time.sleep(1)

    import main

except Exception as e:
    print(e)
