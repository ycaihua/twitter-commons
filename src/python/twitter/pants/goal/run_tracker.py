
import os
import sys
import time

from contextlib import contextmanager

from twitter.pants.goal.run_info import RunInfo
from twitter.pants.goal.aggregated_timings import AggregatedTimings
from twitter.pants.goal.work_unit import WorkUnit
from twitter.pants.reporting.report import default_reporting


class RunTracker(object):
  """Tracks and times the execution of a pants run."""
  def __init__(self, config):
    run_timestamp = time.time()
    # run_id is safe for use in paths.
    millis = (run_timestamp * 1000) % 1000
    run_id = 'pants_run_%s_%d' %\
             (time.strftime('%Y_%m_%d_%H_%M_%S', time.localtime(run_timestamp)), millis)
    cmd_line = ' '.join(['pants'] + sys.argv[1:])
    info_dir = config.getdefault('info_dir')
    self.run_info = RunInfo(os.path.join(info_dir, '%s.info' % run_id))
    self.run_info.add_infos([('id', run_id), ('timestamp', run_timestamp), ('cmd_line', cmd_line)])
    # Create a 'latest' symlink, after we add_infos, so we're guaranteed that the file exists.
    link_to_latest = os.path.join(info_dir, 'latest.info')
    if os.path.exists(link_to_latest):
      os.unlink(link_to_latest)
    os.symlink(self.run_info.path(), link_to_latest)

    self.aggregated_timings = AggregatedTimings()

    self.report = default_reporting(config, self.run_info)
    self.report.open()

    self._root_workunit = WorkUnit(parent=None, aggregated_timings=self.aggregated_timings,
                                   type='root', name='all', cmd=None)
    self._root_workunit.start()
    self.report.start_workunit(self._root_workunit)
    self._current_workunit = self._root_workunit

  def close(self):
    assert self._current_workunit == self._root_workunit
    self._root_workunit.end()
    self.report.end_workunit(self._root_workunit)
    self.report.close()
    try:
      self.run_info.add_info('outcome', self._root_workunit.outcome_string())
    except IOError:
      pass  # If the goal is clean-all then the run info dir no longer exists...

  def current_work_unit(self):
    return self._current_workunit

  @contextmanager
  def new_work_scope(self, name, type='', cmd=''):
    """Creates a (hierarchical) subunit of work for the purpose of timing and reporting.

    - name: A short name for this work. E.g., 'resolve', 'compile', 'scala'.
    - type: An optional string that the report formatters can use to decide how to display
            information about this work. E.g., 'phase', 'goal', 'jvm_tool'. By convention, types
            ending with '_tool' are assumed to be invocations of external tools.
     - cmd: An optional longer description, e.g., the cmd line of a tool invocation.
            Used only for display.

    Use like this:

    with context.new_work_scope(name='compile', type='goal') as workunit:
      <do scoped work here>
      <set the outcome on workunit if necessary>

    Note that the outcome will automatically be set to failure if an exception is raised
    in a workunit, and to success otherwise, so often you only need to set the
    outcome explicitly if you want to set it to warning.
    """
    self._current_workunit = WorkUnit(parent=self._current_workunit,
                                      aggregated_timings=self.aggregated_timings,
                                      name=name, type=type, cmd=cmd)
    self._current_workunit.start()
    raised_exception = True  # Putatively.
    try:
      self.report.start_workunit(self._current_workunit)
      yield self._current_workunit
      raised_exception = False
    finally:
      if raised_exception:  # In case the work code failed to set the outcome.
        self._current_workunit.set_outcome(WorkUnit.FAILURE)
      else:
        self._current_workunit.set_outcome(WorkUnit.SUCCESS)
      self._current_workunit.end()
      self.report.end_workunit(self._current_workunit)
      self._current_workunit = self._current_workunit.parent