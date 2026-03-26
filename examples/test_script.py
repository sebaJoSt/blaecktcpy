import sys
import time

# Basic output
print("Hello from xterm.js!")
print()

# ANSI colors
print(
    "\033[31mRed\033[0m \033[32mGreen\033[0m \033[33mYellow\033[0m \033[34mBlue\033[0m \033[35mMagenta\033[0m \033[36mCyan \033[0m"
)
print("\033[1mBold\033[0m \033[3mItalic\033[0m \033[4mUnderline\033[0m")

print()

# Input test
name = input("Enter your name: ")
print(f"Hello, \033[1;36m{name}\033[0m!")
print()
# Input test
name = input("Enter your name: ")
print(f"Hello, \033[1;36m{name}\033[0m!")
print()
# Input test
name = input("Enter your name: ")
print(f"Hello, \033[1;36m{name}\033[0m!")
print()
# Input test
name = input("Enter your name: ")
print(f"Hello, \033[1;36m{name}\033[0m!")
print()

# Progress-style output
print("Processing: ", end="", flush=True)
for i in range(20):
    print(f"\033[32m█\033[0m", end="", flush=True)
    time.sleep(0.05)
print(" Done!")
print()

# Second input
answer = input("Paste something (Ctrl+V test): ")
print(f"You entered: {answer}")
print()

print("\033[1;32m✓ All tests passed!\033[0m")
