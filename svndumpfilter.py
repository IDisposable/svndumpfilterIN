#!/usr/bin/env python

from optparse import OptionParser
import os
import re
import subprocess
import sys
import time


"""
svndumpfilter output_file [subcommand] [<options>]

This implementation relies on svnlook to pull excluded files/directories that are eventually moved into included
directories.


Optimizations / Improvements

  1.  Drops empty revision record when all node records are excluded from revision.
       Example :  You have a revision record, but all the paths are for excluded directories.
       The result is that the node record will not show up in the final dump file.

  2.  Renumbers revisions based on revisions that were dropped.
       Example: There are 5 revisions and 3 revisions are empty because all their node records are for excluded paths.
       You will have an output dump file with 2 revisions, numbered Revision 1 and Revision 2.

  3.  Scan-only mode where a quick scan of the dump file is done to detect whether untangling repositories will be
      necessary.
       Example of untangling: You have a node record that has a copyfrom-path that refers to an excluded directory.
       You will need to untangle this by retrieving information about the file that you are copying from and add
       it to a prior node record.

  4.  Ability to start filtering at any revision.
       Example: You can start filtering at revision 100 if you have already loaded the first 100 previously from
       another dump file.

  5.  Automatically untangles revisions.
       Example: Whenever you reference an excluded path from an included node-path, you will automatically have the
       excluded data loaded in a prior record.

  6.  Path matching is done on more than just the top-level.
       Example: You can match to 'repo/dir1/dir2' which is more than the 'repo/dir1/' which is as deep as some filters
       can match to.

  7.  Added functionality to add dependent directories due to matching at more than the top-level.
       Example: If you match at more than a top-level, you will need to add dependents for paths that are more than 1
       level deep. For example, if you only include 'repo/dir1', you will need to have a node add 'repo' before the
       node record that adds 'repo/dir1'.

  8.  Paths to include/exclude can now be read from a file.
       Example: You can now add --file to specify a file to read matched paths from.

  9.  Property tags are added to differentiate dump filter generated items.
       Example: For the property header, a key, "K 23" as "svndumpfilter generated", is appended with a value, "V 4"
       as "True".


Before starting the filter, make sure that the user running it has sufficient permissions to perform svnlook on your
target directory.


Example Usage:

sudo python svndumpfilter.py input_name.dump include directory_name -r repo_path -d output_name.dump

Runs the svndumpfilter on 'input_name.dump' from 'repo_path' to carve out 'directory_name'
and save the result to 'output_name.dump'.

"""

"""The number of bytes taken by the entire self-generated property section."""
PROPERTY_BYTES = 48

DUMP_FORMAT_VERSION = 'SVN-fs-dump-format-version'
DUMP_UUID = 'UUID'
REV_NUM = 'Revision-number'
CONTENT_LEN = 'Content-length'
PROP_CONTENT_LEN = 'Prop-content-length'
TEXT_CONTENT_LEN = 'Text-content-length'
NODE_PATH = 'Node-path'
NODE_KIND = 'Node-kind'
NODE_ACTION = 'Node-action'
NODE_COPYFROM_PATH = 'Node-copyfrom-path'
NODE_COPYFROM_REV = 'Node-copyfrom-rev'
PROP_END = 'PROPS-END'


def write_empty_lines(d_file, number=1):
  """
  Writes a variable number of empty lines.
  """
  d_file.write("\n" * number)


class DumpHeader(object):
  """
  Encapsulates the logic for writing out the dump file header.
  """
  def __init__(self, version=None, UUID=None):
    self.version = version
    self.UUID = UUID

  def write_segment(self, d_file):
    """
    Writes out the dump version and repository UUID for the dump file.
    """
    d_file.write("%s: %s\n" % (DUMP_FORMAT_VERSION, self.version))
    write_empty_lines(d_file)
    d_file.write("%s: %s\n" % (DUMP_UUID, self.UUID))
    write_empty_lines(d_file)

  def extract_dump_header(self, d_file):
    """
    Extracts the dump version and repository UUID from the dump file.
    """
    self.version = self._find_version(d_file.readline())
    d_file.readline()
    self.UUID = self._find_UUID(d_file.readline())
    d_file.readline()

  def _find_version(self, line):
    """
    Provides regular expression matching to extract the dump version.
    """
    res = re.match(r"%s: (\d+)" % DUMP_FORMAT_VERSION, line)
    return int(res.group(1))

  def _find_UUID(self, line):
    """
    Provides regular expression matching to extract the UUID of the repository.
    """
    res = re.match(r"%s: ([\w-]+)" % DUMP_UUID, line)
    return res.group(1)


class Record(object):
  """
  Encapsulates the logic for node records and revision records.
  """
  def __init__(self):
    self.head = {}
    self.order_head = []  # This is dictionary of tuples to act as an OrderedDict
    self.order_prop = []
    self.body = None

  def _add_header(self, key, value):
    """
    Adds a header to the dictionary for querying and the ordered dictionary for writing.
    """
    self.head[key] = value
    self.order_head.append((key, value))

  def _add_property(self, key, value):
    """
    Adds a property to the dictionary for querying and the ordered dictionary for writing.
    """
    self.order_prop.append((key, value))

  def _write_end_prop(self, d_file):
    """
    Writes out the PROPS-END tag with proper spacing.
    Accounts for the different spacings created by a standard svn dump.
    """
    if PROP_CONTENT_LEN in self.head:
      if self.type == 'Node' and self.head[NODE_ACTION] == 'delete':
        write_empty_lines(d_file)
      else:
        d_file.write('%s\n' % PROP_END)
        if self.type == 'Node':
          if not self.body:
            write_empty_lines(d_file)
            if not self.order_prop:
              write_empty_lines(d_file)
        else:
          write_empty_lines(d_file)
    else:
      if not self.body:
        write_empty_lines(d_file)

  def _write_header(self, d_file):
    """
    Writes out the RFC822-style headers.
    """
    for kv in self.order_head:
      d_file.write('%s: %s\n' % (kv[0], kv[1]))
    write_empty_lines(d_file)

  def _write_properties(self, d_file):
    """
    Writes out the property section of the record.
    """
    for kv in self.order_prop:
      d_file.write('%s' % kv[0])
      d_file.write('%s' % kv[1])

  def _write_body(self, d_file):
    """
    Writes out the body of the record.
    """
    d_file.write(self.body)
    write_empty_lines(d_file, 2)

  def write_segment(self, d_file):
    """
    Writes out the entire record as a segment.
    """
    self._write_header(d_file)
    self._write_properties(d_file)
    self._write_end_prop(d_file)
    if self.body:
      self._write_body(d_file)

  def _swallow_empty_lines(self, d_file):
    """
    Removes whitespace lines until reaching a line without whitespace. Remains at the line
    without whitespace when finishing.
    """
    pos = d_file.tell()
    line = d_file.readline()
    while line == '\n':
      pos = d_file.tell()
      line = d_file.readline()
    d_file.seek(pos)
    return line != ''

  def _extract_header(self, d_file):
    """
    Extracts the header of a record from a dump file.
    """
    if not self._swallow_empty_lines(d_file):
      raise FinishedFiltering('There are no more records to process.')

    line = d_file.readline()
    while line != '\n':
      key, value = line.split(': ')
      clean_val = value.rstrip('\n')
      self._add_header(key, clean_val)
      line = d_file.readline()

    if REV_NUM in self.head:
      self.type = 'Revision'
    else:
      self.type = 'Node'

  def _extract_properties(self, d_file):
    """
    Extracts the properties of a record from a dump file
    """
    if PROP_CONTENT_LEN in self.head:
      prop_bytes = self.head[PROP_CONTENT_LEN]
      prop = d_file.read(int(prop_bytes))
      prop_list = prop.splitlines()
      try:
        if prop_list[-1] == '':
          prop_list = prop_list[:-1]
      except ValueError:
        pass
      if PROP_END in prop_list:
        prop_list.remove(PROP_END)
      symbol = None
      content = ''
      prog = re.compile('^[KV] [\d]+$')
      for line in prop_list:
        if not symbol:
          symbol = line + '\n'
        else:
          if prog.match(line):
            self._add_property(symbol, content)
            content = ''
            symbol = line + '\n'
          else:
            content = content + line + '\n'
      if symbol:  # The last "Value" and its content should be added.
        self._add_property(symbol, content)

  def _extract_body(self, d_file):
    """
     Extract the body of a record from a dump file.
    """
    if TEXT_CONTENT_LEN in self.head and self.head[TEXT_CONTENT_LEN] > 10:
      self.body = d_file.read(int(self.head[TEXT_CONTENT_LEN]))

  def extract_segment(self, d_file):
    """
    Extracts an entire record from a dump file.
    """
    self._extract_header(d_file)
    self._extract_properties(d_file)
    self._extract_body(d_file)

  def update_head(self, key, value):
    """
    Adds a new header line with the key and value arguments.
    Remove a pre-existing header if it shares the same key.
    """
    self.head[key] = value
    for prop in self.order_head:
      if prop[0] == key:
        self.order_head.remove((prop[0], prop[1]))
    self.order_head.insert(0, (key, value))

  def __repr__(self):
    original = super(Record, self).__repr__()
    if self.type:
      return self.type + original + str(self.head)
    else:
      return "<Null Type>" + original + str(self.head)


class MatchFiles(object):
  """
  Determines which files are included in the final output repository.
  """
  def __init__(self, include):
    self.include = include  # Whether these are matches to include or matches to exclude
    self.matches = {}

  def _extract_path(self, path):
    """
    Split the path into a list of elements
    """
    return path.split('/')

  def add_to_matches(self, path):
    """
    Adds each component of the path to a dictionary where each level of the dictionary represents how far
    into the path you are. The last level for each path added always ends with a {1:1} delimiter.

    Example:
    If you have paths for dir1/dir2/dir3, dir1, and dir4/, the structure of the dictionary
    will look like:

    { dir1 : { dir 2: { dir 3: { 1:1 } }, { 1:1 } }, dir 4 : { 1:1 } }
    """
    if path[-1] == '/':  # Takes care of the case when you have dir1/dir2/dir3/ as input
      path = path[:-1]
    path_comps = self._extract_path(path)
    curr = self.matches  # The level of the directory hierarchy you are on.
    for idx, comp in enumerate(path_comps):
      if comp in curr:
        curr = curr[comp]
      else:
        for elem in path_comps[idx:]:
          # Add the remaining elements because there are no more overlapping components
          curr[elem] = {}
          curr = curr[elem]
        curr[1] = 1
        return
    curr[1] = 1

  def read_matches_from_file(self, filename):
    """
    Reads each path to match from a file and populates a dictionary with this information.
    """
    with open(filename) as d_file:
      for line in d_file:
        if line == "\n":
          continue
        else:
          self.add_to_matches(line.rstrip('\n'))

  def is_included(self, path):
    """
    Checks to see if a path should be included in the output dump file.
    """
    result = False
    path_comps = self._extract_path(path)
    curr = self.matches
    for comp in path_comps:
      if comp not in curr or 1 in curr:
        break
      curr = curr[comp]
    if 1 in curr:
      result = True
    if self.include:
      return result
    else:
      return not result


def write_segments(d_file, segments):
  """
  Writes out the information for each record stored in contents.
  """
  for segment in segments:
    segment.write_segment(d_file)


class SVNLookError(Exception):
  """
  Raised when svnlook runs into an error.
  Common Cases:
  1) User running the filter does not have sufficient permissions to access the repository specified.
  2) Path does not exist or does not point to a repository.
  """
  pass


def run_svnlook_command(command, rev_num, repo_path, file_path, filtering, debug):
  """
  Runs svnlook to grab the contents of a repository or the contents of a file.
  """
  command_list = ['svnlook']
  if filtering:  # svn tree
    command_list.extend([filtering, '-r', rev_num, command, repo_path, file_path])
    if debug:
      print command_list
  else:  # svn cat
    command_list.extend(['-r', rev_num, command, repo_path, file_path])
    if debug:
      print command_list
  process = subprocess.Popen(command_list, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
  out, err = process.communicate()
  if err:
    raise SVNLookError(err)
  else:
    return out


def handle_missing_file(d_file, from_path, destination, rev_num, repo_path, debug):
  """
  If a file is missing from an excluded path and needs to be included in the final
  dump file, an add operation is appended to the dump file with the contents of that
  missing file.
  """
  file_body = run_svnlook_command("cat", rev_num, repo_path, from_path, None, debug)
  add_file_to_dump(d_file, destination, file_body)


def handle_missing_directory(d_file, from_path, destination, rev_num, repo_path, debug):
  """
  If a directory is missing from an excluded path and needs to be included in the final
  dump file, an add operation is appended to the dump directory with the contents of that
  missing directory.

  :param d_file: the file being written to
  :param from_path: where the directory originated from
  :param destination: where you the directory should end at
  :param rev_num: revision number from where the directory originated
  :param repo_path: repository path where the dump file was generated
  """
  output = run_svnlook_command('tree', rev_num, repo_path, from_path, '--full-paths', debug)
  output = output.splitlines()
  files = filter(lambda a: a != ' ' and a != '', output)
  for transfer_file in files:
    if transfer_file[-1] == '/':
      add_dir_to_dump(d_file, destination + '/' + transfer_file[len(from_path)+1:])
    elif transfer_file == from_path + '/':
      add_dir_to_dump(d_file, destination)
    else:
      file_from = from_path + '/' + transfer_file[len(from_path)+1:]
      file_dest = destination + '/' + transfer_file[len(from_path)+1:]
      handle_missing_file(d_file, file_from, file_dest, rev_num, repo_path, debug)


def create_node_record(file_path, kind, body=None):
  """
  Creates a node record for directories to add in excluded items. The node record will
  contain a header with a key of 'svndumpfilter generated' and a value of 'True'.
  """
  node_rec = Record()
  node_rec.type = 'Node'
  header = [(NODE_PATH, file_path), (NODE_ACTION, 'add'), (NODE_KIND, kind), (PROP_CONTENT_LEN, PROPERTY_BYTES)]
  if body:
    header.extend([(TEXT_CONTENT_LEN, len(body)), (CONTENT_LEN, PROPERTY_BYTES + len(body))])
    node_rec.body = body
  node_rec.order_head = header
  node_rec.head = dict(node_rec.order_head)
  # Number on KV header line displays length of KV content without newline character.
  node_rec.order_prop = [('K 23\n', 'svndumpfilter generated\n'), ('V 4\n', 'True\n')]
  return node_rec


def add_dir_to_dump(d_file, file_path):
  """
  Creates a node record that adds a directory to the output dump file.
  """
  node_rec = create_node_record(file_path, 'dir')
  node_rec.write_segment(d_file)


def add_file_to_dump(d_file, file_path, body):
  """
  Creates a node record that adds a file to the output dump file.
  """
  node_rec = create_node_record(file_path, 'file', body)
  node_rec.write_segment(d_file)


class Node(object):
  """
  Represents what components of the path were traversed to have this set of matches.
  """
  def __init__(self, path, matches):
    self.path = path
    self.matches = matches


def add_dependents(to_write, matches):
  """
  Adds dependent directories that are required to start at a non-top-level path for path matching.
  """
  to_process = [Node('', matches)]
  dir_to_add = []
  for node in to_process:
    for item in node.matches:
      if 1 not in node.matches[item]:
        to_process.append(Node(node.path + item + '/', node.matches[item]))
        dir_to_add.append(node.path + item + '/')
  for dir_path in dir_to_add:
    node_rec = create_node_record(dir_path[:-1], 'dir')
    to_write.append(node_rec)


def handle_deleting_file(d_file, file_path):
  """
  Appends a node record to delete a file.
  Not necessary in current implementation v1.0 of this filter.
  """
  node_rec = Record()
  node_rec.type = 'Node'
  node_rec.order_head = [(NODE_PATH, file_path), (NODE_ACTION, 'delete'), (NODE_KIND, 'file')]
  node_rec.head = dict(node_rec.order_head)
  node_rec.write_segment(d_file)


def handle_deleting_directory(d_file, file_path):
  """
  Appends a node record to delete a file.
  Not necessary in current implementation v1.0 of this filter.
  """
  node_rec = Record()
  node_rec.type = 'Node'
  node_rec.order_head = [(NODE_PATH, file_path), (NODE_ACTION, 'add'), (NODE_KIND, 'dir')]
  node_rec.head = dict(node_rec.order_head)
  node_rec.write_segment(d_file)


class FinishedFiltering(Exception):
  """
  Thrown when filtering has been completed.
  """
  pass


def clean_up(filename):
  """
  Remove the old dump file so a new one with the same filename can replace it.
  """
  try:
    os.remove(filename)
  except OSError:
    pass


def create_matcher(include, matches, opt):
  """
  Creates the path matcher with the paths provided by the command-line and optionally paths
  provided by a file.
  """
  matcher = MatchFiles(include)
  for match in matches:
    matcher.add_to_matches(match)
  if opt.file:
    matcher.read_matches_from_file(opt.file)
  return matcher


def write_dump_header(input_file, output_file, opt):
  """
  Write out the header for and check the version of the dump file.
  """
  dump = DumpHeader()
  dump.extract_dump_header(input_file)
  if dump.version != 2:
    if not opt.quiet:
      sys.stderr.write('Version Incompatible (Requires Version 2)')
    sys.exit(1)
  write_segments(output_file, [dump])


def print_scan_results(scan, safe):
  """
  Displays whether the svn dump file is tangled.
  """
  if scan:
    if safe:
      print 'Safe: No untangling is necessary to carve these paths.'
    else:
      print 'Unsafe: Untangling is necessary to carve these paths.'


def process_revision_record(rev_map, include, check, flags, opt):
  """
  Handles renumbering and starting at a specific revision for the revision record.
  Checks to see if dependent files need to be added.
  """
  rev_seg = flags['next_rev']
  if opt.renumber_revs:
    rev_seg.update_head(REV_NUM, str(flags['renum_rev']))
  if opt.start_revision and int(opt.start_revision) <= int(flags['orig_rev']):
    flags['can_write'] = True
  flags['to_write'].append(rev_seg)
  rev_map[str(flags['orig_rev'])] = str(flags['renum_rev'])
  if include and int(rev_seg.head[REV_NUM]) == 1:  # Revision 0 can't contain Node Records
    add_dependents(flags['to_write'], check.matches)
  return rev_seg


def handle_exclude_to_include(node_seg, output_file, flags, opt):
  """
  Write out current records in the queue.
  Process node segments that go from an excluded path to an included path.
  """
  if opt.scan:
    flags['safe'] = False
    raise FinishedFiltering('Tangling is necessary')
  if not flags['warning_given']:
    print 'Warning: svnlook is required to pull missing files'
    flags['warning_given'] = False
  write_segments(output_file, flags['to_write'])
  if opt.renumber_revs and not flags['did_increment']:
    flags['renum_rev'] += 1
    flags['did_increment'] = True
  flags['to_write'] = []  # Need to write items in queue because we know that this revision won't be empty
  if node_seg.head[NODE_KIND] == 'file':
    handle_missing_file(output_file, node_seg.head[NODE_COPYFROM_PATH], node_seg.head[NODE_PATH],
                node_seg.head[NODE_COPYFROM_REV], opt.repo, opt.debug)
  else:
     handle_missing_directory(output_file, node_seg.head[NODE_COPYFROM_PATH], node_seg.head[NODE_PATH],
               node_seg.head[NODE_COPYFROM_REV], opt.repo, opt.debug)


def handle_include_to_exclude(output_file, flags, opt):
  """
  Write out the current records in the queue because we know that this revision won't be empty.
  Process node segments that go from an included path to an excluded path.
  """
  write_segments(output_file, flags['to_write'])
  if opt.renumber_revs and not flags['did_increment']:
    flags['renum_rev'] += 1
    flags['did_increment'] = True
  flags['to_write'] = []


def write_included(rev_map, node_seg, flags, opt):
  """
  Optionally map the current revision to a renumbered revision for the node record. Include the record to be written.
  """
  if opt.renumber_revs:
    if NODE_COPYFROM_REV in node_seg.head:
      orig_copy_rev = node_seg.head[NODE_COPYFROM_REV]
      new_copy_rev = rev_map[orig_copy_rev]
      next = str(int(orig_copy_rev)+1)
      if int(new_copy_rev) == int(flags['renum_rev']) or (next in rev_map and int(new_copy_rev) == int(rev_map[next])):
        new_copy_rev = str(int(new_copy_rev)-1)
      node_seg.update_head(NODE_COPYFROM_REV, new_copy_rev)
  flags['to_write'].append(node_seg)


def parse_dump(input_dump, output_dump, matches, include, opt):
  """
  Handles the logic for parsing the input dumpfile and querying the repository
  to retrieve missing information.

  Revision map is present to map your renumbered revision to the actual revision.
  This is to adjust the 'Node-copyfrom-rev' when you renumber your revisions.
  """

  flags = {
    'can_write': opt.start_revision == None,  # Set to True when your revision number is > start_revision.
    'safe': True,  # False if untangling is necessary ; determines whether svnlook is required
    'warning_given': False,  # Whether a warning has been given for untangling
    'orig_rev': 0,  # Original input dump file's revision number
    'renum_rev': 0,  # Renumbered revision number for output dump file
    'next_rev': None,  # Stores an extracted revision record
    'did_increment': None,  # Prevents multiple increments for 1 revision
    'to_write': [],  # List of items to write
  }

  print "Starting to filter dumpfile : %s " % input_dump
  rev_map = {}  # Stores the mappings for revisions when renumbering { 'original revision': 'renumbered revision' }
  check = create_matcher(include, matches, opt)
  clean_up(output_dump)

  with open(input_dump) as input_file:
    with open(output_dump, 'a+') as output_file:
      write_dump_header(input_file, output_file, opt)
      try:
        while 1:
          if not opt.quiet:
            print '---- Working on Input Revision %s (Renumber Rev: %s) ----' % (flags['orig_rev'], flags['renum_rev'])
          flags['to_write'] = []
          if not flags['next_rev']:  # This is the first revision (rev 0).
            rev_seg = Record()
            rev_seg.extract_segment(input_file)
            flags['to_write'].append(rev_seg)
          else:
            rev_seg = process_revision_record(rev_map, include, check, flags, opt)
          while 1:
            flags['did_increment'] = False  # Want to only increment once for each revision
            node_seg = Record()
            node_seg.extract_segment(input_file)
            if node_seg.type == 'Revision':
              flags['next_rev'] = node_seg
              break  # Finished processing node records and should now look at revision records.
            else:
              if flags['can_write']:
                if (NODE_COPYFROM_PATH in node_seg.head and not check.is_included(node_seg.head[NODE_COPYFROM_PATH])
                    and check.is_included(node_seg.head[NODE_PATH])):
                  # Moving file from excluded to included
                  handle_exclude_to_include(node_seg, output_file, flags, opt)
                elif (NODE_COPYFROM_PATH in node_seg.head and
                      check.is_included(node_seg.head[NODE_COPYFROM_PATH]) and
                      not check.is_included(node_seg.head[NODE_PATH])):
                  # Moving a file from included to excluded should be a delete
                  handle_include_to_exclude(output_file, flags, opt)
                else:
                  if check.is_included(node_seg.head[NODE_PATH]):  # Don't write if not in included path.
                    write_included(rev_map, node_seg, flags, opt)
          if not opt.drop_empty or len(flags['to_write']) > 1:
            if flags['can_write']:
              write_segments(output_file, flags['to_write'])
            if opt.renumber_revs and not flags['did_increment']:
              flags['renum_rev'] += 1
          if (opt.drop_empty or not flags['can_write']) and rev_seg and int(rev_seg.head[REV_NUM]) == 0:
            # Revision 0 can't have any associated node records.
            write_segments(output_file, flags['to_write'])
            flags['renum_rev'] += 1
          flags['orig_rev'] += 1
      except FinishedFiltering:
        if not opt.scan:
          write_segments(output_file, flags['to_write'])
          print 'Filtering Complete : from %s to %s' % (input_dump, output_dump)
      print_scan_results(opt.scan, flags['safe'])


def main():

  parser = OptionParser(
          usage='%prog [OPTIONS] <input_dump> <SUBCOMMAND> [args]',
          version='%prog 1.1')

  parser.add_option('-k', '--keep-empty-revs', dest='drop_empty', action='store_false', default=True,
                  help='If filtering causes any revision to be empty (i.e. has no node records in that revision), \
                  still keep the revision in the final dump file.')

  parser.add_option('-s', '--stop-renumber-revs', dest='renumber_revs', action='store_false', default=True,
                  help="Don't revisions that remain after filtering.")

  parser.add_option('-q', '--quiet', dest='quiet', action='store_true', default=False,
                  help='Does not display filtering statistics.')

  parser.add_option('-n', '--revisions', dest='start_revision',
                  help='Starts filtering at a specified revision and ends at the last revision in the input dump file.')

  parser.add_option('-c', '--scan-only', dest='scan', action='store_true', default=False,
                  help='Scans the dumpfile to see if untangling is necessary.')

  parser.add_option('-f', '--paths-file', dest='file',
                  help='Specifies the file to read matched paths from.')

  parser.add_option('-r', '--repo', dest='repo',
                  help='Specify a repository. This is mandatory when not scanning.')

  parser.add_option('-d', '--debug', dest='debug', action='store_true', default=False,
                  help='Turns on debug statements.')

  parser.add_option('-o', '--output-dump', dest='output_dump',
                  help='Specify an output dump file. This is mandatory when not scanning')

  (opt, args) = parser.parse_args()

  if not opt.file:
    if len(args) < 3:
      parser.error('You must specify a input_dump, a sub-command, and arguments.')
  else:
    if len(args) < 2:
      parser.error('When specifying a file, you must provide an input_dump and a sub-command')

  if not opt.scan:
    if not opt.repo:
        parser.error("When not scanning, you must specify a path to the dump file's repository.")
    elif not opt.output_dump:
        parser.error('When not scanning, you must specify a path to the output dump file.')

  input_dump = args[0]
  subcommand = args[1]

  if subcommand == 'include':
    include = True
  elif subcommand == 'exclude':
    include = False
  else:
    parser.error("Unrecognized subcommand : Must use either 'include' or 'exclude'")

  matches = args[2:]

  parse_dump(input_dump, opt.output_dump, matches, include, opt)


if __name__ == '__main__':
  start_time = time.time()
  main()
  # Note that this calculates "real time".
  print 'Time Running : %s seconds' % (time.time() - start_time)
