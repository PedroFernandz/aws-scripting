#!/usr/bin/python3

# Import required libraries
import boto3
import json
import argparse
import base64
import logging

# AWSLambda class for invoking and handling AWS Lambda functions
class AWSLambda:
    # Initialize the AWSLambda class with required parameters
    def __init__(self, region, access_key, secret_key, debug):
        # Setup the boto3 client with provided credentials and region
        self.client = boto3.client('lambda', region_name=region, aws_access_key_id=access_key, aws_secret_access_key=secret_key)
        
        # Configure logging format and level
        log_fmt = '%(asctime)s- %(message)s'
        logging.basicConfig(level=debug.upper(), format=log_fmt)

    # Invoke AWS Lambda function with provided parameters
    def invokeLambda(self, funcname, invocationtype, logtype, payload):
        logging.debug(json.dumps(payload))
        response = self.client.invoke(
            FunctionName=funcname,
            InvocationType=invocationtype,
            LogType=logtype,
            Payload=json.dumps(payload)
        )
        return response

    # Display the result of the Lambda function execution
    def dispResult(self, response):
        logging.debug("Exec Result: ")
        logging.debug(base64.b64decode(response["LogResult"]))

        payload = json.loads(response['Payload'].read().decode('utf-8'))
        logging.info(payload["message"])

# Main function
if __name__ == '__main__':
    # Argument parser for command line options
    parser = argparse.ArgumentParser(description='Get Lambda Parameter.')

    # Add command line arguments
    parser.add_argument('-r', '--region', required=True, help='Set AWS region name (e.g.: ap-northeast-1)')
    parser.add_argument('-a', '--accesskey', help='Set AWS Access Key ID')
    parser.add_argument('-s', '--secretkey', help='Set AWS Secret Access Key')
    parser.add_argument('-f', '--funcname', required=True, help='Set Function Name of AWS Lambda (e.g: arn:aws:lambda:ap-northeast-1:*******:function:Test')
    parser.add_argument('-i', '--invocationtype', default='RequestResponse', help='Set invocation type: RequestResponse(sync), Event(async) or DryRun(test)')
    parser.add_argument('-l', '--logtype', default='Tail', help='Set log data type. You can set this parameter only if you specify the InvocationType with value RequestResponse. Tail: Returns base64 encoded last 4KB of log. None: No returns log.')
    parser.add_argument('-p', '--payload', default={}, help='Set payload if you want to include instanceid, AWS Service and so on. The payload must be json. (e.g. {"instance_id":"xxxxx"})')
    parser.add_argument('-d', '--debuglevel', default="info", help='Debug Level: INFO, WARNING, ERROR')

    # Parse command line arguments
    args = parser.parse_args()

    # Initialize AWSLambda class
    awslambda = AWSLambda(region=args.region, access_key=args.accesskey, secret_key=args.secretkey, debug=args.debuglevel)
    
    # Invoke the Lambda function and get response
    response = awslambda.invokeLambda(funcname=args.funcname, invocationtype=args.invocationtype, logtype=args.logtype, payload=args.payload)

    # Display the result of Lambda function execution
    awslambda.dispResult(response)
