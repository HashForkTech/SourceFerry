"""Regression tests for request boundaries, capacity, and bounded processing."""

import asyncio
from contextlib import asynccontextmanager
import time
import threading
import unittest
from unittest.mock import patch

import httpx
from test_gateway import AUTH, REAL_ASYNC_CLIENT, gateway, no_upstream, offline_client, results
from gateway.middleware import RequestGuard


@asynccontextmanager
async def async_client(handler):
    def factory(*args, **kwargs):
        kwargs['transport'] = httpx.MockTransport(handler)
        return REAL_ASYNC_CLIENT(*args, **kwargs)
    with patch.object(gateway.httpx, 'AsyncClient', side_effect=factory):
        async with gateway.lifespan(gateway.app):
            async with REAL_ASYNC_CLIENT(transport=httpx.ASGITransport(app=gateway.app), base_url='http://test') as client:
                yield client


class RequestLimitsTests(unittest.TestCase):
    def test_unauthorized_body_is_not_validated_or_reflected(self):
        with offline_client(no_upstream) as client:
            for body in (b'{broken', b'x' * (2 * 1024 * 1024)):
                response = client.post('/search', content=body)
                self.assertEqual(response.status_code, 401)
                self.assertLess(len(response.content), 200)

    def test_authenticated_oversize_and_invalid_fields(self):
        with offline_client(no_upstream) as client:
            self.assertEqual(client.post('/fetch', headers=AUTH, content=b'x' * 65537).status_code, 413)
            for path, body in [('/search', {'queries': ['x' * 2049]}),
                               ('/search', {'queries': ['q'] * 17}),
                               ('/fetch', {'url': 'https://test/' + 'x' * 8192})]:
                response = client.post(path, headers=AUTH, json=body)
                self.assertEqual(response.status_code, 422)
                self.assertNotIn('input', response.json()['detail'][0])
                self.assertLess(len(response.content), 400)

    def test_malformed_json_and_compressed_bodies(self):
        with offline_client(no_upstream) as client:
            response = client.post('/search', headers={**AUTH, 'Content-Type': 'application/json'}, content=b'{bad')
            self.assertEqual(response.status_code, 422)
            self.assertNotIn('{bad', response.text)
            self.assertEqual(client.post('/fetch', headers={**AUTH, 'Content-Encoding': 'gzip'}, content=b'x').status_code, 415)

    def test_upstream_size_limit_fallback_and_fetch_error(self):
        def handler(request):
            if request.url.path == '/search':
                return httpx.Response(200, json={'results': results(1)})
            return httpx.Response(200, content=b'x' * 2000)
        with patch.object(gateway, 'MAX_UPSTREAM_BYTES', 1024), offline_client(handler) as client:
            self.assertEqual(client.post('/fetch', headers=AUTH, json={'url': 'https://example.test'}).status_code, 502)
            response = client.post('/search', headers=AUTH, json={'queries': ['q']})
            self.assertEqual(response.json()['sources'][0]['snippet'], 'Original snippet 0')

    def test_cleanup_of_large_heading_documents_is_bounded(self):
        source = '\n'.join(f'# Heading {index}' for index in range(20000))
        started = time.monotonic()
        snippet = gateway.markdown_to_snippet(source)
        self.assertLessEqual(len(snippet), gateway.SNIPPET_MAX_CHARS)
        # Generous regression bound; the old suffix-copy path took seconds.
        self.assertLess(time.monotonic() - started, 1.0)


class AsyncRequestTests(unittest.IsolatedAsyncioTestCase):
    async def test_snippet_work_does_not_block_the_event_loop(self):
        started = threading.Event()
        release = threading.Event()

        def slow_cleanup(url, markdown):
            started.set()
            release.wait(timeout=2)
            return 'cleaned'

        def handler(request):
            if request.url.host == 'searxng':
                return httpx.Response(200, json={'results': results(1)})
            return httpx.Response(200, json={'success': True, 'markdown': '# content'})

        async with async_client(handler) as client, asyncio.timeout(5):
            with patch.object(gateway, 'page_snippet', side_effect=slow_cleanup):
                search = asyncio.create_task(client.post('/search', headers=AUTH,
                                                        json={'queries': ['topic'], 'max_results': 1}))
                try:
                    while not started.is_set():
                        await asyncio.sleep(0.01)
                    self.assertFalse(search.done(), 'Snippet processing blocked the event loop')
                    health = await asyncio.wait_for(client.get('/health'), timeout=0.5)
                    self.assertEqual(health.status_code, 200)
                finally:
                    release.set()
                    await search

    async def test_chunked_limit_and_authentication_without_reading(self):
        called = False
        async def downstream(scope, receive, send):
            nonlocal called
            called = True
        guard = RequestGuard(downstream, authenticate=gateway.check_auth, max_bytes=20, max_requests=1, deadline=1)
        scope = {'type': 'http', 'path': '/search', 'headers': []}
        async def forbidden_receive():
            raise AssertionError('Unauthenticated body was read')
        messages = []
        async def send(message):
            messages.append(message)
        await guard(scope, forbidden_receive, send)
        self.assertEqual(messages[0]['status'], 401)
        scope['headers'] = [(b'authorization', AUTH['Authorization'].encode())]
        chunks = iter([{'type': 'http.request', 'body': b'x' * 15, 'more_body': True},
                       {'type': 'http.request', 'body': b'x' * 15, 'more_body': False}])
        async def receive():
            return next(chunks)
        messages.clear()
        await guard(scope, receive, send)
        self.assertEqual(messages[0]['status'], 413)
        self.assertFalse(called)
        self.assertEqual(guard.active, 0)

    async def test_mixed_searches_and_fetches_share_crawl_limit(self):
        active = peak = 0
        async def handler(request):
            nonlocal active, peak
            if request.url.path == '/search':
                return httpx.Response(200, json={'results': results(4)})
            active += 1
            peak = max(peak, active)
            try:
                await asyncio.sleep(0.015)
                return httpx.Response(200, json={'success': True, 'markdown': 'Good content.'})
            finally:
                active -= 1
        async with async_client(handler) as client:
            responses = await asyncio.gather(
                *(client.post('/search', headers=AUTH, json={'queries': ['q'], 'max_results': 4}) for _ in range(2)),
                *(client.post('/fetch', headers=AUTH, json={'url': 'https://example.test'}) for _ in range(4)),
            )
        self.assertTrue(all(response.status_code == 200 for response in responses))
        self.assertEqual(peak, 4)
        self.assertEqual(active, 0)

    async def test_admission_and_deadline_release_capacity(self):
        entered = asyncio.Event()
        async def downstream(scope, receive, send):
            entered.set()
            await asyncio.sleep(10)
        guard = RequestGuard(downstream, authenticate=lambda value: None, max_bytes=20, max_requests=1, deadline=0.05)
        scope = {'type': 'http', 'path': '/fetch', 'headers': []}
        async def receive():
            return {'type': 'http.request', 'body': b'{}'}
        first, second = [], []
        async def send_first(message):
            first.append(message)
        async def send_second(message):
            second.append(message)
        task = asyncio.create_task(guard(scope, receive, send_first))
        await entered.wait()
        await guard(scope, receive, send_second)
        await task
        self.assertEqual(second[0]['status'], 503)
        self.assertEqual(first[0]['status'], 504)
        self.assertEqual(guard.active, 0)

    async def test_upstream_stream_limit_without_content_length(self):
        class LargeStream(httpx.AsyncByteStream):
            async def __aiter__(self):
                yield b' ' * 800
                yield b' ' * 800
        def handler(request):
            return httpx.Response(200, stream=LargeStream())
        async with async_client(handler) as client, self._small_upstream():
            response = await client.post('/fetch', headers=AUTH, json={'url': 'https://example.test'})
        self.assertEqual(response.status_code, 502)

    @asynccontextmanager
    async def _small_upstream(self):
        with patch.object(gateway, 'MAX_UPSTREAM_BYTES', 1024):
            yield

    async def test_readiness_reports_backend_failure_and_caches(self):
        calls = []
        def handler(request):
            calls.append(str(request.url))
            return httpx.Response(503)
        async with async_client(handler) as client:
            self.assertEqual((await client.get('/health')).status_code, 200)
            self.assertEqual((await client.get('/ready')).status_code, 503)
            count = len(calls)
            self.assertEqual((await client.get('/ready')).status_code, 503)
            self.assertEqual(len(calls), count)
