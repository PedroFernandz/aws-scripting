#!/usr/bin/env python3
"""Print a single AWS/EC2 CloudWatch metric value for a Zabbix item.

The instance is identified by the value of its Name tag (optionally after
stripping a configurable suffix), not by its raw InstanceId. The resolved
instance and the requested metric are then queried in CloudWatch and the
latest value is printed to stdout as a single number, ready to be consumed
by a Zabbix external check / UserParameter.
"""

import argparse
import logging
import sys
from datetime import datetime, timedelta, timezone

import boto3
from botocore.exceptions import BotoCoreError, ClientError

LOG = logging.getLogger("ec2_stats")

DEFAULT_REGION = "eu-west-1"
DEFAULT_PERIOD = 600
DEFAULT_MINUTES = 10

# Metric name -> (Zabbix value type, default CloudWatch statistic).
# These are the real metrics AWS publishes under the AWS/EC2 CloudWatch
# namespace, plus the "CPUIdle" convenience metric derived below from
# CPUUtilization. Ops/bytes/packets metrics default to "Sum" (AWS reports
# them as per-period totals), status checks default to "Maximum" (so a
# single failed check in the period is never averaged away), and the
# remaining gauge-like metrics default to "Average".
METRICS = {
    "CPUUtilization": ("float", "Average"),
    "CPUIdle": ("float", "Average"),
    "CPUCreditBalance": ("float", "Average"),
    "CPUCreditUsage": ("float", "Sum"),
    "CPUSurplusCreditBalance": ("float", "Average"),
    "NetworkIn": ("float", "Sum"),
    "NetworkOut": ("float", "Sum"),
    "NetworkPacketsIn": ("int", "Sum"),
    "NetworkPacketsOut": ("int", "Sum"),
    "DiskReadOps": ("int", "Sum"),
    "DiskWriteOps": ("int", "Sum"),
    "DiskReadBytes": ("float", "Sum"),
    "DiskWriteBytes": ("float", "Sum"),
    "EBSReadOps": ("int", "Sum"),
    "EBSWriteOps": ("int", "Sum"),
    "EBSReadBytes": ("float", "Sum"),
    "EBSWriteBytes": ("float", "Sum"),
    "StatusCheckFailed": ("int", "Maximum"),
    "StatusCheckFailed_Instance": ("int", "Maximum"),
    "StatusCheckFailed_System": ("int", "Maximum"),
}

STATISTIC_CHOICES = ["Average", "Sum", "Minimum", "Maximum", "SampleCount"]


def build_arg_parser():
    parser = argparse.ArgumentParser(
        description="Print one AWS/EC2 CloudWatch metric value for a Zabbix item.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "examples:\n"
            "  %(prog)s --instance-id web01 --metric CPUUtilization\n"
            "  %(prog)s --instance-id web01 --strip-suffix .srv.example.com \\\n"
            "      --metric CPUCreditBalance --profile prod --region us-east-1 -v\n"
        ),
    )
    parser.add_argument(
        "-i", "--instance-id",
        required=True,
        help="Value of the EC2 instance's Name tag (not its InstanceId)",
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
        "--strip-suffix",
        default=None,
        help=(
            "Suffix to strip from --instance-id before matching it against "
            "the instance's Name tag, e.g. '.srv.example.com' "
            "(default: none, the value is used as-is)"
        ),
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


def resolve_instance_id(ec2_client, identifier, strip_suffix=None):
    """Resolve an EC2 InstanceId from the Name tag matching `identifier`.

    If several instances share the same Name tag, the last one returned by
    the API wins; no error is raised.
    """
    lookup_name = identifier
    if strip_suffix:
        lookup_name = lookup_name.removesuffix(strip_suffix)

    instance_id = None
    paginator = ec2_client.get_paginator("describe_instances")
    pages = paginator.paginate(Filters=[{"Name": "tag:Name", "Values": [lookup_name]}])
    for page in pages:
        for reservation in page.get("Reservations", []):
            for instance in reservation.get("Instances", []):
                instance_id = instance["InstanceId"]
    return instance_id


def main(argv=None):
    parser = build_arg_parser()
    args = parser.parse_args(argv)
    configure_logging(args.verbose)

    session = boto3.Session(profile_name=args.profile, region_name=args.region)
    ec2_client = session.client("ec2")
    cloudwatch = session.client("cloudwatch")

    try:
        instance_id = resolve_instance_id(ec2_client, args.instance_id, args.strip_suffix)
    except (ClientError, BotoCoreError) as exc:
        LOG.error("Error resolving instance id for %s: %s", args.instance_id, exc)
        return 1

    if not instance_id:
        LOG.error("No instance ID found for %s", args.instance_id)
        return 1

    LOG.info("Resolved %s to %s", args.instance_id, instance_id)

    metric = args.metric
    value_type, default_statistic = METRICS[metric]
    aws_metric_name = "CPUUtilization" if metric == "CPUIdle" else metric
    statistic = args.statistic or default_statistic

    end = datetime.now(timezone.utc)
    start = end - timedelta(minutes=args.minutes)

    try:
        response = cloudwatch.get_metric_statistics(
            Namespace="AWS/EC2",
            MetricName=aws_metric_name,
            Dimensions=[{"Name": "InstanceId", "Value": instance_id}],
            StartTime=start,
            EndTime=end,
            Period=args.period,
            Statistics=[statistic],
        )
    except (ClientError, BotoCoreError) as exc:
        LOG.error("Error running ec2_stats: %s", exc)
        return 1

    datapoints = response.get("Datapoints", [])
    if datapoints:
        latest = max(datapoints, key=lambda point: point["Timestamp"])
        value = latest[statistic]
        LOG.debug("Latest datapoint for %s: %s", metric, latest)
    else:
        # A metric with no datapoints (e.g. CPU credit metrics on an
        # instance type without credits) is reported as 100 rather than
        # failing.
        LOG.debug("No datapoints returned for %s; defaulting to 100", metric)
        value = 100.0

    if metric == "CPUIdle":
        value = 100 - value

    if value_type == "float":
        output = "%.4f" % value
    else:
        output = "%i" % value

    print(output)
    return 0


if __name__ == "__main__":
    sys.exit(main())
