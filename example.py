#!/usr/bin/env python3
import logging
import sys
import os
import subprocess
import time

# Add powder directory to path
sys.path.append(os.path.join(os.path.dirname(__file__), 'powder'))

import powder.experiment as pexp

# Configure logging for the application
logging.basicConfig(
    level=logging.INFO, # Set default level to INFO for production
    format="[%(asctime)s] %(levelname)s:%(name)s:%(message)s",
    datefmt='%Y-%m-%d %H:%M:%S'
)
log = logging.getLogger(__name__)

# Experiment and Node Configuration
# Read from environment variables, providing defaults if necessary
DEFAULT_PROJECT_NAME = 'YourCloudlabProject' # Default if PROJECT_NAME env var is not set
DEFAULT_PROFILE_NAME = 'YourCloudlabProfile' # Default if PROFILE_NAME env var is not set
PROJECT_NAME = os.environ.get('PROJECT_NAME', DEFAULT_PROJECT_NAME)
PROFILE_NAME = os.environ.get('PROFILE_NAME', DEFAULT_PROFILE_NAME)
EXPERIMENT_NAME = 'prod' # Target experiment name
TARGET_NODE_ID = 'deploy-node' # Client ID of the node to initialize

# Exit Codes
EXIT_SUCCESS = 0
EXIT_FAILURE_STARTUP = 1
EXIT_FAILURE_NODE_INIT = 2
EXIT_NODE_MISSING = 3

def run_experiment_lifecycle():
    """
    Manages the Powder experiment lifecycle: ensures the experiment is ready
    and then executes the node initialization script, indicating if the
    experiment was pre-existing..
    """
    log.info(f"Starting experiment lifecycle for '{EXPERIMENT_NAME}' (Project: {PROJECT_NAME}, Profile: {PROFILE_NAME})")

    exp = pexp.PowderExperiment(experiment_name=EXPERIMENT_NAME,
                                project_name=PROJECT_NAME,
                                profile_name=PROFILE_NAME)

    # Determine if the experiment might already be running or provisioning
    initial_status = exp.check_status()
    was_already_deployed = initial_status in [
        pexp.PowderExperiment.EXPERIMENT_READY,
        pexp.PowderExperiment.EXPERIMENT_PROVISIONING,
        pexp.PowderExperiment.EXPERIMENT_PROVISIONED
    ]
    log.info(f"Initial experiment status: {initial_status}. Pre-existing: {was_already_deployed}")

    # Ensure the experiment reaches the READY state
    exp_status = exp.start_and_wait()

    if exp_status != exp.EXPERIMENT_READY:
        log.error(f"Experiment '{EXPERIMENT_NAME}' failed to reach READY state. Final status: {exp_status}")
        sys.exit(EXIT_FAILURE_STARTUP)

    log.info(f"Experiment '{EXPERIMENT_NAME}' is READY.")

    # Verify the target node exists within the ready experiment
    if TARGET_NODE_ID not in exp.nodes:
        log.error(f"Target node '{TARGET_NODE_ID}' not found in experiment '{EXPERIMENT_NAME}'. Available nodes: {list(exp.nodes.keys())}")
        # Attempt termination if the required node is missing
        try:
            log.warning(f"Terminating experiment '{EXPERIMENT_NAME}' due to missing target node '{TARGET_NODE_ID}'.")
            exp.terminate()
        except Exception as term_err:
            log.error(f"Error during termination after node missing: {term_err}")
        sys.exit(EXIT_NODE_MISSING)

    target_node = exp.nodes[TARGET_NODE_ID]
    node_ip = target_node.ip_address
    log.info(f"Target node '{TARGET_NODE_ID}' found with IP: {node_ip}")

    # Execute the node initialization script
    init_script_path = os.path.join(os.path.dirname(__file__), 'init_node.py')
    log.info(f"Executing node initialization script: {init_script_path} on {node_ip}")

    command_list = [sys.executable, init_script_path, '--ip', node_ip]
    if was_already_deployed:
        log.info("Passing --isDeployed flag to initialization script.")
        command_list.append('--isDeployed')
    # Add --debug flag to init_node.py if this script's logger is set to DEBUG
    if log.isEnabledFor(logging.DEBUG):
        command_list.append('--debug')


    log.debug(f"Executing command: {' '.join(command_list)}")

    try:
        # Run init_node.py as a subprocess
        process = subprocess.run(
            command_list,
            capture_output=True,
            text=True,
            check=True, # Raise error on non-zero exit code
            timeout=2100 # 35 minutes timeout (covers install + startup)
        )
        # Log script output only if it succeeded but produced output
        if process.stdout:
            log.info(f"init_node.py stdout:\n{process.stdout.strip()}")
        if process.stderr:
            log.info(f"init_node.py stderr:\n{process.stderr.strip()}") # Use INFO for stderr too
        log.info("Node initialization script completed successfully.")
        sys.exit(EXIT_SUCCESS)

    except subprocess.CalledProcessError as e:
        log.error(f"Node initialization script '{init_script_path}' failed (exit code {e.returncode}).")
        log.error(f"Stdout:\n{e.stdout.strip()}")
        log.error(f"Stderr:\n{e.stderr.strip()}")
        sys.exit(EXIT_FAILURE_NODE_INIT)
    except subprocess.TimeoutExpired as e:
         log.error(f"Node initialization script '{init_script_path}' timed out after {e.timeout} seconds.")
         if e.stdout: log.error(f"Stdout:\n{e.stdout.strip()}")
         if e.stderr: log.error(f"Stderr:\n{e.stderr.strip()}")
         sys.exit(EXIT_FAILURE_NODE_INIT)
    except FileNotFoundError:
        log.error(f"Initialization script not found at {init_script_path}")
        sys.exit(EXIT_FAILURE_NODE_INIT)
    except Exception as e:
        log.error(f"An unexpected error occurred while running init_node.py: {e}", exc_info=True)
        sys.exit(EXIT_FAILURE_NODE_INIT)

if __name__ == '__main__':
    # The experiment is intentionally left running after script execution.
    # Termination should be handled manually or by CloudLab's expiration policy.
    run_experiment_lifecycle()