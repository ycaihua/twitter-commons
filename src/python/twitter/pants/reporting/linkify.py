import os
import re

from twitter.pants.base.build_file import BuildFile


# A regex to recognize substrings that are probably URLs or file paths. Broken down for readability.
_PREFIX = r'(https?://)?/?' # http://, https:// or / or nothing.
_REL_PATH_COMPONENT = r'(\w|[-.])+'  # One or more alphanumeric, underscore, dash or dot.
_ABS_PATH_COMPONENT = '/' + _REL_PATH_COMPONENT
_ABS_PATH_COMPONENTS = '(%s)+' % _ABS_PATH_COMPONENT
_OPTIONAL_TARGET_SUFFIX = '(:%s)?' % _REL_PATH_COMPONENT  # For /foo/bar:target.

# Note that we require at least two path components.
# We require the last characgter to be alphanumeric or underscore, because some tools print an
# ellipsis after file names (I'm looking at you, zinc). None of our files end in a dot in practice,
# so this is fine.
_PATH = _PREFIX + _REL_PATH_COMPONENT + _ABS_PATH_COMPONENTS + _OPTIONAL_TARGET_SUFFIX + '\w'
_PATH_RE = re.compile(_PATH)

def linkify(buildroot, s):
  """Augment text by heuristically finding URL and file references and turning them into links/"""
  def to_url(m):
    if m.group(1):
      return m.group(0)  # It's an http(s) url.
    path = m.group(0)
    if path.startswith('/'):
      path = os.path.relpath(path, buildroot)
    else:
      # See if it's a reference to a target in a BUILD file.
      # TODO: Deal with sibling BUILD files?
      parts = path.split(':')
      if len(parts) == 2:
        putative_dir = parts[0]
      else:
        putative_dir = path
      if os.path.isdir(os.path.join(buildroot, putative_dir)):
        path = os.path.join(putative_dir, BuildFile._CANONICAL_NAME)
    if os.path.exists(os.path.join(buildroot, path)):
      # The reporting server serves file content at /browse/<path_from_buildroot>.
      return '/browse/%s' % path
    else:
      return None

  def maybe_add_link(url, text):
    return '<a target="_blank" href="%s">%s</a>' % (url, text) if url else text
  return _PATH_RE.sub(lambda m: maybe_add_link(to_url(m), m.group(0)), s)
