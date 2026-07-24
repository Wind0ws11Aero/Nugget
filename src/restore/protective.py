"""
Protective backup module for iOS 27+.

On iOS 27, the sparse restore triggers a "safe state recovery" that purges
device data not present in the backup (photos, Apple ID credentials, user
settings). To keep user data alive, the three-phase flow in restore.py does:

  Phase 1 (this module): selective device backup. App containers are skipped
      device-side (empty Applications dict in the factory info), and every
      non-protective file the device uploads is discarded mid-stream instead
      of being written to disk. Peak disk usage drops from a full backup
      (10-100+ GB) to just the protective payload. If the selective upload
      fails for any reason, we automatically fall back to a full backup.
  Phase 3 (this module): the same backup directory — with Manifest.db pruned
      to the protective rows and orphan payload files removed — is restored
      back to the device after the security recovery.

Protective scope: HomeDomain/{Accounts, ConfigurationProfiles, Preferences}
(Apple ID + user settings) and, optionally, CameraRollDomain + MediaDomain
(photos). KeychainDomain is intentionally excluded: enabling backup
encryption is slow and keychain data is not needed for tweak functionality.
"""

import asyncio
import os
import shutil
import sqlite3
import struct
import uuid as _uuid
import warnings
import traceback
from collections.abc import Mapping
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Callable, Optional, cast

import pymobiledevice3.exceptions as _pm3_exc
import pymobiledevice3.service_connection as _sc
from pymobiledevice3.lockdown import LockdownClient
from pymobiledevice3.services.mobilebackup2 import Mobilebackup2Service

# Bump SSL handshake timeout — the default 10 seconds is too short for
# mobilebackup2 service startup on busy or post-reboot devices (iOS 27+).
# Importing this module applies it process-wide.
_sc.DEFAULT_SSL_HANDSHAKE_TIMEOUT = 60

# --- DeviceLink protocol constants (from pymobiledevice3.services.device_link) ---
_SIZE_FORMAT = ">I"
_CODE_FORMAT = ">B"
_CODE_FILE_DATA = 0xC
_CODE_ERROR_REMOTE = 0xB
_CODE_SUCCESS = 0

# Backup metadata files that must always be preserved (never filtered out)
_BACKUP_METADATA_FILES = frozenset({
    "Manifest.db",
    "Manifest.plist",
    "Status.plist",
    "Info.plist",
    "backup_manifest.db",
})

# Domains whose files should be kept in the protective backup.
PROTECTIVE_DOMAINS = frozenset({
    "CameraRollDomain",  # Actual photos and videos (DCIM/)
    "MediaDomain",       # Photo metadata (PhotoData/), PhotoStream, other media
})

# Path prefixes within HomeDomain that contain Apple ID account data and
# user settings.
APPLE_ID_PATH_PREFIXES = (
    "Library/Accounts",              # Account database (Accounts3.sqlite)
    "Library/ConfigurationProfiles",  # Configuration profiles
    "Library/Preferences",           # User settings (dark mode, wallpaper, etc.)
)

# Files iOS manages internally and rejects if included in a sparse backup
# with incorrect metadata (e.g. wrong protection class). With copy=True the
# existing on-device data is preserved anyway, so skipping them is safe.
_SKIP_FILES = frozenset({
    "keychain-backup.plist",    # iOS validates protection class, rejects flags=4
    ".GlobalPreferences.plist",  # Written separately as tweaks; skip to avoid overwrite
})


def _is_protective_file(domain: str, relative_path: str, include_photos: bool = True) -> bool:
    """Check if a file belongs in the protective backup."""
    filename = relative_path.rsplit("/", 1)[-1]
    if filename in _SKIP_FILES:
        return False
    if domain == "HomeDomain":
        return relative_path.startswith(APPLE_ID_PATH_PREFIXES)
    if include_photos and domain in PROTECTIVE_DOMAINS:
        return True
    return False


def _create_preserve_callback(include_photos: bool) -> Callable[[str, str], bool]:
    """Create a preserve_file callback for the selective backup.

    Called during DLMessageUploadFiles with:
      - file_name: host-side filename (hash name, or a metadata name)
      - device_name: on-device path (e.g. "HomeDomain/Library/Accounts/Accounts3.sqlite")

    Returns True to write the file to disk, False to discard it mid-stream.
    Anything that does not look like a "<Domain>/<path>" entry (backup
    metadata, unknown layouts) is kept — losing Manifest.db would brick the
    whole backup, keeping a few extra files is harmless.
    """
    def _preserve(file_name: str, device_name: str) -> bool:
        if Path(file_name).name in _BACKUP_METADATA_FILES:
            return True
        if "/" not in device_name:
            return True
        domain, rel_path = device_name.split("/", 1)
        return _is_protective_file(domain, rel_path, include_photos)

    return _preserve


class _SelectiveDeviceLink:
    """DeviceLink wrapper that discards non-protective files mid-stream.

    During backup the device sends every file via DLMessageUploadFiles.
    When ``preserve_file`` returns False for a file, its data is read off
    the socket and thrown away — no placeholder is created (an empty file's
    SHA1 never matches the Manifest.db digest, and clean_backup_for_restore
    removes the dangling Manifest rows before the backup is restored).

    All other DeviceLink operations delegate to the wrapped instance.
    """

    def __init__(self, device_link, preserve_file: Callable[[str, str], bool]):
        self._dl = device_link
        self._preserve_file = preserve_file

    def __getattr__(self, name):
        return getattr(self._dl, name)

    async def _recv_chunk_header(self) -> tuple:
        (size,) = struct.unpack(
            _SIZE_FORMAT, await self._dl.service.recvall(struct.calcsize(_SIZE_FORMAT)))
        (code,) = struct.unpack(
            _CODE_FORMAT, await self._dl.service.recvall(struct.calcsize(_CODE_FORMAT)))
        return size - struct.calcsize(_CODE_FORMAT), code

    async def upload_files(self, _message):
        while True:
            device_name = await self._dl._prefixed_recv()
            if not device_name:
                break
            file_name = await self._dl._prefixed_recv()
            size, code = await self._recv_chunk_header()

            if self._preserve_file(file_name, device_name):
                dest = self._dl.root_path / file_name
                dest.parent.mkdir(parents=True, exist_ok=True)
                with open(dest, "wb") as fd:
                    while size and code == _CODE_FILE_DATA:
                        fd.write(await self._dl.service.recvall(size))
                        size, code = await self._recv_chunk_header()
            else:
                # Discard incoming data — do NOT create an empty placeholder.
                while size and code == _CODE_FILE_DATA:
                    await self._dl.service.recvall(size)
                    size, code = await self._recv_chunk_header()

            if code == _CODE_ERROR_REMOTE:
                error_message = (await self._dl.service.recvall(size)).decode()
                warnings.warn(
                    f"Failed to fully upload: {file_name}. "
                    f"Device file name: {device_name}. Reason: {error_message}",
                    stacklevel=2,
                )
                continue
            assert code == _CODE_SUCCESS
        await self._dl.status_response(0)

    async def move_items(self, message):
        items = cast(Mapping[str, str], message[1])
        for src, dst in items.items():
            source = self._dl.root_path / src
            if not source.exists():
                # File was discarded during upload — nothing to move.
                continue
            dest = self._dl.root_path / dst
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(source, dest)
        await self._dl.status_response(0)

    async def copy_item(self, message):
        # DLMessageCopyItem carries (src, dst) as positional message fields.
        # If the source was discarded during upload, skip instead of failing.
        src, dst = message[1], message[2]
        source = self._dl.root_path / src
        if source.exists():
            dest = self._dl.root_path / dst
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, dest)
        await self._dl.status_response(0)


class ProtectiveBackupService(Mobilebackup2Service):
    """DeviceLink wrapper that discards non-protective files mid-stream.

    During backup the device sends every file via DLMessageUploadFiles.
    When ``preserve_file`` returns False for a file, its data is read off
    the socket and thrown away — no placeholder is created (an empty file's
    SHA1 never matches the Manifest.db digest, and clean_backup_for_restore
    removes the dangling Manifest rows before the backup is restored).

    All other DeviceLink operations delegate to the wrapped instance.
    """

    def __init__(self, device_link, preserve_file: Callable[[str, str], bool]):
        self._dl = device_link
        self._preserve_file = preserve_file

    def __getattr__(self, name):
        return getattr(self._dl, name)

    async def _recv_chunk_header(self) -> tuple:
        (size,) = struct.unpack(
            _SIZE_FORMAT, await self._dl.service.recvall(struct.calcsize(_SIZE_FORMAT)))
        (code,) = struct.unpack(
            _CODE_FORMAT, await self._dl.service.recvall(struct.calcsize(_CODE_FORMAT)))
        return size - struct.calcsize(_CODE_FORMAT), code
        (size,) = struct.unpack(
            _SIZE_FORMAT, self._dl.service.recvall(struct.calcsize(_SIZE_FORMAT)))
        (code,) = struct.unpack(
            _CODE_FORMAT, self._dl.service.recvall(struct.calcsize(_CODE_FORMAT)))
        return size - struct.calcsize(_CODE_FORMAT), code

    async def upload_files(self, _message):
        while True:
            device_name = await self._dl._prefixed_recv()
            if not device_name:
                break
            file_name = await self._dl._prefixed_recv()
            size, code = await self._recv_chunk_header()

            if self._preserve_file(file_name, device_name):
                dest = self._dl.root_path / file_name
                dest.parent.mkdir(parents=True, exist_ok=True)
                with open(dest, "wb") as fd:
                    while size and code == _CODE_FILE_DATA:
                        fd.write(await self._dl.service.recvall(size))
                        size, code = await self._recv_chunk_header()
            else:
                # Discard incoming data — do NOT create an empty placeholder.
                while size and code == _CODE_FILE_DATA:
                    await self._dl.service.recvall(size)
                    size, code = await self._recv_chunk_header()

            if code == _CODE_ERROR_REMOTE:
                error_message = (await self._dl.service.recvall(size)).decode()
                warnings.warn(
                    f"Failed to fully upload: {file_name}. "
                    f"Device file name: {device_name}. Reason: {error_message}",
                    stacklevel=2,
                )
                continue
            assert code == _CODE_SUCCESS
        await self._dl.status_response(0)
        while True:
            device_name = self._dl._prefixed_recv()
            if not device_name:
                break
            file_name = self._dl._prefixed_recv()
            size, code = self._recv_chunk_header()

            if self._preserve_file(file_name, device_name):
                dest = self._dl.root_path / file_name
                dest.parent.mkdir(parents=True, exist_ok=True)
                with open(dest, "wb") as fd:
                    while size and code == _CODE_FILE_DATA:
                        fd.write(self._dl.service.recvall(size))
                        size, code = self._recv_chunk_header()
            else:
                # Discard incoming data — do NOT create an empty placeholder.
                while size and code == _CODE_FILE_DATA:
                    self._dl.service.recvall(size)
                    size, code = self._recv_chunk_header()

            if code == _CODE_ERROR_REMOTE:
                error_message = self._dl.service.recvall(size).decode()
                warnings.warn(
                    f"Failed to fully upload: {file_name}. "
                    f"Device file name: {device_name}. Reason: {error_message}",
                    stacklevel=2,
                )
                continue
            assert code == _CODE_SUCCESS
        self._dl.status_response(0)

    async def move_items(self, message):
        items = cast(Mapping[str, str], message[1])
        for src, dst in items.items():
            source = self._dl.root_path / src
            if not source.exists():
                # File was discarded during upload — nothing to move.
                continue
            dest = self._dl.root_path / dst
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(source, dest)
        await self._dl.status_response(0)
        items = cast(Mapping[str, str], message[1])
        for src, dst in items.items():
            source = self._dl.root_path / src
            if not source.exists():
                # File was discarded during upload — nothing to move.
                continue
            dest = self._dl.root_path / dst
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(source, dest)
        self._dl.status_response(0)

    async def copy_item(self, message):
        # DLMessageCopyItem carries (src, dst) as positional message fields.
        # If the source was discarded during upload, skip instead of failing.
        src, dst = message[1], message[2]
        source = self._dl.root_path / src
        if source.exists():
            dest = self._dl.root_path / dst
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, dest)
        await self._dl.status_response(0)
        # DLMessageCopyItem carries (src, dst) as positional message fields.
        # If the source was discarded during upload, skip instead of failing.
        src, dst = message[1], message[2]
        source = self._dl.root_path / src
        if source.exists():
            dest = self._dl.root_path / dst
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, dest)
        self._dl.status_response(0)


class ProtectiveBackupService(Mobilebackup2Service):
    """Mobilebackup2Service tuned for fast protective backups.

    - ``init_mobile_backup_factory_info`` returns an empty ``Applications``
      dict, so the device skips all app containers (AppDomain-*) entirely —
      they are never uploaded at all.
    - When ``preserve_file`` is provided, the DeviceLink upload/move/copy
      handlers are replaced with selective versions that discard
      non-protective file data mid-stream.
    - ``connect`` retries transient failures with exponential backoff —
      iOS 27+ devices can take a while to spin up mobilebackup2.
    """

    def __init__(self, lockdown, preserve_file: Optional[Callable[[str, str], bool]] = None):
        super().__init__(lockdown)
        self._preserve_file = preserve_file

    async def connect(self, max_retries: int = 5):
        last_error = None
        for attempt in range(1, max_retries + 1):
            try:
                return await super().connect()
            except (_pm3_exc.ConnectionTerminatedError, ConnectionError,
                    OSError, asyncio.TimeoutError) as e:
                last_error = e
                if attempt >= max_retries:
                    break
                delay = min(2 ** attempt, 15)
                print(
                    f"[ProtectiveBackup] mobilebackup2 connect failed "
                    f"(attempt {attempt}/{max_retries}), retrying in {delay}s: {e}"
                )
                await asyncio.sleep(delay)
        raise last_error  # type: ignore[misc]

    async def init_mobile_backup_factory_info(self, afc):
        root_node = self.lockdown.all_values
        return {
            "iTunes Version": "10.0.1",
            "iTunes Files": {},
            "Unique Identifier": self.lockdown.udid.upper(),
            "Target Type": "Device",
            "Target Identifier": root_node["UniqueDeviceID"],
            "Serial Number": root_node["SerialNumber"],
            "Product Version": root_node["ProductVersion"],
            "Product Type": root_node["ProductType"],
            "Installed Applications": [],
            "GUID": _uuid.uuid4().bytes,
            "Display Name": root_node.get("DeviceName", ""),
            "Device Name": root_node.get("DeviceName", ""),
            "Build Version": root_node["BuildVersion"],
            "Applications": {},  # skip all app containers — big speedup
        }

    @asynccontextmanager
    async def device_link(self, backup_directory, **kwargs):
        from pymobiledevice3.services.device_link import DeviceLink
        dl = DeviceLink(self.service, Path(backup_directory))
        await dl.version_exchange()
        await self.version_exchange(dl)
        if self._preserve_file is not None:
            selective = _SelectiveDeviceLink(dl, preserve_file=self._preserve_file)
            handlers = getattr(dl, "_dl_handlers", {})
            # dl_loop dispatches via self._dl_handlers[command](message);
            # swap in the selective handlers where they exist.
            if "DLMessageUploadFiles" in handlers:
                handlers["DLMessageUploadFiles"] = selective.upload_files
            if "DLMessageMoveItems" in handlers:
                handlers["DLMessageMoveItems"] = selective.move_items
            if "DLMessageCopyItem" in handlers:
                handlers["DLMessageCopyItem"] = selective.copy_item
        try:
            yield dl
        finally:
            await dl.disconnect()


async def perform_protective_backup(
    lockdown_client: LockdownClient,
    backup_root: str,
    progress_callback=None,
    include_photos: bool = True,
) -> bool:
    """Run a selective device backup into ``backup_root``.

    Only protective data (photos, Apple ID, user settings) and the backup
    metadata are written to disk; everything else is discarded mid-stream.
    If the selective upload fails, automatically retries once as a full
    (unfiltered) backup so the flow never dies on a filtering edge case.

    Returns True if the device backup is encrypted.
    """
    if progress_callback is None:
        progress_callback = lambda x: None

    is_encrypted = False
    """     try:
        preserve = _create_preserve_callback(include_photos)
        async with ProtectiveBackupService(lockdown_client, preserve_file=preserve) as mb:
            is_encrypted = await mb.get_will_encrypt()
            progress_callback(
                "Creating protective backup (photos, Apple ID, settings)"
                + (" — encrypted" if is_encrypted else "") + "..."
            )
            await mb.backup(full=True, backup_directory=backup_root,
                            progress_callback=progress_callback)
        return is_encrypted
    except Exception as e:
        traceback.print_exception(e)
        print(
            f"[ProtectiveBackup] Selective backup failed "
            f"({type(e).__name__}: {e}); falling back to full backup"
        )
        progress_callback("Selective backup failed, retrying with full backup...")
    """
    # --- Full-backup fallback: no filtering, plain DeviceLink behavior ---
    shutil.rmtree(backup_root, ignore_errors=True)
    Path(backup_root).mkdir(parents=True, exist_ok=True)
    async with ProtectiveBackupService(lockdown_client) as mb:
        try:
            is_encrypted = await mb.get_will_encrypt()
        except Exception:
            pass  # Non-fatal — encryption state only feeds the status label.
        await mb.backup(full=True, backup_directory=backup_root,
                        progress_callback=progress_callback)
    return is_encrypted


def _iter_payload_files(device_dir: Path):
    """Yield every payload file in the backup, flat or in hash subdirectories."""
    for entry in sorted(device_dir.iterdir()):
        if entry.is_file():
            if entry.name not in _BACKUP_METADATA_FILES:
                yield entry
        elif entry.is_dir():
            yield from _iter_payload_files(entry)


def clean_backup_for_restore(backup_dir: "str | Path", udid: str,
                             include_photos: bool = True) -> tuple:
    """Prune a backup directory down to its protective payload.

    1. Deletes every non-protective row from Manifest.db in a single DELETE
       (keep-set staged in a temp table instead of per-row DELETEs).
    2. Deletes payload files not referenced by the keep-set, scanning hash
       subdirectories too (iOS may store payloads as "<aa>/<fileID>").
    3. Removes directories left empty by the pruning.

    Returns (removed_manifest_rows, removed_payload_files).
    """
    device_dir = Path(backup_dir) / udid
    if not device_dir.is_dir():
        # Tolerate backup_dir already pointing at the device directory.
        if (Path(backup_dir) / "Manifest.db").exists():
            device_dir = Path(backup_dir)
        else:
            return 0, 0

    manifest_db = device_dir / "Manifest.db"
    if not manifest_db.exists():
        return 0, 0

    keep_ids = set()
    removed_rows = 0
    conn = sqlite3.connect(str(manifest_db))
    try:
        cur = conn.cursor()
        cur.execute("SELECT fileID, domain, relativePath FROM Files")
        for file_id, domain, rel_path in cur:
            if rel_path and domain and _is_protective_file(domain, rel_path, include_photos):
                keep_ids.add(file_id)

        cur.execute("CREATE TEMP TABLE nugget_keep (fileID TEXT PRIMARY KEY)")
        cur.executemany("INSERT INTO nugget_keep (fileID) VALUES (?)",
                        ((fid,) for fid in keep_ids))
        cur.execute("DELETE FROM Files WHERE fileID NOT IN (SELECT fileID FROM nugget_keep)")
        removed_rows = cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0
        conn.commit()
    finally:
        conn.close()

    removed_files = 0
    for payload in _iter_payload_files(device_dir):
        if payload.name not in keep_ids:
            payload.unlink(missing_ok=True)
            removed_files += 1

    # Remove directories left empty (deepest first).
    for dirpath, _dirnames, _filenames in os.walk(device_dir, topdown=False):
        d = Path(dirpath)
        if d != device_dir and not any(d.iterdir()):
            d.rmdir()

    return removed_rows, removed_files
