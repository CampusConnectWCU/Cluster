#!/usr/bin/env python3
import json
import logging
import sys
import time

import xmltodict

import powder.rpc as prpc
import powder.ssh as pssh # Although Node initializes SSH, keep import if used elsewhere

log = logging.getLogger(__name__)

class PowderExperiment:
    """
    Manages a Powder experiment lifecycle through XML-RPC calls.

    Handles starting, status polling, manifest retrieval/parsing, node representation,
    and termination of experiments based on Powder profiles.
    """

    # Experiment Status Codes (mirroring potential Powder states)
    EXPERIMENT_NOT_STARTED = 0
    EXPERIMENT_PROVISIONING = 1 # Actively setting up resources
    EXPERIMENT_PROVISIONED = 2  # Resources allocated, OS booting/configuring
    EXPERIMENT_READY = 3        # Experiment is up and accessible
    EXPERIMENT_FAILED = 4       # Experiment creation or operation failed
    EXPERIMENT_NULL = 5         # Experiment terminated or does not exist
    EXPERIMENT_UNKNOWN = 6      # Status could not be determined

    # Configuration Constants
    POLL_INTERVAL_S = 20       # Seconds between status checks during provisioning
    PROVISION_TIMEOUT_S = 1800 # 30 minutes maximum wait time for READY state
    MAX_NAME_LENGTH = 16       # Maximum allowed length for experiment names

    def __init__(self, experiment_name, project_name, profile_name):
        """
        Initializes the experiment handler.

        Args:
            experiment_name (str): Name for the Powder experiment.
            project_name (str): Powder project name.
            profile_name (str): Name of the Powder profile to instantiate.

        Raises:
            ValueError: If experiment_name exceeds MAX_NAME_LENGTH.
        """
        if len(experiment_name) > self.MAX_NAME_LENGTH:
            msg = f'Experiment name "{experiment_name}" exceeds max length ({self.MAX_NAME_LENGTH})'
            log.critical(msg)
            raise ValueError(msg)

        self.experiment_name = experiment_name
        self.project_name = project_name
        self.profile_name = profile_name
        self.status = self.EXPERIMENT_NOT_STARTED # Initial assumed status
        self.nodes = {} # Dictionary mapping client_id to Node objects
        self._manifests = None # Raw manifest data from RPC call
        self._poll_count_max = self.PROVISION_TIMEOUT_S // self.POLL_INTERVAL_S
        log.info(f'Initialized handler for experiment "{experiment_name}" (Profile: {profile_name}, Project: {project_name})')

    def check_status(self):
        """
        Retrieves and updates the current status of the experiment via RPC.
        If the status becomes READY, it also attempts to fetch and parse manifests.

        Returns:
            int: The current status code (e.g., EXPERIMENT_READY).
        """
        log.info(f"Checking status for experiment '{self.experiment_name}'...")
        self._get_status() # Internal method performs RPC call and updates state
        log.info(f"Current status code: {self.status}")
        return self.status

    def start_and_wait(self):
        """
        Ensures the experiment is running and waits until it reaches READY state
        or fails/times out.

        Handles starting the experiment if it doesn't exist or is failed,
        and polls the status until completion.

        Returns:
            int: The final status code after waiting (e.g., EXPERIMENT_READY, EXPERIMENT_FAILED).
        """
        current_status = self.check_status()

        # Handle different initial states
        if current_status == self.EXPERIMENT_READY:
            log.info(f"Experiment '{self.experiment_name}' is already READY.")
            # Ensure node info is loaded if missing (e.g., script restarted)
            if not self.nodes:
                 log.warning("Experiment READY but node list empty. Re-fetching/parsing manifests.")
                 try:
                      self._get_manifests()._parse_manifests()
                      if not self.nodes:
                           log.error("Manifest parsing confirmed empty node list for READY experiment. Marking as FAILED.")
                           self.status = self.EXPERIMENT_FAILED
                 except Exception as e:
                      log.error(f"Error fetching/parsing manifests for running experiment: {e}", exc_info=True)
                      self.status = self.EXPERIMENT_FAILED
            return self.status # Return immediately if already ready

        elif current_status in [self.EXPERIMENT_PROVISIONING, self.EXPERIMENT_PROVISIONED]:
            log.info(f"Experiment '{self.experiment_name}' is currently {current_status}. Waiting for READY state...")
            # Proceed to polling loop

        elif current_status == self.EXPERIMENT_FAILED:
             log.warning(f"Experiment '{self.experiment_name}' is in FAILED state. Attempting termination before restart.")
             self.terminate() # Attempt cleanup before trying again
             # Proceed to start attempt

        elif current_status in [self.EXPERIMENT_NOT_STARTED, self.EXPERIMENT_NULL, self.EXPERIMENT_UNKNOWN]:
             log.info(f"Experiment '{self.experiment_name}' not running or status unknown. Attempting to start...")
             # Proceed to start attempt

        else: # Should not happen with defined states
             log.error(f"Experiment '{self.experiment_name}' in unexpected state {current_status}. Aborting wait.")
             return current_status

        # --- Attempt to Start Experiment (if not already provisioning) ---
        if self.status not in [self.EXPERIMENT_PROVISIONING, self.EXPERIMENT_PROVISIONED]:
            log.info(f'Attempting to start experiment "{self.experiment_name}"...')
            rval, response = prpc.start_experiment(self.experiment_name,
                                                self.project_name,
                                                self.profile_name)
            if rval != prpc.RESPONSE_SUCCESS:
                # If start request itself fails
                self.status = self.EXPERIMENT_FAILED
                log.critical(f"Failed to initiate experiment start. RPC Response: {response}")
                return self.status
            log.info("Experiment start request submitted successfully.")
            # Brief pause before first status check after start request
            time.sleep(5)
            self._get_status() # Update status immediately
            # Check if it failed immediately after start request
            if self.status == self.EXPERIMENT_FAILED:
                 log.error("Experiment entered FAILED state immediately after start request.")
                 return self.status

        # --- Polling Loop (Wait for READY state) ---
        log.info(f"Waiting up to {self.PROVISION_TIMEOUT_S}s for experiment '{self.experiment_name}' to become READY...")
        poll_count = 0
        # Continue polling while in intermediate states and within timeout
        while self.status in [self.EXPERIMENT_PROVISIONING, self.EXPERIMENT_PROVISIONED, self.EXPERIMENT_NOT_STARTED, self.EXPERIMENT_UNKNOWN] and poll_count < self._poll_count_max:
            log.info(f"Polling status ({poll_count+1}/{self._poll_count_max}). Current: {self.status}. Waiting {self.POLL_INTERVAL_S}s...")
            time.sleep(self.POLL_INTERVAL_S)
            self._get_status() # RPC call to update status and potentially manifests
            poll_count += 1

        # --- Final Status Evaluation ---
        if self.status == self.EXPERIMENT_READY:
             log.info(f"Experiment '{self.experiment_name}' reached READY state.")
             # Final check/attempt to load node info if needed
             if not self.nodes:
                  log.warning("Experiment became READY but node list is still empty. Final attempt to parse manifests.")
                  try:
                       self._get_manifests()._parse_manifests()
                       if not self.nodes:
                            log.error("Manifest parsing confirmed empty node list after READY state. Marking as FAILED.")
                            self.status = self.EXPERIMENT_FAILED
                  except Exception as e:
                       log.error(f"Error parsing manifests after experiment became ready: {e}", exc_info=True)
                       self.status = self.EXPERIMENT_FAILED
        elif self.status == self.EXPERIMENT_FAILED:
            log.error(f"Experiment '{self.experiment_name}' reached FAILED state during provisioning.")
        else: # Timeout occurred
            log.error(f"Experiment '{self.experiment_name}' did not become READY within the {self.PROVISION_TIMEOUT_S}s timeout. Final status: {self.status}")
            # Mark as failed if it timed out in an intermediate state
            if self.status not in [self.EXPERIMENT_FAILED, self.EXPERIMENT_NULL]:
                 self.status = self.EXPERIMENT_FAILED

        log.info(f"Wait complete. Final experiment status: {self.status}")
        return self.status

    def terminate(self):
        """
        Terminates the experiment via RPC.
        Resets local status to NULL on success.

        Returns:
            int: The status code after the termination attempt (NULL on success, previous status on failure).
        """
        log.info(f'Requesting termination of experiment "{self.experiment_name}"...')
        rval, response = prpc.terminate_experiment(self.project_name, self.experiment_name)
        if rval == prpc.RESPONSE_SUCCESS:
            log.info(f'Experiment "{self.experiment_name}" terminated successfully.')
            self.status = self.EXPERIMENT_NULL
            self.nodes = {} # Clear node data
            self._manifests = None
        else:
            # Avoid changing status if termination fails, as experiment might still exist
            log.error(f'Failed to terminate experiment "{self.experiment_name}". RPC Response: {response}')

        return self.status

    def _get_manifests(self):
        """
        Internal method to retrieve experiment manifests via RPC.
        Parses the XML content into the self._manifests attribute.
        """
        log.debug(f"Requesting manifests for experiment '{self.experiment_name}'...")
        rval, response = prpc.get_experiment_manifests(self.project_name,
                                                       self.experiment_name)
        if rval == prpc.RESPONSE_SUCCESS:
            try:
                # Response output is expected to be a JSON string containing XML manifests
                response_json = json.loads(response['output'])
                log.debug(f"Manifests received (keys: {list(response_json.keys())}). Parsing XML...")

                # Parse each XML manifest string using xmltodict
                self._manifests = [xmltodict.parse(xml_content) for xml_content in response_json.values()]
                log.info(f"Successfully retrieved and parsed {len(self._manifests)} manifests.")

            except json.JSONDecodeError as e:
                log.error(f"Failed to decode JSON response containing manifests: {e}")
                log.debug(f"Raw RPC output: {response.get('output', 'N/A')}")
                self._manifests = None
            except Exception as e: # Catch potential xmltodict parsing errors
                log.error(f"Error parsing XML manifests with xmltodict: {e}", exc_info=True)
                self._manifests = None
        else:
            # Handle RPC failure to get manifests
            log.error(f"Failed to retrieve manifests. RPC Code: {rval}, Output: {response.get('output', 'N/A')}")
            self._manifests = None

        return self # Allow chaining

    def _parse_manifests(self):
        """
        Internal method to parse previously retrieved manifests (self._manifests)
        and populate the self.nodes dictionary with Node objects.
        """
        if not self._manifests:
            log.warning("Manifest parsing skipped: No manifests available (call _get_manifests first or check for errors).")
            return self

        log.info(f"Parsing {len(self._manifests)} manifest(s) for node details...")
        self.nodes = {} # Reset nodes dictionary before parsing

        for i, manifest in enumerate(self._manifests):
            log.debug(f"Processing manifest {i+1}/{len(self._manifests)}...")
            try:
                # Navigate through the expected manifest structure (rspec -> node)
                rspec_data = manifest.get('rspec')
                if not rspec_data:
                    log.warning(f"Manifest {i+1} skipped: Missing 'rspec' top-level key.")
                    continue

                nodes_data = rspec_data.get('node')
                if not nodes_data:
                    # It's possible a manifest might not contain nodes (e.g., network-only)
                    log.debug(f"Manifest {i+1}: No 'node' key found in 'rspec'.")
                    continue

                # Ensure nodes_data is a list, even if only one node exists
                if not isinstance(nodes_data, list):
                    nodes_data = [nodes_data]

                log.debug(f"Manifest {i+1}: Found {len(nodes_data)} node entries.")

                # Iterate through each node entry in the manifest
                for j, node_entry in enumerate(nodes_data):
                    if not isinstance(node_entry, dict):
                        log.warning(f"Skipping node entry {j+1} in manifest {i+1}: Not a dictionary.")
                        continue

                    # Extract essential node attributes using .get for safety
                    client_id = node_entry.get('@client_id')
                    host_data = node_entry.get('host') # Can be dict or list

                    # Find the primary host entry with an IPv4 address
                    # Handles single dict or list of dicts for 'host'
                    host_dict = None
                    if isinstance(host_data, list):
                        # Find first host entry that is a dict and has an ipv4 attribute
                        host_dict = next((h for h in host_data if isinstance(h, dict) and h.get('@ipv4')), None)
                    elif isinstance(host_data, dict):
                        host_dict = host_data

                    # Validate extracted data
                    if not client_id:
                        log.warning(f"Skipping node {j+1} in manifest {i+1}: Missing '@client_id'.")
                        continue
                    if not host_dict:
                        log.warning(f"Skipping node '{client_id}': Missing 'host' dictionary or valid host entry.")
                        continue

                    hostname = host_dict.get('@name')
                    ipv4 = host_dict.get('@ipv4')

                    if not hostname or not ipv4:
                        log.warning(f"Skipping node '{client_id}': Missing '@name' or '@ipv4' in host data.")
                        continue

                    # Create and store the Node object
                    self.nodes[client_id] = Node(client_id=client_id, ip_address=ipv4, hostname=hostname)
                    log.info(f"Parsed node: ID='{client_id}', IP='{ipv4}', Hostname='{hostname}'")

            except Exception as e:
                # Catch unexpected errors during parsing of a specific manifest
                log.error(f"Error parsing manifest {i+1}: {e}. Manifest content (may be large): {manifest}", exc_info=True)

        log.info(f"Finished parsing manifests. Total nodes identified: {len(self.nodes)}")
        return self # Allow chaining


    def _get_status(self):
        """
        Internal method to perform the experimentStatus RPC call, interpret the
        response, and update self.status. Fetches manifests if state becomes READY.
        """
        log.debug(f"Requesting status update for experiment '{self.experiment_name}' via RPC...")
        rval, response = prpc.get_experiment_status(self.project_name,
                                                    self.experiment_name)

        # --- Handle RPC Response ---
        # Check for specific errors indicating non-existence
        if rval == prpc.RESPONSE_BADARGS or \
           (rval == prpc.RESPONSE_ERROR and response and "No such experiment" in response.get('output', '')):
             log.info(f"Experiment '{self.experiment_name}' does not exist or was terminated.")
             if self.status != self.EXPERIMENT_NULL:
                 log.info("Updating local status to EXPERIMENT_NULL.")
                 self.status = self.EXPERIMENT_NULL
                 self.nodes = {} # Clear node info
                 self._manifests = None
             return self # Status is NULL

        # Handle other RPC errors
        elif rval != prpc.RESPONSE_SUCCESS:
            log.error(f"Failed to get experiment status. RPC Code: {rval}, Response: {response}")
            if self.status != self.EXPERIMENT_UNKNOWN:
                 log.warning("Updating local status to EXPERIMENT_UNKNOWN due to RPC error.")
                 self.status = self.EXPERIMENT_UNKNOWN
            return self # Status is UNKNOWN

        # --- Parse Successful Response ---
        output = response.get('output', '').strip()
        log.debug(f"Raw status RPC output: '{output}'")

        previous_status = self.status
        new_status = self.EXPERIMENT_UNKNOWN # Default if parsing fails

        # Interpret status string from RPC output
        if output.startswith('Status: ready'):
            new_status = self.EXPERIMENT_READY
            # If transitioning to READY or nodes are missing, get manifests
            if previous_status != self.EXPERIMENT_READY or not self.nodes:
                log.info("Experiment status is READY. Fetching/parsing manifests...")
                try:
                    self._get_manifests()._parse_manifests()
                    # If parsing fails even when READY, consider it an error state
                    if not self.nodes:
                         log.error("Experiment is READY, but failed to parse node information from manifests.")
                         new_status = self.EXPERIMENT_FAILED
                except Exception as e:
                    log.error("Unexpected error getting/parsing manifests during status update", exc_info=True)
                    new_status = self.EXPERIMENT_FAILED # Mark as failed if manifest handling errors occur

        elif output.startswith('Status: provisioning'):
            new_status = self.EXPERIMENT_PROVISIONING
        elif output.startswith('Status: provisioned'):
            new_status = self.EXPERIMENT_PROVISIONED
        elif output.startswith('Status: failed'):
            new_status = self.EXPERIMENT_FAILED
        else:
            # Handle unrecognized status strings
            log.warning(f"Unrecognized status line from RPC: '{output}'")
            # Attempt to infer if still provisioning based on common output patterns
            if 'UUID:' in output or 'creating image' in output or 'booting' in output:
                 log.warning("Assuming provisioning is still in progress based on output pattern.")
                 new_status = self.EXPERIMENT_PROVISIONING
            else:
                 new_status = self.EXPERIMENT_UNKNOWN # Otherwise, truly unknown

        # Update status if it has changed
        if previous_status != new_status:
            log.info(f"Experiment status changed: {previous_status} -> {new_status}")
            self.status = new_status
        else:
            log.debug(f"Experiment status remains {self.status}")

        return self # Allow chaining


class Node:
    """
    Represents a single node within a Powder experiment.
    Holds identifying information obtained from the experiment manifest.
    """
    def __init__(self, client_id, ip_address, hostname):
        """
        Initializes a Node object.

        Args:
            client_id (str): The client ID defined in the Powder profile (e.g., 'node-0').
            ip_address (str): The public IP address assigned to the node.
            hostname (str): The fully qualified hostname of the node.
        """
        self.client_id = client_id
        self.ip_address = ip_address
        self.hostname = hostname
        # SSH connection can be added here if needed, or managed externally
        # self.ssh = pssh.SSHConnection(ip_address=self.ip_address)
        log.debug(f"Node object created: ID={client_id}, IP={ip_address}, Hostname={hostname}")
