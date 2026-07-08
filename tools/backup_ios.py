#!/usr/bin/env python3
"""
Backup iOS device using Nugget's pymobiledevice3 (MobileBackup2 protocol).
This creates a full iTunes-style backup including HomeDomain.

Usage:
    # Use Nugget's .env Python to run this script:
    /Users/jason/Nugget/.env/bin/python3 backup_ios.py --output ./my_backup

Requirements:
    - Device connected via USB and trusted
    - Device is unlocked (you may need to tap "Trust" on the device)
    - Enough free space on your Mac
"""

import sys
import time
from pathlib import Path

# Use Nugget's .env Python which has pymobiledevice3 installed
# If running from Nugget's .env, pymobiledevice3 should be available

from pymobiledevice3.lockdown import create_using_usbmux
from pymobiledevice3.services.mobilebackup2 import Mobilebackup2Service


def progress_callback(message):
    """Handle backup progress messages."""
    if isinstance(message, (int, float)):
        print(f"\rProgress: {message:.1f}%", end="", flush=True)
    else:
        print(f"\n{message}")


def main():
    import argparse
    parser = argparse.ArgumentParser(description="Backup iOS device (iTunes-style, includes HomeDomain)")
    parser.add_argument("--output", "-o", default="./ios_backup",
                        help="Output directory for backup (default: ./ios_backup)")
    parser.add_argument("--udid", default=None,
                        help="Device UDID (default: first connected device)")
    parser.add_argument("--incremental", action="store_true",
                        help="Do incremental backup (skip if full backup exists)")
    args = parser.parse_args()

    output_dir = Path(args.output)
    output_dir.mkdir(parents=True, exist_ok=True)

    print("=== iOS Device Backup (Nugget/pymobiledevice3) ===")
    print(f"Output directory: {output_dir.absolute()}")
    print("\nConnecting to device...")

    try:
        ld = create_using_usbmux(identifier=args.udid if args.udid else None)
    except TypeError:
        # fallback for older pymobiledevice3 versions
        ld = create_using_usbmux(serial=args.udid if args.udid else None)
        print(f"FAILED to connect: {e}")
        print("\nTroubleshooting:")
        print("  1. Make sure the device is connected via USB")
        print("  2. Unlock the device and tap 'Trust' if prompted")
        print("  3. Make sure the device is not paired with another computer")
        sys.exit(1)

    info = ld.all_values
    udid = ld.udid
    print(f"\nConnected: {info.get('DeviceName', 'Unknown')}")
    print(f"  Model: {info.get('ProductType', '?')}")
    print(f"  iOS:   {info.get('ProductVersion', '?')}")
    print(f"  UDID:  {udid}")
    print()

    # Check if device will encrypt backup
    mb2 = Mobilebackup2Service(ld)
    will_encrypt = mb2.will_encrypt
    if will_encrypt:
        print("WARNING: Device has backup encryption enabled.")
        print("  The backup will be encrypted. To restore, you'll need the password.")
        print("  (Use pymobiledevice3's change-password or disable in Settings)")
        print()

    device_backup_dir = output_dir / udid
    is_full = not args.incremental or not device_backup_dir.exists()

    if is_full:
        print("Starting FULL backup...")
    else:
        print("Starting INCREMENTAL backup...")

    print("Note: You may need to unlock the device and tap 'Allow' if prompted.")
    print("The device screen may show 'Backing Up'.\n")
    print("Starting backup... (this may take several minutes)")
    print("-" * 50)

    start = time.time()
    try:
        mb2.backup(
            full=is_full,
            backup_directory=output_dir,
            progress_callback=progress_callback,
        )
    except KeyboardInterrupt:
        print("\n\nBackup cancelled by user.")
        sys.exit(1)
    except Exception as e:
        print(f"\n\nBackup failed: {e}")
        print("\nTroubleshooting:")
        print("  1. Make sure the device screen is unlocked")
        print("  2. Try disconnecting and reconnecting the USB cable")
        print("  3. For encrypted backups, make sure you have the password")
        sys.exit(1)

    elapsed = time.time() - start
    print(f"\n\nBackup complete! Took {elapsed:.1f} seconds.")
    print(f"Backup saved to: {device_backup_dir}")
    print()
    print("Backup contains the following domains (including HomeDomain):")
    print("  - HomeDomain        (/var/mobile/)")
    print("  - MediaDomain       (/var/mobile/Media/)")
    print("  - SystemPreferencesDomain")
    print("  - MobileDeviceDomain")
    print("  - ... and more")
    print()
    print("To extract files from this backup (no device needed):")
    print(f"  .env/bin/python3 extract_homedomain.py {device_backup_dir.resolve()} ./extracted")
    print(f"  .env/bin/python3 extract_homedomain.py {device_backup_dir.resolve()} ./extracted HomeDomain")
    print(f"  .env/bin/python3 extract_homedomain.py {device_backup_dir.resolve()} ./extracted SystemPreferencesDomain")


if __name__ == "__main__":
    main()
