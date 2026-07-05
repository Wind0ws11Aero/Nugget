"""
Protective backup module for iOS 27+.

On iOS 27, the sparse restore triggers a "safe state recovery" that purges
device data not present in the backup (photos, Apple ID credentials, Keychain).

This module:
1. Performs a selective device backup — only KeychainDomain, HomeDomain/
   Library/Accounts (Apple ID), and optionally MediaDomain (photos) files
   are written to disk. All other files are discarded (empty placeholder),
   reducing peak disk usage from 10-100+ GB to just the protective data.
2. Parses the backup's Manifest.db (SQLite) to locate protective files
3. Extracts the protective files with their real per-file encryption keys
   and the real BackupKeyBag — suitable for inclusion in the sparse backup
   alongside tweak files.
"""

import os
import uuid as _uuid
import sqlite3
import plistlib
import struct
import shutil
import warnings
from pathlib import Path
from typing import Optional, Callable, cast
from collections.abc import Mapping
from contextlib import contextmanager
from hashlib import sha1

from pymobiledevice3.lockdown import LockdownClient
from pymobiledevice3.services.mobilebackup2 import Mobilebackup2Service

from . import backup
from .mbdb import _FileMode


# --- Constants for _SelectiveDeviceLink (from pymobiledevice3.services.device_link) ---
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


class _SelectiveDeviceLink:
    """DeviceLink wrapper that skips writing non-protective files to disk.

    During backup, the device sends every file via DLMessageUploadFiles.
    Each upload carries ``device_name`` (the on-device path, e.g.
    ``HomeDomain/Library/Accounts/Accounts3.sqlite``) and ``file_name``
    (the host-side hash filename).

    When ``preserve_file`` returns False for a given file, we create an
    empty placeholder (touch) and discard the incoming data instead of
    writing it to disk.  This dramatically reduces peak disk usage —
    only Keychain, Apple ID, and (optionally) photo data is written,
    instead of a full device backup.

    We delegate all other DeviceLink operations to the wrapped instance
    to avoid duplicating the full protocol implementation.
    """

    def __init__(self, device_link, preserve_file: Optional[Callable[[str, str], bool]] = None):
        self._dl = device_link
        self._preserve_file = preserve_file

    # --- Delegate attributes to the wrapped DeviceLink ---
    def __getattr__(self, name):
        return getattr(self._dl, name)

    # --- Override upload_files to support selective writing ---
    def upload_files(self, _message):
        while True:
            device_name = self._dl._prefixed_recv()
            if not device_name:
                break
            file_name = self._dl._prefixed_recv()
            (size,) = struct.unpack(_SIZE_FORMAT,
                self._dl.service.recvall(struct.calcsize(_SIZE_FORMAT)))
            (code,) = struct.unpack(_CODE_FORMAT,
                self._dl.service.recvall(struct.calcsize(_CODE_FORMAT)))
            size -= struct.calcsize(_CODE_FORMAT)

            should_preserve = True
            if self._preserve_file is not None:
                should_preserve = self._preserve_file(file_name, device_name)

            if should_preserve:
                # Write actual file data to disk
                with open(self._dl.root_path / file_name, "wb") as fd:
                    while size and code == _CODE_FILE_DATA:
                        fd.write(self._dl.service.recvall(size))
                        (size,) = struct.unpack(_SIZE_FORMAT,
                            self._dl.service.recvall(struct.calcsize(_SIZE_FORMAT)))
                        (code,) = struct.unpack(_CODE_FORMAT,
                            self._dl.service.recvall(struct.calcsize(_CODE_FORMAT)))
                        size -= struct.calcsize(_CODE_FORMAT)
            else:
                # Discard incoming data — do NOT create empty placeholder.
                # Empty files have SHA1 of b"" which never matches Manifest.db
                # digests, causing Error 205 during restore.
                while size and code == _CODE_FILE_DATA:
                    self._dl.service.recvall(size)  # discard
                    (size,) = struct.unpack(_SIZE_FORMAT,
                        self._dl.service.recvall(struct.calcsize(_SIZE_FORMAT)))
                    (code,) = struct.unpack(_CODE_FORMAT,
                        self._dl.service.recvall(struct.calcsize(_CODE_FORMAT)))
                    size -= struct.calcsize(_CODE_FORMAT)

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

    # --- Override move_items to handle skipped (placeholder) files ---
    def move_items(self, message):
        items = cast(Mapping[str, str], message[1])
        for src, dst in items.items():
            source = self._dl.root_path / src
            if not source.exists():
                # File was skipped (empty placeholder not created for this path)
                continue
            dest = self._dl.root_path / dst
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(source, dest)
        self._dl.status_response(0)


class _FastBackupService(Mobilebackup2Service):
    """Mobilebackup2Service subclass that skips app data backup and
    supports selective file preservation during backup.

    The device reads Info.plist's ``Applications`` dict to decide which
    app containers (AppDomain-*) to back up.  Returning an empty dict
    makes the device skip all app containers — significantly speeding up
    the backup while still capturing system domains (HomeDomain,
    KeychainDomain, MediaDomain, etc.).

    When ``preserve_file`` is provided, non-protective files are written
    as empty placeholders during backup, reducing peak disk usage from
    a full device backup (10-100+ GB) to just the protective files
    (Keychain, Apple ID, photos — typically a few MB to a few GB).
    """

    def __init__(self, lockdown, preserve_file: Optional[Callable[[str, str], bool]] = None):
        super().__init__(lockdown)
        self._preserve_file = preserve_file

    def init_mobile_backup_factory_info(self, afc):
        root_node = self.lockdown.get_value()
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
            "Applications": {},
        }

    @contextmanager
    def device_link(self, backup_directory):
        """Override device_link to inject selective file filtering.

        We create the standard DeviceLink, then patch its upload_files
        and move_items handlers with selective versions that check
        preserve_file before writing data to disk.
        """
        from pymobiledevice3.services.device_link import DeviceLink
        dl = DeviceLink(self.service, Path(backup_directory))
        dl.version_exchange()
        self.version_exchange(dl)
        if self._preserve_file is not None:
            selective = _SelectiveDeviceLink(dl, preserve_file=self._preserve_file)
            # Replace handlers that need selective behavior.
            # dl_loop dispatches via self._dl_handlers[command](message),
            # so we patch the dict entries to use our overrides.
            dl._dl_handlers["DLMessageUploadFiles"] = selective.upload_files
            dl._dl_handlers["DLMessageMoveItems"] = selective.move_items
        try:
            yield dl
        finally:
            dl.disconnect()

# Domains whose files should be extracted for data protection
PROTECTIVE_DOMAINS = {
    "CameraRollDomain",  # Actual photos and videos (DCIM/)
    "MediaDomain",       # Photo metadata (PhotoData/), PhotoStream, other media
    "KeychainDomain",    # Keychain items (encrypted backups only)
}

# Specific path prefixes within HomeDomain that contain Apple ID account data
# and user settings.
APPLE_ID_PATH_PREFIXES = [
    "Library/Accounts",              # Account database (Accounts3.sqlite)
    "Library/ConfigurationProfiles",  # Configuration profiles
    "Library/Preferences",           # User settings (dark mode, wallpaper, etc.)
]

# Files that iOS manages internally and will reject if included in a
# sparse backup with incorrect metadata (e.g. wrong protection class).
# These are skipped during protective file extraction.
# With copy=True, the existing data on the device is preserved anyway.
_SKIP_FILES = frozenset({
    "keychain-backup.plist",  # iOS validates protection class, rejects flags=4
})


def _read_varint(data: bytes, pos: int) -> tuple:
    """Read a protobuf varint from data at the given position."""
    value = 0
    shift = 0
    while pos < len(data):
        byte = data[pos]
        value |= (byte & 0x7F) << shift
        pos += 1
        if not (byte & 0x80):
            break
        shift += 7
    return value, pos


def _parse_file_blob(blob: bytes) -> dict:
    """Parse the protobuf 'file' BLOB from Manifest.db's Files table.

    The BLOB is a protobuf message with the following field mapping
    (reverse-engineered from iOS backup format):

        1: size           (varint, int64)
        2: mode           (varint, uint32)
        3: inode          (varint, uint64)
        4: uid            (varint, uint32)
        5: gid            (varint, uint32)
        6: mtime          (varint, int64)
        7: ctime          (varint, int64)
        8: hash           (bytes, SHA1 of file contents)
        9: encryption_key (bytes, per-file encryption key)
       10: flags          (varint, uint32)

    Returns a dict with whatever fields are present.
    """
    result = {}
    pos = 0
    field_map = {
        1: "size",
        2: "mode",
        3: "inode",
        4: "uid",
        5: "gid",
        6: "mtime",
        7: "ctime",
        8: "hash",
        9: "encryption_key",
        10: "flags",
    }
    while pos < len(blob):
        tag, pos = _read_varint(blob, pos)
        field_number = tag >> 3
        wire_type = tag & 0x07

        if wire_type == 0:  # varint
            value, pos = _read_varint(blob, pos)
        elif wire_type == 2:  # length-delimited (bytes/string)
            length, pos = _read_varint(blob, pos)
            value = blob[pos:pos + length]
            pos += length
        elif wire_type == 5:  # 32-bit fixed
            value = int.from_bytes(blob[pos:pos + 4], "little")
            pos += 4
        elif wire_type == 1:  # 64-bit fixed
            value = int.from_bytes(blob[pos:pos + 8], "little")
            pos += 8
        else:
            break  # unknown wire type, stop

        name = field_map.get(field_number)
        if name:
            result[name] = value

    return result


def _as_int(value, default=0):
    """Convert a protobuf-extracted value (int or bytes) to int safely."""
    if value is None:
        return default
    if isinstance(value, int):
        return value
    if isinstance(value, bytes):
        return int.from_bytes(value, "big")
    return default


def _is_protective_file(domain: str, relative_path: str, backup_photos: bool = True) -> bool:
    """Check if a file should be included in the protective backup.

    Apple ID and Keychain are always backed up. Photos (MediaDomain) are
    only included when backup_photos is True.

    Files in _SKIP_FILES are excluded even within protective domains —
    these are special files that iOS manages internally and will reject
    if included in a sparse backup with incorrect metadata (e.g.
    keychain-backup.plist requires a specific protection class).
    With copy=True, the existing data is preserved anyway.
    """
    # Skip files that iOS manages internally
    filename = os.path.basename(relative_path)
    if filename in _SKIP_FILES:
        return False
    if domain == "KeychainDomain":
        return True
    if domain == "HomeDomain":
        for prefix in APPLE_ID_PATH_PREFIXES:
            if relative_path.startswith(prefix):
                return True
    if backup_photos and domain in ("CameraRollDomain", "MediaDomain"):
        return True
    return False


def _create_preserve_callback(backup_photos: bool) -> Callable[[str, str], bool]:
    """Create a preserve_file callback for selective backup.

    The callback is invoked during DLMessageUploadFiles with:
      - file_name: the host-side filename (hash or metadata name like "Manifest.db")
      - device_name: the on-device path (e.g. "HomeDomain/Library/Accounts/Accounts3.sqlite")

    Returns True if the file should be written to disk, False to create
    an empty placeholder and discard the data.
    """
    def _preserve(file_name: str, device_name: str) -> bool:
        # Always preserve backup metadata files (Manifest.db, Manifest.plist, etc.)
        if Path(file_name).name in _BACKUP_METADATA_FILES:
            return True
        # Parse domain from device_name (format: "Domain/relative/path")
        if "/" in device_name:
            domain, rel_path = device_name.split("/", 1)
        else:
            domain = device_name
            rel_path = ""
        return _is_protective_file(domain, rel_path, backup_photos)

    return _preserve


def _find_file_in_backup(backup_dir: Path, file_id: str) -> Optional[Path]:
    """Find a file in the backup directory by its fileID (SHA1 hash).

    iOS backups may store files in a flat structure or in subdirectories
    using the first two characters of the fileID.
    """
    # Try flat structure
    flat = backup_dir / file_id
    if flat.exists():
        return flat
    # Try subdirectory structure (first 2 chars)
    subdir = backup_dir / file_id[:2] / file_id
    if subdir.exists():
        return subdir
    return None


def collect_protective_files(
    lockdown_client: LockdownClient,
    temp_dir: str,
    progress_callback=None,
    backup_photos: bool = True,
    password: str = "",
) -> tuple:
    """
    Perform a selective device backup and extract protective files.

    Instead of a full device backup (10-100+ GB), this uses a selective
    backup that only writes KeychainDomain, HomeDomain/Library/Accounts
    (Apple ID), and optionally MediaDomain (photos) to disk.  All other
    files are received from the device but discarded (empty placeholder),
    reducing peak disk usage to just the protective data.

    The device still transmits all data, so backup time is similar to a
    full backup.  Only disk space is dramatically reduced.

    Args:
        lockdown_client: Connected LockdownClient.
        temp_dir: Temporary directory to store the backup.
        progress_callback: Optional callback for progress updates.
        backup_photos: If True, also extract MediaDomain (photos).
            Apple ID and Keychain are always extracted regardless.
        password: Backup password (required for encrypted backup restore).
            If the device has encryption enabled and no password is
            provided, protective files are skipped — data is still
            protected by copy=True + remove=False.

    Returns:
        A tuple of:
        - files_list: list of backup.ConcreteFile and backup.Directory objects
        - backup_key_bag: raw BackupKeyBag bytes from the real backup (or None)
        - is_encrypted: bool indicating whether the backup was encrypted
    """
    if progress_callback is None:
        progress_callback = lambda x: None

    # Wrap the callback to only pass numeric progress values.
    # pymobiledevice3's dl_loop may pass strings, dicts, or other types —
    # the GUI callback expects floats only (formats as f"{progress:6.1f}%").
    _orig_callback = progress_callback
    def _safe_callback(value):
        try:
            if isinstance(value, (int, float)):
                _orig_callback(float(value))
        except Exception:
            pass

    backup_root = os.path.join(temp_dir, "device_backup")
    os.makedirs(backup_root, exist_ok=True)

    # Check if the device has backup encryption enabled
    with _FastBackupService(lockdown_client) as mb:
        is_encrypted = mb.will_encrypt

    scope = "photos, Apple ID, and Keychain" if backup_photos else "Apple ID and Keychain"
    _safe_callback(f"Creating full backup ({scope})..." +
                   (" (encrypted)" if is_encrypted else ""))

    # Do a FULL backup (no selective filtering during upload).
    # The device verifies all files exist at the end of backup — discarding
    # non-protective data causes "Manifest references files not in backup".
    # We write everything to disk and prune after backup completes.
    with _FastBackupService(lockdown_client) as mb:
        mb.backup(full=True, backup_directory=backup_root,
                  progress_callback=_safe_callback)

    # Locate the device-specific backup directory
    udid = lockdown_client.udid
    device_backup_dir = Path(backup_root) / udid

    if not device_backup_dir.exists():
        print(f"[ProtectiveBackup] Backup directory not found: {device_backup_dir}")
        return [], None, False

    # Read Manifest.plist to get the real BackupKeyBag
    manifest_plist_path = device_backup_dir / "Manifest.plist"
    backup_key_bag = None
    if manifest_plist_path.exists():
        with open(manifest_plist_path, "rb") as f:
            manifest_plist = plistlib.load(f)
        backup_key_bag = manifest_plist.get("BackupKeyBag")
        # Double-check encryption status from the manifest
        if manifest_plist.get("IsEncrypted", False):
            is_encrypted = True

    # Parse Manifest.db (SQLite) to find protective files
    manifest_db_path = device_backup_dir / "Manifest.db"
    if not manifest_db_path.exists():
        print("[ProtectiveBackup] Manifest.db not found in backup")
        return [], backup_key_bag, is_encrypted

    if backup_photos:
        _safe_callback("Extracting photos, Apple ID, and Keychain from backup...")
    else:
        _safe_callback("Extracting Apple ID and Keychain from backup...")

    conn = sqlite3.connect(str(manifest_db_path))
    cursor = conn.cursor()

    # Query all files — columns: fileID, domain, relativePath, flags, file
    cursor.execute("SELECT fileID, domain, relativePath, flags, file FROM Files")
    rows = cursor.fetchall()

    # Filter to protective files only so we can report accurate progress
    protective_rows = [
        (fid, dom, rpath, fl, fblob)
        for fid, dom, rpath, fl, fblob in rows
        if rpath and dom and _is_protective_file(dom, rpath, backup_photos)
    ]
    total = len(protective_rows)

    files_list = []
    file_count = 0
    dir_count = 0

    for idx, (file_id, domain, relative_path, flags, file_blob) in enumerate(protective_rows, 1):
        # Parse the protobuf file metadata BLOB
        meta = _parse_file_blob(file_blob) if file_blob else {}

        # Determine if this is a directory or a regular file
        # In iOS backup format: flags bit 1 (0x1) = file, bit 2 (0x2) = directory
        mode = _as_int(meta.get("mode"), 0)
        is_directory = (mode & 0o170000) == 0o040000 if mode else (_as_int(flags, 0) & 2)

        if is_directory:
            files_list.append(backup.Directory(
                relative_path,
                domain,
                owner=_as_int(meta.get("uid"), 501),
                group=_as_int(meta.get("gid"), 501),
            ))
            dir_count += 1
            continue

        # Find the file data in the backup directory
        file_data_path = _find_file_in_backup(device_backup_dir, file_id)
        if file_data_path is None:
            print(f"[ProtectiveBackup] File data not found for "
                  f"{domain}/{relative_path} (fileID: {file_id})")
            continue

        # Extract the per-file encryption key (empty for unencrypted backups)
        enc_key = meta.get("encryption_key", b"")
        if enc_key is None:
            enc_key = b""

        # Create a ConcreteFile that references the backup file via src_path.
        # The file contents (encrypted or plaintext) are read on demand by
        # read_contents(), so we don't load everything into memory at once.
        # Preserve the original flags (protection class) from the real backup
        # — iOS 27 validates protection classes and rejects wrong values.
        orig_flags = meta.get("flags", 4)
        if orig_flags is None:
            orig_flags = 4
        files_list.append(backup.ConcreteFile(
            relative_path,
            domain,
            owner=_as_int(meta.get("uid"), 501),
            group=_as_int(meta.get("gid"), 501),
            contents=None,
            src_path=str(file_data_path),
            key=enc_key,
            mode=_FileMode(_as_int(meta.get("mode"), backup.DEFAULT.value)),
            inode=_as_int(meta.get("inode")),
            flags=_as_int(orig_flags, 4),
        ))

        file_count += 1
        if idx % 100 == 0 or idx == total:
            pct = int(idx / total * 100) if total else 100
            _safe_callback(f"Extracting protective data... {pct}% "
                           f"({file_count} files, {dir_count} dirs)")

    conn.close()

    _safe_callback(f"Extracted {file_count} protective files "
                   f"({dir_count} directories)")

    return files_list, backup_key_bag, is_encrypted


def enable_encryption(lockdown_client: LockdownClient, password: str = "nugget_temp_keychain_27"):
    """Enable backup encryption on the device so KeychainDomain is included.

    KeychainDomain (AppleID credentials, WiFi passwords, etc.) is only
    included in encrypted backups.  This sets a temporary password on the
    device to enable encryption for the Phase 1 selective backup.
    """
    with Mobilebackup2Service(lockdown_client) as mb:
        if mb.will_encrypt:
            return True  # Already encrypted — nothing to do
        mb.change_password("", password)
    return True


def disable_encryption(lockdown_client: LockdownClient, password: str = "nugget_temp_keychain_27"):
    """Remove the temporary backup encryption password set by enable_encryption."""
    try:
        with Mobilebackup2Service(lockdown_client) as mb:
            if not mb.will_encrypt:
                return True
            mb.change_password(password, "")
    except Exception:
        pass  # Best-effort — don't block the restore if this fails
    return True


def clean_backup_for_restore(backup_dir: str | Path, udid: str):
    """Remove non-protective entries from Manifest.db.

    Non-protective file data was discarded during backup (no empty placeholders),
    so we only need to remove their Manifest.db entries so the restore process
    doesn't request them.
    """
    import sqlite3

    backup_root = Path(backup_dir)
    device_dir = backup_root / udid
    manifest_db = device_dir / "Manifest.db"

    if not manifest_db.exists():
        return 0

    conn = sqlite3.connect(str(manifest_db))
    cursor = conn.cursor()

    cursor.execute("SELECT fileID, domain, relativePath FROM Files")
    rows = cursor.fetchall()

    to_delete = []
    kept = 0
    for file_id, domain, rel_path in rows:
        if not rel_path or not domain:
            to_delete.append(file_id)
        elif not _is_protective_file(domain, rel_path, backup_photos=True):
            to_delete.append(file_id)
        else:
            kept += 1

    for file_id in to_delete:
        cursor.execute("DELETE FROM Files WHERE fileID = ?", (file_id,))
    conn.commit()
    conn.close()

    # Also delete orphan hash files (if any non-protective files were
    # accidentally written). None should exist now that we skip creation.
    protective_set = {
        fid for fid, dom, rp in rows
        if rp and dom and _is_protective_file(dom, rp, backup_photos=True)
    }
    removed_files = 0
    for f in sorted(device_dir.iterdir()):
        if not f.is_file():
            continue
        if f.name in _BACKUP_METADATA_FILES:
            continue
        if f.name not in protective_set:
            f.unlink()
            removed_files += 1

    return len(to_delete)
