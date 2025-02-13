#!/usr/bin/env python
#
# deepzoom_multiserver - Example web application for viewing multiple slides
#
# Copyright (c) 2010-2015 Carnegie Mellon University
# Copyright (c) 2021-2022 Benjamin Gilbert
#
# This library is free software; you can redistribute it and/or modify it
# under the terms of version 2.1 of the GNU Lesser General Public License
# as published by the Free Software Foundation.
#
# This library is distributed in the hope that it will be useful, but
# WITHOUT ANY WARRANTY; without even the implied warranty of MERCHANTABILITY
# or FITNESS FOR A PARTICULAR PURPOSE.  See the GNU Lesser General Public
# License for more details.
#
# You should have received a copy of the GNU Lesser General Public License
# along with this library; if not, write to the Free Software Foundation,
# Inc., 51 Franklin Street, Fifth Floor, Boston, MA 02110-1301 USA.
#

from collections import OrderedDict
from io import BytesIO
from optparse import OptionParser
import os
from threading import Lock

from flask import Flask, abort, make_response, render_template, url_for

if os.name == 'nt':
    _dll_path = os.getenv('OPENSLIDE_PATH')
    if _dll_path is not None:
        if hasattr(os, 'add_dll_directory'):
            # Python >= 3.8
            with os.add_dll_directory(_dll_path):
                import openslide
        else:
            # Python < 3.8
            _orig_path = os.environ.get('PATH', '')
            os.environ['PATH'] = _orig_path + ';' + _dll_path
            import openslide

            os.environ['PATH'] = _orig_path
else:
    import openslide

from openslide import OpenSlide, OpenSlideCache, OpenSlideError, OpenSlideVersionError
from openslide.deepzoom import DeepZoomGenerator


def create_app(config=None, config_file=None):
    # Create and configure app
    app = Flask(__name__)
    app.config.from_mapping(
        SLIDE_DIR='.',
        SLIDE_CACHE_SIZE=10,
        SLIDE_TILE_CACHE_MB=128,
        DEEPZOOM_FORMAT='jpeg',
        DEEPZOOM_TILE_SIZE=254,
        DEEPZOOM_OVERLAP=1,
        DEEPZOOM_LIMIT_BOUNDS=True,
        DEEPZOOM_TILE_QUALITY=75,
    )
    app.config.from_envvar('DEEPZOOM_MULTISERVER_SETTINGS', silent=True)
    if config_file is not None:
        app.config.from_pyfile(config_file)
    if config is not None:
        app.config.from_mapping(config)

    # Set up cache
    app.basedir = os.path.abspath(app.config['SLIDE_DIR'])
    config_map = {
        'DEEPZOOM_TILE_SIZE': 'tile_size',
        'DEEPZOOM_OVERLAP': 'overlap',
        'DEEPZOOM_LIMIT_BOUNDS': 'limit_bounds',
    }
    opts = {v: app.config[k] for k, v in config_map.items()}
    app.cache = _SlideCache(
        app.config['SLIDE_CACHE_SIZE'], app.config['SLIDE_TILE_CACHE_MB'], opts
    )

    # Helper functions
    def get_slide(path):
        path = os.path.abspath(os.path.join(app.basedir, path))
        if not path.startswith(app.basedir + os.path.sep):
            # Directory traversal
            abort(404)
        if not os.path.exists(path):
            abort(404)
        try:
            slide = app.cache.get(path)
            slide.filename = os.path.basename(path)
            return slide
        except OpenSlideError:
            abort(404)

    # Set up routes
    @app.route('/')
    def index():
        return render_template('files.html', root_dir=_Directory(app.basedir))

    @app.route('/<path:path>')
    def slide(path):
        slide = get_slide(path)
        slide_url = url_for('dzi', path=path)
        return render_template(
            'slide-fullpage.html',
            slide_url=slide_url,
            slide_filename=slide.filename,
            slide_mpp=slide.mpp,
        )

    @app.route('/<path:path>.dzi')
    def dzi(path):
        slide = get_slide(path)
        format = app.config['DEEPZOOM_FORMAT']
        resp = make_response(slide.get_dzi(format))
        resp.mimetype = 'application/xml'
        return resp

    @app.route('/<path:path>_files/<int:level>/<int:col>_<int:row>.<format>')
    def tile(path, level, col, row, format):
        slide = get_slide(path)
        format = format.lower()
        if format != 'jpeg' and format != 'png':
            # Not supported by Deep Zoom
            abort(404)
        try:
            tile = slide.get_tile(level, (col, row))
        except ValueError:
            # Invalid level or coordinates
            abort(404)
        buf = BytesIO()
        tile.save(buf, format, quality=app.config['DEEPZOOM_TILE_QUALITY'])
        resp = make_response(buf.getvalue())
        resp.mimetype = 'image/%s' % format
        return resp

    return app


class _SlideCache:
    def __init__(self, cache_size, tile_cache_mb, dz_opts):
        self.cache_size = cache_size
        self.dz_opts = dz_opts
        self._lock = Lock()
        self._cache = OrderedDict()
        # Share a single tile cache among all slide handles, if supported
        try:
            self._tile_cache = OpenSlideCache(tile_cache_mb * 1024 * 1024)
        except OpenSlideVersionError:
            self._tile_cache = None

    def get(self, path):
        with self._lock:
            if path in self._cache:
                # Move to end of LRU
                slide = self._cache.pop(path)
                self._cache[path] = slide
                return slide

        osr = OpenSlide(path)
        if self._tile_cache is not None:
            osr.set_cache(self._tile_cache)
        slide = DeepZoomGenerator(osr, **self.dz_opts)
        try:
            mpp_x = osr.properties[openslide.PROPERTY_NAME_MPP_X]
            mpp_y = osr.properties[openslide.PROPERTY_NAME_MPP_Y]
            slide.mpp = (float(mpp_x) + float(mpp_y)) / 2
        except (KeyError, ValueError):
            slide.mpp = 0

        with self._lock:
            if path not in self._cache:
                if len(self._cache) == self.cache_size:
                    self._cache.popitem(last=False)
                self._cache[path] = slide
        return slide


class _Directory:
    def __init__(self, basedir, relpath=''):
        self.name = os.path.basename(relpath)
        self.children = []
        for name in sorted(os.listdir(os.path.join(basedir, relpath))):
            cur_relpath = os.path.join(relpath, name)
            cur_path = os.path.join(basedir, cur_relpath)
            if os.path.isdir(cur_path):
                cur_dir = _Directory(basedir, cur_relpath)
                if cur_dir.children:
                    self.children.append(cur_dir)
            elif OpenSlide.detect_format(cur_path):
                self.children.append(_SlideFile(cur_relpath))


class _SlideFile:
    def __init__(self, relpath):
        self.name = os.path.basename(relpath)
        self.url_path = relpath


if __name__ == '__main__':
    parser = OptionParser(usage='Usage: %prog [options] [slide-directory]')
    parser.add_option(
        '-B',
        '--ignore-bounds',
        dest='DEEPZOOM_LIMIT_BOUNDS',
        default=True,
        action='store_false',
        help='display entire scan area',
    )
    parser.add_option(
        '-c', '--config', metavar='FILE', dest='config', help='config file'
    )
    parser.add_option(
        '-d',
        '--debug',
        dest='DEBUG',
        action='store_true',
        help='run in debugging mode (insecure)',
    )
    parser.add_option(
        '-e',
        '--overlap',
        metavar='PIXELS',
        dest='DEEPZOOM_OVERLAP',
        type='int',
        help='overlap of adjacent tiles [1]',
    )
    parser.add_option(
        '-f',
        '--format',
        metavar='{jpeg|png}',
        dest='DEEPZOOM_FORMAT',
        help='image format for tiles [jpeg]',
    )
    parser.add_option(
        '-l',
        '--listen',
        metavar='ADDRESS',
        dest='host',
        default='127.0.0.1',
        help='address to listen on [127.0.0.1]',
    )
    parser.add_option(
        '-p',
        '--port',
        metavar='PORT',
        dest='port',
        type='int',
        default=5000,
        help='port to listen on [5000]',
    )
    parser.add_option(
        '-Q',
        '--quality',
        metavar='QUALITY',
        dest='DEEPZOOM_TILE_QUALITY',
        type='int',
        help='JPEG compression quality [75]',
    )
    parser.add_option(
        '-s',
        '--size',
        metavar='PIXELS',
        dest='DEEPZOOM_TILE_SIZE',
        type='int',
        help='tile size [254]',
    )

    (opts, args) = parser.parse_args()
    config = {}
    config_file = opts.config
    # Set only those settings specified on the command line
    for k in dir(opts):
        v = getattr(opts, k)
        if not k.startswith('_') and v is not None:
            config[k] = v
    # Set slide directory if specified
    try:
        config['SLIDE_DIR'] = args[0]
    except IndexError:
        pass
    app = create_app(config, config_file)

    app.run(host=opts.host, port=opts.port, threaded=True)
