#!/usr/bin/env python3
"""Expose Amazon CloudWatch metrics of a resource to Zabbix.

Without --send, prints a Zabbix low-level discovery (LLD) JSON payload
listing the CloudWatch metrics available for the given resource. With
--send, fetches the latest datapoint of each of those metrics and pushes
them to a Zabbix server/proxy as trapper items, using the item key
"cloudwatch.metric[<metric name>]" (or "cloudwatch.metric[<metric
name>.<AWS service name>]" for --service billing).
"""

import argparse
import calendar
import json
import logging
import os
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

import boto3
from botocore.exceptions import BotoCoreError, ClientError
from zabbix_utils import ItemValue, ModuleBaseException, Sender

LOG = logging.getLogger(__name__)

# CloudWatch dimension used to identify a resource, keyed by --service.
DEFAULT_DIMENSIONS = {
    "ec2": "InstanceId",
    "rds": "DBInstanceIdentifier",
    "elb": "LoadBalancerName",
    "ebs": "VolumeId",
    "billing": "Currency",
}

# AWS/ELB metrics that are cumulative counts and must be fetched with the
# "Sum" statistic instead of the default "Average".
SUM_STAT_METRICS = [
    {"namespace": "AWS/ELB", "metricname": name}
    for name in (
        "RequestCount",
        "HTTPCode_Backend_2XX",
        "HTTPCode_Backend_3XX",
        "HTTPCode_Backend_4XX",
        "HTTPCode_Backend_5XX",
        "HTTPCode_ELB_4XX",
        "HTTPCode_ELB_5XX",
    )
]


@dataclass
class Metric:
    name: str = ""
    namespace: str = ""
    unit: str = ""
    dimensions: list = field(default_factory=list)


class CloudWatchZabbix:
    """Lists/reads CloudWatch metrics for a single AWS resource."""

    def __init__(self, client, service, identity, dimension_name, hostname=None, timerange_min=5):
        self.client = client
        self.service = service
        self.identity = identity
        self.dimension_name = dimension_name
        self.hostname = hostname or identity
        self.timerange_min = timerange_min

    def _dimension_value(self):
        return "USD" if self.service == "billing" else self.identity

    def get_metric_list(self):
        paginator = self.client.get_paginator("list_metrics")
        metrics = []
        for page in paginator.paginate(
            Dimensions=[{"Name": self.dimension_name, "Value": self._dimension_value()}]
        ):
            for data in page["Metrics"]:
                metric = Metric(
                    name=data["MetricName"],
                    namespace=data["Namespace"],
                    dimensions=data["Dimensions"],
                )
                if self.service == "elb":
                    for dimension in data["Dimensions"]:
                        if dimension["Name"] == "AvailabilityZone":
                            metric.name = f"{data['MetricName']}.{dimension['Value']}"
                metrics.append(metric)
        return metrics

    def _service_name_for(self, metric):
        """AWS service name for a billing metric, or "" if not per-service."""
        if self.service != "billing":
            return self.service
        return next(
            (d["Value"] for d in metric.dimensions if d["Name"] == "ServiceName"), ""
        )

    @staticmethod
    def _stat_type_for(metric):
        target = {"namespace": metric.namespace, "metricname": metric.name}
        for sum_stat_metric in SUM_STAT_METRICS:
            # Metric names can carry an ".<AvailabilityZone>" suffix (see
            # get_metric_list), so match on prefix.
            if metric.name.find(sum_stat_metric["metricname"]) == 0:
                target["metricname"] = sum_stat_metric["metricname"]
        return "Sum" if target in SUM_STAT_METRICS else "Average"

    def get_metric_stats(
        self, metric_name, metric_namespace, service_name, timerange_min,
        stat_type="Average", period_sec=300,
    ):
        if self.service == "billing":
            dimensions = [{"Name": self.dimension_name, "Value": "USD"}]
            if service_name:
                dimensions.insert(0, {"Name": "ServiceName", "Value": service_name})
        else:
            dimensions = [{"Name": self.dimension_name, "Value": self.identity}]

        if self.service == "elb":
            split_metric_name = metric_name.split(".")
            if len(split_metric_name) == 2:
                metric_name, availability_zone = split_metric_name
                dimensions.append({"Name": "AvailabilityZone", "Value": availability_zone})

        end = datetime.now(timezone.utc)
        start = end - timedelta(minutes=timerange_min)
        return self.client.get_metric_statistics(
            Namespace=metric_namespace,
            MetricName=metric_name,
            Dimensions=dimensions,
            StartTime=start,
            EndTime=end,
            Period=period_sec,
            Statistics=[stat_type],
        )

    def _annotate_units(self, metrics):
        for metric in metrics:
            if self.service == "billing":
                metric.unit = "USD"
                continue
            service_name = self._service_name_for(metric)
            stats = self.get_metric_stats(
                metric.name, metric.namespace, service_name, self.timerange_min
            )
            for datapoint in stats["Datapoints"]:
                metric.unit = datapoint["Unit"]
                break
        return metrics

    @staticmethod
    def _datapoint_value(datapoint):
        if "Average" in datapoint:
            return str(datapoint["Average"])
        if "Sum" in datapoint:
            return str(datapoint["Sum"])
        return None

    def _item_key(self, metric, service_name):
        if self.service == "billing":
            return f"cloudwatch.metric[{metric.name}.{service_name}]"
        return f"cloudwatch.metric[{metric.name}]"

    def _latest_item_value(self, metric, service_name):
        stat_type = self._stat_type_for(metric)
        stats = self.get_metric_stats(
            metric.name, metric.namespace, service_name, self.timerange_min, stat_type
        )
        datapoints = stats.get("Datapoints") or []
        if not datapoints:
            return None
        datapoint = max(datapoints, key=lambda d: d["Timestamp"])
        value = self._datapoint_value(datapoint)
        if value is None:
            return None
        clock = calendar.timegm(datapoint["Timestamp"].utctimetuple())
        return ItemValue(self.hostname, self._item_key(metric, service_name), value, clock)

    def build_send_items(self):
        """Build the list of ItemValue objects for the latest datapoints."""
        items = []
        for metric in self.get_metric_list():
            service_name = self._service_name_for(metric)
            item = self._latest_item_value(metric, service_name)
            if item is not None:
                items.append(item)
        return items

    def build_lld_payload(self):
        """Build the Zabbix low-level discovery JSON payload."""
        data = []
        for metric in self._annotate_units(self.get_metric_list()):
            entry = {
                "{#METRIC.NAME}": metric.name,
                "{#METRIC.NAMESPACE}": metric.namespace,
                "{#METRIC.UNIT}": metric.unit,
            }
            if self.service == "billing":
                entry["{#METRIC.SERVICENAME}"] = self._service_name_for(metric)
            data.append(entry)
        return {"data": data}


def configure_logging(verbosity):
    level = logging.WARNING
    if verbosity == 1:
        level = logging.INFO
    elif verbosity >= 2:
        level = logging.DEBUG
    logging.basicConfig(level=level, format="%(asctime)s %(levelname)s %(message)s")


def parse_args(argv=None):
    services_help = ", ".join(f"{name} ({dim})" for name, dim in DEFAULT_DIMENSIONS.items())
    parser = argparse.ArgumentParser(
        description=(
            "Print a Zabbix low-level discovery JSON payload listing the CloudWatch metrics "
            "of an AWS resource, or send their latest datapoint to Zabbix as trapper items."
        ),
        epilog=(
            "Examples:\n"
            "  cloudwatch_zabbix.py --identity i-0123456789abcdef0 ec2\n"
            "  cloudwatch_zabbix.py --profile prod --region eu-west-1 --identity i-0123456789abcdef0 "
            "--send --zabbix-server zabbix.example.com ec2\n"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--profile", help="AWS named profile to use (default: default profile/credential chain)"
    )
    parser.add_argument(
        "--region", help="AWS region name (default: profile/environment default)"
    )
    parser.add_argument(
        "-i", "--identity", required=True,
        help="Resource identifier for the CloudWatch dimension (e.g. an EC2 instance ID, an "
             "RDS DB instance identifier, an ELB name or an EBS volume ID)",
    )
    parser.add_argument(
        "-H", "--hostname",
        help="Zabbix host name to send data as (default: same as --identity)",
    )
    parser.add_argument(
        "--dimension-name",
        help="Override the CloudWatch dimension name used to identify the resource "
             f"(default depends on --service: {services_help})",
    )
    parser.add_argument(
        "-t", "--timerange", type=int, default=5,
        help="Lookback window in minutes when fetching statistics (default: %(default)s)",
    )
    parser.add_argument(
        "--send", action="store_true",
        help="Send the latest datapoint of each metric to Zabbix instead of printing the "
             "low-level discovery JSON",
    )
    parser.add_argument(
        "--zabbix-server", default=os.environ.get("ZABBIX_SERVER", "localhost"),
        help="Zabbix server/proxy address for the sender (env: ZABBIX_SERVER, "
             "default: %(default)s)",
    )
    parser.add_argument(
        "--zabbix-port", type=int, default=int(os.environ.get("ZABBIX_PORT", 10051)),
        help="Zabbix server/proxy trapper port (env: ZABBIX_PORT, default: %(default)s)",
    )
    parser.add_argument(
        "-v", "--verbose", action="count", default=0,
        help="Increase logging verbosity (-v for INFO, -vv for DEBUG)",
    )
    parser.add_argument(
        "service", metavar="service_name",
        help=f"AWS service to query ({', '.join(DEFAULT_DIMENSIONS)}, or any value when "
             "--dimension-name is given)",
    )
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    configure_logging(args.verbose)

    dimension_name = args.dimension_name or DEFAULT_DIMENSIONS.get(args.service)
    if not dimension_name:
        LOG.error("Unknown service %r: pass --dimension-name explicitly.", args.service)
        return 1

    session = boto3.Session(profile_name=args.profile, region_name=args.region)
    aws_zabbix = CloudWatchZabbix(
        client=session.client("cloudwatch"),
        service=args.service,
        identity=args.identity,
        dimension_name=dimension_name,
        hostname=args.hostname,
        timerange_min=args.timerange,
    )

    try:
        if args.send:
            items = aws_zabbix.build_send_items()
            if not items:
                LOG.warning(
                    "No datapoints found in the last %s minute(s); nothing sent", args.timerange
                )
                return 0
            sender = Sender(server=args.zabbix_server, port=args.zabbix_port)
            response = sender.send(items)
            print(f"processed: {response.processed}; failed: {response.failed}; total: {response.total}")
            return 1 if response.failed else 0

        print(json.dumps(aws_zabbix.build_lld_payload()))
        return 0
    except (ClientError, BotoCoreError) as exc:
        LOG.error("AWS error: %s", exc)
        return 1
    except ModuleBaseException as exc:
        LOG.error("Zabbix error: %s", exc)
        return 1
    except OSError as exc:
        LOG.error("Could not reach Zabbix server %s:%s: %s", args.zabbix_server, args.zabbix_port, exc)
        return 1


if __name__ == "__main__":
    sys.exit(main())
