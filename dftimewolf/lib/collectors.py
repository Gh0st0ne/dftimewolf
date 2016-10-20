#!/usr/bin/env python
"""Timewolf artifact collectors.

Timewolf artifact collectors are responsible for collecting artifacts.
"""

import datetime
import os
import re
import tempfile
import threading
import time
import zipfile

from grr.gui.api_client import api as grr_api
from grr.gui.api_client import errors as grr_errors
from grr.gui.api_client import utils as grr_utils
from dftimewolf.lib import utils as timewolf_utils
from grr.proto import flows_pb2


class BaseArtifactCollector(threading.Thread):
  """Base class for artifact collectors."""

  def __init__(self, verbose):
    """Initialize the base artifact collector object."""
    super(BaseArtifactCollector, self).__init__()
    self.console_out = timewolf_utils.TimewolfConsoleOutput(
        sender=self.__class__.__name__, verbose=verbose)

  def run(self):
    self.Collect()

  def Collect(self):
    """Collect artifacts."""
    raise NotImplementedError

  @property
  def collection_name(self):
    """Name for the collection of artifacts."""
    raise NotImplementedError


class FilesystemCollector(BaseArtifactCollector):
  """Collect artifacts from the local filesystem."""

  def __init__(self, path, name=None, verbose=False):
    super(FilesystemCollector, self).__init__(verbose=verbose)
    self.output_path = path
    self.cname = name

  def Collect(self):
    """Collect the artifacts."""
    self.console_out.VerboseOut(u'Artifact path: {0:s}'.format(
        self.output_path))
    return self.output_path

  @property
  def collection_name(self):
    """Name for the collection of collected artifacts."""
    if not self.cname:
      self.cname = os.path.basename(self.output_path.rstrip(u'/'))
    self.console_out.VerboseOut(u'Artifact collection name: {0:s}'.format(
        self.cname))
    return self.cname


class GrrHuntCollector(BaseArtifactCollector):
  """Collect hunt results with GRR."""
  CHECK_APPROVAL_INTERVAL_SEC = 10

  def __init__(self,
               hunt_id,
               reason,
               grr_server_url,
               username,
               password,
               approvers=None,
               verbose=False):
    """Initialize the GRR hunt result collector object."""
    super(GrrHuntCollector, self).__init__(verbose=verbose)
    self.output_path = tempfile.mkdtemp()
    self.grr_api = grr_api.InitHttp(
        api_endpoint=grr_server_url, auth=(username, password))
    self.approvers = approvers
    self.reason = reason
    self.hunt_id = hunt_id
    self.hunt = self.grr_api.Hunt(hunt_id).Get()

  def Collect(self):
    """Download current set of files in results."""
    if not os.path.isdir(self.output_path):
      os.makedirs(self.output_path)

    output_file_path = os.path.join(self.output_path, u'.'.join(
        (self.hunt_id, u'zip')))

    if os.path.exists(output_file_path):
      print u'{0:s} already exists: Skipping'.format(output_file_path)
      return None

    try:
      self.hunt.GetFilesArchive().WriteToFile(output_file_path)
    except grr_errors.AccessForbiddenError:
      self.console_out.VerboseOut(u'No valid hunt approval found')
      if not self.approvers:
        raise ValueError(u'GRR hunt needs approval but no approvers specified '
                         u'(hint: use --approvers)')
      self.console_out.VerboseOut(
          u'Hunt approval request sent to: {0:s} (reason: {1:s})'.format(
              self.approvers, self.reason))
      self.console_out.VerboseOut(
          u'Waiting for approval (this can take a while..)')
      # Send a request for approval and wait until there is a valid one
      # available in GRR.
      self.hunt.CreateApproval(
          reason=self.reason, notified_users=self.approvers)
      while True:
        try:
          self.hunt.GetFilesArchive().WriteToFile(output_file_path)
          break
        except grr_errors.AccessForbiddenError:
          time.sleep(self.CHECK_APPROVAL_INTERVAL_SEC)

    # Extract items from archive by host for processing
    collection_paths = {}
    with zipfile.ZipFile(output_file_path) as archive:
      items = archive.infolist()
      base = items[0].filename.split(u'/')[0]
      for f in items:
        client_id = f.filename.split(u'/')[1]
        if client_id.startswith(u'C.'):
          client_name = self.grr_api.Client(client_id).Get().data.os_info.fqdn
          client_dir = os.path.join(self.output_path, client_id)
          if not os.path.isdir(client_dir):
            os.makedirs(client_dir)
            collection_paths.update({client_dir: client_name})
          location = os.path.basename(archive.read(f))
          try:
            archive.extract(u'{0:s}/hashes/{1:s}'.format(base, location),
                            client_dir)
          except KeyError, e:
            self.console_out.VerboseOut(u'Extraction error: {0:s}'.format(e))

    os.remove(output_file_path)

    return collection_paths

  @property
  def collection_name(self):
    """Name for the collection of collected artifacts."""
    collection_name = u'{0:s}: {1:s}'.format(
        self.hunt_id, self.hunt.data.hunt_runner_args.description)
    self.console_out.VerboseOut(u'Artifact collection name: {0:s}'.format(
        collection_name))
    return collection_name


class GrrArtifactCollector(BaseArtifactCollector):
  """Collect artifacts with GRR."""
  CHECK_APPROVAL_INTERVAL_SEC = 10
  CHECK_FLOW_INTERVAL_SEC = 10
  DEFAULT_ARTIFACTS_LINUX = [
      u'LinuxAuditLogs', u'LinuxAuthLogs', u'LinuxCronLogs', u'LinuxWtmp',
      u'AllUsersShellHistory', u'ZeitgeistDatabase'
  ]

  DEFAULT_ARTIFACTS_DARWIN = [
      u'OSXAppleSystemLogs', u'OSXAuditLogs', u'OSXBashHistory',
      u'OSXInstallationHistory', u'OSXInstallationLog', u'OSXInstallationTime',
      u'OSXLaunchAgents', u'OSXLaunchDaemons', u'OSXMiscLogs',
      u'OSXRecentItems', u'OSXSystemLogs', u'OSXUserApplicationLogs',
      u'OSXQuarantineEvents'
  ]

  DEFAULT_ARTIFACTS_WINDOWS = [
      u'AppCompatCache', u'EventLogs', u'TerminalServicesEventLogEvtx',
      u'PrefetchFiles', u'SuperFetchFiles', u'WindowsSearchDatabase',
      u'ScheduledTasks', u'WindowsSystemRegistryFiles',
      u'WindowsUserRegistryFiles'
  ]

  def __init__(self,
               host,
               reason,
               grr_server_url,
               username,
               password,
               artifacts=None,
               use_tsk=False,
               approvers=None,
               verbose=False):
    """Initialize the GRR artifact collector object."""
    super(GrrArtifactCollector, self).__init__(verbose=verbose)
    self.output_path = tempfile.mkdtemp()
    self.grr_api = grr_api.InitHttp(
        api_endpoint=grr_server_url, auth=(username, password))
    self.artifacts = artifacts
    self.use_tsk = use_tsk
    self.reason = reason
    self.approvers = approvers
    self.client_id = self._GetClientId(host)
    self.client = None

  def _GetClientId(self, host):
    """Search GRR by hostname provided and get the latest active client."""
    client_id_pattern = re.compile(r'^c\.[0-9a-f]{16}$', re.IGNORECASE)
    if client_id_pattern.match(host):
      return host

    # Search for the host in GRR
    self.console_out.VerboseOut(u'Search for client: {0:s}'.format(host))
    search_result = self.grr_api.SearchClients(host)

    result = {}
    for client in search_result:
      client_id = client.client_id
      client_fqdn = client.data.os_info.fqdn
      client_last_seen_at = client.data.last_seen_at
      if host.lower() in client_fqdn.lower():
        result[client_id] = client_last_seen_at

    if not result:
      raise RuntimeError(u'Could not get client_id for {0:s}'.format(host))

    active_client_id = sorted(result, key=result.get, reverse=True)[0]
    last_seen_timestamp = result[active_client_id]
    # Remove microseconds and create datetime object
    last_seen_datetime = datetime.datetime.utcfromtimestamp(
        last_seen_timestamp / 1000000)
    # Timedelta between now and when the client was last seen, in minutes.
    # First, count total seconds. This will return a float.
    last_seen_seconds = (
        datetime.datetime.utcnow() - last_seen_datetime).total_seconds()
    last_seen_minutes = int(round(last_seen_seconds)) / 60

    self.console_out.VerboseOut(u'Found active client: {0:s}'.format(
        active_client_id))
    self.console_out.VerboseOut(
        u'Client last seen: {0:s} ({1:d} minutes ago)'.format(
            last_seen_datetime.strftime(u'%Y-%m-%dT%H:%M:%S+0000'),
            last_seen_minutes))

    return active_client_id

  def _GetClient(self, client_id, reason, approvers):
    """Get GRR client dictionary and make sure valid approvals exist."""
    client = self.grr_api.Client(client_id)
    self.console_out.VerboseOut(u'Checking for client approval')
    try:
      client.ListFlows()
    except grr_errors.AccessForbiddenError:
      self.console_out.VerboseOut(u'No valid client approval found')
      if not approvers:
        raise ValueError(
            u'GRR client needs approval but no approvers specified '
            u'(hint: use --approvers)')
      self.console_out.VerboseOut(
          u'Client approval request sent to: {0:s} (reason: {1:s})'.format(
              approvers, reason))
      self.console_out.VerboseOut(
          u'Waiting for approval (this can take a while..)')
      # Send a request for approval and wait until there is a valid one
      # available in GRR.
      client.CreateApproval(reason=reason, notified_users=approvers)
      while True:
        try:
          client.ListFlows()
          break
        except grr_errors.AccessForbiddenError:
          time.sleep(self.CHECK_APPROVAL_INTERVAL_SEC)

    self.console_out.VerboseOut(u'Client approval is valid')
    return client.Get()

  def Collect(self):
    """Collect the artifacts."""
    # Create a list of artifacts to collect.
    artifact_registry = {
        u'Linux': self.DEFAULT_ARTIFACTS_LINUX,
        u'Darwin': self.DEFAULT_ARTIFACTS_DARWIN,
        u'Windows': self.DEFAULT_ARTIFACTS_WINDOWS
    }
    self.client = self._GetClient(self.client_id, self.reason, self.approvers)
    system_type = self.client.data.os_info.system
    self.console_out.VerboseOut(u'System type: {0:s}'.format(system_type))
    # If the list is supplied by the user via a flag, honor that.
    if self.artifacts:
      artifact_list = self.artifacts.split(u',')
    else:
      artifact_list = artifact_registry.get(system_type, None)
    if not artifact_list:
      raise RuntimeError(u'No artifacts to collect')

    # Create Artifact collector flow args
    # TODO(berggren): Add flag to use TSK in some cases.
    name = u'ArtifactCollectorFlow'
    args = flows_pb2.ArtifactCollectorFlowArgs(
        artifact_list=artifact_list,
        use_tsk=self.use_tsk,
        ignore_interpolation_errors=True,
        apply_parsers=False,)

    self.console_out.VerboseOut(u'Artifacts to collect: {0:s}'.format(
        artifact_list))

    # Start the flow and get the flow ID
    flow = self.client.CreateFlow(name=name, args=args)
    flow_id = grr_utils.UrnToFlowId(flow.data.urn)
    self.console_out.VerboseOut(u'Flow {0:s}: Scheduled'.format(flow_id))

    # Wait for the flow to finish
    self.console_out.VerboseOut(u'Flow {0:s}: Waiting to finish'.format(
        flow_id))
    while True:
      status = self.client.Flow(flow_id).Get().data
      state = status.state
      if state == flows_pb2.FlowContext.ERROR:
        # TODO(berggren): If one artifact fails, what happens? Test.
        raise RuntimeError(u'Flow {0:s}: FAILED! Backtrace from GRR:\n\n{1:s}'.
                           format(flow_id, status.context.backtrace))
      elif state == flows_pb2.FlowContext.TERMINATED:
        self.console_out.VerboseOut(u'Flow {0:s}: Finished successfully'.format(
            flow_id))
        break
      time.sleep(self.CHECK_FLOW_INTERVAL_SEC)

    # Download the files collected by the flow
    self.console_out.VerboseOut(u'Flow {0:s}: Downloading artifacts'.format(
        flow_id))
    collected_file_path = self._DownloadFiles(flow_id)

    if collected_file_path:
      self.console_out.VerboseOut(u'Flow {0:s}: Downloaded: {1:s}'.format(
          flow_id, collected_file_path))

    return self.output_path

  def _DownloadFiles(self, flow_id):
    """Download files from the specified flow."""
    if not os.path.isdir(self.output_path):
      os.makedirs(self.output_path)

    output_file_path = os.path.join(self.output_path, u'.'.join(
        (flow_id, u'zip')))

    if os.path.exists(output_file_path):
      print u'{0:s} already exists: Skipping'.format(output_file_path)
      return None

    self.client.Flow(flow_id).GetFilesArchive().WriteToFile(output_file_path)

    # Unzip archive for processing and remove redundant zip
    with zipfile.ZipFile(output_file_path) as archive:
      archive.extractall(path=self.output_path)
    os.remove(output_file_path)

    return output_file_path

  @property
  def collection_name(self):
    """Name for the collection of collected artifacts."""
    collection_name = self.client.data.os_info.fqdn
    self.console_out.VerboseOut(u'Artifact collection name: {0:s}'.format(
        collection_name))
    return collection_name


def CollectArtifactsHelper(host_list, hunt_id, path_list, artifact_list,
                           use_tsk, reason, approvers, verbose, grr_server_url,
                           username, password):
  """Helper function to collect artifacts based on command line flags passed."""

  # Build list of artifact collectors and start collection in parallel
  artifact_collectors = []
  collected_artifacts = {}
  for host in host_list:
    collector = GrrArtifactCollector(
        host,
        reason,
        grr_server_url,
        username,
        password,
        artifact_list,
        use_tsk,
        approvers,
        verbose=verbose)
    collector.start()

  for path in path_list:
    collector = FilesystemCollector(path, verbose=verbose)
    collector.start()
    artifact_collectors.append(collector)

  if hunt_id:
    collector = GrrHuntCollector(
        hunt_id,
        reason,
        grr_server_url,
        username,
        password,
        approvers,
        verbose=verbose)
    collected_artifacts.update(collector.Collect())

  # Wait for all collectors to finish
  for collector in artifact_collectors:
    collector.join()

  # Collect the artifacts
  for collector in artifact_collectors:
    collected_artifacts.update({
        collector.output_path: collector.collection_name
    })

  return ((i, collected_artifacts[i]) for i in collected_artifacts)
