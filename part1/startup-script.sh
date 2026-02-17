#!/bin/bash
set -euxo pipefail # makes failures obvious in the log.
                   # -e: exit immediately if any command returns non-zero
                   # -u: error if you use an unset variable
                   # -x: echo commands as they run (great for debugging)
                   # pipefail: if any command in a pipeline fails, the whole 
                   # pipeline fails 

LOG=/var/log/flask-startup.log
exec > >(tee -a "$LOG") 2>&1 # captures all output to /var/log/flask-startup.log.

# Creates and enters a stable working directory.
APP_DIR=/opt/flask-tutorial
REPO_DIR="$APP_DIR/flask-tutorial"
READY_FILE="$APP_DIR/READY"

mkdir -p "$APP_DIR"
cd "$APP_DIR"

# Install dependencies: python3, python3-pip, git (and often python3-venv)
apt-get update
apt-get install -y python3 python3-pip python3-venv git

# Get the app
# Clone the repo: https://github.com/cu-csci-4253-datacenter/flask-tutorial
# If the folder already exists it won't reclone
if [ ! -d "$REPO_DIR" ]; then
  git clone https://github.com/cu-csci-4253-datacenter/flask-tutorial "$REPO_DIR"
fi

# move into the repo directory
cd "$REPO_DIR"

# Create and use a virtual environment to avoid conflicts with OS Python packages
python3 -m venv /opt/flask-tutorial/venv
source /opt/flask-tutorial/venv/bin/activate

python -m pip install --upgrade pip
python -m pip install -e .

export FLASK_APP=flaskr
python -m flask init-db


# Make sure pip is up to date, then install the app
python3 -m pip install --upgrade pip
pip3 install -e .

# Run the app so it’s reachable externally
export FLASK_APP=flaskr

# Initialize DB 
python3 -m flask init-db

# Create a simple systemd service so the app starts on boot - this will make Part 2
# with the creation of clones easier.
# This section of code is a bash shell script writing a systemd service file.
# <<'EOF' means to take everything between here and the line below that says EOF, 
# and write it into the file. Output into /etc/systemd/system/flask-tutorial.service
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

# Mark completion for Part 2 snapshot readiness checks
touch "$READY_FILE"


