#!/usr/bin/env python3
"""
Firmware Updater for ESP32 (HTTP OTA) and STM32 (Serial CLI).

Performs connection handshake over serial packet protocol, acquires target device IP,
uploads ESP32 firmware over HTTP multi-part stream, and flashes STM32 binary over ST-Link/USB CLI.
"""

import os
import sys
import time
import json
import shutil
import subprocess
import argparse
from pathlib import Path
from typing import List, Tuple, Optional
import requests
import serial
import serial.tools.list_ports

from packet import ReliablePacketer, PacketStatus

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
    """Prints formatted string with specified ANSI color code."""
    print(f"{clr}{val}{Colors.RESET}")


def search_usb_ports() -> List[serial.tools.list_ports_common.ListPortInfo]:
    """
    Search for available serial ports across Windows, Linux, and macOS.
    Returns list of PortInfo objects.
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
            usb_ports.append(port_info)

    return usb_ports if usb_ports else all_ports


class MultipartStream:
    """Streams file data in multipart/form-data format with progress display."""

    def __init__(self, filepath: Path):
        self.file_path = filepath
        self.filename = filepath.name
        self.boundary = f"----WebKitFormBoundary{os.urandom(16).hex()}"
        self.header = (
            f"--{self.boundary}\r\n"
            f'Content-Disposition: form-data; name="file"; filename="{self.filename}"\r\n'
            f"Content-Type: application/octet-stream\r\n\r\n"
        ).encode("utf-8")
        self.footer = f"\r\n--{self.boundary}--\r\n".encode("utf-8")
        self.file_size = filepath.stat().st_size
        self.total_size = len(self.header) + self.file_size + len(self.footer)
        self.file = open(filepath, "rb")
        self.read_state = 0  # 0: header, 1: file, 2: footer, 3: done
        self.uploaded = 0

    @property
    def content_type(self) -> str:
        return f"multipart/form-data; boundary={self.boundary}"

    def read(self, size: int = 16384) -> bytes:
        if size is None or size < 0:
            size = 16384

        res = bytearray()
        while len(res) < size and self.read_state < 3:
            if self.read_state == 0:
                res.extend(self.header)
                self.read_state = 1
            elif self.read_state == 1:
                chunk = self.file.read(size - len(res))
                if chunk:
                    res.extend(chunk)
                else:
                    self.read_state = 2
            elif self.read_state == 2:
                res.extend(self.footer)
                self.read_state = 3

        if res:
            self.uploaded += len(res)
            percent = min(100.0, self.uploaded * 100 / self.total_size)
            print(f"\rUploading: {percent:6.1f}%", end="", flush=True)

        return bytes(res)

    def close(self) -> None:
        if self.file and not self.file.closed:
            self.file.close()

    def __len__(self) -> int:
        return self.total_size

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()


def run_and_stream(cmd: List[str], env: dict = None) -> Tuple[int, str]:
    """Execute subprocess command and stream stdout/stderr in real-time cross-platform."""
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


def flash_task(flash_target: str, firmware_dir: Path) -> None:
    """Performs full update sequence: Serial handshake -> ESP32 OTA -> STM32 Flashing."""
    esp_app_file_path = firmware_dir / "BIN-NW-Manager.ino.bin"
    stm_app_file_path = firmware_dir / "axlbin.bin"
    log_file = firmware_dir / "changelog.txt"

    if flash_target in ["all", "esp", "--all", "--esp"] and not esp_app_file_path.exists():
        raise ValueError(f"Required ESP32 firmware file ({esp_app_file_path.name}) is missing in {firmware_dir}!")
    if flash_target in ["all", "stm", "--all", "--stm"] and not stm_app_file_path.exists():
        raise ValueError(f"Required STM32 firmware file ({stm_app_file_path.name}) is missing in {firmware_dir}!")

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

    ser: Optional[serial.Serial] = None
    while True:
        print("Checking device connection...")
        ports = search_usb_ports()
        matched_port = None

        # Auto-detect target device by keywords in port info
        for port_info in ports:
            combined = f"{port_info.device} {port_info.description} {port_info.hwid}".upper()
            if "AXLECU" in combined or "AXL" in combined:
                matched_port = port_info.device
                break

        if not matched_port and ports:
            print("\nAvailable ports:")
            for idx, p in enumerate(ports):
                print(f"  {idx + 1}) {p.device} - {p.description}")
            choice = input("Enter port index or device path (or press Enter to retry auto-detect): ").strip()
            if choice.isdigit() and 1 <= int(choice) <= len(ports):
                matched_port = ports[int(choice) - 1].device
            elif choice:
                matched_port = choice

        if matched_port:
            try:
                ser = serial.Serial(matched_port, 115200, timeout=0.1)
                clr_print(Colors.GREEN, f"Opened serial connection on {matched_port}")
                break
            except Exception as e:
                print(f"Error opening port {matched_port}: {e}")

        c = input("Device not connected. Retry? [Y/N]: ").strip().upper()
        if c != "Y":
            sys.exit(1)

    packeter = ReliablePacketer(ack_timeout_ms=500, serial_obj=ser)

    boot_mode_active = False
    ip_address = "0.0.0.0"

    def on_packet_received(data: bytes):
        nonlocal boot_mode_active, ip_address
        try:
            payload = data.decode("utf-8", errors="ignore")
        except Exception:
            payload = str(data)
        print("Packets received: ", payload)

        if "ack_connection_sync" in payload:
            status = packeter.send(b'{"type":"SYS","cmd":"ip_address"}')
            if status == PacketStatus.PACKET_OK:
                clr_print(Colors.GREEN, "Packet sent and ACKed!")

        if "ack_ip_address" in payload:
            try:
                parsed = json.loads(payload)
                ip_address = parsed.get("ip_address", "0.0.0.0")
            except Exception as e:
                print("Failed to parse IP json:", e)
                ip_address = "0.0.0.0"

            if ip_address == "0.0.0.0":
                c = input("No IP received from device. Retry? [Y/N] or enter IP manually: ").strip()
                if c.upper() == "Y":
                    status = packeter.send(b'{"type":"SYS","cmd":"ip_address"}')
                    if status == PacketStatus.PACKET_OK:
                        clr_print(Colors.GREEN, "Packet sent and ACKed!")
                elif len(c) > 4:
                    ip_address = c
                    status = packeter.send(b'{"type":"SYS","cmd":"enter_boot"}')
                    if status == PacketStatus.PACKET_OK:
                        clr_print(Colors.GREEN, "Packet sent and ACKed!")
                else:
                    print("Operation cancelled!")
                    sys.exit(1)
            else:
                status = packeter.send(b'{"type":"SYS","cmd":"enter_boot"}')
                if status == PacketStatus.PACKET_OK:
                    clr_print(Colors.GREEN, "Packet sent and ACKed!")

        if "ack_enter_boot" in payload:
            boot_mode_active = True

    packeter.on_receive(on_packet_received)

    status = packeter.send(b'{"type":"SYS","cmd":"connection_sync","usb":true}')
    if status == PacketStatus.PACKET_OK:
        clr_print(Colors.GREEN, "Packet sent and ACKed!")

    while not boot_mode_active:
        try:
            packeter.update()
        except Exception:
            pass
        time.sleep(0.001)

    if ser and ser.is_open:
        try:
            ser.close()
        except Exception:
            pass

    # ESP32 OTA Update
    if flash_target in ["all", "esp", "--all", "--esp"]:
        url = f"http://{ip_address}"
        print(f"Connecting to {url} ...")

        retry_connect = 0
        while True:
            try:
                res = requests.get(f"{url}/", timeout=10, auth=("axladmin", "axl@#$admin"))
                if res.status_code == 200:
                    clr_print(Colors.GREEN, "ESP32 Web Server Connected!")
                    break
                else:
                    clr_print(Colors.RED, f"Device returned status {res.status_code}, retrying...")
            except requests.exceptions.RequestException:
                clr_print(Colors.YELLOW, "Waiting for ESP32 web server...")
                retry_connect += 1
                if retry_connect >= 2:
                    ip_address = input("Enter IP manually: ").strip()
                    url = f"http://{ip_address}"
                    print(f"Connecting to {url} ...")
            time.sleep(2)

        with MultipartStream(esp_app_file_path) as stream:
            try:
                response = requests.post(
                    f"{url}/update",
                    headers={
                        "X-Password": "axl@#$admin",
                        "Content-Type": stream.content_type,
                        "Content-Length": str(len(stream)),
                    },
                    data=stream,
                    timeout=60,
                )

                print("\nUpload complete!")
                print(f"HTTP Status: {response.status_code}")
                print(f"Response: {response.text}")

                if response.status_code != 200 or response.text.strip() != "OK":
                    clr_print(Colors.RED, "ESP32 OTA Update failed!")
                    if flash_target in ["all", "stm", "--all", "--stm"]:
                        retry = input("Do you want to continue to STM32 flashing anyway? [Y/N]: ").strip().upper()
                        if retry != "Y":
                            print("Operation cancelled!")
                            sys.exit(1)
            except requests.exceptions.RequestException as e:
                print("\nUpload finished!")
                if stream.uploaded >= stream.total_size:
                    clr_print(Colors.YELLOW, "ESP32 closed connection upon completing firmware upload (device rebooting).")
                else:
                    clr_print(Colors.RED, f"Upload connection error: {e}")
                    if flash_target in ["all", "stm", "--all", "--stm"]:
                        retry = input("Do you want to continue to STM32 flashing anyway? [Y/N]: ").strip().upper()
                        if retry != "Y":
                            print("Operation cancelled!")
                            sys.exit(1)
    else:
        print("Skipping ESP Flashing")

    # STM32 Flashing
    if flash_target in ["all", "stm", "--all", "--stm"]:
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
            retry = input("Do you want to retry? [Y/N]: ").strip().upper()
            if retry != "Y":
                print("Operation cancelled!")
                sys.exit(1)
    else:
        print("Skipping STM flashing")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="ESP32 (OTA) and STM32 Firmware Updater Utility",
        formatter_class=argparse.RawTextHelpFormatter
    )
    target_group = parser.add_mutually_exclusive_group()
    target_group.add_argument(
        "--esp",
        action="store_true",
        help="Update ESP32 OTA only"
    )
    target_group.add_argument(
        "--stm",
        action="store_true",
        help="Flash STM32 only"
    )
    parser.add_argument(
        "--multi",
        action="store_true",
        help="Run continuous updater loop for batch processing (ready for next device without exiting)"
    )
    parser.add_argument(
        "--dir",
        type=str,
        default=None,
        help="Path to directory containing firmware files"
    )

    args = parser.parse_args()

    if args.esp:
        flash_target = "esp"
    elif args.stm:
        flash_target = "stm"
    else:
        flash_target = "all"

    multi_mode = args.multi

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
