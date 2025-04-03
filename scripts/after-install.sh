#!/bin/bash
set -e

echo "Running after install script"

cd /home/ec2-user/prepzo_bot

# Create virtual environment if it doesn't exist
if [ ! -d "venv" ]; then
    echo "Creating virtual environment"
    python3 -m venv venv
fi

# Activate virtual environment and install dependencies
echo "Installing dependencies"
source venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt

# Retrieve secrets from AWS Parameter Store and create .env file
echo "Setting up environment variables"
ENV_FILE="/home/ec2-user/prepzo_bot/.env"
touch $ENV_FILE
> $ENV_FILE  # Clear file content

# Retrieve parameters from SSM Parameter Store and add to .env file
PARAMS_PATH="/prepzo-bot"
aws ssm get-parameters-by-path --path $PARAMS_PATH --with-decryption --query "Parameters[*].[Name,Value]" --output text | while read -r name value; do
    param_name=$(basename "$name")
    echo "$param_name=$value" >> $ENV_FILE
done

# Set appropriate permissions for the .env file
chmod 600 $ENV_FILE

echo "After install script completed" 