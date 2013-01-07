import itertools
import mimetypes
import os
import pystache
import re
import sys
import urllib
import urlparse

import BaseHTTPServer

from collections import namedtuple
from datetime import date, datetime

from twitter.pants.goal.context import RunInfo
from twitter.pants.reporting.renderer import Renderer


# Prettyprint plugin files.
PPP_RE=re.compile("""^lang-.*\.js$""")

Settings = namedtuple('Settings', ['info_dir', 'template_dir', 'assets_dir', 'root', 'allowed_clients'])

class FileRegionHandler(BaseHTTPServer.BaseHTTPRequestHandler):
  """A handler that serves regions of files under a given root:

  /browse/path/to/file?s=x&e=y serves from position x (inclusive) to position y (exclusive).
  /browse/path/to/file?s=x serves from position x (inclusive) until the end of the file.
  /browse/path/to/file serves the entire file.
  """
  def __init__(self, settings, renderer, request, client_address, server):
    self._settings = settings
    self._root = self._settings.root
    self._renderer = renderer
    self._client_address = client_address
    self._handlers = [
      ('/runs/', self._handle_runs),
      ('/browse/', self._handle_browse),
      ('/content/', self._handle_content),
      ('/assets/', self._handle_assets)
    ]
    BaseHTTPServer.BaseHTTPRequestHandler.__init__(self, request, client_address, server)

  def _send_content(self, content, content_type, code=200):
    self.send_response(code)
    self.send_header('Content-Type', content_type)
    self.send_header('Content-Length', str(len(content)))
    self.end_headers()
    self.wfile.write(content)

  def do_GET(self):
    client_ip = self._client_address[0]
    if not client_ip in self._settings.allowed_clients and not 'ALL' in self._settings.allowed_clients:
      self._send_content('Access from host %s forbidden.' % client_ip, 'text/html')
      return

    try:
      (_, _, path, query, _) = urlparse.urlsplit(self.path)
      params = urlparse.parse_qs(query)
      for prefix, handler in self._handlers:
        if self._maybe_handle(prefix, handler, path, params):
          break
    except (IOError, ValueError):
      sys.stderr.write('Invalid request %s' % self.path)

  def _maybe_handle(self, prefix, handler, path, params):
    if path.startswith(prefix):
      relpath = path[len(prefix):]
      handler(relpath, params)
      return True
    else:
      return False

  def _handle_runs(self, relpath, params):
    if relpath == '':
      runs_by_day = self._partition_runs_by_day()
      args = self._default_template_args('run_list')
      args.update({ 'runs_by_day': runs_by_day })
      self._send_content(self._renderer.render('base', args), 'text/html')

  def _handle_browse(self, relpath, params):
    abspath = os.path.normpath(os.path.join(self._root, relpath))
    if not abspath.startswith(self._root):
      raise ValueError  # Prevent using .. to get files from anywhere other than root.
    if os.path.isdir(abspath):
      self._serve_dir(abspath, params)
    elif os.path.isfile(abspath):
      self._serve_file(abspath, params)

  def _handle_content(self, relpath, params):
    abspath = os.path.normpath(os.path.join(self._root, relpath))
    self._serve_file_content(abspath, params)

  def _handle_assets(self, relpath, params):
    abspath = os.path.normpath(os.path.join(self._settings.assets_dir, relpath))
    self._serve_asset(abspath)

  def _partition_runs_by_day(self):
    run_infos = self._get_run_info()
    for x in run_infos:
      ts = float(x['timestamp'])
      x['time_of_day_text'] = datetime.fromtimestamp(ts).strftime('%H:%M:%S.') + '%03d' % (int(ts * 1000) % 1000)

    def date_text(dt):
      delta_days = (date.today() - dt).days
      if delta_days == 0:
        return 'Today'
      elif delta_days == 1:
        return 'Yesterday'
      elif delta_days < 7:
        return dt.strftime('%A')  # Weekday name.
      else:
        d = dt.day % 10
        suffix = 'st' if d == 1 else 'nd' if d == 2 else 'rd' if d == 3 else 'th'
        return dt.strftime('%B %d') + suffix  # E.g., October 30th.

    keyfunc = lambda x: datetime.fromtimestamp(float(x['timestamp']))
    sorted_run_infos = sorted(run_infos, key=keyfunc, reverse=True)
    return [ { 'date_text': date_text(dt), 'run_infos': [x for x in infos] }
             for dt, infos in itertools.groupby(sorted_run_infos, lambda x: keyfunc(x).date()) ]

  def _get_run_info(self):
    if not os.path.isdir(self._settings.info_dir):
      return []
    # We copy the RunInfo as a dict, so we can add stuff to it to pass to the template.
    return [RunInfo(os.path.join(self._settings.info_dir, x)).get_as_dict()
            for x in os.listdir(self._settings.info_dir) if x.endswith('.info')]

  def _serve_dir(self, abspath, params):
    relpath = os.path.relpath(abspath, self._root)
    breadcrumbs = self._create_breadcrumbs(relpath)
    entries = [ {'link_path': os.path.join(relpath, e), 'name': e} for e in os.listdir(abspath)]
    args = self._default_template_args('dir')
    args.update({ 'root_parent': os.path.dirname(self._root),
                  'breadcrumbs': breadcrumbs,
                  'entries': entries,
                  'params': params })
    self._send_content(self._renderer.render('base', args), 'text/html')

  def _serve_file(self, abspath, params):
    relpath = os.path.relpath(abspath, self._root)
    breadcrumbs = self._create_breadcrumbs(relpath)
    link_path = urlparse.urlunparse([None, None, relpath, None, urllib.urlencode(params), None])
    args = self._default_template_args('file')
    args.update({ 'root_parent': os.path.dirname(self._root),
                  'breadcrumbs': breadcrumbs,
                  'link_path': link_path })
    self._send_content(self._renderer.render('base', args), 'text/html')

  def _create_breadcrumbs(self, relpath):
    if relpath == '.':
      breadcrumbs = []
    else:
      path_parts = [os.path.basename(self._root)] + relpath.split(os.path.sep)
      path_links = ['/'.join(path_parts[1:i+1]) for i, name in enumerate(path_parts)]
      breadcrumbs = [{'link_path': link_path, 'name': name } for link_path, name in zip(path_links, path_parts)]
    return breadcrumbs

  def _serve_file_content(self, abspath, params):
    start = int(params.get('s')[0]) if 's' in params else 0
    end = int(params.get('e')[0]) if 'e' in params else None
    with open(abspath, 'r') as infile:
      if start:
        infile.seek(start)
      content = infile.read(end - start) if end else infile.read()
    content_type = mimetypes.guess_type(abspath)[0] or 'text/plain'
    if not content_type.startswith('text/'):
      content = repr(content)[1:-1]  # Will escape non-printables etc. We don't take the surrounding quotes.
      n = 120  # Split into lines of this size.
      content = '\n'.join([content[i:i+n] for i in xrange(0, len(content), n)])
      prettify = False
      prettify_extra_langs = []
    else:
      prettify = True
      prettify_extra_langs = \
        [ {'name': x} for x in os.listdir(os.path.join(self._settings.assets_dir, 'js', 'prettify_extra_langs')) ]
    linenums = True
    args = { 'prettify_extra_langs': prettify_extra_langs, 'content': content,
             'prettify': prettify, 'linenums': linenums }
    self._send_content(self._renderer.render('file_content', args), 'text/html')

  def _serve_asset(self, abspath):
    content_type = mimetypes.guess_type(abspath)[0] or 'text/plain'
    with open(abspath, 'r') as infile:
      content = infile.read()
    self._send_content(content, content_type)

  def _default_template_args(self, content_template):
    def include(text, args):
      template_name = pystache.render(text, args)
      return self._renderer.render(template_name, args)
    ret = { 'content_template': content_template }
    ret['include'] = lambda text: include(text, ret)
    return ret

  def log_message(self, format, *args):  # Silence BaseHTTPRequestHandler's logging.
    pass

class ReportingServer(object):
  def __init__(self, port, settings):
    renderer = Renderer(settings.template_dir, require=['base'])

    class MyHandler(FileRegionHandler):
      def __init__(self, request, client_address, server):
        FileRegionHandler.__init__(self, settings, renderer, request, client_address, server)

    self._httpd = BaseHTTPServer.HTTPServer(('', port), MyHandler)
    self._httpd.timeout = 0.1  # Not the network timeout, but how often handle_request yields.

  def start(self, run_before_blocking=list()):
    for f in run_before_blocking:
      f()
    self._httpd.serve_forever()
