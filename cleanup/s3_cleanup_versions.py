#!/usr/bin/env python3
"""Delete noncurrent object versions and delete markers from a versioned S3
bucket, leaving current versions untouched.

The bucket is scanned with the ``list_object_versions`` paginator so buckets
with more entries than a single page never get silently truncated. Deletions
are issued through batched ``delete_objects`` calls (at most 1000 keys per
call, per the S3 API limit) and any per-key errors reported by S3 are logged
and reflected in the exit code.

An entry is only ever a deletion candidate when S3 reports it as noncurrent
(``IsLatest`` is false); the current version or delete marker of a key is
never selected, so the "live" state of every key is preserved.
"""

import argparse
import itertools
import logging
import sys
from datetime import datetime, timedelta, timezone

import boto3
from botocore.exceptions import BotoCoreError, ClientError

LOGGER = logging.getLogger("s3_cleanup_versions")

# Maximum number of keys accepted by a single S3 delete_objects call.
MAX_DELETE_BATCH = 1000

# Entries requested per list_object_versions page. None lets S3 use its own
# default (up to 1000). Tests may override this module attribute to exercise
# multi-page pagination without needing thousands of objects.
_LIST_PAGE_SIZE = None


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description=(
            "Delete noncurrent object versions and delete markers from a "
            "versioned S3 bucket. Current versions are never touched."
        ),
        epilog=(
            "Examples:\n"
            "  %(prog)s --bucket my-bucket --older-than 90 "
            "--profile prod --region eu-west-1\n"
            "  %(prog)s --bucket my-bucket --prefix logs/ --dry-run\n"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--bucket", required=True, help="Name of the S3 bucket to clean up")
    parser.add_argument(
        "--prefix",
        help="Only consider object keys starting with PREFIX",
    )
    parser.add_argument(
        "--older-than",
        type=int,
        metavar="DAYS",
        help="Only delete noncurrent versions/markers last modified at least DAYS days ago",
    )
    parser.add_argument(
        "--profile",
        help="AWS named profile to use (default: standard credential chain)",
    )
    parser.add_argument(
        "--region",
        help="AWS region to operate in (default: profile/environment default)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="List what would be deleted without deleting anything",
    )
    parser.add_argument(
        "--yes",
        action="store_true",
        help="Do not prompt for confirmation before deleting",
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="count",
        default=0,
        help="Increase log verbosity (-v for INFO, -vv for DEBUG)",
    )

    args = parser.parse_args(argv)

    if args.older_than is not None and args.older_than < 0:
        parser.error("--older-than must be a non-negative integer")

    return args


def configure_logging(verbosity):
    level = logging.WARNING
    if verbosity == 1:
        level = logging.INFO
    elif verbosity >= 2:
        level = logging.DEBUG
    logging.basicConfig(level=level, format="%(asctime)s %(levelname)s %(message)s")


def iter_version_entries(s3_client, bucket, prefix=None, page_size=None):
    """Yield (kind, entry) pairs for every version/delete marker in the bucket.

    ``kind`` is either ``"version"`` or ``"delete_marker"``. Uses the
    ``list_object_versions`` paginator so buckets of any size are covered.
    ``page_size`` overrides the number of entries requested per page (mainly
    useful for tests); when omitted, S3's own default (up to 1000) is used.
    """
    paginator = s3_client.get_paginator("list_object_versions")
    kwargs = {"Bucket": bucket}
    if prefix:
        kwargs["Prefix"] = prefix
    if page_size:
        kwargs["PaginationConfig"] = {"PageSize": page_size}

    for page in paginator.paginate(**kwargs):
        for version in page.get("Versions", []):
            yield "version", version
        for marker in page.get("DeleteMarkers", []):
            yield "delete_marker", marker


def select_entries_to_delete(entries, older_than_days=None):
    """Filter (kind, entry) pairs down to noncurrent ones eligible for deletion.

    Entries with ``IsLatest`` true are always the current version/marker of
    their key and are never selected. When ``older_than_days`` is given,
    only entries last modified at least that many days ago are kept.
    """
    cutoff = None
    if older_than_days is not None:
        cutoff = datetime.now(timezone.utc) - timedelta(days=older_than_days)

    selected = []
    for kind, entry in entries:
        if entry.get("IsLatest"):
            continue
        if cutoff is not None and entry["LastModified"] > cutoff:
            continue
        selected.append((kind, entry))
    return selected


def _chunked(sequence, size):
    iterator = iter(sequence)
    while True:
        batch = list(itertools.islice(iterator, size))
        if not batch:
            return
        yield batch


def delete_entries(s3_client, bucket, entries):
    """Delete the given (kind, entry) pairs via batched delete_objects calls.

    Returns (deleted_count, errors) where errors is a list of the per-key
    error dicts reported by S3 (Key, VersionId, Code, Message).
    """
    objects = [{"Key": entry["Key"], "VersionId": entry["VersionId"]} for _, entry in entries]

    deleted = 0
    errors = []
    for batch in _chunked(objects, MAX_DELETE_BATCH):
        try:
            response = s3_client.delete_objects(
                Bucket=bucket, Delete={"Objects": batch, "Quiet": False}
            )
        except (ClientError, BotoCoreError) as exc:
            LOGGER.error("delete_objects batch of %d key(s) failed: %s", len(batch), exc)
            for obj in batch:
                errors.append(
                    {
                        "Key": obj["Key"],
                        "VersionId": obj["VersionId"],
                        "Code": "RequestFailed",
                        "Message": str(exc),
                    }
                )
            continue

        deleted += len(response.get("Deleted", []))
        for error in response.get("Errors", []):
            LOGGER.error(
                "Failed to delete %s (VersionId=%s): %s %s",
                error.get("Key"),
                error.get("VersionId"),
                error.get("Code"),
                error.get("Message"),
            )
            errors.append(error)

    return deleted, errors


def _format_bytes(num_bytes):
    value = float(num_bytes)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if value < 1024 or unit == "TiB":
            return f"{value:.2f} {unit}" if unit != "B" else f"{int(value)} {unit}"
        value /= 1024
    return f"{value:.2f} TiB"


def _confirm(message):
    try:
        answer = input(f"{message} [y/N]: ").strip().lower()
    except EOFError:
        answer = ""
    return answer in ("y", "yes")


def main(argv=None):
    args = parse_args(argv)
    configure_logging(args.verbose)

    session = boto3.Session(profile_name=args.profile, region_name=args.region)
    s3_client = session.client("s3")

    try:
        entries = list(
            iter_version_entries(
                s3_client, args.bucket, prefix=args.prefix, page_size=_LIST_PAGE_SIZE
            )
        )
    except (ClientError, BotoCoreError) as exc:
        LOGGER.error("Failed to list object versions in bucket %s: %s", args.bucket, exc)
        return 1

    selected = select_entries_to_delete(entries, older_than_days=args.older_than)

    if not selected:
        print("No noncurrent versions or delete markers matched; nothing to do.")
        return 0

    total_bytes = sum(entry.get("Size", 0) for _, entry in selected)
    version_count = sum(1 for kind, _ in selected if kind == "version")
    marker_count = sum(1 for kind, _ in selected if kind == "delete_marker")

    print(
        f"{len(selected)} entr{'y' if len(selected) == 1 else 'ies'} matched in "
        f"bucket {args.bucket} ({version_count} noncurrent version(s), "
        f"{marker_count} delete marker(s), {_format_bytes(total_bytes)} total):"
    )
    for kind, entry in sorted(selected, key=lambda item: (item[1]["Key"], item[1]["VersionId"])):
        label = "version" if kind == "version" else "delete-marker"
        print(f"  [{label}] {entry['Key']}  VersionId={entry['VersionId']}  LastModified={entry.get('LastModified')}")

    if args.dry_run:
        print("Dry run: no changes made.")
        return 0

    if not args.yes and not _confirm(f"Delete these {len(selected)} entr{'y' if len(selected) == 1 else 'ies'}?"):
        print("Aborted: no changes made.")
        return 0

    deleted, errors = delete_entries(s3_client, args.bucket, selected)

    print(f"Summary: {deleted}/{len(selected)} entr{'y' if len(selected) == 1 else 'ies'} deleted, {_format_bytes(total_bytes)} freed.")
    if errors:
        print(f"{len(errors)} error(s) reported by S3:")
        for error in errors:
            print(f"  {error.get('Key')} (VersionId={error.get('VersionId')}): {error.get('Code')} {error.get('Message')}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
