#!/usr/bin/env python3
import logging
import sys
import os
import subprocess # Import subprocess

sys.path.append(os.path.join(os.path.dirname(__file__), 'powder'))

import powder.experiment as pexp

logging.basicConfig(
    level=logging.DEBUG,
    format="[%(asctime)s] %(levelname)s:%(name)s:%(message)s",
    datefmt='%Y-%m-%d %H:%M:%S'
)

# Define constants for experiment details
# Read from environment variables, providing defaults if necessary
DEFAULT_PROJECT_NAME = 'YourCloudlabProject' # CHANGE THIS if not set in env
DEFAULT_PROFILE_NAME = 'YourCloudlabProfile' # CHANGE THIS if not set in env
PROJECT_NAME = os.environ.get('PROJECT_NAME', DEFAULT_PROJECT_NAME)
PROFILE_NAME = os.environ.get('PROFILE_NAME', DEFAULT_PROFILE_NAME)
EXPERIMENT_NAME = 'prod' # Hardcoded experiment name
TARGET_NODE_ID = 'deploy-node' # The node we want to interact with

# Exit codes
EXIT_SUCCESS = 0
EXIT_FAILURE_STARTUP = 1 # Failed to get experiment ready
EXIT_FAILURE_NODE_INIT = 2 # init_node.py failed
EXIT_NODE_MISSING = 3 # Target node not found in ready experiment

def run_experiment_lifecycle():
    """
    Ensures the 'prod' experiment is running and ready, then calls init_node.py,
    passing a flag if the experiment was already deployed.
    """
    logging.info(f"Starting experiment lifecycle for '{EXPERIMENT_NAME}'...")
    logging.info(f"Using Project: {PROJECT_NAME}, Profile: {PROFILE_NAME}")

    exp = pexp.PowderExperiment(experiment_name=EXPERIMENT_NAME,
                                project_name=PROJECT_NAME,
                                profile_name=PROFILE_NAME)

    # Check initial status BEFORE trying to start/wait
    initial_status = exp.check_status()
    was_already_deployed = initial_status in [
        pexp.PowderExperiment.EXPERIMENT_READY,
        pexp.PowderExperiment.EXPERIMENT_PROVISIONING,
        pexp.PowderExperiment.EXPERIMENT_PROVISIONED
    ]
    logging.info(f"Initial experiment status: {initial_status}. Was already deployed: {was_already_deployed}")

    # Ensure the experiment is ready (starts if needed, waits if provisioning)
    exp_status = exp.start_and_wait()

    if exp_status != exp.EXPERIMENT_READY:
        logging.error(f"Experiment '{EXPERIMENT_NAME}' did not become ready. Final status: {exp_status}")
        # No need to terminate here, start_and_wait handles failure logging
        # Termination happens in finally block if needed
        sys.exit(EXIT_FAILURE_STARTUP)

    logging.info(f"Experiment '{EXPERIMENT_NAME}' is READY.")

    # Check if the target node exists
    if TARGET_NODE_ID not in exp.nodes:
        logging.error(f"Target node '{TARGET_NODE_ID}' not found in experiment '{EXPERIMENT_NAME}'. Nodes found: {list(exp.nodes.keys())}")
        # Terminate if the required node is missing
        try:
            exp.terminate()
        except Exception as term_err:
            logging.error(f"Error during termination after node missing: {term_err}")
        sys.exit(EXIT_NODE_MISSING)

    target_node = exp.nodes[TARGET_NODE_ID]
    node_ip = target_node.ip_address
    logging.info(f"Found target node '{TARGET_NODE_ID}' with IP: {node_ip}")

    # --- Execute init_node.py ---
    init_script_path = os.path.join(os.path.dirname(__file__), 'init_node.py')
    logging.info(f"Preparing to execute node initialization script: {init_script_path} for IP {node_ip}")

    # Build the command list for subprocess
    command_list = [sys.executable, init_script_path, '--ip', node_ip]
    if was_already_deployed:
        logging.info("Adding --isDeployed flag as experiment was already running.")
        command_list.append('--isDeployed')
    else:
        logging.info("Not adding --isDeployed flag as experiment was started fresh.")

    logging.info(f"Executing command: {' '.join(command_list)}")

    try:
        # Pass IP and potentially the flag as command line arguments
        process = subprocess.run(
            command_list, # Use the constructed list
            capture_output=True,
            text=True,
            check=True, # Raise CalledProcessError if script returns non-zero exit code
            timeout=300 # Add a timeout (e.g., 5 minutes)
        )
        logging.info(f"init_node.py stdout:\n{process.stdout}")
        logging.info(f"init_node.py stderr:\n{process.stderr}")
        logging.info("Node initialization script completed successfully.")
        sys.exit(EXIT_SUCCESS)

    except subprocess.CalledProcessError as e:
        logging.error(f"Node initialization script '{init_script_path}' failed with exit code {e.returncode}.")
        logging.error(f"Stdout:\n{e.stdout}")
        logging.error(f"Stderr:\n{e.stderr}")
        # Don't terminate the experiment here, allow it to stay running
        sys.exit(EXIT_FAILURE_NODE_INIT)
    except subprocess.TimeoutExpired as e:
         logging.error(f"Node initialization script '{init_script_path}' timed out after {e.timeout} seconds.")
         logging.error(f"Stdout:\n{e.stdout}")
         logging.error(f"Stderr:\n{e.stderr}")
         sys.exit(EXIT_FAILURE_NODE_INIT)
    except FileNotFoundError:
        logging.error(f"Error: Could not find the initialization script at {init_script_path}")
        sys.exit(EXIT_FAILURE_NODE_INIT)
    except Exception as e:
        logging.error(f"An unexpected error occurred while running init_node.py: {e}", exc_info=True)
        sys.exit(EXIT_FAILURE_NODE_INIT)

if __name__ == '__main__':
    # Keep the experiment running even if init_node.py fails, so no explicit terminate here.
    # Termination should be handled manually or by CloudLab's expiration.
    run_experiment_lifecycle()