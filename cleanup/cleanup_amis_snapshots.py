#!/usr/bin/env python3
"""Deregister the account's own AMIs matching explicit criteria and delete
the EBS snapshots that backed them.

AMIs are never selected by a hardcoded date: selection is driven by
``--older-than`` (age in days), ``--name-prefix`` and/or ``--exclude-tag``.
At least one positive selection criterion is required so the tool can never
be run "empty" and accidentally sweep every AMI in the account.

The script only ever looks at images owned by the caller's own account
(``Owners=["self"]``) and only ever deletes snapshots that are both backing
one of the selected AMIs and are themselves owned by the caller.
"""

import argparse
import logging
import sys
from datetime import datetime, timedelta, timezone

import boto3
from botocore.exceptions import BotoCoreError, ClientError

LOGGER = logging.getLogger("cleanup_amis_snapshots")


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description=(
            "Deregister the caller's own Amazon Machine Images (AMIs) that match "
            "explicit selection criteria, then delete the EBS snapshots that "
            "backed them. The tool only ever considers AMIs owned by the "
            "account running it and requires at least one selection filter."
        ),
        epilog=(
            "Examples:\n"
            "  %(prog)s --older-than 180 --name-prefix backup- "
            "--profile prod --region eu-west-1\n"
            "  %(prog)s --name-prefix nightly- --exclude-tag keep=true --dry-run\n"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
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
        "--older-than",
        type=int,
        metavar="DAYS",
        help="Only select AMIs created at least DAYS days ago",
    )
    parser.add_argument(
        "--name-prefix",
        metavar="PREFIX",
        help="Only select AMIs whose Name starts with PREFIX",
    )
    parser.add_argument(
        "--exclude-tag",
        metavar="KEY=VALUE",
        action="append",
        default=[],
        dest="exclude_tag",
        help="Never select an AMI carrying tag KEY=VALUE (repeatable)",
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

    if args.older_than is None and not args.name_prefix:
        parser.error(
            "at least one of --older-than or --name-prefix is required "
            "(this prevents accidentally selecting every AMI in the account)"
        )
    if args.older_than is not None and args.older_than < 0:
        parser.error("--older-than must be a non-negative integer")

    exclude_tags = []
    for item in args.exclude_tag:
        key, sep, value = item.partition("=")
        if not sep or not key:
            parser.error(f"--exclude-tag must be KEY=VALUE, got {item!r}")
        exclude_tags.append((key, value))
    args.exclude_tags = exclude_tags

    return args


def configure_logging(verbosity):
    level = logging.WARNING
    if verbosity == 1:
        level = logging.INFO
    elif verbosity >= 2:
        level = logging.DEBUG
    logging.basicConfig(level=level, format="%(asctime)s %(levelname)s %(message)s")


def _parse_aws_timestamp(value):
    """Parse an AWS API timestamp string into a timezone-aware datetime."""
    for fmt in ("%Y-%m-%dT%H:%M:%S.%fZ", "%Y-%m-%dT%H:%M:%SZ"):
        try:
            return datetime.strptime(value, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    raise ValueError(f"Unrecognized AWS timestamp format: {value!r}")


def _has_tag(tags, key, value):
    for tag in tags or []:
        if tag.get("Key") == key and tag.get("Value") == value:
            return True
    return False


def find_matching_images(ec2_client, older_than_days=None, name_prefix=None, exclude_tags=None):
    """Return the caller's own AMIs matching the given selection criteria.

    Uses the ``describe_images`` paginator; only images owned by the caller
    (``Owners=["self"]``) are ever considered.
    """
    exclude_tags = exclude_tags or []
    cutoff = None
    if older_than_days is not None:
        cutoff = datetime.now(timezone.utc) - timedelta(days=older_than_days)

    filters = []
    if name_prefix:
        escaped = name_prefix.replace("*", "\\*").replace("?", "\\?")
        filters.append({"Name": "name", "Values": [f"{escaped}*"]})

    kwargs = {"Owners": ["self"]}
    if filters:
        kwargs["Filters"] = filters

    paginator = ec2_client.get_paginator("describe_images")
    matches = []
    for page in paginator.paginate(**kwargs):
        for image in page.get("Images", []):
            if name_prefix and not image.get("Name", "").startswith(name_prefix):
                continue
            if cutoff is not None:
                created = _parse_aws_timestamp(image["CreationDate"])
                if created > cutoff:
                    continue
            if any(_has_tag(image.get("Tags"), key, value) for key, value in exclude_tags):
                LOGGER.debug("Excluding %s due to matching --exclude-tag", image["ImageId"])
                continue
            matches.append(image)
    return matches


def collect_snapshot_ids(images):
    """Collect the unique EBS snapshot IDs backing the given AMIs."""
    snapshot_ids = set()
    for image in images:
        for mapping in image.get("BlockDeviceMappings", []):
            ebs = mapping.get("Ebs")
            if ebs and ebs.get("SnapshotId"):
                snapshot_ids.add(ebs["SnapshotId"])
    return snapshot_ids


def describe_owned_snapshots(ec2_client, snapshot_ids):
    """Describe the given snapshot IDs, restricted to ones the caller owns.

    Uses the ``describe_snapshots`` paginator. A snapshot ID that is not
    owned by the caller is silently excluded from the result, so it can
    never be deleted by this tool.
    """
    if not snapshot_ids:
        return []
    paginator = ec2_client.get_paginator("describe_snapshots")
    snapshots = []
    for page in paginator.paginate(OwnerIds=["self"], SnapshotIds=sorted(snapshot_ids)):
        snapshots.extend(page.get("Snapshots", []))
    return snapshots


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
    ec2_client = session.client("ec2")

    try:
        images = find_matching_images(
            ec2_client,
            older_than_days=args.older_than,
            name_prefix=args.name_prefix,
            exclude_tags=args.exclude_tags,
        )
        snapshot_ids = collect_snapshot_ids(images)
        snapshots = describe_owned_snapshots(ec2_client, snapshot_ids)
    except (ClientError, BotoCoreError) as exc:
        LOGGER.error("Failed to list AMIs/snapshots: %s", exc)
        return 1

    if not images:
        print("No AMIs matched the given criteria; nothing to do.")
        return 0

    total_gib = sum(snapshot.get("VolumeSize", 0) for snapshot in snapshots)

    print(f"{len(images)} AMI(s) matched:")
    for image in sorted(images, key=lambda i: i["ImageId"]):
        print(f"  {image['ImageId']}  {image.get('Name', '')}  created {image.get('CreationDate', '?')}")

    print(
        f"{len(snapshots)} associated snapshot(s) to delete "
        f"({total_gib} GiB total, AWS does not report exact bytes for snapshots):"
    )
    for snapshot in sorted(snapshots, key=lambda s: s["SnapshotId"]):
        print(f"  {snapshot['SnapshotId']}  {snapshot.get('VolumeSize', '?')} GiB  {snapshot.get('Description', '')}")

    if args.dry_run:
        print("Dry run: no changes made.")
        return 0

    if not args.yes and not _confirm(
        f"Deregister {len(images)} AMI(s) and delete {len(snapshots)} snapshot(s)?"
    ):
        print("Aborted: no changes made.")
        return 0

    errors = 0
    deregistered = 0
    for image in images:
        image_id = image["ImageId"]
        try:
            ec2_client.deregister_image(ImageId=image_id)
            deregistered += 1
            LOGGER.info("Deregistered AMI %s", image_id)
        except (ClientError, BotoCoreError) as exc:
            errors += 1
            LOGGER.error("Failed to deregister AMI %s: %s", image_id, exc)

    deleted = 0
    freed_gib = 0
    for snapshot in snapshots:
        snapshot_id = snapshot["SnapshotId"]
        try:
            ec2_client.delete_snapshot(SnapshotId=snapshot_id)
            deleted += 1
            freed_gib += snapshot.get("VolumeSize", 0)
            LOGGER.info("Deleted snapshot %s", snapshot_id)
        except (ClientError, BotoCoreError) as exc:
            errors += 1
            LOGGER.error("Failed to delete snapshot %s: %s", snapshot_id, exc)

    print(
        f"Summary: {deregistered}/{len(images)} AMI(s) deregistered, "
        f"{deleted}/{len(snapshots)} snapshot(s) deleted, {freed_gib} GiB freed."
    )
    if errors:
        print(f"{errors} error(s) occurred; see log output for details.")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
