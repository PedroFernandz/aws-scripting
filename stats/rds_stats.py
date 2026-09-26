#!/usr/bin/env python3
"""Print a single AWS/RDS CloudWatch metric value for a Zabbix item.

Given a DBInstanceIdentifier and a metric name, this script queries
CloudWatch for the most recent value of the requested metric and prints it
to stdout as a single number, ready to be consumed by a Zabbix external
check / UserParameter.
"""

import argparse
import logging
import sys
from datetime import datetime, timedelta, timezone

import boto3
from botocore.exceptions import BotoCoreError, ClientError

LOG = logging.getLogger("rds_stats")

DEFAULT_REGION = "eu-west-1"
DEFAULT_PERIOD = 60
DEFAULT_MINUTES = 5

# Metric name -> (Zabbix value type, default CloudWatch statistic).
# Every metric defaults to "Average", including CPUCreditUsage/CPUCreditBalance.
METRICS = {
    "CPUUtilization": ("float", "Average"),
    "CPUCreditUsage": ("float", "Average"),
    "CPUCreditBalance": ("float", "Average"),
    "ReadLatency": ("float", "Average"),
    "DatabaseConnections": ("int", "Average"),
    "FreeableMemory": ("float", "Average"),
    "ReadIOPS": ("int", "Average"),
    "WriteLatency": ("float", "Average"),
    "WriteThroughput": ("float", "Average"),
    "WriteIOPS": ("int", "Average"),
    "SwapUsage": ("float", "Average"),
    "ReadThroughput": ("float", "Average"),
    "DiskQueueDepth": ("float", "Average"),
    "ReplicaLag": ("int", "Average"),
    "NetworkReceiveThroughput": ("float", "Average"),
    "NetworkTransmitThroughput": ("float", "Average"),
    "FreeStorageSpace": ("float", "Average"),
}

# Metrics reported by AWS in bytes; converted to GiB before printing.
BYTES_TO_GIB_METRICS = {"FreeStorageSpace", "FreeableMemory"}

STATISTIC_CHOICES = ["Average", "Sum", "Minimum", "Maximum", "SampleCount"]


def build_arg_parser():
    parser = argparse.ArgumentParser(
        description="Print one AWS/RDS CloudWatch metric value for a Zabbix item.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "examples:\n"
            "  %(prog)s --instance-id mydbinstance --metric CPUUtilization\n"
            "  %(prog)s --instance-id mydbinstance --metric FreeStorageSpace \\\n"
            "      --profile prod --region us-east-1 -v\n"
        ),
    )
    parser.add_argument(
        "-i", "--instance-id",
        required=True,
        help="RDS DBInstanceIdentifier",
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
        help="CloudWatch statistic to request (default: Average for every metric)",
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


def main(argv=None):
    parser = build_arg_parser()
    args = parser.parse_args(argv)
    configure_logging(args.verbose)

    session = boto3.Session(profile_name=args.profile, region_name=args.region)
    cloudwatch = session.client("cloudwatch")

    metric = args.metric
    value_type, default_statistic = METRICS[metric]
    statistic = args.statistic or default_statistic

    end = datetime.now(timezone.utc)
    start = end - timedelta(minutes=args.minutes)

    try:
        response = cloudwatch.get_metric_statistics(
            Namespace="AWS/RDS",
            MetricName=metric,
            Dimensions=[{"Name": "DBInstanceIdentifier", "Value": args.instance_id}],
            StartTime=start,
            EndTime=end,
            Period=args.period,
            Statistics=[statistic],
        )
    except (ClientError, BotoCoreError) as exc:
        LOG.error("Error running rds_stats: %s", exc)
        return 1

    datapoints = response.get("Datapoints", [])
    if not datapoints:
        LOG.error("No datapoints returned for metric %s on %s", metric, args.instance_id)
        return 1

    latest = max(datapoints, key=lambda point: point["Timestamp"])
    value = latest[statistic]
    LOG.debug("Latest datapoint for %s: %s", metric, latest)

    if metric in BYTES_TO_GIB_METRICS:
        value = value / 1024.0 ** 3.0

    if value_type == "float":
        output = "%.4f" % value
    else:
        output = "%i" % value

    print(output)
    return 0


if __name__ == "__main__":
    sys.exit(main())
