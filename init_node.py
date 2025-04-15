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
EXIT_DEPENDENCY_TIMEOUT = 6 # New exit code for dependency check timeout
EXIT_SCRIPT_PERM_ERROR = 7 # New exit code for script permission/existence error

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

def wait_for_log_message(ssh_conn, log_file, message_pattern, timeout=600, interval=10):
    """Waits for a specific message pattern to appear in a remote log file using grep -q."""
    logging.info(f"Waiting for message matching '{message_pattern}' in '{log_file}' (timeout: {timeout}s)...")
    start_time = time.time()
    # Use single quotes around the pattern for grep
    check_command = f"grep -q '{message_pattern}' {log_file}"

    while time.time() - start_time < timeout:
        try:
            logging.debug(f"Executing check command: '{check_command}'")
            # Execute the command. If grep finds the pattern (exit 0), command() should succeed.
            # If grep doesn't find it (exit 1) or file missing (exit 2), command() might raise ExceptionPexpect.
            ssh_conn.command(check_command, timeout=30)
            # If we reach here, the command succeeded (exit 0), meaning grep found the pattern.
            logging.info(f"Found message matching '{message_pattern}' in '{log_file}'.")
            return True
        except pssh.pexpect.exceptions.ExceptionPexpect as e:
            # Assume this exception means grep exited non-zero (pattern not found or file missing).
            output_before_prompt = ""
            if hasattr(ssh_conn, 'ssh') and hasattr(ssh_conn.ssh, 'before'):
                 output_before_prompt = ssh_conn.ssh.before.strip()
            logging.debug(f"Log check command failed (likely pattern not found yet). Output: '{output_before_prompt}'. Retrying...")
        except (TimeoutError, ConnectionAbortedError) as e:
            # Handle SSH-level errors during the check
            logging.warning(f"SSH error during log check command: {e}. Retrying...")
        except Exception as e:
             logging.error(f"Unexpected error during log check for '{message_pattern}' in '{log_file}': {e}", exc_info=True)
             return False # Exit loop on unexpected error

        logging.debug(f"Waiting {interval}s before next check...")
        time.sleep(interval)

    logging.error(f"Timeout waiting for message '{message_pattern}' in '{log_file}' after {timeout} seconds.")
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

        # --- Wait for install_deps.sh completion by checking its log ---
        install_log = "/local/logs/install.log"
        # Note: Using the literal username 'ccuser' as expected in the log message
        completion_message = "Dependencies installed and ccuser is ready to deploy."
        # Wait up to 10 minutes (600 seconds) for the message
        if not wait_for_log_message(ssh_conn, install_log, completion_message, timeout=600):
             logging.error(f"Did not find completion message in '{install_log}' within the timeout.")
             return EXIT_DEPENDENCY_TIMEOUT # Use the specific exit code

        logging.info("Dependency installation confirmed via log message.")


        # --- Run hostname command (example) ---
        hostname_command = "hostname -f"
        logging.info(f"Executing command: '{hostname_command}'")
        output = ssh_conn.command(hostname_command, timeout=60)
        logging.info(f"Command '{hostname_command}' executed successfully.")
        logging.info(f"Output of '{hostname_command}':\n---\n{output.strip()}\n---")

        # --- Verify startup script existence and permissions ---
        startup_script_path = "/local/repository/deploy_scripts/startup.sh"
        check_script_cmd = f"sudo -u ccuser test -x {startup_script_path}"
        logging.info(f"Verifying script permissions: '{check_script_cmd}'")
        try:
            ssh_conn.command(check_script_cmd, timeout=30)
            logging.info(f"Script '{startup_script_path}' exists and is executable by ccuser.")
        except Exception as e:
            logging.error(f"Script verification failed for '{startup_script_path}'. It might not exist or is not executable by ccuser. Error: {e}", exc_info=True)
            # Log ls -l output for debugging
            try:
                ls_output = ssh_conn.command(f"ls -l {startup_script_path}", timeout=30)
                logging.error(f"ls -l output: {ls_output.strip()}")
            except Exception as ls_e:
                logging.error(f"Could not get ls -l output for script: {ls_e}")
            return EXIT_SCRIPT_PERM_ERROR

        # --- Run the deployment startup script ---
        # Remove the explicit env_vars construction for PATH
        # env_vars = f"HOME=/home/ccuser PATH=/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
        secret_env_vars = "" # Still need to pass secrets if it's the initial deployment
        if not is_deployed:
             # Secrets still need to be passed if it's the initial deployment.
             # sudo -i preserves some env vars, but explicitly passing is safer.
             # We need to format them for the 'env' command used *within* the sudo -i context
             # or find another way. Let's stick with the previous 'env' approach for secrets
             # but remove the explicit PATH setting.
             # Revisit this if secrets aren't passed correctly.
             # Alternative: Write secrets to a temp file accessible by ccuser? Seems complex.
             # Let's try passing them via 'env' still, but let -i handle PATH.
             secret_env_vars = ""
             if session_secret: secret_env_vars += f" PROD_SESSION_SECRET='{session_secret}'"
             if redis_password: secret_env_vars += f" PROD_REDIS_PASSWORD='{redis_password}'"
             if encryption_key: secret_env_vars += f" PROD_ENCRYPTION_KEY='{encryption_key}'"


        # Use sudo -i -u ccuser to simulate login and source profile for PATH.
        # Prepend 'env' command *if* we need to pass secrets.
        if secret_env_vars:
            # Run bash via env to pass secrets, rely on -i for PATH and HOME
            full_command = f"sudo -i -u ccuser env {secret_env_vars.strip()} bash {startup_script_path}"
            debug_command = f"sudo -i -u ccuser env PROD_SESSION_SECRET=*** PROD_REDIS_PASSWORD=*** PROD_ENCRYPTION_KEY=*** bash {startup_script_path}"
        else:
            # If no secrets (redeployment), just run bash directly
            full_command = f"sudo -i -u ccuser bash {startup_script_path}"
            debug_command = full_command


        logging.info(f"Executing deployment startup script as ccuser: {startup_script_path}")
        logging.debug(f"Full command: {debug_command}") # Log the potentially redacted command


        # Execute the command locally with a longer timeout
        try:
            # Run with check=True to catch script errors (non-zero exit code)
            # Capture output to log snippet
            # Note: sudo -i changes the working directory to /home/ccuser initially.
            # The startup.sh script handles cd'ing to the correct directories.
            result = run_local_command(full_command, timeout=1800, check=True, capture_output=True)
            logging.info(f"Deployment script '{startup_script_path}' execution finished successfully.")
            # Log snippet of stdout/stderr
            output_snippet = f"STDOUT:\n{result.stdout.strip()[:500]}\n...\nSTDERR:\n{result.stderr.strip()[:500]}\n..."
            logging.debug(f"Output snippet from startup script:\n---\n{output_snippet}\n---")

        except subprocess.CalledProcessError as e:
             logging.error(f"Deployment script '{startup_script_path}' failed with return code {e.returncode}.")
             output_snippet = f"STDOUT:\n{e.stdout.strip()[:500]}\n...\nSTDERR:\n{e.stderr.strip()[:500]}\n..."
             logging.error(f"Output snippet:\n---\n{output_snippet}\n---")
             return EXIT_CMD_ERROR
        except TimeoutError:
             logging.error(f"Deployment script '{startup_script_path}' timed out.")
             return EXIT_CMD_ERROR # Or a specific timeout code if needed
        except Exception as e:
             logging.error(f"An unexpected error occurred during script execution: {e}", exc_info=True)
             return EXIT_CMD_ERROR


        logging.info("Node initialization and deployment commands completed.")
        return EXIT_SUCCESS

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
