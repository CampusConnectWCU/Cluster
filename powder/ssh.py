#!/usr/bin/env python3
import logging
import os
import pexpect
import re
import time
import sys

log = logging.getLogger(__name__)

class SSHConnection:
    """
    Manages an SSH connection to a remote node using pexpect.
    Handles authentication (key-based, optional passphrase), command execution,
    and file transfer (SCP). Includes retry logic for connection establishment.
    """

    DEFAULT_PROMPT = r'\$' # Default expected shell prompt regex

    def __init__(self, ip_address, username=None, password=None, prompt=DEFAULT_PROMPT):
        """
        Initializes the SSH connection parameters.

        Args:
            ip_address (str): IP address of the remote node.
            username (str, optional): Username for SSH login. Defaults to USER env var.
            password (str, optional): Deprecated/unused. Passphrase handled by KEYPWORD env var.
            prompt (str, optional): Regex for the expected shell prompt. Defaults to DEFAULT_PROMPT.
        """
        self.prompt = prompt
        self.ip_address = ip_address

        # Determine username
        if username:
             self.username = username
        else:
            try:
                self.username = os.environ['USER']
            except KeyError:
                log.critical('Username not provided and USER environment variable not set.')
                raise ValueError("Username not provided and USER environment variable not set")

        # Get SSH key passphrase from environment if provided
        self.password = os.environ.get('KEYPWORD')
        if not self.password:
            log.debug('KEYPWORD environment variable not set; assuming unencrypted key or ssh-agent.')

        # Get path to SSH private key from environment
        self.cert_path = os.environ.get('CERT')
        if not self.cert_path:
             log.critical("CERT environment variable (path to SSH key) not set.")
             raise ValueError("CERT environment variable pointing to SSH key is required")
        elif not os.path.exists(self.cert_path):
             log.critical(f"SSH key file not found at path specified by CERT: {self.cert_path}")
             raise FileNotFoundError(f"SSH key file not found: {self.cert_path}")

        self.ssh = None # pexpect child process handle

    def open(self):
        """
        Establishes the SSH connection with retry logic.

        Handles prompts for host key verification and key passphrase.

        Returns:
            SSHConnection: self

        Raises:
            ConnectionError: If connection fails after multiple retries.
            ConnectionRefusedError: If permission is denied.
            ValueError: If authentication requires unexpected input (e.g., user password).
        """
        # SSH command options: use specified key, disable strict host key checking, ignore known_hosts
        ssh_command = (
            f"ssh -i {self.cert_path} "
            f"-o StrictHostKeyChecking=no "
            f"-o UserKnownHostsFile=/dev/null "
            f"{self.username}@{self.ip_address}"
        )
        log.debug(f"Attempting SSH connection: ssh -i ... {self.username}@{self.ip_address}") # Redact key path

        retry_count = 0
        max_retries = 4 # Number of connection attempts
        while retry_count < max_retries:
            try:
                # Spawn the SSH process
                self.ssh = pexpect.spawn(ssh_command, timeout=20, encoding='utf-8', echo=False)

                # Define expected patterns during connection setup
                expected_patterns = [
                    self.prompt,                       # 0: Success (shell prompt)
                    '[Pp]assword:',                    # 1: User password prompt (unexpected)
                    'Enter passphrase for key.*:',     # 2: Key passphrase prompt
                    '[Pp]ermission denied',            # 3: Permission denied error
                    r'Are you sure you want to continue connecting \(yes/no(/\[fingerprint\])?\)\?', # 4: Host key verification
                    pexpect.EOF,                       # 5: Connection closed unexpectedly
                    pexpect.TIMEOUT                    # 6: Timeout waiting for prompt/response
                ]
                i = self.ssh.expect(expected_patterns, timeout=25)

                if i == 0:  # Success: Prompt found
                    log.info(f'SSH session established to {self.ip_address}')
                    return self

                elif i == 1: # Unexpected user password prompt
                     log.error("SSH requested user password; key-based authentication expected but failed or is disabled.")
                     self.ssh.close(force=True)
                     raise ValueError("SSH requested user password, unexpected for key authentication.")

                elif i == 2:  # Key passphrase prompt
                    if self.password:
                        log.debug('SSH key requires passphrase, sending KEYPWORD...')
                        self.ssh.sendline(self.password)
                        # Expect prompt or denial after sending passphrase
                        j = self.ssh.expect([self.prompt, '[Pp]ermission denied'], timeout=15)
                        if j == 0:
                            log.info(f'SSH session established to {self.ip_address} after providing key passphrase.')
                            return self
                        else:
                            log.error('SSH key passphrase authentication failed (Permission denied after sending passphrase).')
                            self.ssh.close(force=True)
                            raise ValueError("Incorrect SSH key passphrase or other permission issue.")
                    else:
                        log.error('SSH key requires a passphrase, but KEYPWORD environment variable is not set.')
                        self.ssh.close(force=True)
                        raise ValueError("Encrypted SSH key requires KEYPWORD environment variable.")

                elif i == 3:  # Permission denied
                    log.error(f"SSH permission denied for {self.username}@{self.ip_address} using key {os.path.basename(self.cert_path)}.")
                    log.error("Verify the public key is in authorized_keys on the remote host and file permissions are correct.")
                    log.debug(f"Output before permission denied: {self.ssh.before.strip()}")
                    self.ssh.close(force=True)
                    raise ConnectionRefusedError("SSH Permission Denied (publickey)")

                elif i == 4: # Host key verification prompt
                     log.info("First connection: Accepting host key.")
                     self.ssh.sendline('yes')
                     # Expect the next step after accepting host key
                     k = self.ssh.expect(expected_patterns, timeout=15) # Re-expect using same patterns

                     # Re-evaluate based on the new state 'k'
                     if k == 0: # Prompt
                         log.info(f'SSH session established to {self.ip_address} after host key confirmation.')
                         return self
                     elif k == 1: # Password prompt
                         log.error("SSH requested user password after host key confirmation.")
                         raise ValueError("SSH requested user password after host key confirmation.")
                     elif k == 2: # Passphrase needed
                          if self.password:
                               log.debug("Sending key passphrase after host key confirmation...")
                               self.ssh.sendline(self.password)
                               l = self.ssh.expect([self.prompt, '[Pp]ermission denied'], timeout=15)
                               if l == 0:
                                   log.info(f'SSH session established to {self.ip_address} after host key confirmation and passphrase.')
                                   return self
                               else:
                                   log.error("Passphrase authentication failed after host key confirmation.")
                                   raise ValueError("Passphrase authentication failed after host key confirmation.")
                          else:
                              log.error("Encrypted key requires KEYPWORD after host key confirmation.")
                              raise ValueError("Encrypted key requires KEYPWORD after host key confirmation.")
                     elif k == 3: # Permission denied
                         log.error("Permission denied after host key confirmation.")
                         raise ConnectionRefusedError("Permission denied after host key confirmation.")
                     else: # EOF, Timeout, or other unexpected state
                          log.warning(f"Unexpected state ({k}) or issue after accepting host key.")
                          if self.ssh and not self.ssh.closed: self.ssh.close(force=True)
                          # Continue to retry logic

                elif i == 5:  # EOF
                    log.warning(f"SSH connection attempt {retry_count+1} failed: Unexpected EOF.")
                    log.debug(f"Output before EOF: {self.ssh.before.strip()}")
                    if self.ssh and not self.ssh.closed: self.ssh.close(force=True)

                elif i == 6: # Timeout
                     log.warning(f"SSH connection attempt {retry_count+1} failed: Timeout waiting for prompt/response.")
                     log.debug(f"Output before timeout: {self.ssh.before.strip()}")
                     if self.ssh and not self.ssh.closed: self.ssh.close(force=True)

            except pexpect.exceptions.ExceptionPexpect as e:
                log.error(f"pexpect exception during SSH connection attempt {retry_count+1}: {e}")
                if hasattr(self, 'ssh') and self.ssh and not self.ssh.closed:
                     self.ssh.close(force=True) # Ensure cleanup on pexpect error

            # --- Retry Logic ---
            retry_count += 1
            if retry_count < max_retries:
                 # Exponential backoff for retries
                 wait_time = 2 ** retry_count
                 log.info(f"Retrying SSH connection in {wait_time} seconds... ({retry_count}/{max_retries})")
                 time.sleep(wait_time)

        # If loop completes without success
        log.critical(f'Failed to establish SSH connection to {self.ip_address} after {max_retries} retries.')
        raise ConnectionError(f"Could not connect via SSH to {self.ip_address} after multiple retries")


    def command(self, commandline, expectedline=None, timeout=60):
        """
        Executes a command on the remote host and waits for expected output.

        Args:
            commandline (str): The command to execute.
            expectedline (str, optional): A regex pattern to wait for after the command.
                                         Defaults to the connection's prompt.
            timeout (int, optional): Maximum time in seconds to wait for expectedline. Defaults to 60.

        Returns:
            str: The output captured *before* the expectedline was matched.

        Raises:
            ConnectionError: If the SSH connection is not open.
            ConnectionAbortedError: If the connection closes unexpectedly during execution.
            TimeoutError: If the expectedline is not seen within the timeout.
            pexpect.exceptions.ExceptionPexpect: For other pexpect-related errors.
        """
        if not self.ssh or self.ssh.closed:
             log.error("Cannot execute command: SSH connection is not open.")
             raise ConnectionError("SSH connection is not open.")

        # Use the default prompt if no specific expectation is provided
        effective_expectedline = expectedline if expectedline is not None else self.prompt

        log.debug(f"Executing command: {commandline}")
        self.ssh.sendline(commandline)

        try:
            # Wait for the expected pattern, EOF, or Timeout
            i = self.ssh.expect([effective_expectedline, pexpect.EOF, pexpect.TIMEOUT], timeout=timeout)

            # Capture output that occurred before the expected pattern/event
            output_before = self.ssh.before.strip()
            log.debug(f"Command output before match:\n{output_before}")

            if i == 0: # Expected pattern matched
                log.debug(f"Command executed successfully, expected pattern matched.")
                return output_before
            elif i == 1: # EOF received
                log.error(f'Command execution failed: Connection closed unexpectedly (EOF). Expected: "{effective_expectedline}"')
                raise ConnectionAbortedError("SSH connection closed unexpectedly during command execution.")
            elif i == 2: # Timeout occurred
                log.error(f'Command execution failed: Timeout waiting for "{effective_expectedline}".')
                raise TimeoutError(f"Timeout waiting for '{effective_expectedline}' after command execution.")

        except pexpect.exceptions.ExceptionPexpect as e:
             log.error(f"pexpect exception during command execution: {e}")
             # Log output preceding the error if possible
             if hasattr(self, 'ssh') and self.ssh and not self.ssh.closed:
                  log.error(f"Output before pexpect error:\n{self.ssh.before.strip()}")
             raise

    def copy_to(self, local_path, remote_path='.'):
        """
        Copies a local file to the remote node using SCP.

        Args:
            local_path (str): Path to the local file.
            remote_path (str, optional): Destination path on the remote node. Defaults to user's home directory.

        Returns:
            bool: True on success, False on failure.

        Raises:
            FileNotFoundError: If the local file does not exist.
        """
        if not os.path.exists(local_path):
             log.error(f"Local file '{local_path}' not found for SCP.")
             raise FileNotFoundError(f"Local file '{local_path}' not found.")

        scp_command = (
            f"scp -i {self.cert_path} "
            f"-o StrictHostKeyChecking=no "
            f"-o UserKnownHostsFile=/dev/null "
            f"{local_path} {self.username}@{self.ip_address}:{remote_path}"
        )
        return self._run_scp(scp_command, f"copy '{os.path.basename(local_path)}' to remote")

    def copy_from(self, remote_path, local_path='.'):
        """
        Copies a remote file from the node using SCP.

        Args:
            remote_path (str): Path to the remote file.
            local_path (str, optional): Destination path on the local machine. Defaults to current directory.

        Returns:
            bool: True on success, False on failure.
        """
        scp_command = (
            f"scp -i {self.cert_path} "
            f"-o StrictHostKeyChecking=no "
            f"-o UserKnownHostsFile=/dev/null "
            f"{self.username}@{self.ip_address}:{remote_path} {local_path}"
        )
        return self._run_scp(scp_command, f"copy '{remote_path}' from remote")

    def _run_scp(self, scp_command, operation_desc="SCP"):
        """
        Internal helper to execute an SCP command using pexpect.run.

        Args:
            scp_command (str): The full SCP command string.
            operation_desc (str): Description of the operation for logging.

        Returns:
            bool: True on success, False on failure.
        """
        log.debug(f"Executing {operation_desc}: scp -i ...") # Redact key path

        # Define events to handle potential passphrase/password prompts during SCP
        events = {}
        if self.password:
             events['Enter passphrase for key.*:'] = f'{self.password}\n'
             events['[Pp]assword:'] = f'{self.password}\n' # Handle unexpected password prompt

        try:
            # pexpect.run is simpler for non-interactive commands like SCP
            output, exit_status = pexpect.run(
                scp_command,
                timeout=180, # Timeout for file transfer
                withexitstatus=True,
                encoding='utf-8',
                events=events,
                # logfile=sys.stdout.buffer # Uncomment for verbose SCP debugging output
            )

            if exit_status == 0:
                log.info(f'{operation_desc} completed successfully.')
                return True
            else:
                log.error(f'{operation_desc} failed with exit status {exit_status}.')
                log.error(f"SCP output:\n{output.strip()}") # Log SCP output on failure
                # Provide hints based on common errors
                if "No such file or directory" in output:
                     log.warning("SCP hint: Check if remote/local paths are correct and exist.")
                elif "Permission denied" in output:
                     log.error("SCP hint: Check key validity, remote user permissions, and file/directory permissions.")
                return False

        except pexpect.exceptions.TIMEOUT:
             log.error(f"{operation_desc} command timed out.")
             return False
        except Exception as e:
             log.error(f"An unexpected error occurred during {operation_desc}: {e}", exc_info=True)
             return False

    def close(self, wait_s=1):
        """
        Closes the SSH connection gracefully if possible, forces close otherwise.

        Args:
            wait_s (int, optional): Time in seconds to wait for 'exit' command to close connection. Defaults to 1.

        Returns:
            bool: True (indicates close attempt was made).
        """
        if self.ssh and not self.ssh.closed:
            try:
                log.debug(f"Closing SSH connection to {self.ip_address}...")
                self.ssh.sendline('exit')
                # Wait briefly for EOF after sending exit
                self.ssh.expect([pexpect.EOF, pexpect.TIMEOUT], timeout=wait_s)
                if not self.ssh.closed:
                     log.debug("Connection not closed by 'exit', forcing close.")
                     self.ssh.close(force=True)
                log.info(f"SSH connection to {self.ip_address} closed.")
            except (pexpect.exceptions.ExceptionPexpect, OSError) as e:
                # Handle errors during close (e.g., process already dead)
                log.warning(f"Exception during SSH close (forcing close): {e}")
                if self.ssh and not self.ssh.closed:
                    self.ssh.close(force=True)
                # Ensure state reflects closure even if force close fails
                self.ssh.closed = True
        else:
            log.debug("SSH connection already closed or was never established.")

        return True
