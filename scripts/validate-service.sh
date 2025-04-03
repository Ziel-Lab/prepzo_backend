#!/bin/bash
set -e

echo "Validating application"

# Check if process is running
if [ -f /home/ec2-user/prepzo_bot/prepzo.pid ]; then
    PID=$(cat /home/ec2-user/prepzo_bot/prepzo.pid)
    if ps -p $PID > /dev/null; then
        echo "Application is running with PID: $PID"
        
        # Wait for application to fully start (adjust sleep time as needed)
        sleep 10
        
        # Check log file for any startup errors
        if grep -i "error" /home/ec2-user/prepzo_bot/application.log; then
            echo "Errors found in application log"
            exit 1
        else
            echo "No startup errors found in log"
        fi
        
        echo "Application validation successful"
        exit 0
    else
        echo "Process with PID $PID is not running"
        exit 1
    fi
else
    echo "PID file not found"
    exit 1
fi 