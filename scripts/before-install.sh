#!/bin/bash
set -e

echo "Running before install script"

# Create necessary directories if they don't exist
mkdir -p /home/ec2-user/prepzo_bot
mkdir -p /home/ec2-user/.env

# Install or update dependencies
sudo yum update -y
sudo yum install -y python3 python3-pip python3-devel gcc

# Set proper permissions
sudo chown -R ec2-user:ec2-user /home/ec2-user/prepzo_bot
sudo chown -R ec2-user:ec2-user /home/ec2-user/.env

echo "Before install script completed" 