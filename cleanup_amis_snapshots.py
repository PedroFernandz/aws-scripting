#!/usr/bin/python
import boto3
from botocore.exceptions import ClientError

# AWS region where EC2 resources are managed
REGION = "us-east-1"

# Create an EC2 client using Boto3
ec2_client = boto3.client("ec2", region_name=REGION)

def deregister_ami_and_delete_snapshot(snapshot_id):
    """
    Searches for AMIs associated with the given snapshot, deregisters them, 
    and then deletes the snapshot.

    Args:
        snapshot_id (str): The ID of the snapshot to be deleted.
    """
    try:
        # Step 1: Find AMIs associated with the given snapshot
        images = ec2_client.describe_images(
            Owners=["self"],
            Filters=[{"Name": "block-device-mapping.snapshot-id", "Values": [snapshot_id]}]
        )
        images = images.get("Images", [])

        # If there are associated AMIs, deregister them
        if images:
            for image in images:
                image_id = image["ImageId"]
                print(f"Deregistering AMI: {image_id} associated with snapshot {snapshot_id}")
                ec2_client.deregister_image(ImageId=image_id)

        # Step 2: Attempt to delete the snapshot
        print(f"Deleting snapshot: {snapshot_id}")
        ec2_client.delete_snapshot(SnapshotId=snapshot_id)
        print(f"Snapshot {snapshot_id} successfully deleted.")

    except ClientError as e:
        # Handle specific AWS errors
        if "InvalidSnapshot.InUse" in str(e):
            print(f"Snapshot {snapshot_id} is currently in use by another AMI or resource.")
        else:
            print(f"Error while processing snapshot {snapshot_id}: {e}")

def main():
    """
    Retrieves a list of snapshots created in 2016, checks for dependencies (AMIs), 
    and deletes them upon confirmation.
    """
    try:
        # Step 1: Retrieve all snapshots created in 2016
        response = ec2_client.describe_snapshots(
            OwnerIds=["self"],
            Filters=[{"Name": "start-time", "Values": ["2022-*"]}]
        )
        snapshots = [snapshot["SnapshotId"] for snapshot in response.get("Snapshots", [])]

        # If no snapshots are found, exit
        if not snapshots:
            print("No snapshots from 2016 found for deletion.")
            return

        print(f"Found {len(snapshots)} snapshots created in 2016.")

        # Step 2: Confirm deletion with the user
        confirm = input(f"Are you sure you want to delete {len(snapshots)} snapshots? (yes/no): ").strip()
        if confirm.lower() not in ["yes", "y"]:
            print("Operation canceled.")
            return

        # Step 3: Delete each snapshot
        for snapshot_id in snapshots:
            deregister_ami_and_delete_snapshot(snapshot_id)

    except ClientError as e:
        print(f"Error while retrieving snapshots: {e}")

if __name__ == "__main__":
    main()
