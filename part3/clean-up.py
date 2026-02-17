#!/usr/bin/env python3

# in google cloud shell run: 
# python3 clean-up.py PROJECT_ID --instance blog-instance
# Double check that everything was removed with:
# gcloud compute instances list
# gcloud compute disks list
# gcloud compute snapshots list

import argparse
import time
import googleapiclient.discovery
from googleapiclient.errors import HttpError

def wait_for_operation(compute, project, zone, operation):
    while True:
        result = (
            compute.zoneOperations()
            .get(project=project, zone=zone, operation=operation)
            .execute()
        )
        if result["status"] == "DONE":
            return
        time.sleep(1)


def wait_for_global_operation(compute, project, operation):
    while True:
        result = (
            compute.globalOperations()
            .get(project=project, operation=operation)
            .execute()
        )
        if result["status"] == "DONE":
            return
        time.sleep(1)


def delete_instance(compute, project, zone, name):
    print(f"Deleting instance: {name}")
    try:
        op = compute.instances().delete(project=project, zone=zone, instance=name).execute()
        wait_for_operation(compute, project, zone, op["name"])
        print(f"✓ Deleted {name}")
    except HttpError as e:
        if e.resp.status == 404:
            print(f"• {name} not found (already deleted).")
        else:
            raise


def delete_snapshot(compute, project, name):
    print(f"Deleting snapshot: {name}")
    try:
        op = compute.snapshots().delete(project=project, snapshot=name).execute()
        wait_for_global_operation(compute, project, op["name"])
        print(f"✓ Deleted snapshot {name}")
    except HttpError as e:
        if e.resp.status == 404:
            print(f"• Snapshot {name} not found (already deleted).")
        else:
            raise

def main(project, zone, base_instance):
    compute = googleapiclient.discovery.build("compute", "v1")

    clone_names = [
        f"{base_instance}-clone-1",
        f"{base_instance}-clone-2",
        f"{base_instance}-clone-3",
    ]

    snapshot_name = f"base-snapshot-{base_instance}"

    print("The following resources will be deleted:")
    print(f"  Instance: {base_instance}")
    for c in clone_names:
        print(f"  Instance: {c}")
    print(f"  Snapshot: {snapshot_name}")

    confirm = input("Type 'yes' to confirm deletion: ")
    if confirm.lower() != "yes":
        print("Aborting cleanup.")
        return

    # Delete clones first
    for clone in clone_names:
        delete_instance(compute, project, zone, clone)

    # Delete original
    delete_instance(compute, project, zone, base_instance)

    # Delete snapshot
    delete_snapshot(compute, project, snapshot_name)

    print("Cleanup complete.")
    print("You may also want to check for leftover disks manually.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("project_id")
    parser.add_argument("--zone", default="us-west1-b")
    parser.add_argument("--instance", default="blog-instance")

    args = parser.parse_args()

    main(args.project_id, args.zone, args.instance)