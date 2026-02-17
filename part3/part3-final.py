#!/usr/bin/env python3

# Part 3 of the lab is to create VM-1, VM-1 authenticates, then VM-1 creates VM-2
# VM-2 is the Flask VM.
#
# Big-picture - Part 3 builds on Parts 1 and 2, where:
# - Part 1 programatically create the vm infrastructure 
#   went from laptop -> Google API -> VM Flask
#   created a base machine where my laptop holds the credentials
# 
# - Part 2 takes a snapshot and clones the vm
#   went from laptop -> Google API -> Snapshot -> 3 cloned VMs
#   creates pre-configured machine from a base image
# 
# - Part 3 delegates infrastructure creation to another machine
#   laptop -> VM-1 -> VM-2 Flask
#   machines inside the cloud can manage cloud infrastructure where 
#   VM-1 holds the credentials to create VM-2.
#
# Part 3 includes the following where VM-1 and VM-2 are both in zone = 'us-west1-b'
# 1. part3-final.py 
#    - authenticates - Attach a service account to VM-1.
#      VM-1 uses ADC via the metadata server (google.auth.default()), which gives 
#      it short-lived access tokens. No JSON key file is needed anywhere.
#      This seemed like a safer option. I based this decision on information that 
#      A service account JSON key is long-lived, can be copied, works from anywhere,
#      must be manually rotated and is hard to revoke if leaked. Conversely, metadata-
#      based service account tokens only work from that VM, are short-lived, are 
#      auto-rotated, cannot be downloaded as a private key and can be revoked by 
#      removing the service account from the VM.
#        - VM-1’s attached service account needs IAM permissions to create VM-2 
#          For example, Compute Instance Admin v1. VM-2 does not receive any key 
#          files or credentials via metadata. Therefore, no service account
#          keys are distributed to VM-2.
#    - creates VM-1 
#      - Writes out VM-1 startup script (runs on VM-1 at boot)
#          - Installs python + pip + google client libraries
#          - Downloads the metadata into files from the metadata server
#          - Runs vm1-launch-vm2-code.py
#    - passes metadata to VM-1: 
#       a) VM-2 startup script, 
#       b) VM-1 "launch VM-2" Python code
#    - Writes out vm1-launch-vm2-code.py
#        - Authenticates via the metadata-based service account 
#        - Calls Compute API to create VM-2 with the Flask startup script
#          Creates VM-2 with startup-script metadata that is the Flask/systemd script.
#        - Ensures VM-2 gets the network tag allow-5000 so your firewall applies.
#        - VM-2 should be created with an external IP (ONE_TO_ONE_NAT) so that we will 
#          be able to reach Flask from my laptop.
#        - Waits and prints VM-2 IP to VM-1 logs
#        NOTE: VM-1 does not pass service credentials to VM-2
# 2. clean-up.py
#    - deletes all of the instances and snapshot


# Code for part3-final

# Run using the following code in the google cloud shell
# python3 part3-final.py PROJECT_ID --zone us-west1-b --vm1-name vm-1 --vm2-name vm-2-flask
# Then:
# Go to Compute Engine → VM Instances
# Watch VM-1 logs (serial output or SSH and sudo tail -f /var/log/vm1-startup.log)
# Once VM-2 is created, you should see a line like:
# Flask should be at: http://<VM2_IP>:5000

import argparse
import time
import googleapiclient.discovery


# Create the global and zonal wait helper functions (as was done for the other parts of the lab)
def wait_for_global_operation(compute, project: str, operation: str) -> dict:
    print("Waiting for global operation to finish...")
    while True:
        result = compute.globalOperations().get(project=project, operation=operation).execute()
        if result["status"] == "DONE":
            print("done.")
            if "error" in result:
                raise Exception(result["error"])
            return result
        time.sleep(1)


def wait_for_local_operation(compute, project: str, zone: str, operation: str) -> dict:
    print("Waiting for zonal operation to finish...")
    while True:
        result = compute.zoneOperations().get(project=project, zone=zone, operation=operation).execute()
        if result["status"] == "DONE":
            print("done.")
            if "error" in result:
                raise Exception(result["error"])
            return result
        time.sleep(1)


# Firewall (global) functions from part 1.
def firewall_rule_exists(compute, project: str, name: str) -> bool:
    resp = compute.firewalls().list(project=project).execute()
    for rule in resp.get("items", []):
        if rule.get("name") == name:
            return True
    return False


def ensure_allow_5000_firewall(compute, project: str, network_tag: str = "allow-5000") -> None:
    rule_name = "allow-5000"
    if firewall_rule_exists(compute, project, rule_name):
        print("Firewall rule allow-5000 already exists.")
        return

    body = {
        "name": rule_name,
        "network": "global/networks/default",
        "direction": "INGRESS",
        "priority": 1000,
        "sourceRanges": ["0.0.0.0/0"],
        "targetTags": [network_tag],
        "allowed": [{"IPProtocol": "tcp", "ports": ["5000"]}],
    }

    print("Creating firewall rule allow-5000 ...")
    op = compute.firewalls().insert(project=project, body=body).execute()
    wait_for_global_operation(compute, project, op["name"])


# Create VM-1 
# First step is to build the vm1 start-up script that is called later in
# create instance
def build_vm1_startup_script() -> str:
    """
    VM-1 startup script:
    - installs python3 + pip + curl
    - installs google api client libs
    - downloads metadata attributes into files
    - runs launcher python to create VM-2
    """    
    return r"""#!/bin/bash
set -euxo pipefail

LOG=/var/log/vm1-startup.log
exec > >(tee -a "$LOG") 2>&1

apt-get update
apt-get install -y python3 python3-pip curl ca-certificates

python3 -m pip install --upgrade pip
python3 -m pip install google-api-python-client google-auth

WORKDIR=/opt/part3
mkdir -p "$WORKDIR"
cd "$WORKDIR"

# Helper to fetch instance metadata attributes
fetch_attr () {
  local key="$1"
  curl -fsS -H "Metadata-Flavor: Google" \
    "http://metadata.google.internal/computeMetadata/v1/instance/attributes/${key}"
}

# Download launcher python + VM-2 startup script from metadata
fetch_attr "vm1-launcher-py" > vm1-launcher.py
fetch_attr "vm2-startup-script" > vm2-startup.sh
chmod +x vm2-startup.sh

# Run launcher (creates VM-2)
python3 vm1-launcher.py
"""

# build the vm1 launcher script that creates VM-2 with the VM-2 starter script.
# these are called later in the code.
def build_vm1_launcher_py(project: str, zone: str, vm2_name: str) -> str:
    """
    This code runs on VM-1.
    It uses ADC via metadata server (google.auth.default()) to call Compute API
    and create VM-2 with the VM-2 startup script (downloaded to /opt/part3/vm2-startup.sh).
    """
    # Self-contained: reads vm2-startup.sh from local disk (written by VM-1 startup script)
    return f'''#!/usr/bin/env python3
import time
import googleapiclient.discovery
import google.auth

PROJECT = "{project}"
ZONE = "{zone}"
VM2_NAME = "{vm2_name}"
VM2_STARTUP_PATH = "/opt/part3/vm2-startup.sh"

def wait_for_local_operation(compute, project: str, zone: str, operation: str) -> None:
    while True:
        result = compute.zoneOperations().get(project=project, zone=zone, operation=operation).execute()
        if result["status"] == "DONE":
            if "error" in result:
                raise Exception(result["error"])
            return
        time.sleep(1)

def get_instance_external_ip(compute, project: str, zone: str, instance: str) -> str | None:
    inst = compute.instances().get(project=project, zone=zone, instance=instance).execute()
    nics = inst.get("networkInterfaces", [])
    if not nics:
        return None
    access = nics[0].get("accessConfigs", [])
    if not access:
        return None
    return access[0].get("natIP")

def main():
    # ADC on VM uses metadata server + attached service account
    creds, _ = google.auth.default(scopes=["https://www.googleapis.com/auth/cloud-platform"])
    compute = googleapiclient.discovery.build("compute", "v1", credentials=creds)

    with open(VM2_STARTUP_PATH, "r", encoding="utf-8") as f:
        vm2_startup = f.read()

    # VM-2 from Ubuntu 22.04 LTS family
    image_response = (
        compute.images()
        .getFromFamily(project="ubuntu-os-cloud", family="ubuntu-2204-lts")
        .execute()
    )
    source_disk_image = image_response["selfLink"]

    config = {{
        "name": VM2_NAME,
        "machineType": f"zones/{{ZONE}}/machineTypes/f1-micro",
        "tags": {{"items": ["allow-5000"]}},
        "disks": [
            {{
                "boot": True,
                "autoDelete": True,
                "initializeParams": {{
                    "sourceImage": source_disk_image,
                }},
            }}
        ],
        "networkInterfaces": [
            {{
                "network": "global/networks/default",
                "accessConfigs": [{{"type": "ONE_TO_ONE_NAT", "name": "External NAT"}}],
            }}
        ],
        # VM-2 can use default SA; it does NOT receive any key files via metadata
        "serviceAccounts": [
            {{
                "email": "default",
                "scopes": ["https://www.googleapis.com/auth/cloud-platform"],
            }}
        ],
        "metadata": {{
            "items": [
                {{"key": "startup-script", "value": vm2_startup}},
            ]
        }},
    }}

    print(f"Creating VM-2 {{VM2_NAME}} in {{ZONE}} ...")
    op = compute.instances().insert(project=PROJECT, zone=ZONE, body=config).execute()
    wait_for_local_operation(compute, PROJECT, ZONE, op["name"])

    ip = get_instance_external_ip(compute, PROJECT, ZONE, VM2_NAME)
    if ip:
        print(f"VM-2 external IP: {{ip}}")
        print(f"Flask should be at: http://{{ip}}:5000")
    else:
        print("VM-2 created but no external IP found yet.")

if __name__ == "__main__":
    main()
'''

# VM-2 starter script used by the launcher to create VM-2
def build_vm2_startup_script() -> str:
    """
    Your updated Flask/systemd startup script (from your last message),
    with two small robustness tweaks:
    - use python3 -m pip consistently
    - verify service is active before READY
    """
    return r"""#!/bin/bash
set -euxo pipefail

LOG=/var/log/flask-startup.log
exec > >(tee -a "$LOG") 2>&1

APP_DIR=/opt/flask-tutorial
REPO_DIR="$APP_DIR/flask-tutorial"
VENV_DIR="$APP_DIR/venv"
READY_FILE="$APP_DIR/READY"

mkdir -p "$APP_DIR"
cd "$APP_DIR"

apt-get update
apt-get install -y python3 python3-pip python3-venv git ca-certificates

if [ ! -d "$REPO_DIR" ]; then
  git clone https://github.com/cu-csci-4253-datacenter/flask-tutorial "$REPO_DIR"
fi

cd "$REPO_DIR"

# Create and use a venv to avoid Ubuntu distutils package conflicts (e.g., blinker)
python3 -m venv "$VENV_DIR"
source "$VENV_DIR/bin/activate"

python -m pip install --upgrade pip
python -m pip install -e .

export FLASK_APP=flaskr
python -m flask init-db

cat >/etc/systemd/system/flask-tutorial.service <<'EOF'
[Unit]
Description=Flask Tutorial App
After=network.target

[Service]
WorkingDirectory=/opt/flask-tutorial/flask-tutorial
Environment=FLASK_APP=flaskr
ExecStart=/opt/flask-tutorial/venv/bin/python -m flask run -h 0.0.0.0 -p 5000
Restart=always
RestartSec=2

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable --now flask-tutorial.service

sleep 2
systemctl is-active --quiet flask-tutorial.service
touch "$READY_FILE"
"""


# Function to create VM-1 instance
def create_vm1_instance(
    compute,
    project: str,
    zone: str,
    vm1_name: str,
    vm1_service_account_email: str,
    vm1_startup_script: str,
    vm1_launcher_py: str,
    vm2_startup_script: str,
) -> dict:
    machine_type = f"zones/{zone}/machineTypes/f1-micro"

    # Ubuntu image for VM-1
    image_response = (
        compute.images()
        .getFromFamily(project="ubuntu-os-cloud", family="ubuntu-2204-lts")
        .execute()
    )
    source_disk_image = image_response["selfLink"]

    # VM-1 metadata carries code payloads. Metadata is readable on the VM.
    # This is okay in our case since we didn't include a JSON Key.
    metadata_items = [
        {"key": "startup-script", "value": vm1_startup_script},
        {"key": "vm1-launcher-py", "value": vm1_launcher_py},
        {"key": "vm2-startup-script", "value": vm2_startup_script},
    ]

    config = {
        "name": vm1_name,
        "machineType": machine_type,
        "disks": [
            {
                "boot": True,
                "autoDelete": True,
                "initializeParams": {"sourceImage": source_disk_image},
            }
        ],
        "networkInterfaces": [
            {
                "network": "global/networks/default",
                "accessConfigs": [{"type": "ONE_TO_ONE_NAT", "name": "External NAT"}],
            }
        ],
        # Attach a service account to VM-1 (ADC via metadata server). 
        # As explained above, opted not to use JSON key files.
        "serviceAccounts": [
            {
                "email": vm1_service_account_email,  # "default" is allowed
                "scopes": ["https://www.googleapis.com/auth/cloud-platform"],
            }
        ],
        "metadata": {"items": metadata_items},
    }

    print(f"Creating VM-1 {vm1_name} in {zone} ...")
    return compute.instances().insert(project=project, zone=zone, body=config).execute()



# Run the code per the outline above
def main(project: str, zone: str, vm1_name: str, vm2_name: str, vm1_sa_email: str) -> None:
    compute = googleapiclient.discovery.build("compute", "v1")

    ensure_allow_5000_firewall(compute, project, network_tag="allow-5000")

    vm1_startup = build_vm1_startup_script()
    vm2_startup = build_vm2_startup_script()
    vm1_launcher = build_vm1_launcher_py(project, zone, vm2_name)

    op = create_vm1_instance(
        compute=compute,
        project=project,
        zone=zone,
        vm1_name=vm1_name,
        vm1_service_account_email=vm1_sa_email,
        vm1_startup_script=vm1_startup,
        vm1_launcher_py=vm1_launcher,
        vm2_startup_script=vm2_startup,
    )
    wait_for_local_operation(compute, project, zone, op["name"])

    print("\nVM-1 created.")
    print("VM-1 startup will run automatically and then create VM-2.")
    print("To watch progress, check VM-1 serial port output or /var/log/vm1-startup.log on VM-1.")
    print(f"Expected VM-2 name: {vm2_name}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Part 3: Create VM-1 which creates VM-2 (Flask).")
    parser.add_argument("project_id", help="Your Google Cloud project ID.")
    parser.add_argument("--zone", default="us-west1-b", help="Zone for VM-1 and VM-2.")
    parser.add_argument("--vm1-name", default="vm-1", help="Name for VM-1.")
    parser.add_argument("--vm2-name", default="vm-2-flask", help="Name for VM-2 (Flask).")
    parser.add_argument(
        "--vm1-service-account",
        default="default",
        help='Service account email for VM-1 (use "default" for the project default SA).',
    )
    args = parser.parse_args()

    main(args.project_id, args.zone, args.vm1_name, args.vm2_name, args.vm1_service_account)
