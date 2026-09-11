#!/usr/bin/env python3
"""
Firmware Flasher for ESP32 and STM32 Devices.

Flashes ESP32 firmware using esptool and STM32 firmware using STM32_Programmer_CLI.
Supports single-target flashing, combined flashing, and multi-device sequential mode.
"""

import os
import sys
import shutil
import subprocess
import argparse
from pathlib import Path
from typing import List, Tuple
import serial.tools.list_ports

try:
    import colorama
    colorama.init(autoreset=True)
except ImportError:
    pass


class Colors:
    CYAN = "\033[96m"
    GREEN = "\033[92m"
    YELLOW = "\033[93m"
    RED = "\033[91m"
    BOLD = "\033[1m"
    RESET = "\033[0m"


def clr_print(clr: str = Colors.RESET, val: str = "") -> None:
    """Prints formatted string with specified ANSI color."""
    print(f"{clr}{val}{Colors.RESET}")


def search_usb_ports() -> List[str]:
    """
    Search for available serial ports across Windows, Linux, and macOS.
    Returns port device paths.
    """
    print("Searching for Serial ports...")
    all_ports = list(serial.tools.list_ports.comports())
    usb_ports = []

    for port_info in all_ports:
        device = port_info.device or ""
        desc = port_info.description or ""
        hwid = port_info.hwid or ""
        mfg = getattr(port_info, "manufacturer", "") or ""
        prod = getattr(port_info, "product", "") or ""

        combined = f"{device} {desc} {hwid} {mfg} {prod}".upper()
        if any(kw in combined for kw in ["USB", "COM", "ACM", "FTDI", "CH340", "CP210", "SILICON", "STLINK", "SERIAL"]):
            usb_ports.append(device)

    # Fallback to all devices if no USB filter matched
    if not usb_ports and all_ports:
        usb_ports = [p.device for p in all_ports]

    return usb_ports


def run_and_stream(cmd: List[str], env: dict = None) -> Tuple[int, str]:
    """
    Execute subprocess command and stream stdout/stderr in real-time cross-platform.
    """
    process = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        env=env,
        bufsize=0
    )

    captured_bytes = bytearray()

    while True:
        try:
            chunk = process.stdout.read(1024)
        except (OSError, ValueError):
            break

        if not chunk:
            break

        sys.stdout.buffer.write(chunk)
        sys.stdout.buffer.flush()
        captured_bytes.extend(chunk)

    process.wait()
    full_output = captured_bytes.decode("utf-8", errors="replace")
    return process.returncode, full_output


def resolve_tool_file(filename: str) -> Path:
    """Resolves standard flashtool binary helper files relative to script or current directory."""
    script_dir = Path(__file__).resolve().parent
    local_file = script_dir / filename
    if local_file.exists():
        return local_file
    
    cwd_file = Path.cwd() / filename
    if cwd_file.exists():
        return cwd_file

    return local_file


def flash_task(flash_target: str, firmware_dir: Path) -> None:
    """Executes firmware flashing procedure for ESP32, STM32, or both."""
    esp_app_file_path = firmware_dir / "BIN-NW-Manager.ino.bin"
    stm_app_file_path = firmware_dir / "axlbin.bin"
    log_file = firmware_dir / "changelog.txt"

    if not esp_app_file_path.exists() or not stm_app_file_path.exists():
        raise ValueError(f"Required firmware files are missing in {firmware_dir}!")

    if log_file.exists():
        try:
            clr_print(Colors.YELLOW, log_file.read_text(encoding="utf-8"))
        except Exception as e:
            clr_print(Colors.YELLOW, f"Could not read changelog: {e}")
    else:
        clr_print(Colors.YELLOW, "Changelog file doesn't exist.")

    confirm = input("Do you need to start flashing [Y/N]: ").strip().upper()
    if confirm != "Y":
        clr_print(Colors.RED, "Operation cancelled!")
        sys.exit(0)

    print("All required files available!")

    # ESP32 Flashing
    if flash_target in ["--all", "--esp"]:
        while True:
            port_names = search_usb_ports()
            if not port_names:
                print("No Serial ports Found\n")
                cmd = input("Enter S to scan again or C to cancel: ").strip().upper()
                if cmd == "C":
                    print("Exiting...")
                    sys.exit(0)
            else:
                break

        for index, port in enumerate(port_names):
            clr_print(Colors.GREEN, f"{index + 1}) {port}")

        try:
            selected_idx = int(input("Select the port for ESP32 (e.g. 1 or 2): ").strip()) - 1
            if not (0 <= selected_idx < len(port_names)):
                raise ValueError("Port index out of range!")
            esp_port = port_names[selected_idx]
        except ValueError as e:
            clr_print(Colors.RED, f"Invalid selection: {e}")
            sys.exit(1)

        print("Flashing ESP32!..\n")

        # Resolve helper boot binaries
        bootloader_bin = resolve_tool_file("BIN-NW-Manager.ino.bootloader.bin")
        partitions_bin = resolve_tool_file("BIN-NW-Manager.ino.partitions.bin")
        boot_app0_bin = resolve_tool_file("boot_app0.bin")

        esptool_bin = shutil.which("esptool") or shutil.which("esptool.exe")
        if esptool_bin:
            base_esptool_cmd = [esptool_bin]
        else:
            base_esptool_cmd = [sys.executable, "-m", "esptool"]

        esp_cmd = base_esptool_cmd + [
            "--chip", "esp32",
            "--port", esp_port,
            "--baud", "921600",
            "write-flash",
            "0x1000", str(bootloader_bin),
            "0x8000", str(partitions_bin),
            "0xe000", str(boot_app0_bin),
            "0x10000", str(esp_app_file_path),
        ]

        while True:
            return_code, _ = run_and_stream(esp_cmd)
            if return_code == 0:
                clr_print(Colors.GREEN, "\nESP32 flashing successful!")
                break

            clr_print(Colors.RED, "\nESP32 Flash failed!")
            retry = input("Do you want to retry? Y/N: ").strip().upper()
            if retry != "Y":
                print("Operation cancelled!")
                sys.exit(1)
    else:
        print("Skipping ESP Flashing")

    # STM32 Flashing
    if flash_target in ["--all", "--stm"]:
        stm_cli = shutil.which("STM32_Programmer_CLI") or shutil.which("STM32_Programmer_CLI.exe") or "STM32_Programmer_CLI"

        stm_cmd = [
            stm_cli,
            "-c", "port=USB1",
            "-w", str(stm_app_file_path), "0x08000000",
            "-v",
        ]

        while True:
            return_code, _ = run_and_stream(stm_cmd)
            if return_code == 0:
                clr_print(Colors.GREEN, "\nSTM32 flashing successful!")
                break

            clr_print(Colors.RED, "\nSTM32 Flash failed!")
            retry = input("Do you want to retry? Y/N: ").strip().upper()
            if retry != "Y":
                print("Operation cancelled!")
                sys.exit(1)
    else:
        print("Skipping STM flashing")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="ESP32 and STM32 Firmware Flashing Utility",
        formatter_class=argparse.RawTextHelpFormatter
    )
    parser.add_argument(
        "target",
        nargs="?",
        default="--all",
        choices=["--all", "--esp", "--stm", "--multi"],
        help="Target option:\n  --all   Flash both ESP32 and STM32 (default)\n  --esp   Flash ESP32 only\n  --stm   Flash STM32 only\n  --multi Run continuous flashing loop for batch programming"
    )
    parser.add_argument(
        "--dir",
        type=str,
        default=None,
        help="Path to directory containing firmware files"
    )

    args = parser.parse_args()

    multi_mode = args.target == "--multi"
    flash_target = "--all" if multi_mode else args.target

    if args.dir:
        firmware_dir = Path(args.dir).resolve()
    else:
        raw_path = input("Enter the file path of firmware: ").strip()
        firmware_dir = Path(raw_path).resolve()

    if not firmware_dir.is_dir():
        clr_print(Colors.RED, f"Error: '{firmware_dir}' is not a directory!")
        sys.exit(1)

    if multi_mode:
        while True:
            try:
                flash_task(flash_target, firmware_dir)
                print("\n" + "=" * 50)
                print("Ready for next device batch...")
                print("=" * 50 + "\n")
            except KeyboardInterrupt:
                print("\nMulti-mode terminated by user.")
                break
    else:
        flash_task(flash_target, firmware_dir)


if __name__ == "__main__":
    main()
