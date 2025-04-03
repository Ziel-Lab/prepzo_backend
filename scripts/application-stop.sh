#!/bin/bash
set -e

echo "Stopping application"

# Check if the application is running and stop it
if [ -f /home/ec2-user/prepzo_bot/prepzo.pid ]; then
    PID=$(cat /home/ec2-user/prepzo_bot/prepzo.pid)
    if ps -p $PID > /dev/null; then
        echo "Stopping process with PID: $PID"
        kill $PID || true
        sleep 5
        # Force kill if still running
        if ps -p $PID > /dev/null; then
            echo "Force killing process with PID: $PID"
            kill -9 $PID || true
        fi
    else
        echo "Process with PID $PID is not running"
    fi
    rm -f /home/ec2-user/prepzo_bot/prepzo.pid
else
    echo "PID file not found, application may not be running"
fi

echo "Application stop script completed" 