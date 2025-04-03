#!/bin/bash
set -e

echo "Starting application"

cd /home/ec2-user/prepzo_bot

# Activate virtual environment
source venv/bin/activate

# Start the application in the background and save the PID
nohup python3 main.py start > /home/ec2-user/prepzo_bot/application.log 2>&1 &
echo $! > /home/ec2-user/prepzo_bot/prepzo.pid

echo "Application started with PID: $(cat /home/ec2-user/prepzo_bot/prepzo.pid)"
echo "Application start script completed" 