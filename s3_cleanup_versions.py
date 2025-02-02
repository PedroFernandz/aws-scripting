#!/usr/bin/python

import boto3

# Configure your S3 bucket and AWS region
bucket_name = "bucket"  # Replace with your actual S3 bucket name
region_name = "us-east-1"  # Specify the AWS region

# Initialize the S3 client using boto3
s3_client = boto3.client("s3", region_name=region_name)

def delete_all_versions(bucket):
    """
    Deletes all object versions and delete markers in the specified S3 bucket.
    
    This function retrieves all versions and delete markers from the bucket
    and permanently deletes them using the AWS S3 API.
    
    :param bucket: Name of the S3 bucket where objects should be deleted.
    """
    
    print(f"Deleting all object versions and delete markers in bucket: {bucket}")

    try:
        # Retrieve the list of object versions in the specified bucket
        response = s3_client.list_object_versions(Bucket=bucket)

        # Check if there are object versions and delete them
        if "Versions" in response:
            for version in response["Versions"]:
                key = version["Key"]
                version_id = version["VersionId"]
                print(f"Deleting object: {key}, VersionID: {version_id}")
                
                # Delete the specific object version
                s3_client.delete_object(Bucket=bucket, Key=key, VersionId=version_id)

        # Check if there are delete markers and remove them
        if "DeleteMarkers" in response:
            for marker in response["DeleteMarkers"]:
                key = marker["Key"]
                version_id = marker["VersionId"]
                print(f"Deleting delete marker: {key}, VersionID: {version_id}")
                
                # Delete the delete marker (acts like an object version)
                s3_client.delete_object(Bucket=bucket, Key=key, VersionId=version_id)

        print("Deletion process completed successfully.")

    except Exception as e:
        # Handle any exceptions that occur during the deletion process
        print(f"Error while deleting versions: {str(e)}")

# Execute the function to delete all object versions and delete markers
delete_all_versions(bucket_name)
