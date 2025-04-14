#!/usr/bin/env python3
import logging
import sys
import os
import argparse
import time # Import time module

# Add powder directory to path to import ssh module
sys.path.append(os.path.join(os.path.dirname(__file__), 'powder'))
try:
    import powder.ssh as pssh
except ImportError:
    print("Error: Could not import powder.ssh. Ensure powder directory is in the same directory as this script or in PYTHONPATH.", file=sys.stderr)
    sys.exit(1)

# Basic Logging Setup
logging.basicConfig(
    level=logging.DEBUG, # Log everything for now
    format="[%(asctime)s] %(levelname)s:%(name)s:%(message)s",
    datefmt='%Y-%m-%d %H:%M:%S'
)

# Exit Codes
EXIT_SUCCESS = 0
EXIT_SSH_ERROR = 1
EXIT_CMD_ERROR = 2
EXIT_ARG_ERROR = 3
EXIT_MISSING_SECRET = 4 # New exit code for missing secrets
EXIT_USER_TIMEOUT = 5 # New exit code for user creation timeout

def wait_for_user(ssh_conn, username, timeout=300, interval=10):
    """Waits for a user to exist on the remote system."""
    logging.info(f"Waiting for user '{username}' to be created (timeout: {timeout}s)...")
    start_time = time.time()
    while time.time() - start_time < timeout:
        try:
            # Use a simple command that succeeds if the user exists
            check_command = f"id {username}"
            logging.debug(f"Executing check command: '{check_command}'")
            # Expect the prompt, indicating command finished.
            # We don't need the output unless debugging.
            ssh_conn.command(check_command, expectedline=ssh_conn.prompt, timeout=30)
            # Check the output *before* the prompt for error messages
            if "no such user" not in ssh_conn.ssh.before:
                logging.info(f"User '{username}' found.")
                return True
            else:
                logging.debug(f"User '{username}' not found yet. Output: {ssh_conn.ssh.before.strip()}")
        except (TimeoutError, ConnectionAbortedError, pssh.pexpect.exceptions.ExceptionPexpect) as e:
            # Handle potential errors during the check command itself
            logging.warning(f"Error during user check command: {e}. Retrying...")
        except Exception as e:
             logging.error(f"Unexpected error during user check for '{username}': {e}", exc_info=True)
             return False # Exit loop on unexpected error

        logging.debug(f"Waiting {interval}s before next check...")
        time.sleep(interval)

    logging.error(f"Timeout waiting for user '{username}' to be created after {timeout} seconds.")
    return False

def initialize_node(ip_address, is_deployed):
    """
    Connects to the specified node via SSH, optionally sets up secrets,
    and runs the deployment startup script.
    """
    logging.info(f"Attempting to initialize node at IP: {ip_address}")

    # --- Retrieve Secrets ---
    # These secrets are expected to be in the environment where this script (init_node.py) runs
    session_secret = os.environ.get('PROD_SESSION_SECRET')
    redis_password = os.environ.get('PROD_REDIS_PASSWORD')
    encryption_key = os.environ.get('PROD_ENCRYPTION_KEY')

    if not is_deployed:
        logging.info("Node was newly deployed. Performing full initialization including secret setup.")
        if not all([session_secret, redis_password, encryption_key]):
            missing = [
                var for var, val in [
                    ('PROD_SESSION_SECRET', session_secret),
                    ('PROD_REDIS_PASSWORD', redis_password),
                    ('PROD_ENCRYPTION_KEY', encryption_key)
                ] if not val
            ]
            logging.error(f"Missing required secret environment variables for initial deployment: {', '.join(missing)}")
            return EXIT_MISSING_SECRET
        # Construct the command prefix to export secrets
        secret_exports = (
            f"export PROD_SESSION_SECRET='{session_secret}'; "
            f"export PROD_REDIS_PASSWORD='{redis_password}'; "
            f"export PROD_ENCRYPTION_KEY='{encryption_key}'; "
        )
    else:
        logging.info("Node was already deployed. Skipping secret injection.")
        secret_exports = "" # No secrets needed for subsequent runs

    ssh_conn = None # Initialize to None
    try:
        # SSHConnection will use environment variables for USER, CERT, KEYPWORD
        ssh_conn = pssh.SSHConnection(ip_address=ip_address)
        logging.info("Opening SSH connection...")
        ssh_conn.open() # This will raise exceptions on failure
        logging.info("SSH connection established.")

        # --- Wait for ccuser to exist ---
        if not wait_for_user(ssh_conn, "ccuser"):
            return EXIT_USER_TIMEOUT # Exit if user creation timed out

        # --- Run hostname command (example) ---
        hostname_command = "hostname -f"
        logging.info(f"Executing command: '{hostname_command}'")
        output = ssh_conn.command(hostname_command, timeout=60)
        logging.info(f"Command '{hostname_command}' executed successfully.")
        logging.info(f"Output of '{hostname_command}':\n---\n{output.strip()}\n---")

        # --- Run the deployment startup script ---
        startup_script_path = "/local/repository/deploy_scripts/startup.sh"
        # Combine secret exports (if any) with the script execution command
        # Use bash -c to handle the exports and script execution in the same subshell
        # Ensure proper quoting, especially around the secrets
        full_command = f"sudo -u ccuser bash -c \"{secret_exports} bash {startup_script_path}\""

        logging.info(f"Executing deployment startup script as ccuser: {startup_script_path}")
        logging.debug(f"Full command (secrets redacted for safety in debug): sudo -u ccuser -i bash -c \"... bash {startup_script_path}\"")

        # Execute the command with a longer timeout suitable for deployment
        # Note: Output might be extensive, adjust logging/handling as needed
        # We expect the prompt ($) after the script finishes (or fails)
        output = ssh_conn.command(full_command, timeout=1800) # e.g., 30 minutes timeout
        logging.info(f"Deployment script '{startup_script_path}' execution finished.")
        # Log only a portion of the output to avoid flooding logs, or check specific parts
        logging.debug(f"Output snippet from startup script:\n---\n{output.strip()[:500]}\n---") # Log first 500 chars

        logging.info("Node initialization and deployment commands completed.")
        return EXIT_SUCCESS # Return success code

    except (ValueError, FileNotFoundError, ConnectionError, pssh.pexpect.exceptions.ExceptionPexpect) as e:
        logging.error(f"SSH connection failed: {e}", exc_info=True)
        return EXIT_SSH_ERROR
    except (TimeoutError, ConnectionAbortedError) as e:
         logging.error(f"SSH command execution failed: {e}", exc_info=True)
         return EXIT_CMD_ERROR
    except Exception as e:
        logging.error(f"An unexpected error occurred during node initialization: {e}", exc_info=True)
        # Determine if it was more likely an SSH or command error if possible
        if ssh_conn and ssh_conn.ssh and not ssh_conn.ssh.closed:
             return EXIT_CMD_ERROR # Assume command error if connection was open
        else:
             return EXIT_SSH_ERROR # Assume connection error otherwise
    finally:
        if ssh_conn:
            logging.info("Closing SSH connection.")
            ssh_conn.close()

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Initialize a node via SSH.")
    parser.add_argument('--ip', required=True, help="IP address of the node to initialize.")
    parser.add_argument('--isDeployed', action='store_true', help="Flag indicating the experiment was already running.")

    # Check if environment variables are set (optional but good practice)
    required_env_vars = ['USER', 'CERT', 'PROJECT_NAME', 'PROFILE_NAME'] # PWORD/KEYPWORD checked by ssh/rpc
    missing_vars = [var for var in required_env_vars if not os.environ.get(var)]
    if missing_vars:
         logging.warning(f"Missing recommended environment variables: {', '.join(missing_vars)}. SSH/RPC calls might fail.")
         # Decide if this should be fatal - for now, just warn.
         # print(f"Error: Missing required environment variables: {', '.join(missing_vars)}", file=sys.stderr)
         # sys.exit(EXIT_ARG_ERROR)

    try:
        args = parser.parse_args()
        logging.info(f"Received arguments: IP={args.ip}, isDeployed={args.isDeployed}")
        exit_code = initialize_node(args.ip, args.isDeployed) # Pass the flag to the function
        sys.exit(exit_code)
    except Exception as e:
         # Catch potential argparse errors or other unexpected issues before initialization starts
         logging.error(f"Script setup error: {e}", exc_info=True)
         sys.exit(EXIT_ARG_ERROR)

