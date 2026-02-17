#!/usr/bin/env python3

# reads command-line arguments like project_id, --zone, --name.
import argparse
# file paths used to load startup-script.sh
import os
import time
# Google API client + authentication helper
import googleapiclient.discovery
import google.auth

# The following code was modified from the GitHub repository at: 
# https://github.com/GoogleCloudPlatform/python-docs-samples/tree/main#
# Copyright 2015 Google Inc. https://cloud.google.com/compute/docs/tutorials/python-guide

# The updated code includes the following functions: 
# 1. Wait functions to help with managing interactions with the API calls.
#    a. wait_for_global_operation: operations like creating a firewall are global. 
#       This function sets up the wait time for global operations so that the API 
#       isn't called continuously.
#    b. wait_for_local_operation: VM create/delete/tag are usually local operations. 
#       This function sets up the wait time for global operations so that the API 
#       isn't called continuously. [Note the VM is not deleted in this program bc
#       we need it for Part 2]
# 2. Checking for the firewall and setting up, if needed.
#    a. firewall_rule_exists: Lists firewall rules in the project. Scans them for 
#       one named "allow-5000", since this is what we need to have for the blog to 
#       work.
#    b. ensure_allow_5000_firewall: Ensure firewall for TCP 5000 exists or create it
# 3. Mange the VM instances
#    a. list_instances: provides a list of the running virtual machine instances in 
#       the specified zone.
#    b. set_instance_tags: Sets a “fingerprint” to update instance tags.     
#    c. create_instance: Creates a VM instance in a specified zone
#    d. get_instance_external_ip: Reads the VM details and drills down to find the external
#       NAT IP
# 4. main: runs the program from creating a Compute Engine API client to deleting the VM

# NOTES:
# NAT IP => Network Address Translation (NAT) maps private, local IP addresses (e.g., 192.168.x.x) to a 
# single public IP address for internet communication, acting as an edge router translator. This technique 
# conserves limited IPv4 addresses, improves security by hiding internal network structures, and allows 
# multiple devices to share one, routable IP. 

# Application Default Credentials (ADC) is the recommended strategy for authenticating applications to 
# Google Cloud APIs, as it automatically finds credentials based on the application's environment. This 
# allows code to run in both local development and production environments without changing how 
# authentication is handled. 

"""Using the Compute Engine API to create instances.

Creates a new Compute Engine VM instance and installs the Flask tutorial application.

"""

credentials, project = google.auth.default()

# Some GCP operations are global and not tied to a zone, like creating firewall rules.
# This function polls until the global operation status is "DONE".
def wait_for_global_operation(compute: object, project: str, operation: str) -> dict:
    print("Waiting for global operation to finish...")
    while True:
        result = compute.globalOperations().get(project=project, operation=operation).execute()
        if result["status"] == "DONE":
            print("done.")
            if "error" in result:
                raise Exception(result["error"])
            return result
        time.sleep(1)

# VM create/delete/tag changes are usually zonal operations.
# Same polling logic as globalOperations(), but uses zoneOperations().
# Has a sleep time of 1 to avoid hammering the API in a tight loop.
def wait_for_local_operation(
    compute: object,
    project: str,
    zone: str,
    operation: str,
) -> dict:
    """Waits for the given operation to complete.

    Args:
      compute: an initialized compute service object.
      project: the Google Cloud project ID.
      zone: the name of the zone in which the operation should be executed.
      operation: the operation ID.

    Returns:
      The result of the operation.
    """
    print("Waiting for operation to finish...")
    while True:
        result = (
            compute.zoneOperations()
            .get(project=project, zone=zone, operation=operation)
            .execute()
        )

        if result["status"] == "DONE":
            print("done.")
            if "error" in result:
                raise Exception(result["error"])
            return result

        time.sleep(1)

# Lists firewall rules in the project. 
# Scans them for one named "allow-5000", since this is what we need to have for the blog to work.
def firewall_rule_exists(compute, project: str, name: str) -> bool:
    resp = compute.firewalls().list(project=project).execute()
    for rule in resp.get("items", []):
        if rule.get("name") == name:
            return True
    return False

# Ensure firewall for TCP 5000 exists or create it
def ensure_allow_5000_firewall(compute, project: str, network_tag: str = "allow-5000") -> None:
    rule_name = "allow-5000"

    if firewall_rule_exists(compute, project, rule_name):
        return # If the rule already exists, do nothing.

    # Otherwise, creates the firewall rule to allow 5000
    body = {
        "name": rule_name,
        "network": "global/networks/default",
        "direction": "INGRESS", # inbound traffic
        "priority": 1000,
        "sourceRanges": ["0.0.0.0/0"], # allow from anywhere on the internet. As noted in the video
                                       # this is a bad idea and would be risky for real deployments
        "targetTags": [network_tag], # this firewall rule only applies to VMs with the network tag allow-5000
        "allowed": [{"IPProtocol": "tcp", "ports": ["5000"]}], # allow TCP port 5000
    }

    # If needed, creates the firewall and waits for the global operation to finish
    op = compute.firewalls().insert(project=project, body=body).execute()
    wait_for_global_operation(compute, project, op["name"])
    
# Provides a list of the running virtual machine instances in the specified zone
def list_instances(
    compute: object,
    project: str,
    zone: str,
) -> list:
    """Lists all instances in the specified zone.

    Args:
      compute: an initialized compute service object.
      project: the Google Cloud project ID.
      zone: the name of the zone in which the instances should be listed.

    Returns:
      A list of instances.
    """
    result = compute.instances().list(project=project, zone=zone).execute()
    return result.get("items", [])

# Sets a “fingerprint” to update instance tags.
# Required by Google Cloud, as it prevents you from overwriting someone else’s concurrent update.
# Fetch the instance first to get the current tag fingerprint; otherwise, setTags will fail.
def set_instance_tags(compute, project: str, zone: str, instance: str, tags: list[str]) -> dict:
    inst = compute.instances().get(project=project, zone=zone, instance=instance).execute()
    fingerprint = inst.get("tags", {}).get("fingerprint")
    body = {
        "items": tags,
        "fingerprint": fingerprint,
    }
    return compute.instances().setTags(project=project, zone=zone, instance=instance, body=body).execute()

# Creates a VM instance in a specified zone
def create_instance(
    compute: object,
    project: str,
    zone: str,
    name: str,
) -> str:
    """Creates an instance in the specified zone.

    Args:
      compute: an initialized compute service object.
      project: the Google Cloud project ID.
      zone: the name of the zone in which the instances should be created.
      name: the name of the instance.
      
    Returns:
      The instance object.
    """
    # Choose an OS image:
    # ubuntu-os-cloud → Google’s official Ubuntu image project
    # ubuntu-2204-lts → Ubuntu 22.04 LTS family = Jammy Jellyfish (no longer "Bionic Beaver")
    # getFromFamily() → Always gets the newest patched image in that family
    # selfLink is the URL/identifier of that image to use for the boot disk.
    image_response = (
        compute.images()
        .getFromFamily(project="ubuntu-os-cloud", family="ubuntu-2204-lts")
        .execute()
    )
    source_disk_image = image_response["selfLink"]

    # Configure the machine - Picks an f1-micro machine in that zone, 
    # which is a very small VM. NOTE: f1-micro is no longer free, even though the assignment
    # says free.
    machine_type = f"zones/{zone}/machineTypes/f1-micro"
    
    startup_script = open(
        os.path.join(os.path.dirname(__file__), "startup-script.sh")
    ).read()

    # The VM config dictionary is the request body for instances().insert().    
    config = {
        "name": name,
        "machineType": machine_type,
        # network tag so the firewall rule applies
        "tags": {"items": ["allow-5000"]},
        # Creates a boot disk from the Ubuntu image.
        # autoDelete=True means deleting the VM deletes the disk too.
        "disks": [
            {
                "boot": True,
                "autoDelete": True,
                "initializeParams": {
                    "sourceImage": source_disk_image,
                },
            }
        ],
        # Specify a network interface with NAT to access the public internet.
        # "network": "global/networks/default" means attach this VM to the default VPC network in this project.
        # "accessConfigs": [{"type": "ONE_TO_ONE_NAT", "name": "External NAT"}] means give this VM a public external IP address.
        # One internal IP is mapped to One external IP <- Direct public mapping
        "networkInterfaces": [
            {
                "network": "global/networks/default",
                "accessConfigs": [{"type": "ONE_TO_ONE_NAT", "name": "External NAT"}],
            }
        ],
        # Gives the VM a service account identity and permissions to
        # allow the instance to access cloud storage and logging.        
        "serviceAccounts": [
            {
                "email": "default",
                "scopes": [
                    "https://www.googleapis.com/auth/devstorage.read_write",
                    "https://www.googleapis.com/auth/logging.write",
                ],
            }
        ],
        # Metadata is readable from the instance and allows you to
        # pass configuration from deployment scripts to instances.
        "metadata": {
            "items": [
                {
                    # Startup script is automatically executed by the
                    # instance upon startup.
                    "key": "startup-script",
                    "value": startup_script,
                },               
            ],
        },
    }
    # Sends the request to create the VM, returning an operation object
    # not the VM itself.
    return compute.instances().insert(project=project, zone=zone, body=config).execute()

# Reads the VM details and drills down to find the external NAT IP:
# networkInterfaces[0] → accessConfigs[0] → natIP
# Returns None if there is nothing there yet.
def get_instance_external_ip(compute, project: str, zone: str, instance: str) -> str | None:
    inst = compute.instances().get(project=project, zone=zone, instance=instance).execute()
    nics = inst.get("networkInterfaces", [])
    if not nics:
        return None
    access = nics[0].get("accessConfigs", [])
    if not access:
        return None
    return access[0].get("natIP")

# Runs the program
# creates a Compute Engine API client
# does the firewall rule
# Creates VM
# Sets tags again (just to ensure that this was done, since it is needed
# for the rest of the flow)
# Prints URL
# List instances in the zone

def main(
    project: str,
    zone: str,
    instance_name: str,
    wait=True,
) -> None:
    """Runs the program.

    Args:
      project: the Google Cloud project ID.
      instance_name: the name of the instance.
      wait: whether to wait for the operation to complete.

    Returns:
      None.
    """

    # build('compute','v1') creates a Compute Engine API client.
    compute = googleapiclient.discovery.build("compute", "v1")

    # 1) create firewall rule (we only need to do this once)
    ensure_allow_5000_firewall(compute, project, network_tag="allow-5000")

    # 2) create instance
    operation = create_instance(compute, project, zone, instance_name)
    wait_for_local_operation(compute, project, zone, operation["name"])

    # 3) apply tag using setTags (as required)
    op = set_instance_tags(compute, project, zone, instance_name, ["allow-5000"])
    wait_for_local_operation(compute, project, zone, op["name"])

    # 4) print URL
    ip = get_instance_external_ip(compute, project, zone, instance_name)
    if ip:
        print(f"Your blog is running at http://{ip}:5000")
    else:
        print("Instance has no external IP yet.")
    
    instances = list_instances(compute, project, zone)

    print(f"Instances in project {project} and zone {zone}:")
    for instance in instances:
        print(f' - {instance["name"]}')

    print("It may take a minute for the startup script to finish installing dependencies.")
    if ip:
        print(f"Visit: http://{ip}:5000")
    else:
        print("No external IP yet—check the instance details and try again.")
        

if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("project_id", help="Your Google Cloud project ID.")
    parser.add_argument(
        "--zone", default="us-west1-b", help="Compute Engine zone to deploy to."
    )
    parser.add_argument("--name", default="blog-instance", help="New instance name.")

    args = parser.parse_args()

    main(args.project_id, args.zone, args.name)