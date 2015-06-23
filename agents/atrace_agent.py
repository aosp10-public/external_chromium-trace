# Copyright (c) 2015 The Chromium Authors. All rights reserved.
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.

import Queue
import re
import subprocess
import sys
import threading
import time
import zlib

import systrace_agent
import util

# Text that ADB sends, but does not need to be displayed to the user.
ADB_IGNORE_REGEXP = r'^capturing trace\.\.\. done|^capturing trace\.\.\.'
# The number of seconds to wait on output from ADB.
ADB_STDOUT_READ_TIMEOUT = 0.2
# The adb shell command to initiate a trace.
ATRACE_BASE_ARGS = ['atrace']
# If a custom list of categories is not specified, traces will include
# these categories (if available on the device).
DEFAULT_CATEGORIES = 'sched gfx view dalvik webview input disk am wm'.split()
# The command to list trace categories.
LIST_CATEGORIES_ARGS = ATRACE_BASE_ARGS + ['--list_categories']
# Minimum number of seconds between displaying status updates.
MIN_TIME_BETWEEN_STATUS_UPDATES = 0.2
# ADB sends this text to indicate the beginning of the trace data.
TRACE_START_REGEXP = r'TRACE\:'
# Plain-text trace data should always start with this string.
TRACE_TEXT_HEADER = '# tracer'


def try_create_agent(options, categories):
  return AtraceAgent(options, categories)


class AtraceAgent(systrace_agent.SystraceAgent):
  def __init__(self, options, categories):
    super(AtraceAgent, self).__init__(options, categories)
    self._expect_trace = False
    self._adb = None
    self._trace_data = None

  def start(self):
    tracer_args = self._construct_trace_command()
    try:
      self._adb = subprocess.Popen(tracer_args, stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE)
    except OSError as error:
      print >> sys.stderr, (
          'The command "%s" failed with the following error:' %
          ' '.join(tracer_args))
      print >> sys.stderr, '    ', error
      sys.exit(1)

  def collect_result(self):
    trace_data = self._collect_trace_data();
    if self._expect_trace:
      self._trace_data = self._preprocess_trace_data(trace_data);

  def expect_trace(self):
    return self._expect_trace

  def get_trace_data(self):
    return self._trace_data

  def get_class_name(self):
    return 'trace-data'

  def _construct_trace_command(self):
    """Builds a command-line used to invoke a trace process.

    Returns:
      A tuple where the first element is an array of command-line arguments, and
      the second element is a boolean which will be true if the commend will
      stream trace data.
    """
    if self._options.list_categories:
      tracer_args = util.construct_adb_shell_command(
          LIST_CATEGORIES_ARGS, self._options.device_serial)
      self._expect_trace = False
    elif self._options.from_file is not None:
      tracer_args = ['cat', self._options.from_file]
      self._expect_trace = True
    else:
      atrace_args = ATRACE_BASE_ARGS
      self._expect_trace = True
      if self._options.compress_trace_data:
        atrace_args.extend(['-z'])

      if ((self._options.trace_time is not None)
          and (self._options.trace_time > 0)):
        atrace_args.extend(['-t', str(self._options.trace_time)])

      if ((self._options.trace_buf_size is not None)
          and (self._options.trace_buf_size > 0)):
        atrace_args.extend(['-b', str(self._options.trace_buf_size)])

      if self._options.app_name is not None:
        atrace_args.extend(['-a', self._options.app_name])

      if self._options.kfuncs is not None:
        atrace_args.extend(['-k', self._options.kfuncs])

      if not self._categories:
        self._categories = get_default_categories(self._options.device_serial)
      atrace_args.extend(self._categories)

      if self._options.fix_threads:
        atrace_args.extend([';', 'ps', '-t'])
      tracer_args = util.construct_adb_shell_command(
          atrace_args, self._options.device_serial)

    return tracer_args

  def _collect_trace_data(self):
    # Read the output from ADB in a worker thread.  This allows us to monitor
    # the progress of ADB and bail if ADB becomes unresponsive for any reason.

    # Limit the stdout_queue to 128 entries because we will initially be reading
    # one byte at a time.  When the queue fills up, the reader thread will
    # block until there is room in the queue.  Once we start downloading the
    # trace data, we will switch to reading data in larger chunks, and 128
    # entries should be plenty for that purpose.
    stdout_queue = Queue.Queue(maxsize=128)
    stderr_queue = Queue.Queue()

    if self._expect_trace:
      # Use stdout.write() (here and for the rest of this function) instead
      # of print() to avoid extra newlines.
      sys.stdout.write('Capturing trace...')

    # Use a chunk_size of 1 for stdout so we can display the output to
    # the user without waiting for a full line to be sent.
    stdout_thread = FileReaderThread(self._adb.stdout, stdout_queue,
                                     text_file=False, chunk_size=1)
    stderr_thread = FileReaderThread(self._adb.stderr, stderr_queue,
                                     text_file=True)
    stdout_thread.start()
    stderr_thread.start()

    # Holds the trace data returned by ADB.
    trace_data = []
    # Keep track of the current line so we can find the TRACE_START_REGEXP.
    current_line = ''
    # Set to True once we've received the TRACE_START_REGEXP.
    reading_trace_data = False

    last_status_update_time = time.time()

    while (stdout_thread.isAlive() or stderr_thread.isAlive() or
           not stdout_queue.empty() or not stderr_queue.empty()):
      if self._expect_trace:
        last_status_update_time = status_update(last_status_update_time)

      while not stderr_queue.empty():
        # Pass along errors from adb.
        line = stderr_queue.get()
        sys.stderr.write(line)

      # Read stdout from adb.  The loop exits if we don't get any data for
      # ADB_STDOUT_READ_TIMEOUT seconds.
      while True:
        try:
          chunk = stdout_queue.get(True, ADB_STDOUT_READ_TIMEOUT)
        except Queue.Empty:
          # Didn't get any data, so exit the loop to check that ADB is still
          # alive and print anything sent to stderr.
          break

        if reading_trace_data:
          # Save, but don't print, the trace data.
          trace_data.append(chunk)
        else:
          if not self._expect_trace:
            sys.stdout.write(chunk)
          else:
            # Buffer the output from ADB so we can remove some strings that
            # don't need to be shown to the user.
            current_line += chunk
            if re.match(TRACE_START_REGEXP, current_line):
              # We are done capturing the trace.
              sys.stdout.write('Done.\n')
              # Now we start downloading the trace data.
              sys.stdout.write('Downloading trace...')
              current_line = ''
              # Use a larger chunk size for efficiency since we no longer
              # need to worry about parsing the stream.
              stdout_thread.set_chunk_size(4096)
              reading_trace_data = True
            elif chunk == '\n' or chunk == '\r':
              # Remove ADB output that we don't care about.
              current_line = re.sub(ADB_IGNORE_REGEXP, '', current_line)
              if len(current_line) > 1:
                # ADB printed something that we didn't understand, so show it
                # it to the user (might be helpful for debugging).
                sys.stdout.write(current_line)
              # Reset our current line.
              current_line = ''

    if self._expect_trace:
      if reading_trace_data:
        # Indicate to the user that the data download is complete.
        sys.stdout.write('Done.\n')
      else:
        # We didn't receive the trace start tag, so something went wrong.
        sys.stdout.write('ERROR.\n')
        # Show any buffered ADB output to the user.
        current_line = re.sub(ADB_IGNORE_REGEXP, '', current_line)
        if current_line:
          sys.stdout.write(current_line)
          sys.stdout.write('\n')

    # The threads should already have stopped, so this is just for cleanup.
    stdout_thread.join()
    stderr_thread.join()

    self._adb.stdout.close()
    self._adb.stderr.close()

    # The adb process should be done since it's io pipes are closed.  Call
    # poll() to set the returncode.
    self._adb.poll()

    if self._adb.returncode != 0:
      print >> sys.stderr, ('The command "%s" returned error code %d.' %
                            (' '.join(tracer_args), self._adb.returncode))
      sys.exit(1)

    return trace_data

  def _preprocess_trace_data(self, trace_data):
    """Performs various processing on atrace data.

    Args:
      trace_data: The raw trace data.
    Returns:
      The processed trace data.
    """
    trace_data = ''.join(trace_data)

    if self._options.fix_threads:
      # Extract the thread list dumped by ps.
      trace_data, thread_names = extract_thread_list(trace_data)

    if trace_data:
      trace_data = strip_and_decompress_trace(trace_data)

    if not trace_data:
      print >> sys.stderr, ('No data was captured.  Output file was not '
                            'written.')
      sys.exit(1)

    if self._options.fix_threads:
      trace_data = fix_thread_names(trace_data, thread_names)

    if self._options.fix_circular:
      trace_data = fix_circular_traces(trace_data)

    return trace_data


class FileReaderThread(threading.Thread):
  """Reads data from a file/pipe on a worker thread.

  Use the standard threading. Thread object API to start and interact with the
  thread (start(), join(), etc.).
  """

  def __init__(self, file_object, output_queue, text_file, chunk_size=-1):
    """Initializes a FileReaderThread.

    Args:
      file_object: The file or pipe to read from.
      output_queue: A Queue.Queue object that will receive the data
      text_file: If True, the file will be read one line at a time, and
          chunk_size will be ignored.  If False, line breaks are ignored and
          chunk_size must be set to a positive integer.
      chunk_size: When processing a non-text file (text_file = False),
          chunk_size is the amount of data to copy into the queue with each
          read operation.  For text files, this parameter is ignored.
    """
    threading.Thread.__init__(self)
    self._file_object = file_object
    self._output_queue = output_queue
    self._text_file = text_file
    self._chunk_size = chunk_size
    assert text_file or chunk_size > 0

  def run(self):
    """Overrides Thread's run() function.

    Returns when an EOF is encountered.
    """
    if self._text_file:
      # Read a text file one line at a time.
      for line in self._file_object:
        self._output_queue.put(line)
    else:
      # Read binary or text data until we get to EOF.
      while True:
        chunk = self._file_object.read(self._chunk_size)
        if not chunk:
          break
        self._output_queue.put(chunk)

  def set_chunk_size(self, chunk_size):
    """Change the read chunk size.

    This function can only be called if the FileReaderThread object was
    created with an initial chunk_size > 0.
    Args:
      chunk_size: the new chunk size for this file.  Must be > 0.
    """
    # The chunk size can be changed asynchronously while a file is being read
    # in a worker thread.  However, type of file can not be changed after the
    # the FileReaderThread has been created.  These asserts verify that we are
    # only changing the chunk size, and not the type of file.
    assert not self._text_file
    assert chunk_size > 0
    self._chunk_size = chunk_size


def get_default_categories(device_serial):
  categories_output, return_code = util.run_adb_shell(LIST_CATEGORIES_ARGS,
                                                    device_serial)

  if return_code == 0 and categories_output:
    categories = [c.split('-')[0].strip()
                  for c in categories_output.splitlines()]
    return [c for c in categories if c in DEFAULT_CATEGORIES]

  return []


def status_update(last_update_time):
  current_time = time.time()
  if (current_time - last_update_time) >= MIN_TIME_BETWEEN_STATUS_UPDATES:
    # Gathering a trace may take a while.  Keep printing something so users
    # don't think the script has hung.
    sys.stdout.write('.')
    sys.stdout.flush()
    return current_time

  return last_update_time


def extract_thread_list(trace_data):
  """Removes the thread list from the given trace data.

  Args:
    trace_data: The raw trace data (before decompression).
  Returns:
    A tuple containing the trace data and a map of thread ids to thread names.
  """
  threads = {}
  parts = re.split('USER +PID +PPID +VSIZE +RSS +WCHAN +PC +NAME',
                   trace_data, 1)
  if len(parts) == 2:
    trace_data = parts[0]
    for line in parts[1].splitlines():
      cols = line.split(None, 8)
      if len(cols) == 9:
        tid = int(cols[1])
        name = cols[8]
        threads[tid] = name

  return (trace_data, threads)


def strip_and_decompress_trace(trace_data):
  """Fixes new-lines and decompresses trace data.

  Args:
    trace_data: The trace data returned by atrace.
  Returns:
    The decompressed trace data.
  """
  # Collapse CRLFs that are added by adb shell.
  if trace_data.startswith('\r\n'):
    trace_data = trace_data.replace('\r\n', '\n')
  elif trace_data.startswith('\r\r\n'):
    # On windows, adb adds an extra '\r' character for each line.
    trace_data = trace_data.replace('\r\r\n', '\n')

  # Skip the initial newline.
  trace_data = trace_data[1:]

  if not trace_data.startswith(TRACE_TEXT_HEADER):
    # No header found, so assume the data is compressed.
    trace_data = zlib.decompress(trace_data)

  # Enforce Unix line-endings.
  trace_data = trace_data.replace('\r', '')

  # Skip any initial newlines.
  while trace_data and trace_data[0] == '\n':
    trace_data = trace_data[1:]

  return trace_data


def fix_thread_names(trace_data, thread_names):
  """Replaces thread ids with their names.

  Args:
    trace_data: The atrace data.
    thread_names: A mapping of thread ids to thread names.
  Returns:
    The updated trace data.
  """
  def repl(m):
    tid = int(m.group(2))
    if tid > 0:
      name = thread_names.get(tid)
      if name is None:
        name = m.group(1)
        if name == '<...>':
          name = '<' + str(tid) + '>'
        thread_names[tid] = name
      return name + '-' + m.group(2)
    else:
      return m.group(0)
  trace_data = re.sub(r'^\s*(\S+)-(\d+)', repl, trace_data,
                      flags=re.MULTILINE)
  return trace_data


def fix_circular_traces(out):
  """Fix inconsistentcies in traces due to circular buffering.

  The circular buffers are kept per CPU, so it is not guaranteed that the
  beginning of a slice is overwritten before the end. To work around this, we
  throw away the prefix of the trace where not all CPUs have events yet.

  Args:
    out: The data to fix.
  Returns:
    The updated trace data.
  """
  # If any of the CPU's buffers have filled up and
  # older events have been dropped, the kernel
  # emits markers of the form '##### CPU 2 buffer started ####' on
  # the line before the first event in the trace on that CPU.
  #
  # No such headers are emitted if there were no overflows or the trace
  # was captured with non-circular buffers.
  buffer_start_re = re.compile(r'^#+ CPU \d+ buffer started', re.MULTILINE)

  start_of_full_trace = 0

  while True:
    result = buffer_start_re.search(out, start_of_full_trace + 1)
    if result:
      start_of_full_trace = result.start()
    else:
      break

  if start_of_full_trace > 0:
    # Need to keep the header intact to make the importer happy.
    end_of_header = re.search(r'^[^#]', out, re.MULTILINE).start()
    out = out[:end_of_header] + out[start_of_full_trace:]
  return out