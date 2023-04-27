#!/usr/bin/python3

# Import necessary modules
import warnings
import datetime
import sys
from optparse import OptionParser
import boto.ec2.cloudwatch
import boto3

# Ignore deprecation warnings
warnings.filterwarnings("ignore", category=DeprecationWarning)

# Define function to dump object attributes
def dump(obj):
    for attr in dir(obj):
        print("obj.%s = %s" % (attr, getattr(obj, attr)))


# Define command line options
parser = OptionParser()
parser.add_option("-i", "--instance-id", dest="instance_id", help="InstanceIdentifier")
parser.add_option("-a", "--access-key", dest="access_key", help="AWS Access Key")
parser.add_option("-k", "--secret-key", dest="secret_key", help="AWS Secret Access Key")
parser.add_option("-m", "--metric", dest="metric", help="EC2 cloudwatch metric")
parser.add_option("-r", "--region", dest="region", help="EC2 region")

# Parse command line arguments
(options, args) = parser.parse_args()

# Ensure required arguments are provided
if options.instance_id is None:
    parser.error("-i InstanceIdentifier is required")
if options.access_key is None:
    parser.error("-a AWS Access Key is required")
if options.secret_key is None:
    parser.error("-k AWS Secret Key is required")
if options.metric is None:
    parser.error("-m EC2 cloudwatch metric is required")
  
# Define metrics to query
metrics = {
    "CPUUtilization": {"type": "float", "value": None},
    "CPUIdle": {"type": "float", "value": None},
    "ReadLatency": {"type": "float", "value": None},
    "DatabaseConnections": {"type": "int", "value": None},
    "FreeableMemory": {"type": "float", "value": None},
    "ReadIOPS": {"type": "int", "value": None},
    "CPUCreditUsage": {"type": "float", "value": None},
    "CPUCreditBalance": {"type": "int", "value": None},
    "WriteLatency": {"type": "float", "value": None},
    "WriteThroughput": {"type": "float", "value": None},
    "WriteIOPS": {"type": "int", "value": None},
    "SwapUsage": {"type": "float", "value": None},
    "ReadThroughput": {"type": "float", "value": None},
    "DiskQueueDepth": {"type": "float", "value": None},
    "ReplicaLag": {"type": "int", "value": None},
    "NetworkReceiveThroughput": {"type": "float", "value": None},
    "NetworkTransmitThroughput": {"type": "float", "value": None},
    "FreeStorageSpace": {"type": "float", "value": None}
}

# Set time range to query
end = datetime.datetime.utcnow()
start = end - datetime.timedelta(minutes=10)

# Set default region
if options.region is None:
    options.region = "eu-west-1"

# Connect to CloudWatch
for r in boto.ec2.cloudwatch.regions():
    if r.name == options.region:
        region = r
        break

conn = boto.ec2.cloudwatch.CloudWatchConnection(options.access_key, options.secret_key, region=region)

# Find instance ID from provided identifier
instance_id = ""
ec2 = boto3.resource('ec2', aws_access_key_id=options.access_key, aws_secret_access_key=options.secret_key, region_name=region.name)
for instance in ec2.instances.filter(Filters=[{'Name': 'tag:Name', 'Values': [options.instance_id.replace(".srv.joopbox.com", "")]}]):
    instance_id = instance.instance_id

if not instance_id:
    print("No instance ID found for %s" % options.instance_id)
    sys.exit(1)

average = 0
for k, vh in metrics.items():
    if k == options.metric:
        if k == "CPUIdle":
            aws_k = "CPUUtilization"
        else:
            aws_k = k

        try:
            if k == "CPUCreditUsage":
                cw_operation = "Sum"
            else:
                cw_operation = "Average"
            res = conn.get_metric_statistics(600, start, end, aws_k, "AWS/EC2", cw_operation, {"InstanceId": instance_id})
        except (Exception, e):
            print("status err Error running ec2_stats: %s" % e.error_message)
            sys.exit(1)

        # Instance with credits
        if len(res) > 0:
            average = res[-1][cw_operation]  # last item in result set
        # Instance without credits
        else:
            average = 100
        if k == "FreeStorageSpace" or k == "FreeableMemory":
            average = average / 1024.0 ** 3.0

        if k == "CPUIdle":
            average = 100 - average

        if vh["type"] == "float":
            metrics[k]["value"] = "%.4f" % average
        if vh["type"] == "int":
            metrics[k]["value"] = "%i" % average

        print("%s" % (vh["value"]))
        break

