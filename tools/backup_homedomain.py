#!/usr/bin/env python3
"""
Backup iOS HomeDomain using Nugget's pymobiledevice3 methods.
Reads files from /var/mobile/ on the device and saves them locally.

Usage:
    python3 backup_homedomain.py [--output DIR] [--udid UDID]

Requires:
    - Device connected via USB and trusted
    - pymobiledevice3 installed
"""

import argparse
import os
import sys
import plistlib
from pathlib import Path

from pymobiledevice3.lockdown import create_using_usbmux
from pymobiledevice3.services.afc import AfcService
from pymobiledevice3.exceptions import MuxException, ConnectionTerminatedError


def connect_device(udid=None):
    """Connect to iOS device via USB."""
    try:
        ld = create_using_usbmux(udid=udid)
        info = ld.all_values
        print(f"Connected to: {info.get('DeviceName', 'Unknown')} "
              f"(iOS {info.get('ProductVersion', '?')}, UDID: {info.get('UniqueDeviceID', '?')})")
        return ld
    except MuxException as e:
        print(f"Failed to connect to device: {e}")
        sys.exit(1)


def afc_read_file(afc, device_path):
    """Read a file from device via AFC. Returns bytes or None if failed."""
    try:
        return afc.get_file_contents(device_path)
    except Exception as e:
        return None


def afc_list_dir(afc, device_path):
    """List directory contents via AFC. Returns list of names or None if not a dir."""
    try:
        return afc.listdir(device_path)
    except Exception:
        return None


def afc_walk(afc, device_path):
    """
    Walk the device filesystem starting at device_path using AFC.
    Yields (current_dir, subdirs, files) tuples similar to os.walk().
    Only works for AFC-accessible paths (typically under /var/mobile/Media/).
    """
    try:
        entries = afc.listdir(device_path)
    except Exception:
        return

    dirs = []
    files = []
    for entry in entries:
        # Skip . and ..
        if entry in (".", ".."):
            continue
        entry_path = device_path.rstrip("/") + "/" + entry
        try:
            # Try to listdir to check if it's a directory
            afc.listdir(entry_path)
            dirs.append(entry)
        except Exception:
            files.append(entry)

    yield device_path, dirs, files

    for d in dirs:
        subdir_path = device_path.rstrip("/") + "/" + d
        yield from afc_walk(afc, subdir_path)


def try_afc_backup(ld, output_dir):
    """
    Attempt to backup via AFC service.
    Note: AFC root service typically only accesses /var/mobile/Media/
    """
    print("\n--- Trying AFC backup (limited to /var/mobile/Media/) ---")
    afc = AfcService(lockdown=ld)

    # AFC typically maps root to /var/mobile/Media/
    # Let's try to walk from root
    media_path = "/"
    count = 0
    skipped = 0

    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    for current_dir, dirs, files in afc_walk(afc, media_path):
        # Create local directory
        rel_path = current_dir.lstrip("/")
        local_dir = output_dir / rel_path
        local_dir.mkdir(parents=True, exist_ok=True)

        for fname in files:
            device_file_path = current_dir.rstrip("/") + "/" + fname
            local_file_path = local_dir / fname

            contents = afc_read_file(afc, device_file_path)
            if contents is not None:
                try:
                    local_file_path.write_bytes(contents)
                    count += 1
                    if count % 100 == 0:
                        print(f"  Downloaded {count} files...")
                except Exception as e:
                    print(f"  Failed to write {local_file_path}: {e}")
                    skipped += 1
            else:
                skipped += 1

    print(f"AFC backup complete: {count} files downloaded, {skipped} skipped.")
    return count > 0


def try_house_arrest_backup(ld, output_dir, bundle_id="com.apple.Preferences"):
    """
    Use House Arrest service to access app containers.
    This gives access to /var/mobile/Containers/ for the specified app.
    """
    from pymobiledevice3.services.house_arrest import HouseArrestService

    print(f"\n--- Trying House Arrest backup for {bundle_id} ---")
    try:
        ha = HouseArrestService(lockdown=ld, bundle_id=bundle_id, documents_only=False)
        # House arrest gives access to the app's container
        # This is limited to specific apps and their containers
        print("House Arrest connected. Note: This only gives access to specific app containers.")
        print("For full HomeDomain backup, a proper backup protocol is needed.")
        return False
    except Exception as e:
        print(f"House Arrest failed: {e}")
        return False


def main():
    parser = argparse.ArgumentParser(description="Backup iOS HomeDomain using Nugget methods")
    parser.add_argument("--output", "-o", default="./homedomain_backup",
                        help="Output directory (default: ./homedomain_backup)")
    parser.add_argument("--udid", default=None,
                        help="Device UDID (default: first connected device)")
    args = parser.parse_args()

    print("=== Nugget HomeDomain Backup ===")
    print(f"Output directory: {args.output}")

    # Connect to device
    ld = connect_device(udid=args.udid)

    try:
        # Try AFC backup (limited scope)
        success = try_afc_backup(ld, args.output)

        if not success:
            print("\nNote: AFC backup only accesses /var/mobile/Media/.")
            print("For a full HomeDomain backup, you need to use the iOS Backup protocol")
            print("(MobileBackup2 service), which requires the device to be paired and")
            print("may require the user to accept the backup on the device screen.")
            print("\nAlternatively, if the device is jailbroken, you can use SSH.")

    finally:
        try:
            ld.service.close()
        except Exception:
            pass

    print("\n=== Backup Complete ===")


if __name__ == "__main__":
    main()
