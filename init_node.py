import logging
import sys
import os
import argparse
import time

# Add powder library path
sys.path.append(os.path.join(os.path.dirname(__file__), 'powder'))
try:
    import powder.ssh as pssh
except ImportError:
    print("Error: Could not import powder.ssh.", file=sys.stderr)
    sys.exit(1)

# Configure Logging
logging.basicConfig(
    level=logging.INFO, # Default level
    format="[%(asctime)s] %(levelname)s:%(name)s:%(message)s",
    datefmt='%Y-%m-%d %H:%M:%S'
)
log = logging.getLogger(__name__)

# Exit Codes
EXIT_SUCCESS = 0
EXIT_SSH_ERROR = 1
EXIT_CMD_ERROR = 2
EXIT_ARG_ERROR = 3
EXIT_MISSING_SECRET = 4

def check_hostname(ip_address):
    """
    Connects to an existing node, retrieves and logs its hostname.
    Used when --isDeployed flag is set.
    """
    log.info(f"Node at {ip_address} is pre-existing. Verifying connectivity and getting hostname.")
    ssh_conn = None
    try:
        ssh_conn = pssh.SSHConnection(ip_address=ip_address)
        log.info("Opening SSH connection...")
        ssh_conn.open()
        log.info("SSH connection established.")

        hostname_cmd = "hostname -f"
        log.info(f"Executing: {hostname_cmd}")
        # Execute command and extract the last line of output as the hostname
        hostname_output = ssh_conn.command(hostname_cmd, timeout=30)
        hostname = hostname_output.strip().splitlines()[-1]
        log.info(f"Node hostname: {hostname}")
        log.info("Skipping installation and deployment steps as --isDeployed flag was provided.")
        return EXIT_SUCCESS

    except (ValueError, FileNotFoundError, ConnectionError, ConnectionRefusedError, pssh.pexpect.exceptions.ExceptionPexpect, TimeoutError, ConnectionAbortedError) as e:
        log.error(f"Failed to connect or execute command on existing node {ip_address}: {e}", exc_info=log.isEnabledFor(logging.DEBUG)) # Show traceback only in debug
        return EXIT_SSH_ERROR
    except Exception as e:
        log.error(f"An unexpected error occurred during hostname check: {e}", exc_info=True)
        return EXIT_SSH_ERROR
    finally:
        if ssh_conn:
            log.info("Closing SSH connection.")
            ssh_conn.close()


def initialize_node(ip_address):
    """
    Performs first-time initialization of the node: installs dependencies
    and runs the main startup script.
    """
    log.info(f"Starting initialization process for node at IP: {ip_address}")

    # --- Retrieve and Validate Secrets ---
    session_secret = os.environ.get('PROD_SESSION_SECRET')
    redis_password = os.environ.get('PROD_REDIS_PASSWORD')
    encryption_key = os.environ.get('PROD_ENCRYPTION_KEY')

    if not all([session_secret, redis_password, encryption_key]):
        missing = [
            var for var, val in [
                ('PROD_SESSION_SECRET', session_secret),
                ('PROD_REDIS_PASSWORD', redis_password),
                ('PROD_ENCRYPTION_KEY', encryption_key)
            ] if not val
        ]
        log.error(f"Missing required secret environment variables: {', '.join(missing)}")
        return EXIT_MISSING_SECRET
    log.debug("All required secrets found in environment.")

    # Format secrets for passing via 'env' command, ensuring proper quoting
    secret_env_vars = (
        f"PROD_SESSION_SECRET='{session_secret}' "
        f"PROD_REDIS_PASSWORD='{redis_password}' "
        f"PROD_ENCRYPTION_KEY='{encryption_key}'"
    )
    # Redacted version for logging
    debug_secret_env_vars = "PROD_SESSION_SECRET=*** PROD_REDIS_PASSWORD=*** PROD_ENCRYPTION_KEY=***"

    ssh_conn = None
    try:
        # Establish SSH connection (uses USER, CERT, KEYPWORD from env)
        ssh_conn = pssh.SSHConnection(ip_address=ip_address)
        log.info("Opening SSH connection...")
        ssh_conn.open()
        log.info("SSH connection established.")

        # --- Define Script Paths ---
        install_deps_script_path = "/local/repository/deploy_scripts/install_deps.sh"
        startup_script_path = "/local/repository/deploy_scripts/startup.sh"

        # --- Execute Dependency Installation ---
        install_deps_command = f"sudo bash {install_deps_script_path}"
        log.info(f"Executing dependency installation script: {install_deps_script_path}")
        log.debug(f"Install command: {install_deps_command}")
        try:
            # Run install_deps.sh as root (via sudo)
            output_install = ssh_conn.command(install_deps_command, timeout=900) # 15 min timeout
            log.info(f"Dependency installation script finished.")
            log.debug(f"Install script output:\n---\n{output_install.strip()}\n---")
        except (TimeoutError, ConnectionAbortedError, pssh.pexpect.exceptions.ExceptionPexpect) as e:
             log.error(f"Dependency installation script failed: {e}", exc_info=log.isEnabledFor(logging.DEBUG))
             return EXIT_CMD_ERROR
        except Exception as e:
             log.error(f"An unexpected error occurred during install script execution: {e}", exc_info=True)
             return EXIT_CMD_ERROR

        # --- Execute Main Startup Script ---
        # Command runs startup.sh as 'ccuser', sourcing their profile and passing secrets
        # PATH is explicitly set to include /usr/local/bin where tools are installed
        startup_command_base = (
            "export PATH=/usr/local/bin:$PATH && "
            # Source profile only if it exists to avoid errors
            "[ -f /home/ccuser/.profile ] && source /home/ccuser/.profile ; "
            f"bash {startup_script_path}"
        )
        startup_full_command = f"sudo -i -u ccuser env {secret_env_vars} bash -c '{startup_command_base}'"
        startup_debug_command = f"sudo -i -u ccuser env {debug_secret_env_vars} bash -c '{startup_command_base}'" # For logging

        log.info(f"Executing deployment startup script as ccuser: {startup_script_path}")
        log.debug(f"Startup command (secrets redacted): {startup_debug_command}")
        try:
            # Run startup.sh as ccuser
            output_startup = ssh_conn.command(startup_full_command, timeout=1800) # 30 min timeout
            log.info(f"Deployment script finished.")
            log.debug(f"Startup script output:\n---\n{output_startup.strip()}\n---")

            # Check for common errors in startup script output
            if "minikube: command not found" in output_startup:
                 log.error("Startup script failed: 'minikube: command not found'. Check PATH setup.")
                 # Attempt to log the PATH as seen by the user for diagnostics
                 try:
                     path_check_output = ssh_conn.command("sudo -i -u ccuser env bash -c 'echo $PATH'")
                     log.error(f"PATH inside 'sudo -i -u ccuser': {path_check_output.strip()}")
                 except Exception as path_e:
                     log.error(f"Could not retrieve PATH for debugging: {path_e}")
                 return EXIT_CMD_ERROR
            # Generic check for error indicators - review node logs for specifics
            elif "Error:" in output_startup or "failed" in output_startup.lower():
                 log.warning("Potential error indicators ('Error:', 'failed') found in startup script output. Review node logs for details.")
                 # Consider making this fatal depending on expected script behavior
                 # return EXIT_CMD_ERROR

        except TimeoutError:
             log.error(f"Deployment script '{startup_script_path}' timed out after 1800 seconds.")
             return EXIT_CMD_ERROR
        except ConnectionAbortedError:
             log.error(f"SSH connection aborted during deployment script execution.")
             return EXIT_CMD_ERROR
        except pssh.pexpect.exceptions.ExceptionPexpect as e:
             log.error(f"pexpect exception during deployment script execution: {e}", exc_info=log.isEnabledFor(logging.DEBUG))
             # Log output preceding the error if available
             if hasattr(ssh_conn, 'ssh') and ssh_conn.ssh and not ssh_conn.ssh.closed:
                  log.error(f"Output before pexpect error:\n{ssh_conn.ssh.before.strip()}")
             return EXIT_CMD_ERROR
        except Exception as e:
             log.error(f"An unexpected error occurred during startup script execution via SSH: {e}", exc_info=True)
             return EXIT_CMD_ERROR

        log.info("Node initialization and deployment scripts completed successfully.")
        return EXIT_SUCCESS

    except (ValueError, FileNotFoundError, ConnectionError, ConnectionRefusedError, pssh.pexpect.exceptions.ExceptionPexpect) as e:
        log.error(f"SSH connection or initial setup failed: {e}", exc_info=log.isEnabledFor(logging.DEBUG))
        return EXIT_SSH_ERROR
    except Exception as e:
        log.error(f"An unexpected error occurred during node initialization setup: {e}", exc_info=True)
        return EXIT_SSH_ERROR
    finally:
        if ssh_conn:
            log.info("Closing SSH connection.")
            ssh_conn.close()

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Initialize a node via SSH, or check hostname if already deployed.")
    parser.add_argument('--ip', required=True, help="IP address of the target node.")
    parser.add_argument('--isDeployed', action='store_true', help="Indicates the node is pre-existing; skips install/deploy.")
    parser.add_argument('--debug', action='store_true', help="Enable debug level logging.")

    # --- Environment Variable Check ---
    required_env_vars = ['USER', 'CERT', 'PROJECT_NAME', 'PROFILE_NAME',
                         'PROD_SESSION_SECRET', 'PROD_REDIS_PASSWORD', 'PROD_ENCRYPTION_KEY']
    missing_vars = [var for var in required_env_vars if not os.environ.get(var)]
    if missing_vars:
         secret_vars_missing = [var for var in missing_vars if 'PROD_' in var]
         if secret_vars_missing:
              # Fatal error if secrets are missing
              log.critical(f"Error: Missing required secret environment variables: {', '.join(secret_vars_missing)}")
              sys.exit(EXIT_MISSING_SECRET)
         else:
              # Warning for non-secret variables (e.g., USER, CERT)
              log.warning(f"Missing recommended environment variables: {', '.join(missing_vars)}. SSH/RPC calls might fail.")

    try:
        args = parser.parse_args()

        # Set logging level if --debug is passed
        if args.debug:
            logging.getLogger().setLevel(logging.DEBUG)
            log.debug("Debug logging enabled.")

        log.info(f"Script started for IP: {args.ip}, Pre-deployed: {args.isDeployed}")

        # Execute the appropriate function based on the --isDeployed flag
        if args.isDeployed:
            exit_code = check_hostname(args.ip)
        else:
            exit_code = initialize_node(args.ip)

        log.info(f"Script finished with exit code {exit_code}.")
        sys.exit(exit_code)

    except Exception as e:
         # Catch-all for errors during argument parsing or initial setup
         log.critical(f"Script setup failed: {e}", exc_info=True)
         sys.exit(EXIT_ARG_ERROR)
