"""Experimental, finite routed SSG with no source execution (RFC-0003)."""
from __future__ import annotations

import json
from pathlib import Path
import re
from urllib.parse import unquote

from . import web_frontend as F
from .web_static import escaped, evaluate, render, text


MAX_PATHS = 1000


def canonical(path, line=1):
    """Map a bounded URL spelling to a portable, directory-style output path."""
    if (not isinstance(path, str) or not path.startswith('/') or len(path) > 8192
            or any(c in path for c in '\\?#') or '//' in path):
        F.fail(line, 'Use an absolute local static route path without query or fragment.', 'NW6003')
    raw = path[1:-1] if path.endswith('/') else path[1:]
    if not raw:
        return '/', []
    if re.search(r'%(?![0-9a-fA-F]{2})', raw):
        F.fail(line, 'Invalid percent encoding in static route.', 'NW6003')
    try:
        parts = [unquote(p, errors='strict') for p in raw.split('/')]
    except UnicodeError:
        F.fail(line, 'Invalid UTF-8 in static route.', 'NW6003')
    # A portable first SSG slice: slugs, signed integers and booleans. Excludes
    # traversal, encoded separators, Windows device names and filesystem aliases.
    for part in parts:
        if (len(part) > 128 or not re.fullmatch(r'[A-Za-z0-9_-]+', part)
                or part.upper() in {'CON', 'PRN', 'AUX', 'NUL', *(f'COM{i}' for i in range(1, 10)), *(f'LPT{i}' for i in range(1, 10))}):
            F.fail(line, 'Static route segments require portable ASCII letters, digits, underscores or hyphens.', 'NW6003')
    if parts[0].casefold() == '_nexo':
        F.fail(line, 'The /_nexo/ path is reserved for static build assets.', 'NW6003')
    return '/' + '/'.join(parts) + '/', parts


def match(pages, segments):
    """Use the fragment router's specificity order and typed parameter rules."""
    for page in sorted(pages, key=lambda d: -d.route['score']):
        if len(page.route['parts']) != len(segments):
            continue
        props = {}
        for part, segment in zip(page.route['parts'], segments):
            if 'literal' in part:
                if part['literal'] != segment:
                    break
            elif part['type'] == 'str':
                props[part['name']] = segment
            elif part['type'] == 'bool':
                if segment not in ('true', 'false'):
                    break
                props[part['name']] = segment == 'true'
            else:
                if len(segment) > 20 or not re.fullmatch(r'-?(0|[1-9][0-9]*)', segment):
                    break
                value = int(segment)
                if not -(2**63) <= value < 2**63:
                    break
                props[part['name']] = value
        else:
            return page, props
    return None


def enumerate_routes(program, route_file):
    pages = [d for u in program.units for d in u.declarations.values()
             if d.kind == 'page' and d.public and 'fallback' not in d.route]
    requested = []
    if route_file is not None:
        try:
            with Path(route_file).open('rb') as source:
                data = source.read(1024 * 1024 + 1)
            if len(data) > 1024 * 1024:
                raise ValueError()
            requested = json.loads(data.decode('utf-8'))
        except (ValueError, RecursionError):
            F.fail(1, 'Route enumeration must be a JSON array of concrete paths (at most 1 MiB).', 'NW6003')
        if not isinstance(requested, list) or len(requested) > MAX_PATHS:
            F.fail(1, f'Route enumeration requires an array of at most {MAX_PATHS} paths.', 'NW6003')
    paths = {}
    explicit = set()
    for path in requested:
        url, segments = canonical(path)
        if url in explicit:
            F.fail(1, 'Duplicate concrete static route path.', 'NW6003')
        explicit.add(url)
        paths[url] = segments
    for page in pages:
        if not page.props:
            url, segments = canonical(page.route['path'], page.line)
            paths[url] = segments
    if '/' not in paths:
        F.fail(1, 'A routed static site requires a public root page at /.', 'NW6003')
    if len(paths) > MAX_PATHS:
        F.fail(1, f'Static generation is limited to {MAX_PATHS} paths.', 'NW6003')
    result = {}
    seen = {}
    for url, segments in sorted(paths.items()):
        for i in range(1, len(segments) + 1):
            prefix = '/'.join(segments[:i])
            key = prefix.casefold()
            if key in seen and seen[key] != prefix:
                F.fail(1, 'Static output routes collide on a case-insensitive filesystem.', 'NW6003')
            seen[key] = prefix
        found = match(pages, segments)
        if found is None:
            F.fail(1, f'Enumerated path {url} does not match a typed public page.', 'NW6003')
        result[url] = found
    generated = {d.symbol for d, _ in result.values()}
    for page in pages:
        if page.props and page.symbol not in generated:
            F.fail(page.line, f'Enumerate at least one concrete path for dynamic page {page.name}.', 'NW6003')
    return result


def document(markup, title, description=None):
    meta = '' if description is None else '<meta name="description" content="' + escaped(description) + '">'
    return ('<!doctype html>\n<html lang="en"><head><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width, initial-scale=1">'
            '<meta http-equiv="Content-Security-Policy" content="default-src \'none\'; '
            'connect-src \'none\'; script-src \'none\'; style-src \'self\'; base-uri \'none\'; form-action \'none\'">'
            '<title>' + escaped(title) + '</title>' + meta +
            '<link rel="stylesheet" href="/_nexo/app.css"></head><body><div id="app">' +
            markup + '</div></body></html>\n')


def assets(files, program, route_file=None):
    from .web_release import MANIFEST, metadata, serialized, input_revision
    from . import __version__
    routes = enumerate_routes(program, route_file)

    def link(path, line):
        url, _ = canonical(path, line)
        if url not in routes:
            F.fail(line, f'Static link {url} has no generated page; enumerate its path.', 'NW6003')
        return url

    output = {'_nexo/app.css': files['app.css']}
    mapping = {}
    entries = [(url, page, props) for url, (page, props) in routes.items()]
    fallback = next((d for u in program.units for d in u.declarations.values()
                     if d.kind == 'page' and d.public and d.route.get('fallback') == 'not_found'), None)
    if fallback:
        entries.append((None, fallback, {}))
    for url, page, props in entries:
        markup = render(program, [], entry=page, props=props, routed=True, link=link)
        meta = {key: text(evaluate(expr, props, line)) for key, (expr, line) in page.metadata.items()
                if key in ('title', 'description')}
        name = '404.html' if url is None else url.strip('/') + '/index.html' if url != '/' else 'index.html'
        output[name] = document(markup, meta.get('title', 'Nexo Web'), meta.get('description'))
        if url is not None:
            mapping[url] = name
    manifest = {'schema_version': 1, 'format': 'nexo-web-static', 'entry': 'index.html',
                'routes': mapping, 'sourcemap': False, 'observe': False,
                'input_sha256': input_revision({'units': [(u.key, u.source) for u in program.units],
                                                'styles': program.css_sources, 'paths': sorted(routes)}),
                'toolchain': {'compiler': __version__},
                'files': {name: metadata(value) for name, value in sorted(output.items())}}
    return {**output, MANIFEST: serialized(manifest)}
