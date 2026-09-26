#!/usr/bin/env python3
"""Invoke an AWS Lambda function and print its result for Zabbix.

Invokes the given Lambda function with a JSON payload and prints the
"message" field of its JSON response to stdout, so it can be used as the
value of a Zabbix item (e.g. a UserParameter or external check).
"""

import argparse
import base64
import json
import logging
import sys

import boto3
from botocore.exceptions import BotoCoreError, ClientError

LOG = logging.getLogger(__name__)


def invoke_lambda(client, function_name, invocation_type, log_type, payload):
    """Invoke `function_name` and return the raw boto3 response."""
    LOG.debug("Invoking %s (%s) with payload: %s", function_name, invocation_type, json.dumps(payload))
    return client.invoke(
        FunctionName=function_name,
        InvocationType=invocation_type,
        LogType=log_type,
        Payload=json.dumps(payload).encode("utf-8"),
    )


def extract_message(response):
    """Pull the "message" field out of a Lambda invocation response.

    Logs the execution log (when present) at DEBUG level and raises
    ValueError if the response cannot be turned into a Zabbix item value,
    e.g. because the function errored or the invocation type was not
    RequestResponse.
    """
    if "LogResult" in response:
        LOG.debug(
            "Execution log:\n%s",
            base64.b64decode(response["LogResult"]).decode("utf-8", "replace"),
        )

    body = response["Payload"].read()
    if not body:
        raise ValueError(
            "Empty response payload (the invocation type may not be RequestResponse)"
        )

    payload = json.loads(body.decode("utf-8"))
    if response.get("FunctionError"):
        raise ValueError(f"Lambda function raised an error: {payload}")

    return payload["message"]


def configure_logging(verbosity):
    level = logging.WARNING
    if verbosity == 1:
        level = logging.INFO
    elif verbosity >= 2:
        level = logging.DEBUG
    logging.basicConfig(level=level, format="%(asctime)s %(levelname)s %(message)s")


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description=(
            "Invoke an AWS Lambda function and print the 'message' field of its JSON "
            "response, for use as a Zabbix item value."
        ),
        epilog=(
            "Examples:\n"
            "  lambda_zabbix.py --profile prod --region eu-west-1 --function-name my-check\n"
            "  lambda_zabbix.py --region us-east-1 --function-name my-check "
            '--payload \'{"instance_id": "i-0123456789abcdef0"}\'\n'
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
        "-f", "--function-name", required=True,
        help="Name or ARN of the Lambda function to invoke "
             "(e.g. my-function or arn:aws:lambda:us-east-1:123456789012:function:my-function)",
    )
    parser.add_argument(
        "-i", "--invocation-type", default="RequestResponse",
        choices=["RequestResponse", "Event", "DryRun"],
        help="Lambda invocation type: RequestResponse (sync), Event (async) or DryRun "
             "(test) (default: %(default)s)",
    )
    parser.add_argument(
        "-l", "--log-type", default="Tail", choices=["Tail", "None"],
        help="Whether to request the last 4 KB of execution logs (only used with "
             "RequestResponse) (default: %(default)s)",
    )
    parser.add_argument(
        "-p", "--payload", default="{}",
        help="JSON payload to send to the function, e.g. '{\"instance_id\": \"xxxxx\"}' "
             "(default: %(default)s)",
    )
    parser.add_argument(
        "-v", "--verbose", action="count", default=0,
        help="Increase logging verbosity (-v for INFO, -vv for DEBUG)",
    )
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    configure_logging(args.verbose)

    try:
        payload = json.loads(args.payload)
    except json.JSONDecodeError as exc:
        LOG.error("--payload is not valid JSON: %s", exc)
        return 1

    session = boto3.Session(profile_name=args.profile, region_name=args.region)
    client = session.client("lambda")

    try:
        response = invoke_lambda(
            client, args.function_name, args.invocation_type, args.log_type, payload
        )
        message = extract_message(response)
    except (ClientError, BotoCoreError) as exc:
        LOG.error("AWS error invoking %s: %s", args.function_name, exc)
        return 1
    except (ValueError, KeyError, json.JSONDecodeError) as exc:
        LOG.error("Unexpected Lambda response: %s", exc)
        return 1

    print(message)
    return 0


if __name__ == "__main__":
    sys.exit(main())
