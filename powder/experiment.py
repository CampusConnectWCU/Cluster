#!/usr/bin/env python3
import json
import logging
import sys
import time

import xmltodict

import powder.rpc as prpc
import powder.ssh as pssh


class PowderExperiment:
    """Represents a single powder experiment. Can be used to start, interact with,
    and terminate the experiment. After an experiment is ready, this object
    holds references to the nodes in the experiment, which can be interacted
    with via ssh.

    Args:
        experiment_name (str): A name for the experiment. Must be less than 16 characters.
        project_name (str): The name of the Powder Project associated with the experiment.
        profile_name (str): The name of an existing Powder profile you want to use for the experiment.

    Attributes:
        status (int): Represents the last known status of the experiment as
            retrieved from the Powder RPC servqer.
        nodes (dict of str: Node): A lookup table mapping node ids to Node instances
            in the experiment.
        experiment_name (str)
        project_name (str)
        profile_name (str)

    """

    EXPERIMENT_NOT_STARTED = 0
    EXPERIMENT_PROVISIONING = 1
    EXPERIMENT_PROVISIONED = 2
    EXPERIMENT_READY = 3
    EXPERIMENT_FAILED = 4
    EXPERIMENT_NULL = 5
    EXPERIMENT_UNKNOWN = 6 # Add an unknown status

    POLL_INTERVAL_S = 20
    PROVISION_TIMEOUT_S = 1800
    MAX_NAME_LENGTH = 16

    def __init__(self, experiment_name, project_name, profile_name):
        if len(experiment_name) > 16:
            logging.error('Experiment name {} is too long (cannot exceed {} characters)'.format(experiment_name,
                                                                                                self.MAX_NAME_LENGTH))
            sys.exit(1)

        self.experiment_name = experiment_name
        self.project_name = project_name
        self.profile_name = profile_name
        self.status = self.EXPERIMENT_NOT_STARTED
        self.still_provisioning = False  # Initialize this explicitly
        self.nodes = dict()
        self._manifests = None
        self._poll_count_max = self.PROVISION_TIMEOUT_S // self.POLL_INTERVAL_S
        logging.info('initialized experiment {} based on profile {} under project {}'.format(experiment_name,
                                                                                             profile_name,
                                                                                             project_name))

    def check_status(self):
        """Checks the current status of the experiment without attempting to start it."""
        logging.info(f"Checking status for experiment '{self.experiment_name}'...")
        self._get_status() # Updates self.status and potentially fetches manifests if ready
        logging.info(f"Status check complete. Current status: {self.status}")
        return self.status

    def start_and_wait(self):
        """Start the experiment if not already running and wait for READY or FAILED status."""
        # First, check the status without trying to start
        current_status = self.check_status()

        if current_status == self.EXPERIMENT_READY:
            logging.info(f"Experiment '{self.experiment_name}' is already running and ready.")
            # Manifests should have been fetched by check_status -> _get_status
            if not self.nodes:
                 logging.warning("Experiment is READY but no nodes found. Attempting to fetch/parse manifests again.")
                 try:
                      self._get_manifests()._parse_manifests()
                 except Exception as e:
                      logging.error(f"Error fetching/parsing manifests for already running experiment: {e}", exc_info=True)
                      self.status = self.EXPERIMENT_FAILED # Mark as failed if manifests can't be read
                      return self.status
            return self.status
        elif current_status in [self.EXPERIMENT_PROVISIONING, self.EXPERIMENT_PROVISIONED]:
            logging.info(f"Experiment '{self.experiment_name}' is currently provisioning/provisioned. Waiting for it to become ready...")
            # Fall through to the polling loop below
        elif current_status == self.EXPERIMENT_FAILED:
             logging.warning(f"Experiment '{self.experiment_name}' is in a failed state. Attempting to terminate and restart.")
             self.terminate() # Attempt cleanup
             # Proceed to start below
        elif current_status == self.EXPERIMENT_NOT_STARTED or current_status == self.EXPERIMENT_NULL or current_status == self.EXPERIMENT_UNKNOWN:
             logging.info(f"Experiment '{self.experiment_name}' not running or in unknown state. Attempting to start...")
             # Proceed to start below
        else:
             logging.error(f"Experiment '{self.experiment_name}' in unexpected state {current_status}. Aborting.")
             return current_status # Return the unexpected status

        # --- Attempt to start if needed ---
        if self.status != self.EXPERIMENT_PROVISIONING and self.status != self.EXPERIMENT_PROVISIONED:
            logging.info('Starting experiment {}'.format(self.experiment_name))
            rval, response = prpc.start_experiment(self.experiment_name,
                                                self.project_name,
                                                self.profile_name)
            if rval != prpc.RESPONSE_SUCCESS:
                self.status = self.EXPERIMENT_FAILED
                logging.error(f"Failed to initiate experiment start. Response: {response}")
                return self.status
            # Update status immediately after attempting start
            self._get_status()
            # Handle immediate failure after start attempt
            if self.status == self.EXPERIMENT_FAILED:
                 logging.error("Experiment entered failed state immediately after start request.")
                 return self.status

        # --- Wait loop (common for both starting and already provisioning) ---
        logging.info(f"Waiting for experiment '{self.experiment_name}' to become ready...")
        poll_count = 0
        # Use self.status which is updated by _get_status
        while self.status in [self.EXPERIMENT_PROVISIONING, self.EXPERIMENT_PROVISIONED, self.EXPERIMENT_NOT_STARTED, self.EXPERIMENT_UNKNOWN] and poll_count < self._poll_count_max:
            logging.info(f"Polling experiment status (attempt {poll_count+1}/{self._poll_count_max}). Current status: {self.status}")
            
            # Wait before checking status again
            logging.info(f"Waiting {self.POLL_INTERVAL_S} seconds before next status check...")
            time.sleep(self.POLL_INTERVAL_S)
            
            self._get_status() # Update status and potentially manifests
            poll_count += 1

        # --- Final status check ---
        if self.status == self.EXPERIMENT_READY:
             logging.info(f"Experiment '{self.experiment_name}' is now READY.")
             # Ensure nodes are populated if they weren't already
             if not self.nodes:
                  logging.warning("Experiment became READY but nodes list is empty. This might indicate a manifest parsing issue.")
                  # Attempt parse again just in case
                  try:
                       self._get_manifests()._parse_manifests()
                       if not self.nodes:
                            logging.error("Manifest parsing confirmed empty node list even though experiment is READY.")
                            self.status = self.EXPERIMENT_FAILED # Treat as failure if node info missing
                  except Exception as e:
                       logging.error(f"Error parsing manifests after experiment became ready: {e}", exc_info=True)
                       self.status = self.EXPERIMENT_FAILED
        elif self.status == self.EXPERIMENT_FAILED:
            logging.error(f"Experiment '{self.experiment_name}' failed during provisioning.")
        else: # Timeout or other unexpected state
            logging.error(f"Experiment '{self.experiment_name}' did not become ready within the timeout period. Final status: {self.status}")
            if self.status not in [self.EXPERIMENT_FAILED, self.EXPERIMENT_NULL]:
                 self.status = self.EXPERIMENT_FAILED # Mark as failed if timed out

        logging.info(f"Final experiment status after start_and_wait: {self.status}")
        return self.status

    def terminate(self):
        """Terminate the experiment. All allocated resources will be released."""
        logging.info('terminating experiment {}'.format(self.experiment_name))
        rval, response = prpc.terminate_experiment(self.project_name, self.experiment_name)
        if rval == prpc.RESPONSE_SUCCESS:
            self.status = self.EXPERIMENT_NULL
        else:
            logging.error('failed to terminate experiment')
            logging.error('output {}'.format(response['output']))

        return self.status

    def _get_manifests(self):
        """Get experiment manifests, translate to list of dicts."""
        rval, response = prpc.get_experiment_manifests(self.project_name,
                                                       self.experiment_name)
        if rval == prpc.RESPONSE_SUCCESS:
            try:
                response_json = json.loads(response['output'])
                # --- Logging for raw XML ---
                logging.debug("Raw manifests received from API:")
                for key, xml_content in response_json.items():
                     logging.debug(f"--- Manifest Key: {key} ---")
                     logging.debug(xml_content)
                     logging.debug("--- End Manifest ---")
                
                # --- Add logging before parsing ---
                logging.debug("Attempting to parse XML manifests using xmltodict...")
                self._manifests = [xmltodict.parse(response_json[key]) for key in response_json.keys()]
                # --- Add logging after parsing ---
                logging.debug(f"Successfully parsed {len(self._manifests)} manifests using xmltodict.")
                logging.info('got manifests')
                # --- End logging ---
                
            except json.JSONDecodeError as e:
                logging.error(f"Failed to decode JSON response from get_experiment_manifests: {e}")
                logging.error(f"Raw response output: {response.get('output', 'N/A')}")
                self._manifests = None # Ensure manifests is None if parsing fails
            except Exception as e:
                logging.error(f"Error parsing manifests with xmltodict: {e}", exc_info=True)
                self._manifests = None # Ensure manifests is None if parsing fails
        else:
            logging.error(f"Failed to get manifests. API response code: {rval}, Output: {response.get('output', 'N/A')}")
            self._manifests = None # Ensure manifests is None on API failure

        return self

    # ... inside the PowderExperiment class ...

    def _parse_manifests(self):
        """Parse experiment manifests and add nodes to lookup table.
        Includes detailed logging by default for debugging manifest structure issues.
        """
        # 1. Check if manifests were successfully retrieved
        if not self._manifests:
            logging.warning("Manifest parsing skipped: No manifests were retrieved or available (self._manifests is empty).")
            return self
        
        logging.info(f"Starting to parse {len(self._manifests)} manifest(s).")

        for i, manifest in enumerate(self._manifests):
            logging.debug(f"Processing manifest {i+1}/{len(self._manifests)}.")
            
            # 2. Check for top-level 'rspec' key
            if 'rspec' not in manifest:
                logging.warning(f"Manifest parsing warning: Manifest {i+1} skipped - missing 'rspec' key. Manifest content: {manifest}")
                continue
                
            rspec_data = manifest['rspec']
            
            # 3. Check for 'node' key within 'rspec'
            if 'node' not in rspec_data:
                logging.warning(f"Manifest parsing warning: Manifest {i+1} skipped - 'rspec' dictionary missing 'node' key. Rspec content: {rspec_data}")
                continue
                
            nodes_data = rspec_data['node']
            
            # 4. Handle case where 'node' might be a single dict instead of a list
            if not isinstance(nodes_data, list):
                logging.debug(f"Manifest {i+1}: 'node' data was a single dictionary, converting to list.")
                nodes_data = [nodes_data]
                
            logging.info(f"Manifest {i+1}: Found {len(nodes_data)} node entries to process.")

            for j, node in enumerate(nodes_data):
                logging.debug(f"Processing node {j+1}/{len(nodes_data)} in manifest {i+1}.")
                
                # 5. Basic check: Ensure 'node' entry is a dictionary
                if not isinstance(node, dict):
                    logging.warning(f"Manifest parsing warning: Node entry {j+1} in manifest {i+1} is not a dictionary, skipping. Node data: {node}")
                    continue

                # 6. Use .get() for safer access and add detailed checks
                try:
                    client_id = node.get('@client_id')
                    host_data = node.get('host') # Get the 'host' dictionary safely

                    if not client_id:
                        logging.warning(f"Manifest parsing warning: Node {j+1} in manifest {i+1} skipped - missing '@client_id'. Node data: {node}")
                        continue

                    # 7. Check if 'host' data exists and is a dictionary
                    if not isinstance(host_data, dict):
                        logging.warning(f"Manifest parsing warning: Node '{client_id}' (entry {j+1}, manifest {i+1}) skipped - 'host' key is missing or its value is not a dictionary. Host data: {host_data}")
                        continue
                        
                    # 8. Safely get hostname and ipv4 from the 'host' dictionary
                    hostname = host_data.get('@name')
                    ipv4 = host_data.get('@ipv4')

                    # 9. Check if essential host details were found
                    if not hostname:
                        logging.warning(f"Manifest parsing warning: Node '{client_id}' (entry {j+1}, manifest {i+1}) skipped - missing '@name' in 'host' dictionary. Host data: {host_data}")
                        continue
                    if not ipv4:
                        logging.warning(f"Manifest parsing warning: Node '{client_id}' (entry {j+1}, manifest {i+1}) skipped - missing '@ipv4' in 'host' dictionary. Host data: {host_data}")
                        continue

                    # 10. If all checks pass, create the Node object
                    # Ensure the Node class is defined correctly elsewhere
                    self.nodes[client_id] = Node(client_id=client_id, ip_address=ipv4,
                                                 hostname=hostname)
                    logging.info(f"Successfully parsed and added node: client_id='{client_id}', ip_address='{ipv4}', hostname='{hostname}'")

                except Exception as e:
                    # Catch any other unexpected errors during node processing
                    logging.error(f"Manifest parsing error: Unexpected exception while processing node {j+1} in manifest {i+1}. Error: {e}. Node data: {node}", exc_info=True) # Log traceback

        logging.info(f"Finished parsing manifests. Total nodes added: {len(self.nodes)}")
        return self

    def _get_status(self):
        """Get experiment status and update local state. If the experiment is ready, get
        and parse the associated manifests.
        """
        rval, response = prpc.get_experiment_status(self.project_name,
                                                    self.experiment_name)
        
        # --- Handle case where experiment doesn't exist ---
        # Check rval first, as 'output' might not be present on error
        if rval == prpc.RESPONSE_BADARGS or (rval == prpc.RESPONSE_ERROR and response and "No such experiment" in response.get('output', '')):
             logging.info(f"Experiment '{self.experiment_name}' does not exist.")
             self.status = self.EXPERIMENT_NOT_STARTED
             self.still_provisioning = False
             self.nodes = {} # Clear nodes if experiment doesn't exist
             self._manifests = None
             return self
        elif rval != prpc.RESPONSE_SUCCESS:
            logging.error(f"Failed to get experiment status. Rval: {rval}, Response: {response}")
            # Keep previous status? Or set to unknown/failed? Let's try unknown.
            self.status = self.EXPERIMENT_UNKNOWN 
            self.still_provisioning = False # Assume not provisioning if status check failed
            return self
        # --- End non-existence check ---

        # Proceed with parsing if rval was SUCCESS
        output = response.get('output', '') # Use .get for safety
        stripped_output = output.strip()
        logging.info(f"Raw status response: '{stripped_output}'")

        new_status = self.EXPERIMENT_UNKNOWN # Default if parsing fails
        new_still_provisioning = False

        if stripped_output.startswith('Status: ready'):
            new_status = self.EXPERIMENT_READY
            new_still_provisioning = False
            # --- Add logging before the call ---
            logging.debug("Status is READY. Attempting to get and parse manifests...")
            try:
                # Only fetch/parse if nodes aren't already populated
                if not self.nodes:
                     self._get_manifests()._parse_manifests()
                     logging.debug("Successfully returned from _get_manifests()._parse_manifests()")
                else:
                     logging.debug("Nodes already populated, skipping manifest fetch/parse.")
            except Exception as e:
                logging.error("An unexpected error occurred during manifest fetching/parsing in _get_status", exc_info=True)
                new_status = self.EXPERIMENT_FAILED # If manifest fails for a ready experiment, mark failed
        elif stripped_output.startswith('Status: provisioning'):
            new_status = self.EXPERIMENT_PROVISIONING
            new_still_provisioning = True
        elif stripped_output.startswith('Status: provisioned'):
            new_status = self.EXPERIMENT_PROVISIONED
            new_still_provisioning = True
        elif stripped_output.startswith('Status: failed'):
            new_status = self.EXPERIMENT_FAILED
            new_still_provisioning = False
        else:
            logging.warning(f"Unknown status response: '{stripped_output}'")
            # Keep polling if status is unknown but looks like provisioning output
            if 'UUID:' in stripped_output: # Basic check for ongoing process
                 logging.warning("Assuming provisioning is still in progress despite unknown status line.")
                 new_status = self.EXPERIMENT_PROVISIONING # Treat as provisioning
                 new_still_provisioning = True
            else:
                 new_status = self.EXPERIMENT_UNKNOWN # Set to unknown otherwise
                 new_still_provisioning = False

        self.status = new_status
        self.still_provisioning = new_still_provisioning
        logging.info(f"Updated status to {self.status}, still_provisioning={self.still_provisioning}")

        return self


class Node:
    """Represents a node on the Powder platform. Holds an SSHConnection instance for
    interacting with the node.

    Attributes:
        client_id (str): Matches the id defined for the node in the Powder profile.
        ip_address (str): The public IP address of the node.
        hostname (str): The hostname of the node.
        ssh (SSHConnection): For interacting with the node via ssh through pexpect.

    """
    def __init__(self, client_id, ip_address, hostname):
        self.client_id = client_id
        self.ip_address = ip_address
        self.hostname = hostname
        self.ssh = pssh.SSHConnection(ip_address=self.ip_address)
