"""Tests for the scripts in cleanup/.

The scripts are standalone (no shared local modules), so they are loaded
directly from cleanup/ with importlib rather than imported as a package.
AWS interaction is exercised either against moto's in-memory mocks (for
realistic end-to-end coverage of list/delete/pagination behaviour) or, where
moto cannot faithfully simulate a scenario (backdated AMI creation dates,
per-key S3 delete errors, batch sizing), against small fake botocore-shaped
clients that isolate the exact logic under test.
"""

import importlib.util
from datetime import datetime, timedelta, timezone
from pathlib import Path

import boto3
import pytest
from botocore.exceptions import ClientError
from moto import mock_aws

CLEANUP_DIR = Path(__file__).resolve().parent.parent / "cleanup"


def _load_module(module_name, filename):
    spec = importlib.util.spec_from_file_location(module_name, CLEANUP_DIR / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(autouse=True)
def aws_credentials(monkeypatch):
    """Fake credentials so boto3/moto never touch real AWS accounts."""
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_SECURITY_TOKEN", "testing")
    monkeypatch.setenv("AWS_SESSION_TOKEN", "testing")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")


@pytest.fixture(scope="module")
def ami_module():
    return _load_module("cleanup_amis_snapshots", "cleanup_amis_snapshots.py")


@pytest.fixture(scope="module")
def s3_module():
    return _load_module("s3_cleanup_versions", "s3_cleanup_versions.py")


def _deny_input(*_args, **_kwargs):
    raise AssertionError("input() must not be called in this scenario")


def _snapshot_exists(ec2, snapshot_id):
    # moto's describe_snapshots does not honor OwnerIds filtering (it also
    # returns its large seeded catalog of public AMI snapshots), so presence
    # is checked with an explicit SnapshotIds lookup instead of enumerating
    # "all owned snapshots" and diffing sets.
    try:
        ec2.describe_snapshots(SnapshotIds=[snapshot_id])
    except ClientError as exc:
        if exc.response["Error"]["Code"] == "InvalidSnapshot.NotFound":
            return False
        raise
    return True


# ---------------------------------------------------------------------------
# cleanup_amis_snapshots.py
# ---------------------------------------------------------------------------


class _FakePaginator:
    """Minimal stand-in for a botocore paginator: yields pre-baked pages."""

    def __init__(self, pages):
        self._pages = pages

    def paginate(self, **_kwargs):
        return iter(self._pages)


class _FakeEC2Client:
    """Stand-in exposing only the paginators cleanup_amis_snapshots.py uses."""

    def __init__(self, image_pages=None, snapshot_pages=None):
        self._image_pages = image_pages or []
        self._snapshot_pages = snapshot_pages or []
        self.paginator_calls = []

    def get_paginator(self, name):
        self.paginator_calls.append(name)
        if name == "describe_images":
            return _FakePaginator(self._image_pages)
        if name == "describe_snapshots":
            return _FakePaginator(self._snapshot_pages)
        raise AssertionError(f"Unexpected paginator requested: {name}")


def _iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%S.000Z")


def _create_ami(ec2, name, tags=None):
    # moto's register_image auto-generates its own backing snapshot and
    # ignores any BlockDeviceMappings passed in, so the real snapshot id is
    # read back from the registered image rather than tracked separately.
    image_id = ec2.register_image(Name=name)["ImageId"]
    if tags:
        ec2.create_tags(Resources=[image_id], Tags=[{"Key": k, "Value": v} for k, v in tags.items()])
    image = ec2.describe_images(ImageIds=[image_id])["Images"][0]
    snapshot_id = image["BlockDeviceMappings"][0]["Ebs"]["SnapshotId"]
    return image_id, snapshot_id


def test_ami_module_has_no_import_time_side_effects(ami_module):
    # No AWS client or region is bound at module level; all setup happens
    # inside main().
    assert callable(ami_module.main)
    assert not hasattr(ami_module, "ec2_client")
    assert not hasattr(ami_module, "REGION")


def test_parse_args_requires_a_selection_filter(ami_module):
    with pytest.raises(SystemExit):
        ami_module.parse_args([])


def test_parse_args_rejects_malformed_exclude_tag(ami_module):
    with pytest.raises(SystemExit):
        ami_module.parse_args(["--name-prefix", "x-", "--exclude-tag", "no-equals-sign"])


def test_parse_args_accepts_name_prefix_only(ami_module):
    args = ami_module.parse_args(["--name-prefix", "backup-"])
    assert args.name_prefix == "backup-"
    assert args.older_than is None
    assert args.exclude_tags == []


def test_find_matching_images_applies_age_prefix_and_exclude_tag_filters(ami_module):
    now = datetime.now(timezone.utc)
    old_untagged = {
        "ImageId": "ami-old-untagged",
        "Name": "backup-old",
        "CreationDate": _iso(now - timedelta(days=400)),
        "Tags": [],
    }
    old_tagged_keep = {
        "ImageId": "ami-old-keep",
        "Name": "backup-keep",
        "CreationDate": _iso(now - timedelta(days=400)),
        "Tags": [{"Key": "keep", "Value": "true"}],
    }
    recent = {
        "ImageId": "ami-recent",
        "Name": "backup-recent",
        "CreationDate": _iso(now - timedelta(days=1)),
        "Tags": [],
    }
    other_prefix = {
        "ImageId": "ami-other",
        "Name": "other-old",
        "CreationDate": _iso(now - timedelta(days=400)),
        "Tags": [],
    }
    # Split across two pages to exercise pagination handling too.
    fake = _FakeEC2Client(image_pages=[
        {"Images": [old_untagged, old_tagged_keep]},
        {"Images": [recent, other_prefix]},
    ])

    matches = ami_module.find_matching_images(
        fake, older_than_days=30, name_prefix="backup-", exclude_tags=[("keep", "true")]
    )

    assert [i["ImageId"] for i in matches] == ["ami-old-untagged"]
    assert fake.paginator_calls == ["describe_images"]


def test_describe_owned_snapshots_aggregates_pages_and_trusts_owner_filter(ami_module):
    # Simulate two pages of results, and simulate that AWS's own OwnerIds
    # filter already dropped a requested id that isn't owned by the caller.
    snap_a = {"SnapshotId": "snap-a", "VolumeSize": 8}
    snap_b = {"SnapshotId": "snap-b", "VolumeSize": 4}
    fake = _FakeEC2Client(snapshot_pages=[{"Snapshots": [snap_a]}, {"Snapshots": [snap_b]}])

    result = ami_module.describe_owned_snapshots(fake, {"snap-a", "snap-b", "snap-not-owned"})

    assert {s["SnapshotId"] for s in result} == {"snap-a", "snap-b"}


def test_describe_owned_snapshots_skips_call_when_no_ids(ami_module):
    fake = _FakeEC2Client()
    assert ami_module.describe_owned_snapshots(fake, set()) == []
    assert fake.paginator_calls == []


def test_ami_dry_run_deletes_nothing(ami_module, monkeypatch):
    with mock_aws():
        ec2 = boto3.client("ec2", region_name="us-east-1")
        image_id, snapshot_id = _create_ami(ec2, "old-image-1")
        monkeypatch.setattr("builtins.input", _deny_input)

        rc = ami_module.main(["--name-prefix", "old-", "--dry-run", "--region", "us-east-1"])

        assert rc == 0
        assert [i["ImageId"] for i in ec2.describe_images(Owners=["self"])["Images"]] == [image_id]
        assert _snapshot_exists(ec2, snapshot_id)


def test_ami_confirmation_declined_deletes_nothing(ami_module, monkeypatch):
    with mock_aws():
        ec2 = boto3.client("ec2", region_name="us-east-1")
        image_id, snapshot_id = _create_ami(ec2, "old-image-1")
        monkeypatch.setattr("builtins.input", lambda *_a, **_k: "no")

        rc = ami_module.main(["--name-prefix", "old-", "--region", "us-east-1"])

        assert rc == 0
        assert [i["ImageId"] for i in ec2.describe_images(Owners=["self"])["Images"]] == [image_id]
        assert _snapshot_exists(ec2, snapshot_id)


def test_ami_yes_flag_skips_confirmation_prompt(ami_module, monkeypatch):
    with mock_aws():
        ec2 = boto3.client("ec2", region_name="us-east-1")
        _create_ami(ec2, "old-image-1")
        monkeypatch.setattr("builtins.input", _deny_input)

        rc = ami_module.main(["--name-prefix", "old-", "--yes", "--region", "us-east-1"])

        assert rc == 0
        assert ec2.describe_images(Owners=["self"])["Images"] == []


def test_ami_real_run_deletes_exact_selection_and_keeps_others(ami_module):
    with mock_aws():
        ec2 = boto3.client("ec2", region_name="us-east-1")
        del_id_1, del_snap_1 = _create_ami(ec2, "old-image-1")
        del_id_2, del_snap_2 = _create_ami(ec2, "old-image-2")
        keep_tagged_id, keep_tagged_snap = _create_ami(ec2, "old-image-3", tags={"keep": "true"})
        other_id, other_snap = _create_ami(ec2, "unrelated-image")

        rc = ami_module.main(
            [
                "--name-prefix", "old-",
                "--exclude-tag", "keep=true",
                "--yes",
                "--region", "us-east-1",
            ]
        )

        assert rc == 0
        remaining_images = {i["ImageId"] for i in ec2.describe_images(Owners=["self"])["Images"]}
        assert remaining_images == {keep_tagged_id, other_id}
        assert not _snapshot_exists(ec2, del_snap_1)
        assert not _snapshot_exists(ec2, del_snap_2)
        assert _snapshot_exists(ec2, keep_tagged_snap)
        assert _snapshot_exists(ec2, other_snap)


def test_ami_no_matches_is_a_clean_no_op(ami_module, monkeypatch):
    with mock_aws():
        ec2 = boto3.client("ec2", region_name="us-east-1")
        _create_ami(ec2, "unrelated-image")
        monkeypatch.setattr("builtins.input", _deny_input)

        rc = ami_module.main(["--name-prefix", "old-", "--region", "us-east-1"])

        assert rc == 0
        assert len(ec2.describe_images(Owners=["self"])["Images"]) == 1


def test_ami_listing_failure_returns_exit_code_1(ami_module, monkeypatch):
    class _FailingEC2Client:
        def get_paginator(self, _name):
            raise ClientError(
                {"Error": {"Code": "UnauthorizedOperation", "Message": "nope"}}, "DescribeImages"
            )

    class _FailingSession:
        def client(self, _service_name):
            return _FailingEC2Client()

    monkeypatch.setattr(ami_module.boto3, "Session", lambda **_kw: _FailingSession())

    rc = ami_module.main(["--name-prefix", "old-", "--region", "us-east-1"])

    assert rc == 1


# ---------------------------------------------------------------------------
# s3_cleanup_versions.py
# ---------------------------------------------------------------------------


def _put_two_versions(s3, bucket, key, body1=b"v1", body2=b"v2-current"):
    s3.put_object(Bucket=bucket, Key=key, Body=body1)
    s3.put_object(Bucket=bucket, Key=key, Body=body2)


def _make_versioned_bucket(s3, bucket):
    s3.create_bucket(Bucket=bucket)
    s3.put_bucket_versioning(Bucket=bucket, VersioningConfiguration={"Status": "Enabled"})


def test_s3_module_has_no_import_time_side_effects(s3_module):
    # No S3 client or bucket name is bound at module level; all setup
    # happens inside main().
    assert callable(s3_module.main)
    assert not hasattr(s3_module, "bucket_name")
    assert not hasattr(s3_module, "s3_client")


def test_select_entries_to_delete_never_selects_current_and_honors_older_than(s3_module):
    now = datetime.now(timezone.utc)
    current = {"Key": "a", "VersionId": "v-current", "IsLatest": True, "LastModified": now}
    old_noncurrent = {
        "Key": "a", "VersionId": "v-old", "IsLatest": False, "LastModified": now - timedelta(days=400)
    }
    recent_noncurrent = {"Key": "a", "VersionId": "v-recent", "IsLatest": False, "LastModified": now}
    entries = [("version", current), ("version", old_noncurrent), ("version", recent_noncurrent)]

    everything_noncurrent = s3_module.select_entries_to_delete(entries)
    assert {e["VersionId"] for _, e in everything_noncurrent} == {"v-old", "v-recent"}

    only_old = s3_module.select_entries_to_delete(entries, older_than_days=30)
    assert {e["VersionId"] for _, e in only_old} == {"v-old"}


def test_delete_entries_batches_at_max_1000(s3_module):
    class _RecordingClient:
        def __init__(self):
            self.batch_sizes = []

        def delete_objects(self, Bucket, Delete):  # noqa: N803 (boto3 casing)
            objects = Delete["Objects"]
            self.batch_sizes.append(len(objects))
            return {"Deleted": list(objects), "Errors": []}

    entries = [("version", {"Key": f"k{i}", "VersionId": f"v{i}"}) for i in range(1050)]
    client = _RecordingClient()

    deleted, errors = s3_module.delete_entries(client, "some-bucket", entries)

    assert client.batch_sizes == [1000, 50]
    assert deleted == 1050
    assert errors == []


def test_delete_entries_reports_per_key_errors(s3_module):
    class _PartialFailureClient:
        def delete_objects(self, Bucket, Delete):  # noqa: N803 (boto3 casing)
            objects = Delete["Objects"]
            return {
                "Deleted": [objects[0]],
                "Errors": [
                    {
                        "Key": objects[1]["Key"],
                        "VersionId": objects[1]["VersionId"],
                        "Code": "AccessDenied",
                        "Message": "nope",
                    }
                ],
            }

    entries = [("version", {"Key": "a", "VersionId": "v1"}), ("version", {"Key": "b", "VersionId": "v2"})]

    deleted, errors = s3_module.delete_entries(_PartialFailureClient(), "some-bucket", entries)

    assert deleted == 1
    assert errors == [{"Key": "b", "VersionId": "v2", "Code": "AccessDenied", "Message": "nope"}]


def test_s3_dry_run_deletes_nothing(s3_module, monkeypatch):
    with mock_aws():
        s3 = boto3.client("s3", region_name="us-east-1")
        bucket = "dry-run-bucket"
        _make_versioned_bucket(s3, bucket)
        _put_two_versions(s3, bucket, "a.txt")
        monkeypatch.setattr("builtins.input", _deny_input)

        rc = s3_module.main(["--bucket", bucket, "--dry-run", "--region", "us-east-1"])

        assert rc == 0
        assert len(s3.list_object_versions(Bucket=bucket)["Versions"]) == 2


def test_s3_confirmation_declined_deletes_nothing(s3_module, monkeypatch):
    with mock_aws():
        s3 = boto3.client("s3", region_name="us-east-1")
        bucket = "decline-bucket"
        _make_versioned_bucket(s3, bucket)
        _put_two_versions(s3, bucket, "a.txt")
        monkeypatch.setattr("builtins.input", lambda *_a, **_k: "no")

        rc = s3_module.main(["--bucket", bucket, "--region", "us-east-1"])

        assert rc == 0
        assert len(s3.list_object_versions(Bucket=bucket)["Versions"]) == 2


def test_s3_real_run_deletes_noncurrent_keeps_current_and_delete_markers(s3_module):
    with mock_aws():
        s3 = boto3.client("s3", region_name="us-east-1")
        bucket = "real-run-bucket"
        _make_versioned_bucket(s3, bucket)
        _put_two_versions(s3, bucket, "a.txt")
        s3.put_object(Bucket=bucket, Key="b.txt", Body=b"gone-soon")
        s3.delete_object(Bucket=bucket, Key="b.txt")

        rc = s3_module.main(["--bucket", bucket, "--yes", "--region", "us-east-1"])

        assert rc == 0
        remaining = s3.list_object_versions(Bucket=bucket)
        versions = remaining.get("Versions", [])
        markers = remaining.get("DeleteMarkers", [])
        assert len(versions) == 1
        assert versions[0]["Key"] == "a.txt" and versions[0]["IsLatest"]
        assert len(markers) == 1
        assert markers[0]["Key"] == "b.txt" and markers[0]["IsLatest"]


def test_s3_prefix_scopes_deletion(s3_module):
    with mock_aws():
        s3 = boto3.client("s3", region_name="us-east-1")
        bucket = "prefix-bucket"
        _make_versioned_bucket(s3, bucket)
        _put_two_versions(s3, bucket, "keep/a.txt")
        _put_two_versions(s3, bucket, "cleanup/b.txt")

        rc = s3_module.main(["--bucket", bucket, "--prefix", "cleanup/", "--yes", "--region", "us-east-1"])

        assert rc == 0
        versions = s3.list_object_versions(Bucket=bucket)["Versions"]
        by_key = {}
        for version in versions:
            by_key.setdefault(version["Key"], []).append(version)
        assert len(by_key["keep/a.txt"]) == 2
        assert len(by_key["cleanup/b.txt"]) == 1
        assert by_key["cleanup/b.txt"][0]["IsLatest"]


def test_s3_pagination_end_to_end(s3_module, monkeypatch):
    with mock_aws():
        s3 = boto3.client("s3", region_name="us-east-1")
        bucket = "paginated-bucket"
        _make_versioned_bucket(s3, bucket)
        key_count = 12
        for i in range(key_count):
            _put_two_versions(s3, bucket, f"k{i}.txt")

        # Sanity check: with a small page size S3 really does split this
        # bucket's 24 version entries across several pages.
        raw_pages = list(
            s3.get_paginator("list_object_versions").paginate(
                Bucket=bucket, PaginationConfig={"PageSize": 3}
            )
        )
        assert len(raw_pages) > 1

        # Force the script itself to use that small page size so main()'s
        # own pagination loop is what gets exercised end to end.
        monkeypatch.setattr(s3_module, "_LIST_PAGE_SIZE", 3)

        rc = s3_module.main(["--bucket", bucket, "--yes", "--region", "us-east-1"])

        assert rc == 0
        versions = s3.list_object_versions(Bucket=bucket)["Versions"]
        assert len(versions) == key_count
        assert all(version["IsLatest"] for version in versions)


def test_s3_reported_errors_cause_exit_code_1(s3_module, monkeypatch):
    with mock_aws():
        s3 = boto3.client("s3", region_name="us-east-1")
        bucket = "error-bucket"
        _make_versioned_bucket(s3, bucket)
        _put_two_versions(s3, bucket, "a.txt")

        def fake_delete_entries(_client, _bucket, _entries):
            return 0, [{"Key": "a.txt", "VersionId": "whatever", "Code": "AccessDenied", "Message": "nope"}]

        monkeypatch.setattr(s3_module, "delete_entries", fake_delete_entries)

        rc = s3_module.main(["--bucket", bucket, "--yes", "--region", "us-east-1"])

        assert rc == 1


def test_s3_listing_failure_returns_exit_code_1(s3_module, monkeypatch):
    class _FailingS3Client:
        def get_paginator(self, _name):
            raise ClientError({"Error": {"Code": "NoSuchBucket", "Message": "nope"}}, "ListObjectVersions")

    class _FailingSession:
        def client(self, _service_name):
            return _FailingS3Client()

    monkeypatch.setattr(s3_module.boto3, "Session", lambda **_kw: _FailingSession())

    rc = s3_module.main(["--bucket", "doesnt-matter", "--region", "us-east-1"])

    assert rc == 1
