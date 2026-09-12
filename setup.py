#!/usr/bin/env python3
"""
Setup script for AXL Flashtool package.

Installs required Python dependencies and provides CLI entry points:
  - axl-flash: Firmware flasher (ESP32 / STM32)
  - axl-update: OTA firmware updater over HTTP & serial
  - axl-packet: Packet serial monitor CLI
  - axl-setup: STM32_Programmer_CLI system PATH setter utility
"""

import os
import sys
import platform
import subprocess
from pathlib import Path
try:
    from setuptools import setup, find_packages
except ImportError:
    setup = None
    find_packages = None


def find_stm32_cli():
    """Locate the STM32_Programmer_CLI executable on Windows, Linux, or macOS."""
    current_os = platform.system()
    target_binary = "STM32_Programmer_CLI.exe" if current_os == "Windows" else "STM32_Programmer_CLI"

    if current_os == "Windows":
        search_roots = [
            os.environ.get("ProgramFiles", r"C:\Program Files"),
            os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"),
            str(Path.home()),
        ]
    elif current_os == "Darwin":
        search_roots = [
            str(Path.home()),
            "/Applications",
            "/usr/local",
            "/opt",
        ]
    else:  # Linux / Unix
        search_roots = [
            str(Path.home()),
            "/opt",
            "/usr/local",
            "/usr/bin",
        ]

    print(f" Operating System: {current_os}")
    print(f" Searching for '{target_binary}'... This may take a moment.")

    for root_dir in search_roots:
        if not os.path.exists(root_dir):
            continue
        print(f"   Scanning: {root_dir}...")
        for root, _, files in os.walk(root_dir):
            if target_binary in files:
                full_path = os.path.join(root, target_binary)
                if current_os != "Windows" and not os.access(full_path, os.X_OK):
                    continue
                return os.path.abspath(root)

    return None


def set_windows_path(bin_dir: str) -> bool:
    """Add directory to Windows User PATH environment variable."""
    try:
        cmd = "[Environment]::GetEnvironmentVariable('Path', 'User')"
        current_path = subprocess.check_output(["powershell", "-Command", cmd], text=True).strip()

        if bin_dir in current_path:
            print(f"  Path '{bin_dir}' is already in your Windows User PATH.")
            return True

        new_path = f"{current_path};{bin_dir}" if current_path else bin_dir
        set_cmd = f"[Environment]::SetEnvironmentVariable('Path', '{new_path}', 'User')"
        subprocess.run(["powershell", "-Command", set_cmd], check=True)

        print(" Successfully added to Windows User PATH environment variable.")
        print("\n" + "=" * 60)
        print(" STEP COMPLETE! Please CLOSE this terminal window and open a ")
        print("   NEW Command Prompt or PowerShell window for changes to take effect.")
        print("=" * 60)
        return True
    except Exception as e:
        print(f" Failed to set Windows Path: {e}")
        return False


def set_unix_path(bin_dir: str) -> bool:
    """Add directory to shell startup configuration files (~/.bashrc, ~/.zshrc, ~/.bash_profile)."""
    home = Path.home()
    target_files = []

    if (home / ".bashrc").exists():
        target_files.append(home / ".bashrc")
    if (home / ".zshrc").exists():
        target_files.append(home / ".zshrc")
    if (home / ".bash_profile").exists():
        target_files.append(home / ".bash_profile")

    if not target_files:
        target_files.append(home / ".bashrc")

    path_line = f'export PATH="$PATH:{bin_dir}"\n'
    configured = False

    for rc_file in target_files:
        content = ""
        if rc_file.exists():
            content = rc_file.read_text(encoding="utf-8")

        if bin_dir in content:
            print(f"  Path '{bin_dir}' is already configured in {rc_file}.")
            configured = True
            continue

        with open(rc_file, "a", encoding="utf-8") as f:
            f.write("\n# Added by STM32 Universal Path Auto-Setter Script\n")
            f.write(path_line)
        print(f" Successfully appended the path to {rc_file}")
        configured = True

    if configured:
        print("\n" + "=" * 60)
        print(" STEP COMPLETE! To start using the command right now, restart your terminal or run:")
        print(f"   source {target_files[0]}")
        print("=" * 60)

    return configured


def install_linux_udev_rules():
    """Configure udev permissions for STM32 DFU and ST-LINK devices on Linux."""
    if platform.system() != "Linux":
        return

    rules_path = Path("/etc/udev/rules.d/49-stm32dfu.rules")
    if rules_path.exists():
        print(" Linux udev rules for STM32 USB DFU are already present.")
        return

    rules_content = (
        '# USB DFU mode and ST-LINK rules for STM32 (AXL Flashtool)\n'
        'SUBSYSTEMS=="usb", ATTRS{idVendor}=="0483", ATTRS{idProduct}=="df11", MODE="0666", TAG+="uaccess"\n'
        'SUBSYSTEMS=="usb", ATTRS{idVendor}=="0483", ATTRS{idProduct}=="df00", MODE="0666", TAG+="uaccess"\n'
        'SUBSYSTEMS=="usb", ATTRS{idVendor}=="0483", ATTRS{idProduct}=="3748", MODE="0666", TAG+="uaccess"\n'
        'SUBSYSTEMS=="usb", ATTRS{idVendor}=="0483", ATTRS{idProduct}=="374b", MODE="0666", TAG+="uaccess"\n'
        'SUBSYSTEMS=="usb", ATTRS{idVendor}=="0483", ATTRS{idProduct}=="374e", MODE="0666", TAG+="uaccess"\n'
        'SUBSYSTEMS=="usb", ATTRS{idVendor}=="0483", ATTRS{idProduct}=="3752", MODE="0666", TAG+="uaccess"\n'
        'SUBSYSTEMS=="usb", ATTRS{idVendor}=="0483", ATTRS{idProduct}=="3753", MODE="0666", TAG+="uaccess"\n'
    )
    print("\n Configuring Linux udev rules for STM32 USB DFU mode...")
    try:
        cmd = f"echo '{rules_content}' | sudo tee {rules_path} >/dev/null && sudo udevadm control --reload-rules && sudo udevadm trigger"
        res = subprocess.run(cmd, shell=True)
        if res.returncode == 0:
            print(" Successfully installed Linux udev rules for STM32 USB DFU.")
        else:
            print(" Could not auto-install udev rules. Please run manually if required:")
            print(f"   sudo tee {rules_path} << 'EOF'\n{rules_content}EOF")
            print("   sudo udevadm control --reload-rules && sudo udevadm trigger")
    except Exception as e:
        print(f" Failed to set udev rules: {e}")


def cli_setup():
    """CLI utility function to discover and configure STM32_Programmer_CLI PATH."""
    cli_dir = find_stm32_cli()

    if not cli_dir:
        print("\n Error: Could not find STM32_Programmer_CLI executable on your system.")
        print("Please verify that STM32CubeProgrammer is properly installed.")
        sys.exit(1)

    print(f"\n Found executable directory at: {cli_dir}")

    if platform.system() == "Windows":
        set_windows_path(cli_dir)
    else:
        set_unix_path(cli_dir)
        install_linux_udev_rules()


if __name__ == "__main__":
    # If setup.py is run directly without arguments, or with 'path', run CLI PATH setter.
    if len(sys.argv) == 1 or (len(sys.argv) == 2 and sys.argv[1] in ["path", "configure"]):
        cli_setup()
    else:
        if setup is None:
            from setuptools import setup, find_packages
        setup(
            name="axl-flashtool",
            version="1.0.0",
            description="Universal Firmware Flashing and Update Tool for ESP32 and STM32",
            author="AXL Drone",
            py_modules=["flash", "update", "packet", "setup"],
            packages=find_packages(),
            python_requires=">=3.8",
            install_requires=[
                "pyserial>=3.5",
                "requests>=2.25.0",
                "colorama>=0.4.4",
                "esptool>=4.0",
            ],
            entry_points={
                "console_scripts": [
                    "axlbin-flash = flash:main",
                    "axlbin-update = update:main",
                    "axlbin-packet = packet:main",
                    "axlbin-setup = setup:cli_setup",
                ],
            },
        )
