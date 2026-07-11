import asyncio

from tempfile import TemporaryDirectory
from pathlib import Path

from pymobiledevice3.lockdown import create_using_usbmux
from pymobiledevice3.services.mobilebackup2 import Mobilebackup2Service
from pymobiledevice3.exceptions import PyMobileDevice3Exception
from pymobiledevice3.services.diagnostics import DiagnosticsService
from pymobiledevice3.lockdown import LockdownClient

from . import backup

async def reboot_device(reboot: bool = False, lockdown_client: LockdownClient = None):
    if reboot and lockdown_client != None:
        print("Success! Rebooting your device...")
        async with DiagnosticsService(lockdown_client) as diagnostics_service:
            await diagnostics_service.restart()
        print("Remember to turn Find My back on!")

async def perform_restore(backup: backup.Backup, reboot: bool = False, lockdown_client: LockdownClient = None, progress_callback = lambda x: None):
    own_lockdown = (lockdown_client is None)
    try:
        with TemporaryDirectory() as backup_dir:
            backup.write_to_directory(Path(backup_dir))

            if own_lockdown:
                lockdown_client = await create_using_usbmux()
            async with Mobilebackup2Service(lockdown_client) as mb:
                # skip_apps=False: required for AppDomain-* domains (PosterBoard).
                # When True, the device-side restore daemon skips restoring
                # data for every app in Manifest.plist's Applications dict.
                # PosterBoard (the only tweak using AppDomain-*) must be
                # registered there to avoid MBErrorDomain/205.
                # Note: may trigger an iOS passcode prompt — unlock the
                # device to proceed.
                await mb.restore(backup_dir, system=True, reboot=False, copy=False, source=".", progress_callback=progress_callback, skip_apps=False)
            # reboot the device
            await reboot_device(reboot, lockdown_client)
    except PyMobileDevice3Exception as e:
        if "Find My" in str(e):
            print("Find My must be disabled in order to use this tool.")
            print("Disable Find My from Settings (Settings -> [Your Name] -> Find My) and then try again.")
            raise e
        elif "crash_on_purpose" not in str(e):
            raise e
        else:
            await reboot_device(reboot, lockdown_client)
    finally:
        # If we created this lockdown_client ourselves, close it safely.
        # After a device reboot the connection is severed and close() will
        # raise ConnectionTerminatedError — suppress it to avoid misleading
        # "Connection Lost" errors in upstream callers.
        if own_lockdown and lockdown_client is not None:
            try:
                await lockdown_client.close()
            except Exception:
                pass