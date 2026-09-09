#!/bin/bash
set -e

VENV_DIR="$(cd "$(dirname "$0")" && pwd)/.venv"

python3 -m venv "$VENV_DIR"
source "$VENV_DIR/bin/activate"

pip install --upgrade pip
pip install ansible boto3 botocore

ansible-galaxy collection install -r "$(dirname "$0")/requirements.yml"

echo ""
echo "Virtual environment ready at $VENV_DIR"
echo "Activate it with:"
echo "  source $VENV_DIR/bin/activate"
echo ""
echo "Then configure AWS credentials (if not already done):"
echo "  aws configure"
echo ""
echo "Then run the playbook:"
echo '  ansible-playbook site.yml -e vnc_password="..." -e rhel_vm_password="..." -e rhel_vm_root_password="..." -e rhel_iso_url="https://..."'
