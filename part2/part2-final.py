#!/usr/bin/env python3
import argparse
import os
import time

import googleapiclient.discovery
import google.auth

# call the program with:
# python3 part2-final.py PROJECT_ID --zone us-west1-b --instance blog-instance

# The code produces:
# A snapshot of the disk from Part 1: base-snapshot-blog-instance
# Specifically, read instance → find boot disk → snapshot that disk
# 3 copies of the snapshot - Instances: blog-instance-clone-1, -2, -3
# A file with the timing of each copy: TIMING.md

# Defines a helper that blocks until a zonal operation finishes (VM insert, 
# disk snapshot on a zonal disk, etc.).
# compute is the API client.
# returns the final operation JSON as a Python dict.
def wait_for_local_operation(compute, project: str, zone: str, operation: str) -> dict:
    # Print this once to alert the user that the operation is happening
    print("Waiting for operation to finish...")
    while True:
        result = (
            # Calls the Compute Engine API:
            # “zoneOperations.get” is the endpoint to check status of a zonal operation.
            # .execute() actually sends the HTTP request and returns the response dict.
            compute.zoneOperations()
            .get(project=project, zone=zone, operation=operation)
            .execute()
        )
        if result["status"] == "DONE":
            print("done.")
            if "error" in result:
                raise Exception(result["error"])
            return result
        # Wait 1 second between polls so that the API isn't overwhelmed
        time.sleep(1)

# We are taking a snapshot of the disk.
# This function finds the boot disk’s resource name (in our case: blog-instance)
# that belongs to the VM.
def get_boot_disk_name(compute, project: str, zone: str, instance_name: str) -> str:
    """Returns the *disk resource name* (not the full URL) for the instance's boot disk."""
     # Fetches the VM’s full instance resource, which is a big dictionary.
    inst = compute.instances().get(project=project, zone=zone, instance=instance_name).execute()
    # inst["disks"] is a list of disks attached to the VM. Otherwise, sends an error message   
    disks = inst.get("disks", [])
    if not disks:
        raise RuntimeError(f"No disks found on instance {instance_name}")

    # Find the disk in the list has "boot": True, otherwise fall back to the first disk.
    boot = None
    for d in disks:
        if d.get("boot"):
            boot = d
            break
    if boot is None:
        boot = disks[0]

    # source is the full URL of the disk resource, e.g.
    # https://www.googleapis.com/compute/v1/projects/<project>/zones/<zone>/disks/<disk-name>    
    source = boot.get("source")  
    if not source:
        raise RuntimeError("Could not find boot disk 'source' URL on instance.")

    # Extracts just the disk name from the URL.
    # splitting on "/disks/" leaves "<disk-name>" at the end.
    return source.split("/disks/")[-1]

# Creates a snapshot of the instance’s boot disk and returns the snapshot name.
# Snapshots are immutible.
# Note: the naming convention follows the instructions we were given
def create_snapshot_from_instance_boot_disk(compute, project: str, zone: str, instance_name: str) -> str:
    disk_name = get_boot_disk_name(compute, project, zone, instance_name)
    snapshot_name = f"base-snapshot-{instance_name}"

    print(f"Boot disk for {instance_name}: {disk_name}")
    print(f"Creating snapshot: {snapshot_name}")

    body = {"name": snapshot_name}
    # API call to the Disk interface.
    # disk=disk_name is the boot disk we extracted.
    # Returns an operation object. Does not the snapshot itself.
    op = compute.disks().createSnapshot(
        project=project,
        zone=zone,
        disk=disk_name,
        body=body,
    ).execute()

    # Wait until the snapshot operation is done.
    # Return the snapshot’s name so later code can reference it.
    wait_for_local_operation(compute, project, zone, op["name"])
    return snapshot_name

# Creates a new VM whose boot disk is initialized from the snapshot and returns timing metrics.
def time_create_clone(compute, project: str, zone: str, instance_name: str, snapshot_name: str) -> dict:
    # When you create a disk from a snapshot, you reference it as a global resource path.
    snapshot_link = f"global/snapshots/{snapshot_name}"

    config = {
        "name": instance_name, # VM name in GCP.
        "machineType": f"zones/{zone}/machineTypes/f1-micro", # VM size. Used f1-micro like 
                                                              # we did for Part 1
        "tags": {"items": ["allow-5000"]}, # applies the firewall tag so port 5000 is reachable.
        "disks": [
            {
                "boot": True, # Boot disk
                "autoDelete": True, # deleting the VM deletes this disk too.
                # Cloning - create a brand new boot disk whose initial contents come 
                # from the snapshot.
                "initializeParams": {
                    "sourceSnapshot": snapshot_link,
                },
            }
        ],
        # attaches to the default VPC 
        # adds an external IP (ONE_TO_ONE_NAT) so you can reach Flask.
        "networkInterfaces": [
            {
                "network": "global/networks/default",
                "accessConfigs": [{"type": "ONE_TO_ONE_NAT", "name": "External NAT"}],
            }
        ],
    }

    # Start timing
    # perf_counter() measures “wall clock” time with high precision.
    # os.times() measures CPU time used by your local Python process, which are usually 
    # small compared to real time because most work is remote in GCP.
    t0_cpu = os.times()
    t0_real = time.perf_counter()

    # Create VM and wait for operation completion.
    op = compute.instances().insert(project=project, zone=zone, body=config).execute()
    wait_for_local_operation(compute, project, zone, op["name"])

    # Stop timing.
    t1_real = time.perf_counter()
    t1_cpu = os.times()

    # For each cloned instance - Calculate the real, CPU user and CPU Sys times and 
    # return them
    return {
        "instance": instance_name,
        "real_seconds": t1_real - t0_real,
        "user_seconds": t1_cpu.user - t0_cpu.user,
        "sys_seconds": t1_cpu.system - t0_cpu.system,
    }

# Output the timing results to a markdown table.
def write_timing_md(results: list[dict], filename: str = "TIMING.md") -> None:
    lines = []
    lines.append("# Clone Timing Results\n")
    lines.append("| instance | real_seconds | user_seconds | sys_seconds |\n")
    lines.append("|---|---:|---:|---:|\n")
    for r in results:
        lines.append(
            f'| {r["instance"]} | {r["real_seconds"]:.3f} | {r["user_seconds"]:.3f} | {r["sys_seconds"]:.3f} |\n'
        )
    with open(filename, "w", encoding="utf-8") as f:
        f.writelines(lines)
    print(f"Wrote {filename}")

# Run the program:
# 1) Build the API client for Compute Engine v1.
# 2) Snapshot the boot disk of our Part 1 VM.
# 3) Creates 3 clone VMs, timing each.
# 4) Save the timings to TIMING.md.

def main(project: str, zone: str, base_instance: str) -> None:
    compute = googleapiclient.discovery.build("compute", "v1")

    # 1) Snapshot the boot disk of the Part 1 instance
    snapshot_name = create_snapshot_from_instance_boot_disk(compute, project, zone, base_instance)

    # 2) Create 3 clones + time them
    results = []
    for i in range(1, 4):
        clone_name = f"{base_instance}-clone-{i}"
        print(f"Creating clone instance: {clone_name} from snapshot {snapshot_name}")
        results.append(time_create_clone(compute, project, zone, clone_name, snapshot_name))

    # 3) Save results
    write_timing_md(results)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Part 2: Snapshot boot disk, then create 3 clones.")
    parser.add_argument("project_id", help="Your Google Cloud project ID.")
    parser.add_argument("--zone", default="us-west1-b", help="Zone where the base instance lives.")
    parser.add_argument("--instance", default="blog-instance", help="Name of the Part 1 instance.")
    args = parser.parse_args()

    main(args.project_id, args.zone, args.instance)