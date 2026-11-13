import socketserver
import threading

import pytest

from reader import USER_AGENT
from reader import ParseError
from reader import StorageError
from reader._types import FeedFilter
from utils import make_url_base
from utils import utc_datetime


@pytest.fixture
def server_factory(request):
    servers = []

    def factory():
        server = HTTPServer(('127.0.0.1', 0))
        thread = threading.Thread(target=server.serve_forever, args=(0.005,))
        thread.start()
        servers.append((server, thread))
        return server

    yield factory

    for server, thread in servers:
        server.shutdown()
        thread.join()


@pytest.fixture
def server(server_factory):
    return server_factory()


class HTTPServer(socketserver.TCPServer):

    class Handler(socketserver.StreamRequestHandler):
        def handle(self):
            lines = iter(self.rfile.readline, b'\r\n')
            self.server.request = parse_request(b''.join(lines))
            self.wfile.writelines(self.server.response)

    def __init__(self, server_address, bind_and_activate=True):
        super().__init__(server_address, self.Handler, bind_and_activate)
        self.response = None
        self.request = None

    @property
    def url(self):
        return f"http://{self.server_address[0]}:{self.server_address[1]}/"

    def set_response(self, *args, **kwargs):
        self.response = list(generate_response(*args, **kwargs))


def parse_request(request):
    header, _, body = request.partition(b'\r\n\r\n')
    lines = header.removesuffix(b'\r\n').split(b'\r\n')
    headers = dict(l.partition(b': ')[::2] for l in lines[1:])
    return lines[0], headers, body


def generate_response(body=b'', *, status='200 OK', **headers):
    def ensure_bytes(s):
        return s.encode() if isinstance(s, str) else s

    body = ensure_bytes(body)
    processed_headers = {}
    if body:
        processed_headers['Content-Length'] = str(len(body))
        processed_headers['Content-Type'] = 'text/xml'
    for name, value in headers.items():
        processed_headers[name.replace('_', '-')] = value

    yield b'HTTP/1.1 ' + ensure_bytes(status) + b'\r\n'
    for name, value in processed_headers.items():
        yield ensure_bytes(name) + b': ' + ensure_bytes(value) + b'\r\n'
    yield b'\r\n'
    yield body


@pytest.mark.parametrize('feed_type', ['rss', 'atom', 'json'])
def test_local(reader, feed_type, data_dir, monkeypatch_datetime):
    datetime = monkeypatch_datetime('reader.core.datetime')

    feed_filename = f'full.{feed_type}'
    feed_url = str(data_dir.joinpath(feed_filename))

    datetime.now.set(datetime(2010, 1, 1))
    reader.add_feed(feed_url)
    datetime.now.set(datetime(2010, 1, 2))
    reader.update_feeds()

    (feed,) = reader.get_feeds()
    entries = sorted(reader.get_entries(), key=lambda e: e.id)

    url_base, rel_base = make_url_base(feed_url)
    expected = {'url_base': url_base, 'rel_base': rel_base}
    exec(data_dir.joinpath(feed_filename + '.py').read_text(), expected)

    expected_feed = expected['feed'].as_feed(
        added=utc_datetime(2010, 1, 1),
        last_updated=utc_datetime(2010, 1, 2),
        last_retrieved=utc_datetime(2010, 1, 2),
        update_after=utc_datetime(2010, 1, 2, 1),
    )

    assert feed == expected_feed
    assert entries == [
        e.as_entry(
            feed=feed,
            added=utc_datetime(2010, 1, 2),
            last_updated=utc_datetime(2010, 1, 2),
        )
        for e in sorted(expected['entries'], key=lambda e: e.id)
    ]

    # TODO: same tests, but with http server


@pytest.mark.slow
def test_fixed_headers(reader, data_dir, server):
    reader.add_feed(server.url)

    server.set_response(data_dir.joinpath('empty.atom').read_text())
    reader.update_feed(server.url)

    _, headers, _ = server.request

    assert headers[b'User-Agent'] == USER_AGENT.encode()
    assert headers[b'User-Agent'].startswith(b'python-reader/')
    assert headers[b'User-Agent'].endswith(b' (+https://github.com/lemon24/reader)')

    assert headers[b'Accept'] == (
        b'application/atom+xml,application/rdf+xml,application/rss+xml'
        b',application/x-netcdf,application/feed+json,application/xml'
        b';q=0.9,application/json'
        b';q=0.9,text/xml'
        b';q=0.2,*/*'
        b';q=0.1'
    )

    assert headers[b'A-IM'] == b'feed'


@pytest.mark.slow
@pytest.mark.noscheduled
def test_conditional_requests(reader, data_dir, server):
    """Check ETag / Last-Modified are sent back, at the wire level.

    Ensures https://rachelbythebay.com/w/2023/01/18/http/ cannot happen.

    """
    etag = b'"00000-67890abcdef12"'
    last_modified = b'Thu, 1 Jan 2020 00:00:00 GMT'

    reader.add_feed(server.url)

    # server response with caching headers
    server.set_response(
        data_dir.joinpath('full.atom').read_text(),
        ETag=etag,
        Last_Modified=last_modified,
    )
    assert reader.update_feed(server.url).new == 5
    request_line, headers, body = server.request
    assert request_line == b'GET / HTTP/1.1'
    assert body == b''
    assert b'If-None-Match' not in headers
    assert b'If-Modified-Since' not in headers

    # assert caching headers are used
    server.set_response(status='304 Not Modified')
    assert reader.update_feed(server.url) is None
    request_line, headers, body = server.request
    assert headers[b'If-None-Match'] == etag
    assert headers[b'If-Modified-Since'] == last_modified

    # server responds with new caching headers
    server.set_response(
        data_dir.joinpath('empty.atom').read_text(),
        ETag=b'"11111-67890abcdef12"',
        Last_Modified=b'Thu, 1 Jan 2020 11:11:11 GMT',
    )
    assert reader.update_feed(server.url).new == 0
    request_line, headers, body = server.request
    assert headers[b'If-None-Match'] == etag
    assert headers[b'If-Modified-Since'] == last_modified

    # assert new caching headers are used
    server.set_response(status='304 Not Modified')
    assert reader.update_feed(server.url) is None
    request_line, headers, body = server.request
    assert headers[b'If-None-Match'] == b'"11111-67890abcdef12"'
    assert headers[b'If-Modified-Since'] == b'Thu, 1 Jan 2020 11:11:11 GMT'


@pytest.mark.slow
def test_cache_control(reader, data_dir, server, monkeypatch_datetime):
    datetime = monkeypatch_datetime('reader.core.datetime')
    datetime.now.set(datetime(2010, 1, 1))
    reader.add_feed(server.url)

    server.set_response(
        data_dir.joinpath('empty.atom').read_text(),
        Cache_Control=f'max-age={3600 * 12}',
    )

    def update():
        return set(dict(reader.update_feeds_iter(scheduled=True)))

    datetime.now.set(datetime(2010, 1, 2))
    assert update() == {server.url}

    datetime.now.set(datetime(2010, 1, 2, 12, 59, 59))
    assert update() == set()

    datetime.now.set(datetime(2010, 1, 2, 13))
    assert update() == {server.url}


def get_persisted_caching_info(reader, url):
    """The ETag / Last-Modified validators stored for a feed, as a dict."""
    (feed_for_update,) = reader._storage.get_feeds_for_update(FeedFilter(url))
    return feed_for_update.caching_info


def get_entry_count(reader, url):
    return len(list(reader.get_entries(feed=url)))


@pytest.mark.slow
@pytest.mark.noscheduled
def test_conditional_requests_update_chain(reader, data_dir, server):
    """End-to-end consistency of conditional requests along a full update chain:
    first 200, then 304, then validator change, then a server that ignores
    the conditional headers. At every step, check the request headers on
    the wire, entry de-duplication, and the persisted validator version.

    """
    etag_one = b'"00000-67890abcdef12"'
    last_modified_one = b'Thu, 1 Jan 2020 00:00:00 GMT'
    caching_info_one = {
        'etag': etag_one.decode(),
        'last-modified': last_modified_one.decode(),
    }
    etag_two = b'"11111-67890abcdef12"'
    last_modified_two = b'Thu, 1 Jan 2020 11:11:11 GMT'
    caching_info_two = {
        'etag': etag_two.decode(),
        'last-modified': last_modified_two.decode(),
    }

    reader.add_feed(server.url)
    assert get_persisted_caching_info(reader, server.url) is None

    # first update: 200, no validators to send yet
    server.set_response(
        data_dir.joinpath('full.atom').read_text(),
        ETag=etag_one,
        Last_Modified=last_modified_one,
    )
    assert reader.update_feed(server.url).new == 5
    _, headers, _ = server.request
    assert b'If-None-Match' not in headers
    assert b'If-Modified-Since' not in headers
    assert get_persisted_caching_info(reader, server.url) == caching_info_one
    assert get_entry_count(reader, server.url) == 5

    # second update: 304, first validators are sent back and stay persisted
    server.set_response(status='304 Not Modified')
    assert reader.update_feed(server.url) is None
    _, headers, _ = server.request
    assert headers[b'If-None-Match'] == etag_one
    assert headers[b'If-Modified-Since'] == last_modified_one
    assert get_persisted_caching_info(reader, server.url) == caching_info_one
    assert get_entry_count(reader, server.url) == 5

    # third update: 200 with changed validators;
    # the old ones are sent on the wire, the new ones get persisted
    server.set_response(
        data_dir.joinpath('empty.atom').read_text(),
        ETag=etag_two,
        Last_Modified=last_modified_two,
    )
    assert reader.update_feed(server.url).new == 0
    _, headers, _ = server.request
    assert headers[b'If-None-Match'] == etag_one
    assert headers[b'If-Modified-Since'] == last_modified_one
    assert get_persisted_caching_info(reader, server.url) == caching_info_two
    assert get_entry_count(reader, server.url) == 5

    # fourth update: the server ignores the conditional request headers
    # and responds 200 with the same body and validators;
    # entries are de-duplicated and the validators are kept
    assert reader.update_feed(server.url).new == 0
    _, headers, _ = server.request
    assert headers[b'If-None-Match'] == etag_two
    assert headers[b'If-Modified-Since'] == last_modified_two
    assert get_persisted_caching_info(reader, server.url) == caching_info_two
    assert get_entry_count(reader, server.url) == 5

    # fifth update: 304, the changed validators are still in use
    server.set_response(status='304 Not Modified')
    assert reader.update_feed(server.url) is None
    _, headers, _ = server.request
    assert headers[b'If-None-Match'] == etag_two
    assert headers[b'If-Modified-Since'] == last_modified_two
    assert get_persisted_caching_info(reader, server.url) == caching_info_two
    assert get_entry_count(reader, server.url) == 5


@pytest.mark.slow
@pytest.mark.noscheduled
def test_conditional_requests_parse_error_keeps_state(reader, data_dir, server):
    """A failed update must not clobber the persisted validators nor
    the already stored entries; the next request re-sends the old validators.

    """
    etag = b'"00000-67890abcdef12"'
    last_modified = b'Thu, 1 Jan 2020 00:00:00 GMT'
    caching_info = {
        'etag': etag.decode(),
        'last-modified': last_modified.decode(),
    }

    reader.add_feed(server.url)

    server.set_response(
        data_dir.joinpath('full.atom').read_text(),
        ETag=etag,
        Last_Modified=last_modified,
    )
    assert reader.update_feed(server.url).new == 5
    assert get_persisted_caching_info(reader, server.url) == caching_info

    # the server responds 200 with a body that cannot be parsed
    # and new validators; the update fails
    server.set_response(
        b'this is not a feed',
        ETag=b'"11111-67890abcdef12"',
        Last_Modified=b'Thu, 1 Jan 2020 11:11:11 GMT',
    )
    with pytest.raises(ParseError):
        reader.update_feed(server.url)
    _, headers, _ = server.request
    assert headers[b'If-None-Match'] == etag
    assert headers[b'If-Modified-Since'] == last_modified

    # the old validators and the stored entries are untouched
    assert get_persisted_caching_info(reader, server.url) == caching_info
    assert get_entry_count(reader, server.url) == 5
    assert reader.get_feed(server.url).last_exception is not None

    # the next request re-sends the old validators, not the new ones
    server.set_response(status='304 Not Modified')
    assert reader.update_feed(server.url) is None
    _, headers, _ = server.request
    assert headers[b'If-None-Match'] == etag
    assert headers[b'If-Modified-Since'] == last_modified
    assert get_persisted_caching_info(reader, server.url) == caching_info
    assert get_entry_count(reader, server.url) == 5


@pytest.mark.slow
@pytest.mark.noscheduled
def test_conditional_requests_storage_error_keeps_state(
    reader, data_dir, server, monkeypatch
):
    """If storing the update fails, the old validators are still sent
    on the next request, and entries already saved are de-duplicated
    when the server re-sends them.

    """
    etag_one = b'"00000-67890abcdef12"'
    last_modified_one = b'Thu, 1 Jan 2020 00:00:00 GMT'
    caching_info_one = {
        'etag': etag_one.decode(),
        'last-modified': last_modified_one.decode(),
    }
    etag_two = b'"11111-67890abcdef12"'
    last_modified_two = b'Thu, 1 Jan 2020 11:11:11 GMT'
    caching_info_two = {
        'etag': etag_two.decode(),
        'last-modified': last_modified_two.decode(),
    }

    reader.add_feed(server.url)

    server.set_response(
        data_dir.joinpath('full.atom').read_text(),
        ETag=etag_one,
        Last_Modified=last_modified_one,
    )
    assert reader.update_feed(server.url).new == 5
    assert get_persisted_caching_info(reader, server.url) == caching_info_one

    # the feed gets a new entry and new validators,
    # but storing the feed update fails
    new_entry = """
    <entry>
        <title>New Entry</title>
        <id>urn:uuid:new-entry</id>
        <updated>2003-12-14T00:00:00Z</updated>
    </entry>
"""
    full_atom = data_dir.joinpath('full.atom').read_text()
    server.set_response(
        full_atom.replace('</feed>', new_entry + '</feed>'),
        ETag=etag_two,
        Last_Modified=last_modified_two,
    )

    def failing_update_feed(intent):
        raise StorageError('simulated storage failure')

    monkeypatch.setattr(reader._storage, 'update_feed', failing_update_feed)
    with pytest.raises(StorageError):
        reader.update_feed(server.url)
    monkeypatch.undo()

    # the failed request carried the old validators
    _, headers, _ = server.request
    assert headers[b'If-None-Match'] == etag_one
    assert headers[b'If-Modified-Since'] == last_modified_one

    # the old validators are still persisted;
    # entries are stored before the feed update, so the new entry
    # made it to storage even though the update failed
    assert get_persisted_caching_info(reader, server.url) == caching_info_one
    assert get_entry_count(reader, server.url) == 6

    # the retry re-sends the old validators, and the already stored
    # entry is de-duplicated, not added again
    assert reader.update_feed(server.url).new == 0
    _, headers, _ = server.request
    assert headers[b'If-None-Match'] == etag_one
    assert headers[b'If-Modified-Since'] == last_modified_one
    assert get_entry_count(reader, server.url) == 6

    # the stored content is already up to date with the response,
    # so the new validators are not persisted yet
    assert get_persisted_caching_info(reader, server.url) == caching_info_one

    # once the feed content changes again,
    # the new validators finally get persisted
    another_entry = """
    <entry>
        <title>Another New Entry</title>
        <id>urn:uuid:another-new-entry</id>
        <updated>2003-12-15T00:00:00Z</updated>
    </entry>
"""
    server.set_response(
        full_atom.replace('</feed>', new_entry + another_entry + '</feed>'),
        ETag=etag_two,
        Last_Modified=last_modified_two,
    )
    assert reader.update_feed(server.url).new == 1
    _, headers, _ = server.request
    assert headers[b'If-None-Match'] == etag_one
    assert headers[b'If-Modified-Since'] == last_modified_one
    assert get_persisted_caching_info(reader, server.url) == caching_info_two
    assert get_entry_count(reader, server.url) == 7


@pytest.mark.slow
@pytest.mark.noscheduled
def test_conditional_requests_feeds_do_not_share_validators(
    reader, data_dir, server_factory
):
    """Concurrent updates of different feeds must not mix up
    their ETag / Last-Modified validators.

    """
    server_one = server_factory()
    server_two = server_factory()

    etag_one = b'"00000-67890abcdef12"'
    last_modified_one = b'Thu, 1 Jan 2020 00:00:00 GMT'
    etag_two = b'"11111-67890abcdef12"'
    last_modified_two = b'Thu, 1 Jan 2020 11:11:11 GMT'

    reader.add_feed(server_one.url)
    reader.add_feed(server_two.url)

    server_one.set_response(
        data_dir.joinpath('full.atom').read_text(),
        ETag=etag_one,
        Last_Modified=last_modified_one,
    )
    server_two.set_response(
        data_dir.joinpath('empty.atom').read_text(),
        ETag=etag_two,
        Last_Modified=last_modified_two,
    )

    results = dict(reader.update_feeds_iter(workers=2))
    assert results[server_one.url].new == 5
    assert results[server_two.url].new == 1
    assert get_entry_count(reader, server_one.url) == 5
    assert get_entry_count(reader, server_two.url) == 1

    _, headers_one, _ = server_one.request
    _, headers_two, _ = server_two.request
    assert b'If-None-Match' not in headers_one
    assert b'If-None-Match' not in headers_two

    # each feed persists its own validators
    assert get_persisted_caching_info(reader, server_one.url) == {
        'etag': etag_one.decode(),
        'last-modified': last_modified_one.decode(),
    }
    assert get_persisted_caching_info(reader, server_two.url) == {
        'etag': etag_two.decode(),
        'last-modified': last_modified_two.decode(),
    }

    # on the next round of concurrent updates,
    # each feed sends back its own validators
    server_one.set_response(status='304 Not Modified')
    server_two.set_response(status='304 Not Modified')

    results = dict(reader.update_feeds_iter(workers=2))
    assert results[server_one.url] is None
    assert results[server_two.url] is None

    _, headers_one, _ = server_one.request
    _, headers_two, _ = server_two.request
    assert headers_one[b'If-None-Match'] == etag_one
    assert headers_one[b'If-Modified-Since'] == last_modified_one
    assert headers_two[b'If-None-Match'] == etag_two
    assert headers_two[b'If-Modified-Since'] == last_modified_two
