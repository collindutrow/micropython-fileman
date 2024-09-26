# ----------------------------------------------------------------------------
# Author: Collin Dutrow
# Date: 2024-09-26
# Description: Intuitive file management for MicroPython MCUs.
# 
# Usage instructions:
# 1. Connect microcontroller to the computer via USB.
# 2. Run the script: python mpfm.py -s COM1
# 3. Create, delete, and modify files in the local temporary directory.
# 4. Profit!
# ----------------------------------------------------------------------------

import argparse
import os
import platform
import serial
import shutil
import signal
import subprocess
import tempfile
import time
from functools import partial
from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer

# ----------------------------------------------------------------------------
# NOTES:
#  * WARNING: This script uses a number of time.sleep() calls that are critical
#       to the proper operation of the script. At this time they have only
#       been tested on a Windows machine targeting a Raspberry Pi Pico W. 
#       Different environments may require different timing.
#  * WARNING: When passing paths to the MCU replace Windows file separators 
#       with Unix separators, otherwise MicroPython will use the backslash 
#       in the filename rather than use it to determine the directory.
#  * WARNING: Be careful when sending commands that are multiline.
#       REPL may not handle them as expected.
#  * NOTICE: Files are only synced from the MCU to the local directory once.
#       Files are synced from the local directory to the MCU continuously.
# ----------------------------------------------------------------------------

# ----------------------------------------------------------------------------
# Default Configuration
# ----------------------------------------------------------------------------

# The default serial port to connect to the MCU if --serial-port is not specified.
serial_port = "COM1"

# Time to allow for in_waiting to be populated with data from the MCU.
serial_in_waiting_time = 0.1

# Time to allow for the MCU to process a command before reading the response.
serial_default_process_time = 0.1

# Time to wait after sending Ctrl+D to reboot the MCU during the startup sequence.
mcu_reboot_wait_time = 0.25

# Time to wait after sending Ctrl+C to the MCU during the startup sequence.
mcu_interrupt_wait_time = 0.25

# Auto open the temporary directory in file manager.
auto_explore_tmp = False

# Auto open the temporary directory in editor.
auto_edit_tmp = True

# Default editor to open the temporary directory in.
default_editor = "code"

# xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
# END OF USER CONFIGURATION. MODIFY THE CODE BELOW AT YOUR OWN RISK.
# xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx

# ----------------------------------------------------------------------------
# Global Variables
# ----------------------------------------------------------------------------
tmp_dir = '' # Will be set later.
starting_dir = os.getcwd()
observer = None
cleanup_called = False

# ----------------------------------------------------------------------------
# Ceanup Function
# ----------------------------------------------------------------------------

def cleanup(signum, frame, ser):
    global tmp_dir, observer, cleanup_called

    if cleanup_called:
        return
    
    cleanup_called = True

    print("Starting cleanup...")

    # Change back to the original directory so we can delete the temporary directory.
    os.chdir(starting_dir)

    if observer:
        observer.stop()
        observer.join()

    print(f"Cleaning up temporary directory: {tmp_dir}")
    try :
        shutil.rmtree(tmp_dir)
    except Exception as e:
        print(f"Error deleting temporary directory: {e}")

    soft_reboot_mcu(ser, wait=False)
    close_connection(ser)
    exit(0)

# ----------------------------------------------------------------------------
# MCU Serial Communications Functions
# ----------------------------------------------------------------------------

def connect_to_mcu(port, baudrate=115200):
    """Connect to the MCU via serial communication and prepare REPL."""
    ser = serial.Serial(port, baudrate, timeout=1)

    # TODO Implement a more robust connection check. This will usually always pass.

    try:
        if ser.is_open:
            print("Connected to MCU")
        else:
            print("ERROR: Failed to open serial port.")
            exit(1)
    except serial.SerialException as e:
        print(f"ERROR: {e}")
        exit(1)

    return ser

def initialize_repl_env(ser):
    # Get the MCU into a clean state)
    print("Soft rebooting the MCU")
    soft_reboot_mcu(ser)

    """Initialize the REPL environment on the MCU."""
    # Give time for the REPL to initialize
    #time.sleep(2)
    
    # Send Ctrl-C to interrupt any running code and ensure REPL mode
    print("Starting MCU REPL")
    send_interrupt(ser)
    
    print("Creating file operation functions")
    print("... defining read_file")
    create_read_file_function(ser)
    print("... defining write_file")
    create_write_file_function(ser)
    print("... defining mkdir_recursive")
    create_mkdir_function(ser)
    print("... defining rmdir_recursive")
    create_rmdir_function(ser)
    print("... defining list_files_recursively")
    create_list_files_function(ser)


def close_connection(ser):
    ser.close()

def soft_reboot_mcu(ser, wait=True):
    """Reboot the MCU."""
    global mcu_reboot_wait_time
    ser.write(b'\x04')  # Sends Ctrl+D to soft reboot and enter the REPL
    if wait:
        time.sleep(mcu_reboot_wait_time)


def send_interrupt(ser):
    """Send an interrupt signal to the MCU."""
    global mcu_interrupt_wait_time
    ser.write(b'\x03')  # Sends Ctrl+C to stop any running code
    time.sleep(mcu_interrupt_wait_time)


def exec_repl(ser, command, process_time=serial_default_process_time, ignore_response=False, debug=False):
    """Sends a command to the MCU REPL and retrieves the output."""
    global serial_in_waiting_time
    ser.write((command + '\r\n').encode())  # Send the command
    time.sleep(process_time)  # Allow the MCU some time to process

    if ignore_response and not debug:
        # Clear the buffer so we don't get any extra output in the next command
        ser.reset_input_buffer()
        return ""

    response = []
    full_response = []

    command_found = False
    
    while ser.in_waiting > 0:
        line = ser.readline().decode().strip()
        if debug:
            # Ignore the command in response, ignore ... and >>> lines
            stripped_line = line.strip()
            if stripped_line != command.strip() and stripped_line != '>>>' and stripped_line != '...':
                full_response.append(line)

        if line and (line.startswith('>>> ' + command) or line.startswith(command)):  # Ignore empty lines
            command_found = True
        elif line and command_found and not line.startswith('>>>'):
            # We are only interested in the lines after the repl command.
            response.append(line)
        time.sleep(serial_in_waiting_time)  # Give the MCU some time to send more data

    # Clear the buffer so we don't get any extra output in the next command
    ser.reset_input_buffer()
    response = '\n'.join(response).strip()

    if debug:
        full_response_str = '\n'.join(full_response)
        if full_response_str:
            print("--------------------")
            print("   EXEC REPL DUMP   ")
            print("--------------------")
            print("== COMMAND ==")
            print(command)
            print("== RESPONSE ==")
            print(full_response_str)
            print("--------------------")

    return response


def create_function(ser, function_string, process_time=0.1, append_newlines=True, ignore_response=True,  debug=False):
    # Append two blank lines at the end of the function code, as it is how REPL knows the function is complete.
    if append_newlines:
        function_string += "\r\n\r\n"
    exec_repl(ser, function_string, process_time, ignore_response=ignore_response, debug=debug)


def create_read_file_function(ser):
    """Uploads the read_file function to the MCU."""
    function_code = """def read_file(filename): return open(filename, 'rb').read()"""
    create_function(ser, function_code, ignore_response=True, debug=False)


def create_write_file_function(ser):
    """Uploads the write_file function to the MCU."""
    function_code = """def write_file(filename, content): f = open(filename, 'wb'); f.write(content); f.close()"""
    create_function(ser, function_code, ignore_response=True, debug=False)


def create_mkdir_function(ser):
    """Uploads the mkdir function to the MCU."""
    # I was unable to get this working as a oneliner, special handling is needed via a for loop.
    function_code = """def mkdir_recursive(directory):
    import os; path = "";
    for d in directory.split('/'):
        path = f"{path}/{d}" if path else d;
        try: os.stat(path);
        except OSError: os.mkdir(path);
"""
    # NOTE this function generates an indent error when debug is true. But no negligeable impact has been noted...
    for line in function_code.split('\n'):
        create_function(ser, line, append_newlines=True, ignore_response=True, debug=False)
    # Caused new lines to be added to the REPL to complete the function.
    create_function(ser, "", ignore_response=True, debug=False)


def create_rmdir_function(ser):
    """Uploads the rmdir function to the MCU."""
    function_code = """def rmdir_recursive(directory): import os; [rmdir_recursive(directory + "/" + entry) if os.stat(directory + "/" + entry)[0] & 0x4000 else os.remove(directory + "/" + entry) for entry in os.listdir(directory)]; os.rmdir(directory)"""
    create_function(ser, function_code, ignore_response=True, debug=False)


def create_list_files_function(ser):
    # This function uses the read_file function when the param get_content is set to True.
    """Uploads the list_files_recursively function to the MCU."""
    function_code = """def list_files_recursively(directory="", get_contents=False): import os; return [{"path": entry if directory == "" else directory + "/" + entry, "type": "directory" if os.stat(entry if directory == "" else directory + "/" + entry)[0] & 0x4000 else "file", "contents": read_file(entry if directory == "" else directory + "/" + entry) if get_contents and not os.stat(entry if directory == "" else directory + "/" + entry)[0] & 0x4000 else None} for entry in os.listdir(directory)] + [item for entry in os.listdir(directory) if os.stat(entry if directory == "" else directory + "/" + entry)[0] & 0x4000 for item in list_files_recursively(entry if directory == "" else directory + "/" + entry, get_contents)]"""
    create_function(ser, function_code, ignore_response=True, debug=False)


# ----------------------------------------------------------------------------
# MCU File Operation Interface Functions
# ----------------------------------------------------------------------------

def list_files(ser, directory="", get_contents=False):
    """
    Recursively list files on the MCU, by default returns all files and directories.
    Returns a list of dictionaries containing the path, type, and (optionally) contents of each file.
    """
    command = f"list_files_recursively(\"{directory}\", get_contents={get_contents})"
    response = exec_repl(ser, command)

    assert isinstance(response, str)
    assert response.startswith('[') and response.endswith(']')

    return eval(response)


def read_file(ser, filename):
    """Read the contents of a file from the MCU."""
    filename = filename.replace('\\\\', '/').replace('\\', '/')
    command = f"read_file('{filename}')"
    response = exec_repl(ser, command)

    assert isinstance(response, str)
    assert response.startswith('b\'')

    # Decode the bytes to string
    return eval(response).decode('unicode_escape')


def write_file(ser, filename, content):
    """Write content to a file on the MCU."""
    filename = filename.replace('\\\\', '/').replace('\\', '/')
    bytes_content = content.encode('unicode_escape')
    directory = os.path.dirname(filename)
    command = f"mkdir_recursive('{directory}'); write_file('{filename}', {bytes_content})"
    exec_repl(ser, command, debug=True)


def delete_file(ser, filename):
    """Delete a file from the MCU."""
    filename = filename.replace('\\\\', '/').replace('\\', '/')
    command = f"import os; os.remove('{filename}')"
    exec_repl(ser, command)


def stat_file(ser, filename):
    """Get the stat information of a file on the MCU."""
    filename = filename.replace('\\\\', '/').replace('\\', '/')
    command = f"import os; os.stat('{filename}')"
    response = exec_repl(ser, command)
    return response


def create_dir(ser, directory):
    """Create a directory on the MCU."""
    directory = directory.replace('\\\\', '/').replace('\\', '/')
    command = f"mkdir_recursive('{directory}')"
    exec_repl(ser, command)


def delete_dir(ser, directory):
    """Delete a directory from the MCU."""
    directory = directory.replace('\\\\', '/').replace('\\', '/')
    command = f"rmdir_recursive('{directory}')"
    exec_repl(ser, command)

# ----------------------------------------------------------------------------
# Watchdog Directory Monitoring
# ----------------------------------------------------------------------------

class SyncHandler(FileSystemEventHandler):
    def __init__(self, ser):
        self.ser = ser
        self.file_snapshot = {}

    def update_snapshot(self):
        """
        Builds a snapshot of the current files and directories in tmp_dir.
        on_delete can't distinguish between files and directories when they are already deleted.
        """
        global tmp_dir
        self.file_snapshot = {}
        for root, dirs, files in os.walk(tmp_dir):
            for directory in dirs:
                rel_path = os.path.relpath(os.path.join(root, directory), tmp_dir)
                self.file_snapshot[rel_path] = 'directory'
            for file in files:
                rel_path = os.path.relpath(os.path.join(root, file), tmp_dir)
                self.file_snapshot[rel_path] = 'file'
        #print(f"Snapshot updated: {self.file_snapshot}")

    def snap_is_dir(self, relative_path):
        return self.file_snapshot.get(relative_path) == 'directory'

    def on_created(self, event):
        global tmp_dir
        if event.is_directory:
            directory = os.path.relpath(event.src_path, tmp_dir)
            print(f"Directory created: {directory}")
            create_dir(self.ser, directory)
        else:
            local_file = event.src_path
            file = os.path.relpath(event.src_path, tmp_dir)
            parent_dir = os.path.dirname(file)
            with open(local_file, 'r') as f:
                content = f.read()
            print(f"File created: {file}")
            
            write_file(self.ser, file, content)
        self.update_snapshot()

    def on_modified(self, event):
        global tmp_dir

        # Prevent conflicts with the on_deleted event.
        if not os.path.exists(event.src_path):
            return

        if not event.is_directory:
            local_file = event.src_path
            mcu_file = os.path.relpath(event.src_path, tmp_dir)
            with open(local_file, 'r') as f:
                content = f.read()
            print(f"File modified: {mcu_file}")
            write_file(self.ser, mcu_file, content)
        self.update_snapshot()

    def on_deleted(self, event):
        global tmp_dir
        path = os.path.relpath(event.src_path, tmp_dir)

        if self.snap_is_dir(path):
            print(f"Directory deleted: {path}")
            delete_dir(self.ser, path)
        else:
            print(f"File deleted: {path}")
            delete_file(self.ser, path)
        self.update_snapshot()

def start_monitoring(directory, ser):
    global observer
    event_handler = SyncHandler(ser)
    observer = Observer()
    observer.schedule(event_handler, path=directory, recursive=True)
    observer.start()

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        observer.stop()

    observer.join()

# ----------------------------------------------------------------------------
# File Synchronization Function
# ----------------------------------------------------------------------------

def sync_files(ser, temp_dir):
    """Synchronize files from MCU to local temp directory."""
    print("Beginning file sync...")
    file_list = list_files(ser, get_contents=True)

    # EXMAMPLE file_list: [{'path': 'index.html', 'type': 'file'}, {'path': 'src', 'type': 'directory'}, {'path': 'src/testfile.txt', 'type': 'file'}]

    for file in file_list:
        print(f"Syncing file: {file['path']}")
        if file['type'] == 'file':
            # Decode the expected bytes to string, normalize line endings by replacing CRLF with LF.
            content = file['contents'].decode('unicode_escape').replace('\r\n', '\n') if file['contents'] else ''
            with open(os.path.join(temp_dir, file['path']), 'w') as f:
                f.write(content)
        else:
            os.makedirs(os.path.join(temp_dir, file['path']), exist_ok=True)
    
    print("File sync complete.")


# ----------------------------------------------------------------------------
# File Explorer and File Editor Functions
# ----------------------------------------------------------------------------

def open_directory(path):
    system_name = platform.system() # type: ignore

    if system_name == "Windows":
        os.startfile(path)  # Windows
    elif system_name == "Darwin":
        subprocess.Popen(["open", path])  # macOS
    elif system_name == "Linux":
        subprocess.Popen(["xdg-open", path])  # Linux
    else:
        raise OSError(f"Unsupported operating system: {system_name}")
    
def open_editor(editor, path):
    system_name = platform.system() # type: ignore

    if system_name == "Windows":
        subprocess.Popen([editor, path], shell=True)  # Windows
    elif system_name == "Darwin":
        subprocess.Popen(["open", "-a", editor, path])  # macOS
    elif system_name == "Linux":
        subprocess.Popen([editor, path])  # Linux
    else:
        raise OSError(f"Unsupported operating system: {system_name}")

# ----------------------------------------------------------------------------
# Main Function
# ----------------------------------------------------------------------------

def main():
    global serial_port, tmp_dir, auto_explore_tmp, auto_edit_tmp, default_editor

    # Set up argument parser
    parser = argparse.ArgumentParser(description="Open a file in an editor via the specified serial port.")
    parser.add_argument("-s", "--serial-port", type=str, required=False, help="Specify the serial port (e.g., COM1, /dev/ttyACM0)")

    # Parse arguments
    args = parser.parse_args()

    # Access the serial port argument
    if args.serial_port: # type: ignore
        serial_port = args.serial_port # type: ignore
    print(f"Serial Port: {serial_port}")

    # Connect to the MCU via serial.
    ser = connect_to_mcu(serial_port)

    # Register the cleanup function to handle SIGINT (Ctrl+C) and SIGTERM
    signal.signal(signal.SIGINT, partial(cleanup, ser=ser)) # type: ignore
    signal.signal(signal.SIGTERM, partial(cleanup, ser=ser)) # type: ignore

    # Initialize the REPL environment on the MCU.
    initialize_repl_env(ser)

    # Create a temporary directory to store files
    try:
        tmp_dir = tempfile.mkdtemp()
    except Exception as e:
        print(f"Error creating temporary directory: {e}")
        exit(1)

    try:
        os.chdir(tmp_dir)
    except Exception as e:
        print(f"Error changing to temporary directory: {e}")
        exit(1)

    # Open the temporary directory in the file explorer
    if auto_explore_tmp:
        open_directory(tmp_dir)

    if auto_edit_tmp:
        open_editor(default_editor, tmp_dir)

    # Sync files from MCU to local directory
    sync_files(ser, tmp_dir)

    # Start monitoring the local directory for changes
    print("Starting file monitoring...")
    print(f"Temporary directory created: {tmp_dir}")
    start_monitoring(tmp_dir, ser)
    # Code after start_monitoring will not be executed until the observer is stopped during cleanup or from a keyboard interrupt.

if __name__ == "__main__":
    main()
