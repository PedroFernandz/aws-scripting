#!/usr/bin/env python3
"""Synchronize AWS Auto Scaling group members into Zabbix as hosts.

For every Auto Scaling group found in the target AWS account/region, this
script:
  * creates (or reuses) a Zabbix host group named after the Auto Scaling
    group;
  * creates or updates a Zabbix host for every EC2 instance currently in
    the group, with its private/public IP addresses as agent interfaces;
  * links the Zabbix templates named in the group's "ZabbixTemplates" tag
    (a comma separated list of template names), if present;
  * disables any Zabbix host that belonged to the group but whose instance
    is no longer part of it.
"""

import argparse
import logging
import os
import sys

import boto3
from botocore.exceptions import BotoCoreError, ClientError
from zabbix_utils import ModuleBaseException, ZabbixAPI

LOG = logging.getLogger(__name__)

DEFAULT_TEMPLATE_TAG = "ZabbixTemplates"
DEFAULT_AGENT_PORT = 10050


class AutoScalingZabbixSync:
    """Mirrors EC2 instances of Auto Scaling groups into Zabbix hosts."""

    def __init__(
        self, session, zapi, preferred_interface="Private", agent_port=DEFAULT_AGENT_PORT,
        template_tag=DEFAULT_TEMPLATE_TAG, set_macros=False,
    ):
        self.autoscaling = session.client("autoscaling")
        self.ec2 = session.resource("ec2")
        self.region = session.region_name
        self.zapi = zapi
        self.preferred_interface = preferred_interface
        self.agent_port = agent_port
        self.template_tag = template_tag
        self.set_macros = set_macros

    def iter_groups(self, group_names=None):
        paginator = self.autoscaling.get_paginator("describe_auto_scaling_groups")
        kwargs = {"AutoScalingGroupNames": group_names} if group_names else {}
        for page in paginator.paginate(**kwargs):
            yield from page["AutoScalingGroups"]

    def _interfaces_for(self, instance_id):
        instance = self.ec2.Instance(instance_id)
        private = {
            "type": 1,
            "useip": 1,
            "main": 1 if self.preferred_interface == "Private" else 0,
            "ip": instance.private_ip_address,
            "dns": "",
            "port": str(self.agent_port),
        }
        interfaces = [private]
        if instance.public_ip_address:
            interfaces.append(
                {
                    "type": 1,
                    "useip": 1,
                    "main": 1 if self.preferred_interface == "Public" else 0,
                    "ip": instance.public_ip_address,
                    "dns": "",
                    "port": str(self.agent_port),
                }
            )
        else:
            private["main"] = 1
        return interfaces

    def _find_hostid(self, host_name):
        hosts = self.zapi.host.get({"filter": {"host": host_name}})
        return hosts[0]["hostid"] if hosts else None

    def _upsert_interfaces(self, hostid, interfaces):
        for interface in interfaces:
            interface = dict(interface, hostid=hostid)
            existing = self.zapi.hostinterface.get(
                {"filter": {"hostid": hostid, "ip": interface["ip"]}}
            )
            if existing:
                interface["interfaceid"] = existing[0]["interfaceid"]
                self.zapi.hostinterface.update(interface)
            else:
                self.zapi.hostinterface.create(interface)

    def _upsert_host(self, host_name, interfaces, template_ids, groupid):
        hostid = self._find_hostid(host_name)
        if hostid is None:
            params = {"host": host_name, "interfaces": interfaces, "groups": [{"groupid": groupid}]}
            if template_ids:
                params["templates"] = template_ids
            hostid = self.zapi.host.create(params)["hostids"][0]
            LOG.info("Created Zabbix host %s", host_name)
        else:
            params = {"hostid": hostid, "groups": [{"groupid": groupid}]}
            if template_ids:
                params["templates"] = template_ids
            self.zapi.host.update(params)
            self._upsert_interfaces(hostid, interfaces)
            LOG.info("Updated Zabbix host %s", host_name)
        return hostid

    def _upsert_usermacro(self, hostid, macro, value):
        existing = self.zapi.usermacro.get({"filter": {"macro": macro}, "hostids": hostid})
        if existing:
            self.zapi.usermacro.update({"hostmacroid": existing[0]["hostmacroid"], "value": value})
        else:
            self.zapi.usermacro.create({"hostid": hostid, "macro": macro, "value": value})

    def _get_or_create_hostgroup(self, name):
        existing = self.zapi.hostgroup.get({"filter": {"name": [name]}, "selectHosts": "extend"})
        if existing:
            return existing[0]["groupid"], [h["host"] for h in existing[0]["hosts"]]
        created = self.zapi.hostgroup.create({"name": name})
        LOG.info("Created Zabbix host group %s", name)
        return created["groupids"][0], []

    def _template_ids_for(self, tags):
        names = []
        for tag in tags:
            if tag["Key"] == self.template_tag:
                names = [name.strip() for name in tag["Value"].split(",") if name.strip()]
        if not names:
            return []
        templates = self.zapi.template.get({"filter": {"host": names}})
        return [{"templateid": template["templateid"]} for template in templates]

    def plan(self, group_names=None):
        """Create/update hosts for current instances.

        Returns the list of (host_name, group_name) pairs whose instance is
        no longer in its Auto Scaling group, and that a caller may want to
        disable via disable_hosts().
        """
        stale_hosts = []
        for group in self.iter_groups(group_names):
            group_name = group["AutoScalingGroupName"]
            groupid, existing_hosts = self._get_or_create_hostgroup(group_name)
            template_ids = self._template_ids_for(group.get("Tags", []))

            current_instance_ids = [i["InstanceId"] for i in group["Instances"]]
            for instance_id in current_instance_ids:
                if instance_id in existing_hosts:
                    existing_hosts.remove(instance_id)
                interfaces = self._interfaces_for(instance_id)
                hostid = self._upsert_host(instance_id, interfaces, template_ids, groupid)
                if self.set_macros:
                    self._upsert_usermacro(hostid, "{$REGION}", self.region)

            for stale_host in existing_hosts:
                stale_hosts.append((stale_host, group_name))

        return stale_hosts

    def disable_hosts(self, stale_hosts):
        for host_name, _group_name in stale_hosts:
            hostid = self._find_hostid(host_name)
            if hostid is None:
                LOG.warning("Host %s not found in Zabbix, skipping", host_name)
                continue
            self.zapi.host.update({"hostid": hostid, "status": 1})
            LOG.info("Disabled Zabbix host %s", host_name)


def configure_logging(verbosity):
    level = logging.WARNING
    if verbosity == 1:
        level = logging.INFO
    elif verbosity >= 2:
        level = logging.DEBUG
    logging.basicConfig(level=level, format="%(asctime)s %(levelname)s %(message)s")


def confirm(prompt):
    try:
        answer = input(f"{prompt} [y/N] ")
    except EOFError:
        return False
    return answer.strip().lower() in ("y", "yes")


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description=(
            "Create or update Zabbix hosts for the EC2 instances of AWS Auto Scaling groups, "
            "and disable Zabbix hosts for instances no longer in their group."
        ),
        epilog=(
            "Examples:\n"
            "  autoscaling_zabbix.py --profile prod --region eu-west-1 "
            "--zabbix-url https://zabbix.example.com/api_jsonrpc.php \\\n"
            "      --zabbix-user api-user --zabbix-password secret\n"
            "  autoscaling_zabbix.py --region us-east-1 --group-name my-asg --dry-run\n"
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
        "--group-name", action="append", metavar="NAME",
        help="Only process this Auto Scaling group (repeatable; default: all groups)",
    )
    parser.add_argument(
        "--preferred-interface", choices=["Private", "Public"], default="Private",
        help="Which IP address to mark as the main Zabbix agent interface (default: %(default)s)",
    )
    parser.add_argument(
        "--agent-port", type=int, default=DEFAULT_AGENT_PORT,
        help="Zabbix agent port to register on created host interfaces (default: %(default)s)",
    )
    parser.add_argument(
        "--template-tag", default=DEFAULT_TEMPLATE_TAG,
        help="Auto Scaling group tag key holding a comma separated list of Zabbix template "
             "names to link to each host (default: %(default)s)",
    )
    parser.add_argument(
        "--set-macros", action="store_true",
        help="Also set the {$REGION} user macro on every synced host",
    )
    parser.add_argument(
        "--zabbix-url", default=os.environ.get("ZABBIX_URL"),
        help="Zabbix API URL, e.g. https://zabbix.example.com/api_jsonrpc.php (env: ZABBIX_URL)",
    )
    parser.add_argument(
        "--zabbix-token", default=os.environ.get("ZABBIX_TOKEN"),
        help="Zabbix API token (env: ZABBIX_TOKEN)",
    )
    parser.add_argument(
        "--zabbix-user", default=os.environ.get("ZABBIX_USER"),
        help="Zabbix API username, used if --zabbix-token is not set (env: ZABBIX_USER)",
    )
    parser.add_argument(
        "--zabbix-password", default=os.environ.get("ZABBIX_PASSWORD"),
        help="Zabbix API password, used if --zabbix-token is not set (env: ZABBIX_PASSWORD)",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Show which hosts would be disabled and change nothing",
    )
    parser.add_argument(
        "--yes", action="store_true",
        help="Disable stale hosts without asking for confirmation",
    )
    parser.add_argument(
        "-v", "--verbose", action="count", default=0,
        help="Increase logging verbosity (-v for INFO, -vv for DEBUG)",
    )
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    configure_logging(args.verbose)

    if not args.zabbix_url:
        LOG.error("--zabbix-url (or the ZABBIX_URL environment variable) is required")
        return 1
    if not (args.zabbix_token or (args.zabbix_user and args.zabbix_password)):
        LOG.error("Provide --zabbix-token, or both --zabbix-user and --zabbix-password")
        return 1

    session = boto3.Session(profile_name=args.profile, region_name=args.region)

    try:
        zapi = ZabbixAPI(
            url=args.zabbix_url,
            token=args.zabbix_token,
            user=args.zabbix_user,
            password=args.zabbix_password,
        )

        sync = AutoScalingZabbixSync(
            session=session,
            zapi=zapi,
            preferred_interface=args.preferred_interface,
            agent_port=args.agent_port,
            template_tag=args.template_tag,
            set_macros=args.set_macros,
        )

        stale_hosts = sync.plan(args.group_name)

        if stale_hosts:
            names = ", ".join(sorted({host for host, _ in stale_hosts}))
            if args.dry_run:
                LOG.info("Dry run: would disable %d host(s): %s", len(stale_hosts), names)
            elif args.yes or confirm(
                f"Disable {len(stale_hosts)} host(s) no longer in their Auto Scaling group ({names})?"
            ):
                sync.disable_hosts(stale_hosts)
            else:
                LOG.info("Aborted: no hosts were disabled")
    except (ClientError, BotoCoreError) as exc:
        LOG.error("AWS error: %s", exc)
        return 1
    except ModuleBaseException as exc:
        LOG.error("Zabbix error: %s", exc)
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
