#!/usr/bin/env python3
"""Print a single AWS/EBS CloudWatch metric value for a Zabbix item.

Queries the AWS/EBS CloudWatch namespace for the requested metric. The
volume to report on can be given directly with --volume-id, or resolved
from an EC2 instance with --instance-id (its attached volume(s) are looked
up with describe_volumes). Most instances have more than one attached
volume (at least a root volume), so when --instance-id matches more than
one, pass --device too (e.g.
/dev/sdf) to pick which one; otherwise the script fails and lists the
candidates so the caller can pick one explicitly with --device or
--volume-id.
"""

import argparse
import logging
import sys
from datetime import datetime, timedelta, timezone

import boto3
from botocore.exceptions import BotoCoreError, ClientError

LOG = logging.getLogger("ebs_stats")

DEFAULT_REGION = "eu-west-1"
DEFAULT_PERIOD = 300
DEFAULT_MINUTES = 10

# Metric name -> (Zabbix value type, default CloudWatch statistic).
# Covers the standard AWS/EBS CloudWatch metrics. Ops/bytes/time metrics
# default to "Sum" (AWS reports them as per-period totals), gauge-like
# metrics default to "Average", matching how the AWS console graphs them.
METRICS = {
    "VolumeReadOps": ("int", "Sum"),
    "VolumeWriteOps": ("int", "Sum"),
    "VolumeReadBytes": ("float", "Sum"),
    "VolumeWriteBytes": ("float", "Sum"),
    "VolumeTotalReadTime": ("float", "Sum"),
    "VolumeTotalWriteTime": ("float", "Sum"),
    "VolumeIdleTime": ("float", "Sum"),
    "VolumeQueueLength": ("float", "Average"),
    "VolumeThroughputPercentage": ("float", "Average"),
    "VolumeConsumedReadWriteOps": ("int", "Sum"),
    "BurstBalance": ("float", "Average"),
}

STATISTIC_CHOICES = ["Average", "Sum", "Minimum", "Maximum", "SampleCount"]


def build_arg_parser():
    parser = argparse.ArgumentParser(
        description="Print one AWS/EBS CloudWatch metric value for a Zabbix item.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "examples:\n"
            "  %(prog)s --volume-id vol-1234567890abcdef0 --metric VolumeReadOps\n"
            "  %(prog)s --instance-id i-0123456789abcdef0 --device /dev/sdf \\\n"
            "      --metric BurstBalance --profile prod --region us-east-1 -v\n"
        ),
    )
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument(
        "--volume-id",
        dest="volume_id",
        help="EBS VolumeId to report on",
    )
    target.add_argument(
        "-i", "--instance-id",
        dest="instance_id",
        help=(
            "EC2 InstanceId whose attached volume(s) are resolved via "
            "describe_volumes; combine with --device if more than one "
            "volume is attached (most instances have at least a root volume)"
        ),
    )
    parser.add_argument(
        "-d", "--device",
        default=None,
        help=(
            "Device name to disambiguate when --instance-id has more than "
            "one attached volume, e.g. /dev/sdf (default: none, ignored "
            "with --volume-id)"
        ),
    )
    parser.add_argument(
        "-m", "--metric",
        required=True,
        choices=sorted(METRICS),
        help="CloudWatch metric to report",
    )
    parser.add_argument(
        "-r", "--region",
        default=DEFAULT_REGION,
        help="AWS region (default: %(default)s)",
    )
    parser.add_argument(
        "--profile",
        default=None,
        help="AWS named profile to use (default: standard credential chain)",
    )
    parser.add_argument(
        "--period",
        type=int,
        default=DEFAULT_PERIOD,
        help="CloudWatch statistics period in seconds (default: %(default)s)",
    )
    parser.add_argument(
        "--minutes",
        type=int,
        default=DEFAULT_MINUTES,
        help="How many minutes to look back from now (default: %(default)s)",
    )
    parser.add_argument(
        "--statistic",
        choices=STATISTIC_CHOICES,
        default=None,
        help="CloudWatch statistic to request (default: chosen automatically per metric)",
    )
    parser.add_argument(
        "-v", "--verbose",
        action="count",
        default=0,
        help="Increase verbosity (-v for INFO, -vv for DEBUG)",
    )
    return parser


def configure_logging(verbosity):
    level = logging.WARNING
    if verbosity == 1:
        level = logging.INFO
    elif verbosity >= 2:
        level = logging.DEBUG
    logging.basicConfig(level=level, format="%(levelname)s: %(message)s", stream=sys.stderr)


def resolve_volume_id(ec2_client, instance_id, device=None):
    """Return the single EBS VolumeId attached to instance_id (at `device`
    when given). Raises ValueError if zero or more than one volume match.
    """
    filters = [{"Name": "attachment.instance-id", "Values": [instance_id]}]
    if device:
        filters.append({"Name": "attachment.device", "Values": [device]})

    candidates = []  # list of (volume_id, device)
    paginator = ec2_client.get_paginator("describe_volumes")
    for page in paginator.paginate(Filters=filters):
        for volume in page.get("Volumes", []):
            for attachment in volume.get("Attachments", []):
                if attachment.get("InstanceId") == instance_id:
                    candidates.append((volume["VolumeId"], attachment.get("Device", "?")))

    if not candidates:
        if device:
            raise ValueError(f"No EBS volume found attached to instance {instance_id} at device {device}")
        raise ValueError(f"No EBS volumes found attached to instance {instance_id}")
    if len(candidates) > 1:
        listing = ", ".join(f"{vol_id} ({dev})" for vol_id, dev in candidates)
        raise ValueError(
            "Instance %s has more than one attached volume (%s); "
            "use --device or --volume-id to pick one" % (instance_id, listing)
        )
    return candidates[0][0]


def main(argv=None):
    parser = build_arg_parser()
    args = parser.parse_args(argv)
    configure_logging(args.verbose)

    session = boto3.Session(profile_name=args.profile, region_name=args.region)
    cloudwatch = session.client("cloudwatch")

    volume_id = args.volume_id
    if volume_id is None:
        ec2_client = session.client("ec2")
        try:
            volume_id = resolve_volume_id(ec2_client, args.instance_id, args.device)
        except ValueError as exc:
            LOG.error("%s", exc)
            return 1
        except (ClientError, BotoCoreError) as exc:
            LOG.error("Error resolving volumes for instance %s: %s", args.instance_id, exc)
            return 1
        LOG.info("Resolved instance %s to volume %s", args.instance_id, volume_id)

    metric = args.metric
    value_type, default_statistic = METRICS[metric]
    statistic = args.statistic or default_statistic

    end = datetime.now(timezone.utc)
    start = end - timedelta(minutes=args.minutes)

    try:
        response = cloudwatch.get_metric_statistics(
            Namespace="AWS/EBS",
            MetricName=metric,
            Dimensions=[{"Name": "VolumeId", "Value": volume_id}],
            StartTime=start,
            EndTime=end,
            Period=args.period,
            Statistics=[statistic],
        )
    except (ClientError, BotoCoreError) as exc:
        LOG.error("Error running ebs_stats: %s", exc)
        return 1

    datapoints = response.get("Datapoints", [])
    if not datapoints:
        LOG.error("No datapoints returned for metric %s on %s", metric, volume_id)
        return 1

    latest = max(datapoints, key=lambda point: point["Timestamp"])
    value = latest[statistic]
    LOG.debug("Latest datapoint for %s: %s", metric, latest)

    if value_type == "float":
        output = "%.4f" % value
    else:
        output = "%i" % value

    print(output)
    return 0


if __name__ == "__main__":
    sys.exit(main())
