#!/usr/bin/env python3
import logging
import os
import pexpect
import re
import time
import sys # Ensure sys is imported

class SSHConnection:
    """A simple ssh/scp wrapper for creating and interacting with ssh sessions via
    pexpect.

    Args:
        ip_address (str): IP address of the node.
        username (str): A username with access to the node.
        prompt (str) (optional): Expected prompt on the host.

    Attributes:
        ssh (pexpect child): A handle to a session started by pexpect.spawn()
    """

    DEFAULT_PROMPT = r'\$' # Use raw string

    def __init__(self, ip_address, username=None, password=None, prompt=DEFAULT_PROMPT):
        self.prompt = prompt
        self.ip_address = ip_address
        if username is None:
            try:
                self.username = os.environ['USER']
            except KeyError:
                logging.error('no USER variable in environment variable and no username provided')
                raise ValueError("Username not provided and USER environment variable not set")

        # Check for SSH key passphrase (used by pexpect for password prompts if key is encrypted)
        # Note: 'PWORD' from rpc.py is for the XML-RPC cert, 'KEYPWORD' is for the SSH key itself if encrypted
        self.password = os.environ.get('KEYPWORD') # Use .get() for safer access
        if not self.password:
            logging.info('no KEYPWORD variable in environment, assuming unencrypted SSH key or using ssh-agent')
        
        # Get certificate path used for SSH identity
        self.cert_path = os.environ.get('CERT')
        if not self.cert_path:
             logging.error("CERT environment variable not set, cannot determine SSH key path.")
             raise ValueError("CERT environment variable pointing to SSH key is required")
        elif not os.path.exists(self.cert_path):
             logging.error(f"SSH certificate file not found at path specified by CERT: {self.cert_path}")
             raise FileNotFoundError(f"SSH certificate file not found: {self.cert_path}")
        
        self.ssh = None # Initialize ssh attribute

    def open(self):
        """Opens an ssh session to the node using the specified identity file.

        Returns:
            SSHConnection: self
        """
        # Construct the SSH command with the identity file and options
        ssh_command = (
            f"ssh -i {self.cert_path} "
            f"-o StrictHostKeyChecking=no "
            f"-o UserKnownHostsFile=/dev/null "
            f"{self.username}@{self.ip_address}"
        )
        logging.debug(f"Attempting SSH connection with command: {ssh_command}")

        retry_count = 0
        max_retries = 4
        while retry_count < max_retries:
            try:
                self.ssh = pexpect.spawn(ssh_command, timeout=15, encoding='utf-8', echo=False) # Use longer timeout, disable echo
                
                # Handle different outcomes: prompt, password, permission denied, EOF, Timeout
                # Added 'yes/no' for first connection host key check (though StrictHostKeyChecking=no should prevent it)
                i = self.ssh.expect([
                    self.prompt,                 # 0: Success (prompt found)
                    '[Pp]assword:',              # 1: Password prompt (for user password, less common with keys)
                    'Enter passphrase for key.*:', # 2: Passphrase prompt (for encrypted key)
                    '[Pp]ermission denied',      # 3: Permission denied
                    'Are you sure you want to continue connecting \(yes/no(/\[fingerprint\])?\)\?', # 4: Host key check prompt
                    pexpect.EOF,                 # 5: End of File
                    pexpect.TIMEOUT              # 6: Timeout
                ], timeout=20) # Increased expect timeout

                if i == 0:  # Expected prompt
                    logging.info(f'SSH session open to {self.ip_address}')
                    return self
                
                elif i == 1: # Password prompt (less likely with key auth)
                     logging.warning("SSH asking for user password, not key passphrase. Check SSH server config or if key auth failed silently.")
                     # If you have a user password in an env var, you could try sending it here
                     # self.ssh.sendline(os.environ.get('USER_PASSWORD')) 
                     # ... then expect prompt or denial ...
                     self.ssh.close(force=True)
                     raise ValueError("SSH requested user password, which is unexpected for key-based auth.")

                elif i == 2:  # Passphrase prompt for encrypted key
                    if self.password:
                        logging.debug('SSH key passphrase prompt received, sending passphrase...')
                        self.ssh.sendline(self.password)
                        # Expect prompt or denial after sending passphrase
                        j = self.ssh.expect([self.prompt, '[Pp]ermission denied'], timeout=15)
                        if j == 0:
                            logging.info(f'SSH session open to {self.ip_address} after key passphrase')
                            return self
                        else:
                            logging.error('SSH key passphrase authentication failed (permission denied after passphrase)')
                            self.ssh.close(force=True)
                            raise ValueError("SSH key passphrase authentication failed")
                    else:
                        logging.error('SSH key passphrase prompt received, but no KEYPWORD provided/found.')
                        self.ssh.close(force=True)
                        raise ValueError("Encrypted SSH key requires KEYPWORD environment variable")
                
                elif i == 3:  # Permission denied
                    logging.error(f"SSH Permission denied for {self.username}@{self.ip_address} using key {self.cert_path}.")
                    logging.error(f"Ensure the public key corresponding to '{os.path.basename(self.cert_path)}' is added to user '{self.username}' on CloudLab.")
                    logging.debug(f"ssh.before: {self.ssh.before}") 
                    self.ssh.close(force=True)
                    raise ValueError("SSH Permission denied (publickey)") # No point retrying if key is explicitly denied

                elif i == 4: # Host key check prompt
                     logging.debug("Received host key check prompt, sending 'yes'.")
                     self.ssh.sendline('yes')
                     # After sending 'yes', expect the next step (passphrase, prompt, denial, etc.)
                     k = self.ssh.expect([
                         self.prompt, 
                         '[Pp]assword:', 
                         'Enter passphrase for key.*:', 
                         '[Pp]ermission denied',
                         pexpect.EOF, 
                         pexpect.TIMEOUT
                         ], timeout=15)
                     # Re-evaluate based on 'k' similar to how 'i' was handled above
                     if k == 0: return self # Prompt
                     elif k == 1: raise ValueError("SSH requested user password after host key check.")
                     elif k == 2: # Passphrase needed
                          if self.password:
                               self.ssh.sendline(self.password)
                               l = self.ssh.expect([self.prompt, '[Pp]ermission denied'], timeout=15)
                               if l == 0: return self
                               else: raise ValueError("Passphrase auth failed after host key check.")
                          else: raise ValueError("Encrypted key requires KEYPWORD after host key check.")
                     elif k == 3: raise ValueError("Permission denied after host key check.")
                     else: 
                          logging.debug(f"Unexpected state {k} after sending 'yes' to host key check.")
                          self.ssh.close(force=True) # Close on unexpected state
                          # Continue to retry logic below

                elif i == 5:  # EOF
                    logging.debug(f"SSH connection attempt {retry_count+1} failed: Unexpected EOF")
                    logging.debug(f"ssh.before: {self.ssh.before}") 
                    if self.ssh and not self.ssh.closed: self.ssh.close(force=True) 
                
                elif i == 6: # Timeout
                     logging.debug(f"SSH connection attempt {retry_count+1} failed: TIMEOUT")
                     logging.debug(f"ssh.before: {self.ssh.before}")
                     if self.ssh and not self.ssh.closed: self.ssh.close(force=True)

            except pexpect.exceptions.ExceptionPexpect as e:
                logging.error(f"pexpect exception during SSH connection attempt {retry_count+1}: {e}")
                if hasattr(self, 'ssh') and self.ssh and not self.ssh.closed:
                     self.ssh.close(force=True) # Ensure closed on exception

            # If we reached here, it means EOF, Timeout, or certain other recoverable issues occurred
            retry_count += 1
            logging.debug(f"SSH connection failed, retry count: {retry_count}/{max_retries}")
            if retry_count < max_retries:
                 wait_time = retry_count * 2 # Basic backoff
                 logging.info(f"Retrying SSH connection in {wait_time} seconds...")
                 time.sleep(wait_time) 

        # If loop finishes without returning, connection failed definitively
        logging.error(f'Failed to establish SSH connection to {self.ip_address} after {max_retries} retries.')
        raise ConnectionError(f"Could not connect via SSH to {self.ip_address} after multiple retries")


    def command(self, commandline, expectedline=None, timeout=60):
        """Sends `commandline` to `self.ip_address` and waits for `expectedline`."""
        if not self.ssh or self.ssh.closed:
             logging.error("Cannot send command, SSH connection is not open.")
             raise ConnectionError("SSH connection is not open.")
             
        if expectedline is None:
             expectedline = self.prompt # Use default prompt if none provided

        logging.debug(f"Executing command: {commandline}")
        self.ssh.sendline(commandline)
        
        try:
            # Expect the prompt or specific output
            i = self.ssh.expect([expectedline, pexpect.EOF, pexpect.TIMEOUT], timeout=timeout)
            
            # Log output before the expected line for context
            logging.debug(f"Command output before expected '{expectedline}':\n{self.ssh.before.strip()}")

            if i == 0:
                logging.debug(f"Command executed successfully, expected line '{expectedline}' found.")
                return self.ssh.before # Return output before the match
            elif i == 1:
                logging.error(f'Command execution failed: Unexpected EOF. Expected: {expectedline}')
                raise ConnectionAbortedError("SSH connection closed unexpectedly during command execution.")
            elif i == 2:
                logging.error(f'Command execution failed: Unexpected Timeout. Expected: {expectedline}')
                raise TimeoutError(f"Timeout waiting for '{expectedline}' after command execution.")
                
        except pexpect.exceptions.ExceptionPexpect as e:
             logging.error(f"pexpect exception during command execution: {e}")
             raise

    def copy_to(self, local_path, remote_path='.'):
        """Copies a file to the node via scp."""
        if not os.path.exists(local_path):
             logging.error(f"Local file '{local_path}' not found for copy_to.")
             raise FileNotFoundError(f"Local file '{local_path}' not found.")
        
        scp_command = (
            f"scp -i {self.cert_path} "
            f"-o StrictHostKeyChecking=no "
            f"-o UserKnownHostsFile=/dev/null "
            f"{local_path} {self.username}@{self.ip_address}:{remote_path}"
        )
        return self._run_scp(scp_command)

    def copy_from(self, remote_path, local_path='.'):
        """Copies a file from the node via scp."""
        scp_command = (
            f"scp -i {self.cert_path} "
            f"-o StrictHostKeyChecking=no "
            f"-o UserKnownHostsFile=/dev/null "
            f"{self.username}@{self.ip_address}:{remote_path} {local_path}"
        )
        return self._run_scp(scp_command)

    def _run_scp(self, scp_command):
        """Executes an scp command using pexpect.run."""
        logging.debug(f"Attempting SCP with command: {scp_command}")
        
        # Prepare events dictionary for potential passphrase prompt
        events = {}
        if self.password:
             events['Enter passphrase for key.*:'] = f'{self.password}\n'
             # Add password prompt too, just in case, though less likely
             events['[Pp]assword:'] = f'{self.password}\n' 

        try:
            # Use pexpect.run for simpler execution when interaction isn't complex
            # Capture output and exit status
            output, exit_status = pexpect.run(
                scp_command, 
                timeout=120, # Increased timeout for potentially large files
                withexitstatus=True, 
                encoding='utf-8', 
                events=events,
               # logfile=sys.stdout.buffer # Log SCP output directly for debugging
            )
            
            if exit_status == 0:
                logging.info('SCP command completed successfully.')
                return True
            else:
                logging.error(f'SCP command failed with exit status {exit_status}')
                # Output is already logged via logfile=sys.stdout.buffer
                # Add specific checks if needed
                if "No such file or directory" in output:
                     logging.warning("SCP failed: Remote file or local directory might not exist.")
                elif "Permission denied" in output:
                     logging.error("SCP failed: Permission denied. Check key, user, and file permissions.")
                return False
                
        except pexpect.exceptions.TIMEOUT:
             logging.error("SCP command timed out.")
             return False
        except Exception as e:
             logging.error(f"An unexpected error occurred during SCP execution: {e}", exc_info=True)
             return False

    def close(self, wait_s=2): # Reduced default wait time
        """Closes the ssh session."""
        if self.ssh and not self.ssh.closed:
            try:
                logging.debug(f"Attempting to close SSH connection to {self.ip_address}...")
                self.ssh.sendline('exit')
                # Expect EOF quickly after exit
                self.ssh.expect([pexpect.EOF, pexpect.TIMEOUT], timeout=wait_s) 
                if not self.ssh.closed:
                     self.ssh.close(force=True) # Force close if exit didn't work
                logging.info(f"SSH connection to {self.ip_address} closed.")
            except pexpect.exceptions.ExceptionPexpect as e:
                logging.warning(f"Exception during SSH close: {e}. Forcing close.")
                if self.ssh and not self.ssh.closed:
                    self.ssh.close(force=True)
            except OSError as e:
                 # Handle cases where the process might already be dead
                 logging.warning(f"OS error during SSH close (process likely already ended): {e}")
                 self.ssh.closed = True # Mark as closed
        else:
            logging.debug("SSH connection already closed or not established.")
        
        return True # Return True indicating close attempt was made
