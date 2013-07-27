
from multiprocessing.pool import ThreadPool
import threading
from twitter.pants.reporting.report import Report


class Work(object):
  """Represents multiple concurrent calls to the same callable."""
  def __init__(self, func, args_tuples, workunit_name=None):
    # A callable.
    self.func = func

    # A list of tuples of args. func will be called once per tuple, concurrently.
    # The length of this list is the cardinality of the work.
    self.args_tuples = args_tuples

    # If specified, each invocation will be executed in a workunit of this name.
    self.workunit_name = workunit_name


class WorkerPool(object):
  """A pool of workers.

  Workers are threads, and so are subject to GIL constraints. Submitting CPU-bound work
  may not be effective. Use this class primarily for IO-bound work.
  """

  def __init__(self, parent_workunit, run_tracker, num_workers):
    self._run_tracker = run_tracker
    # All workers accrue work to the same root.
    self._pool = ThreadPool(processes=num_workers,
                            initializer=self._run_tracker.register_thread,
                            initargs=(parent_workunit, ))
    # We mustn't shutdown when there are pending workchains, as they may need to submit work
    # in the future, and the pool doesn't know about this yet.
    self._pending_workchains = 0
    self._pending_workchains_cond = threading.Condition()  # Protects self._pending_workchains.

    self._shutdown_hooks = []

  def add_shutdown_hook(self, hook):
    self._shutdown_hooks.append(hook)

  def submit_async_work(self, work, callback=None):
    """Submit work to be executed in the background.

    - work: The work to execute.
    - callback: If specified, a callable taking a single argument, which will be a list
                of return values of each invocation, in order. Called only if all work succeeded.

    Don't do work in callback: not only will it block the result handling thread, but
    that thread is not a worker and doesn't have a logging context etc. Use callback just to
    submit further work to the pool.
    """
    if work is None or len(work.args_tuples) == 0:  # map_async hangs on 0-length iterables.
      if callback:
        callback([])
    else:
      def do_work(*args):
        self._do_work(work.func, *args, workunit_name=work.workunit_name)
      self._pool.map_async(do_work, work.args_tuples, chunksize=1, callback=callback)

  def submit_async_work_chain(self, work_chain):
    """Submit work to be executed in the background.

    - work_chain: An iterable of Work instances. Will be invoked serially. Each instance may
                  have a different cardinality. There is no output-input chaining: the argument
                  tuples must already be present in each workunit.
    """
    with self._pending_workchains_cond:
      self._pending_workchains += 1
    def wrap(work):
      def wrapper(*args):
        try:
          work.func(*args)
        except Exception as e:
          with self._pending_workchains_cond:
            self._pending_workchains -= 1
            self._pending_workchains_cond.notify()
          self._run_tracker.log(Report.ERROR, '%s' % e)
          raise
      return Work(wrapper, work.args_tuples, work.workunit_name)

    work_iter = iter(work_chain)
    def submit_next():
      try:
        self.submit_async_work(wrap(work_iter.next()), callback=lambda x: submit_next())
      except StopIteration:
        with self._pending_workchains_cond:
          self._pending_workchains -= 1
          self._pending_workchains_cond.notify()
    submit_next()

  def submit_work_and_wait(self, work):
    """Submit work to be executed on this pool, but wait for it to complete.

    Returns a list of return values of each invocation, in order.  Throws if any invocation does.
    """
    if work is None or len(work.args_tuples) == 0:  # map hangs on 0-length iterables.
      return []
    else:
      def do_work(*args):
        return self._do_work(work.func, *args, workunit_name=work.workunit_name)
      return self._pool.map(do_work, work.args_tuples, chunksize=1)

  def _do_work(self, func, args_tuple, workunit_name):
    if workunit_name:
      with self._run_tracker.new_workunit(name=workunit_name):
        return func(*args_tuple)
    else:
      return func(*args_tuple)

  def shutdown(self):
    with self._pending_workchains_cond:
      while self._pending_workchains > 0:
        self._pending_workchains_cond.wait()
      self._pool.close()
      self._pool.join()
      for hook in self._shutdown_hooks:
        hook()