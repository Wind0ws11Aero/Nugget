"""
Filter out PosterBoard files from a restore list to avoid MBErrorDomain/205.

Error 205 ("Unknown domain name") is raised by the device-side restore daemon
when it encounters an AppDomain-* domain that isn't registered in its internal
app registry OR in Manifest.plist's Applications dictionary.

This script provides a temporary workaround by removing PosterBoard entries
so the remaining files can be restored without error 205.

Usage:
    from tools.filter_posterboard_files import filter_posterboard_files

    files = [
        {"Domain": "AppDomain-com.apple.PosterBoard", "Path": "/Library/..."},
        {"Domain": "HomeDomain", "Path": "Library/Preferences/..."},
    ]
    clean = filter_posterboard_files(files)
    # clean == [{"Domain": "HomeDomain", ...}]
"""

from typing import Any

# Bundle identifiers whose AppDomain files should be excluded from restore.
# These apps are not recognized by the restore daemon when skip_apps=True
# and they're absent from Manifest.plist's Applications dictionary.
EXCLUDED_BUNDLE_IDS = frozenset({
    "com.apple.PosterBoard",
})

# Domain prefixes that match excluded bundle IDs.
EXCLUDED_DOMAIN_PREFIXES = tuple(
    f"AppDomain-{bid}" for bid in EXCLUDED_BUNDLE_IDS
)


def filter_posterboard_files(
    files: list[dict[str, Any]],
    *,
    exclude_prefixes: tuple[str, ...] = EXCLUDED_DOMAIN_PREFIXES,
) -> list[dict[str, Any]]:
    """Remove files whose Domain starts with any excluded prefix.

    Args:
        files: List of file dicts, each with at least a "Domain" key.
        exclude_prefixes: Domain prefixes to filter out.

    Returns:
        New list with excluded entries removed.
    """
    if not files:
        return []

    filtered: list[dict[str, Any]] = []
    removed: list[str] = []

    for entry in files:
        domain = entry.get("Domain", "")
        if isinstance(domain, str) and domain.startswith(exclude_prefixes):
            removed.append(entry.get("Path", "<no path>"))
            continue
        filtered.append(entry)

    if removed:
        print(
            f"[PosterBoardFilter] Removed {len(removed)} file(s) from"
            f" restore list to avoid MBErrorDomain/205:"
        )
        for path in removed:
            print(f"  - {path}")

    return filtered


def filter_posterboard_from_file_objects(
    file_objects: list[Any],
    *,
    exclude_prefixes: tuple[str, ...] = EXCLUDED_DOMAIN_PREFIXES,
) -> list[Any]:
    """Same as filter_posterboard_files but operates on FileToRestore objects.

    Files are identified by their .domain attribute rather than dict key.
    """
    if not file_objects:
        return []

    filtered: list[Any] = []
    removed: list[str] = []

    for f in file_objects:
        domain = getattr(f, "domain", "")
        if isinstance(domain, str) and domain.startswith(exclude_prefixes):
            removed.append(getattr(f, "restore_path", "<no path>"))
            continue
        filtered.append(f)

    if removed:
        print(
            f"[PosterBoardFilter] Removed {len(removed)} file object(s)"
            f" from restore list to avoid MBErrorDomain/205:"
        )
        for path in removed:
            print(f"  - {path}")

    return filtered


# ---------------------------------------------------------------------------
# Standalone usage
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    sample_files = [
        {"Domain": "AppDomain-com.apple.PosterBoard",
         "Path": "/Library/Application Support/PRBPosterExtensionDataStore/61/Extensions/com.apple.WallpaperKit.CollectionsPoster/descriptors/ABC123/file.plist"},
        {"Domain": "AppDomain-com.apple.PosterBoard",
         "Path": "/Library/Preferences/com.apple.PosterBoard.unprotectedUserDefaults.plist"},
        {"Domain": "HomeDomain",
         "Path": "Library/Preferences/com.apple.springboard.plist"},
        {"Domain": "RootDomain",
         "Path": "private/var/root/test"},
        {"Domain": "",
         "Path": "/var/mobile/Library/test"},
    ]

    cleaned = filter_posterboard_files(sample_files)
    print(f"\nBefore: {len(sample_files)} files")
    print(f"After:  {len(cleaned)} files")
    for f in cleaned:
        print(f"  {f['Domain']:45s} {f['Path']}")
