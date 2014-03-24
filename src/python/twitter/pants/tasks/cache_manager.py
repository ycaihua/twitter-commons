# ==================================================================================================
# Copyright 2012 Twitter, Inc.
# --------------------------------------------------------------------------------------------------
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this work except in compliance with the License.
# You may obtain a copy of the License in the LICENSE file, or at:
#
#  http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# ==================================================================================================

try:
  import cPickle as pickle
except ImportError:
  import pickle

from twitter.pants.graph.build_graph import sort_targets
from twitter.pants.base.build_invalidator import BuildInvalidator, CacheKeyGenerator
from twitter.pants.base.target import Target
from twitter.pants.targets.external_dependency import ExternalDependency
from twitter.pants.targets.internal import InternalTarget
from twitter.pants.targets.jar_library import JarLibrary
from twitter.pants.targets.pants_target import Pants
from twitter.pants.targets.with_sources import TargetWithSources


# class VersionedTarget(object):
#   def __init__(self, cache_manager, target):
#     self._cache_manager = cache_manager
#     self.target = target
#     self.cache_key = cache_manager.key_for_target(target.cache_key


class VersionedTargetSet(object):
  """Represents a list of targets, a corresponding CacheKey, and a flag determining whether the
  list of targets is currently valid.

  When invalidating a single target, this can be used to represent that target as a singleton.
  When checking the artifact cache, this can also be used to represent a list of targets that are
  built together into a single artifact.
  """

  @staticmethod
  def from_versioned_targets(versioned_targets):
    first_target = versioned_targets[0]
    cache_manager = first_target._cache_manager

    # Quick sanity check; all the versioned targets should have the same cache manager.
    # TODO(ryan): the way VersionedTargets store their own links to a single CacheManager instance
    # feels hacky; see if there's a cleaner way for callers to handle awareness of the CacheManager.
    for versioned_target in versioned_targets:
      if versioned_target._cache_manager != cache_manager:
        raise ValueError("Attempting to combine versioned targets %s and %s with different"
                         " CacheManager instances: %s and %s" % (first_target, versioned_target,
                                                                 cache_manager,
                                                                 versioned_target._cache_manager))
    return VersionedTargetSet(cache_manager, versioned_targets)

  def __init__(self, cache_manager, versioned_targets):
    self._cache_manager = cache_manager
    self.versioned_targets = versioned_targets
    self.targets = [vt.target for vt in versioned_targets]
    # The following line is a no-op if cache_key was set in the VersionedTarget __init__ method.
    self.cache_key = CacheKeyGenerator.combine_cache_keys([vt.cache_key
                                                           for vt in versioned_targets])
    self.payloads = self.cache_key.payloads
    self.valid = not cache_manager.needs_update(self.cache_key)

  def update(self):
    self._cache_manager.update(self)

  def force_invalidate(self):
    self._cache_manager.force_invalidate(self)

  def __repr__(self):
    return 'VTS(%s, %s)' % (','.join(target.id for target in self.targets),
                            'valid' if self.valid else 'invalid')


class VersionedTarget(VersionedTargetSet):
  """This class represents a singleton VersionedTargetSet, and has links to VersionedTargets that
  the wrapped target depends on (after having resolved through any "alias" targets.
  """
  def __init__(self, cache_manager, target, cache_key):
    if not isinstance(target, Target):
      raise ValueError("The target %s must be an instance of Target but is not." % target.id)

    self.target = target
    self.cache_key = cache_key
    # Must come after the assignments above, as they are used in the parent's __init__.
    VersionedTargetSet.__init__(self, cache_manager, [self])
    self.id = target.id
    self._dependencies = set()

  @property
  def dependencies(self):
    import pdb; pdb.set_trace()  
    return self._dependencies

  @dependencies.setter
  def dependencies(self, value):
    print "SETTING DEPENDENCIES"
    self._dependencies = value
  


# The result of calling check() on a CacheManager.
# Each member is a list of VersionedTargetSet objects in topological order.
# Tasks may need to perform no, some or all operations on either of these, depending on how they
# are implemented.
class InvalidationCheck(object):
  @classmethod
  def _partition_versioned_targets(cls, versioned_targets, partition_size_hint):
    """Groups versioned targets so that each group has roughly the same number of sources.

    versioned_targets is a list of VersionedTarget objects  [vt1, vt2, vt3, vt4, vt5, vt6, ...].

    Returns a list of VersionedTargetSet objects, e.g., [VT1, VT2, VT3, ...] representing the
    same underlying targets. E.g., VT1 is the combination of [vt1, vt2, vt3], VT2 is the combination
    of [vt4, vt5] and VT3 is [vt6].

    The new versioned targets are chosen to have roughly partition_size_hint sources.

    This is useful as a compromise between flat mode, where we build all targets in a
    single compiler invocation, and non-flat mode, where we invoke a compiler for each target,
    which may lead to lots of compiler startup overhead. A task can choose instead to build one
    group at a time.
    """
    res = []

    # Hack around the python outer scope problem.
    class VtGroup(object):
      def __init__(self):
        self.vts = []
        self.total_sources = 0

    current_group = VtGroup()

    def add_to_current_group(vt):
      current_group.vts.append(vt)

    def close_current_group():
      if len(current_group.vts) > 0:
        new_vt = VersionedTargetSet.from_versioned_targets(current_group.vts)
        res.append(new_vt)
        current_group.vts = []
        current_group.total_sources = 0

    for vt in versioned_targets:
      add_to_current_group(vt)
      if current_group.total_sources > 1.5 * partition_size_hint and len(current_group.vts) > 1:
        # Too big. Close the current group without this vt and add it to the next one.
        current_group.vts.pop()
        close_current_group()
        add_to_current_group(vt)
      elif current_group.total_sources > partition_size_hint:
        close_current_group()
    close_current_group()  # Close the last group, if any.

    return res

  def __init__(self, all_vts, invalid_vts, partition_size_hint=None):
    # All the targets, valid and invalid.
    self.all_vts = all_vts

    # All the targets, partitioned if so requested.
    self.all_vts_partitioned = self._partition_versioned_targets(
      all_vts, partition_size_hint) if partition_size_hint else all_vts

    # Just the invalid targets.
    self.invalid_vts = invalid_vts

    # Just the invalid targets, partitioned if so requested.
    self.invalid_vts_partitioned = self._partition_versioned_targets(
        invalid_vts, partition_size_hint) if partition_size_hint else invalid_vts


class CacheManager(object):
  """Manages cache checks, updates and invalidation keeping track of basic change
  and invalidation statistics.
  Note that this is distinct from the ArtifactCache concept, and should probably be renamed.
  """
  def __init__(self,
               cache_key_generator,
               build_invalidator_dir,
               invalidate_dependents,
               extra_data):
    self._cache_key_generator = cache_key_generator
    self._invalidate_dependents = invalidate_dependents
    self._extra_data = pickle.dumps(extra_data)  # extra_data may be None.
    self._invalidator = BuildInvalidator(build_invalidator_dir)

  def update(self, vts):
    """Mark a changed or invalidated VersionedTargetSet as successfully processed."""
    for vt in vts.versioned_targets:
      self._invalidator.update(vt.cache_key)
      vt.valid = True
    self._invalidator.update(vts.cache_key)
    vts.valid = True

  def force_invalidate(self, vts):
    """Force invalidation of a VersionedTargetSet."""
    for vt in vts.versioned_targets:
      self._invalidator.force_invalidate(vt.cache_key)
      vt.valid = False
    self._invalidator.force_invalidate(vts.cache_key)
    vts.valid = False

  def check(self, targets, partition_size_hint=None):
    """Checks whether each of the targets has changed and invalidates it if so.

    Returns a list of VersionedTargetSet objects (either valid or invalid). The returned sets
    'cover' the input targets, possibly partitioning them, and are in topological order.
    The caller can inspect these in order and, e.g., rebuild the invalid ones.
    """
    all_vts = self._sort_and_validate_targets(targets)
    invalid_vts = filter(lambda vt: not vt.valid, all_vts)
    return InvalidationCheck(all_vts, invalid_vts, partition_size_hint)

  def _sort_and_validate_targets(self, targets):
    """Validate each target.

    Returns a topologically ordered set of VersionedTargets, each representing one input target.
    """
    # We must check the targets in this order, to ensure correctness if invalidate_dependents=True,
    # since we use earlier cache keys to compute later cache keys in this case.
    ordered_targets = self._order_target_list(targets)

    # This will be a list of VersionedTargets that correspond to @targets.
    versioned_targets = []

    # This will be a mapping from each target to its corresponding VersionedTarget.
    versioned_targets_by_target = {}

    # Map from id to current fingerprint of the target with that id. We update this as we iterate,
    # in topological order, so when handling a target, this will already contain all its deps (in
    # this round).
    id_to_hash = {}

    for target in ordered_targets:
      cache_key = self._key_for(target, transitive=self._invalidate_dependents)
      id_to_hash[target.id] = cache_key.hash

      # Create a VersionedTarget corresponding to @target.
      versioned_target = VersionedTarget(self, target, cache_key)

      # Add the new VersionedTarget to the list of computed VersionedTargets.
      versioned_targets.append(versioned_target)

    return versioned_targets

  def needs_update(self, cache_key):
    return self._invalidator.needs_update(cache_key)

  def _order_target_list(self, targets):
    """Orders the targets topologically, from least to most dependent."""
    return filter(targets.__contains__, reversed(sort_targets(targets)))

  def _key_for(self, target, transitive=False):
    return self._cache_key_generator.key_for_target(target, transitive=transitive)

