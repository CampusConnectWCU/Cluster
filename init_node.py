import logging
import sys
import os
import argparse
import time # Import time module
# Removed subprocess import as it's no longer used directly here

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

# Simplified Exit Codes
EXIT_SUCCESS = 0
EXIT_SSH_ERROR = 1
EXIT_CMD_ERROR = 2
EXIT_ARG_ERROR = 3
EXIT_MISSING_SECRET = 4

# Removed wait_for_user and wait_for_log_message functions

def initialize_node(ip_address): # Removed is_deployed parameter
    """
    Connects to the specified node via SSH, runs install_deps.sh,
    then runs startup.sh with secrets.
    """
    logging.info(f"Attempting to initialize node at IP: {ip_address}")

    # --- Retrieve Secrets ---
    session_secret = os.environ.get('PROD_SESSION_SECRET')
    redis_password = os.environ.get('PROD_REDIS_PASSWORD')
    encryption_key = os.environ.get('PROD_ENCRYPTION_KEY')

    # Always require secrets for this simplified version
    if not all([session_secret, redis_password, encryption_key]):
        missing = [
            var for var, val in [
                ('PROD_SESSION_SECRET', session_secret),
                ('PROD_REDIS_PASSWORD', redis_password),
                ('PROD_ENCRYPTION_KEY', encryption_key)
            ] if not val
        ]
        logging.error(f"Missing required secret environment variables: {', '.join(missing)}")
        return EXIT_MISSING_SECRET

    # Format secrets for the 'env' command within sudo -i -u ccuser
    secret_env_vars = (
        f"PROD_SESSION_SECRET='{session_secret}' "
        f"PROD_REDIS_PASSWORD='{redis_password}' "
        f"PROD_ENCRYPTION_KEY='{encryption_key}'"
    )
    # For logging purposes, redact secrets
    debug_secret_env_vars = "PROD_SESSION_SECRET=*** PROD_REDIS_PASSWORD=*** PROD_ENCRYPTION_KEY=***"


    ssh_conn = None # Initialize to None
    try:
        # SSHConnection will use environment variables for USER, CERT, KEYPWORD
        ssh_conn = pssh.SSHConnection(ip_address=ip_address)
        logging.info("Opening SSH connection...")
        ssh_conn.open() # This will raise exceptions on failure
        logging.info("SSH connection established.")

        # --- Define commands ---
        install_deps_script_path = "/local/repository/deploy_scripts/install_deps.sh"
        startup_script_path = "/local/repository/deploy_scripts/startup.sh"

        # Command 1: Run install_deps.sh using sudo
        install_deps_command = f"sudo bash {install_deps_script_path}"

        # Command 2: Run startup.sh as ccuser with secrets and sourced profile
        startup_command_base = ("export PATH=/usr/local/bin:$PATH && "
                                "source /home/ccuser/.profile && "
                                f"bash {startup_script_path}")
        startup_full_command = f"sudo -i -u ccuser env {secret_env_vars} bash -c '{startup_command_base}'"
        startup_debug_command = f"sudo -i -u ccuser env {debug_secret_env_vars} bash -c '{startup_command_base}'"


        # --- Execute install_deps.sh ---
        logging.info(f"Executing dependency installation script: {install_deps_script_path}")
        logging.debug(f"Install command: {install_deps_command}")
        try:
            # Run as root (or user with sudo)
            output_install = ssh_conn.command(install_deps_command, timeout=900) # 15 min timeout
            logging.info(f"Dependency installation script '{install_deps_script_path}' finished.")
            # Log the full output
            logging.debug(f"Full output from install script:\n---\n{output_install.strip()}\n---")
        except (TimeoutError, ConnectionAbortedError, pssh.pexpect.exceptions.ExceptionPexpect) as e:
             logging.error(f"Dependency installation script failed: {e}", exc_info=True)
             return EXIT_CMD_ERROR
        except Exception as e:
             logging.error(f"An unexpected error occurred during install script execution: {e}", exc_info=True)
             return EXIT_CMD_ERROR

        # --- Execute startup.sh ---
        logging.info(f"Executing deployment startup script as ccuser: {startup_script_path}")
        logging.debug(f"Startup command: {startup_debug_command}")
        try:
            # Run as ccuser via sudo -i
            output_startup = ssh_conn.command(startup_full_command, timeout=1800) # 30 min timeout
            logging.info(f"Deployment script '{startup_script_path}' finished.")
            # Log the full output
            logging.debug(f"Full output from startup script:\n---\n{output_startup.strip()}\n---")

            # Optional: Check for specific errors in output_startup if needed
            if "minikube: command not found" in output_startup:
                 logging.error("Detected 'minikube: command not found' in startup output.")
                 # Log path again for debugging
                 try:
                     path_check_output = ssh_conn.command("sudo -i -u ccuser env bash -c 'echo $PATH'")
                     logging.error(f"PATH inside sudo -i -u ccuser: {path_check_output.strip()}")
                 except Exception as path_e:
                     logging.error(f"Could not check PATH: {path_e}")
                 return EXIT_CMD_ERROR
            elif "Error:" in output_startup or "failed" in output_startup.lower():
                 logging.warning("Potential error indicators found in startup script output.")
                 # Decide if this should be fatal, for now just warn

        except TimeoutError:
             logging.error(f"Deployment script '{startup_script_path}' timed out via SSH.")
             return EXIT_CMD_ERROR
        except ConnectionAbortedError:
             logging.error(f"SSH connection aborted during deployment script execution.")
             return EXIT_CMD_ERROR
        except pssh.pexpect.exceptions.ExceptionPexpect as e:
             logging.error(f"pexpect exception during deployment script execution: {e}", exc_info=True)
             # Log output before the error if available
             if hasattr(ssh_conn, 'ssh') and not ssh_conn.ssh.closed:
                  logging.error(f"Output before pexpect error:\n{ssh_conn.ssh.before.strip()}")
             return EXIT_CMD_ERROR
        except Exception as e:
             logging.error(f"An unexpected error occurred during startup script execution via SSH: {e}", exc_info=True)
             return EXIT_CMD_ERROR

        logging.info("Node initialization and deployment scripts completed.")
        return EXIT_SUCCESS

    except (ValueError, FileNotFoundError, ConnectionError, pssh.pexpect.exceptions.ExceptionPexpect) as e:
        logging.error(f"SSH connection failed: {e}", exc_info=True)
        return EXIT_SSH_ERROR
    # Removed specific TimeoutError/ConnectionAbortedError catch here as they are handled per-command
    except Exception as e:
        logging.error(f"An unexpected error occurred during node initialization setup: {e}", exc_info=True)
        return EXIT_SSH_ERROR # Assume SSH error if connection couldn't be established/setup failed
    finally:
        if ssh_conn:
            logging.info("Closing SSH connection.")
            ssh_conn.close()

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Initialize a node via SSH.")
    parser.add_argument('--ip', required=True, help="IP address of the node to initialize.")
    # Restore --isDeployed argument
    parser.add_argument('--isDeployed', action='store_true', help="Flag indicating the experiment was already running (currently ignored).")

    # Check if environment variables are set (optional but good practice)
    required_env_vars = ['USER', 'CERT', 'PROJECT_NAME', 'PROFILE_NAME',
                         'PROD_SESSION_SECRET', 'PROD_REDIS_PASSWORD', 'PROD_ENCRYPTION_KEY'] # Added secrets check
    missing_vars = [var for var in required_env_vars if not os.environ.get(var)]
    if missing_vars:
         # Make missing secrets fatal before attempting connection
         if any(secret in missing_vars for secret in ['PROD_SESSION_SECRET', 'PROD_REDIS_PASSWORD', 'PROD_ENCRYPTION_KEY']):
              logging.error(f"Error: Missing required secret environment variables: {', '.join(var for var in missing_vars if 'PROD_' in var)}")
              sys.exit(EXIT_MISSING_SECRET)
         else:
              # Warn for non-secret missing vars
              logging.warning(f"Missing recommended environment variables: {', '.join(missing_vars)}. SSH/RPC calls might fail.")

    try:
        args = parser.parse_args()
        # Update log message to include isDeployed value
        logging.info(f"Received arguments: IP={args.ip}, isDeployed={args.isDeployed} (isDeployed flag is currently ignored)")
        exit_code = initialize_node(args.ip) # Call without isDeployed flag
        sys.exit(exit_code)
    except Exception as e:
         # Catch potential argparse errors or other unexpected issues before initialization starts
         logging.error(f"Script setup error: {e}", exc_info=True)
         sys.exit(EXIT_ARG_ERROR)
