"""Offline tests of the pinned crawler's actual broker, proxy, and Chromium.

Run inside the crawler image with --network none. One named public-site fixture
is mapped to an in-container HTTP server; all private targets use the real rule.
"""

import asyncio
import os
import socket
import sys
import unittest
from unittest.mock import AsyncMock, patch

sys.path.insert(0, '/app')
import egress_broker as broker
from egress_proxy import PinningProxy
from playwright.async_api import async_playwright


class AddressTests(unittest.TestCase):
    def test_private_metadata_and_transition_addresses_are_blocked(self):
        for host in ('127.0.0.1', '10.1.2.3', '169.254.169.254', '[::1]', '[::]',
                     '[::ffff:127.0.0.1]', '[64:ff9b::a00:1]', '[2002:7f00:1::]'):
            with self.subTest(host=host), self.assertRaises(broker.EgressBlocked):
                broker.resolve_and_pin(f'http://{host}/')


class ProxyTests(unittest.IsolatedAsyncioTestCase):
    async def test_dns_answer_is_pinned_before_connect(self):
        answers = [[(socket.AF_INET, socket.SOCK_STREAM, 6, '', ('93.184.216.34', 443))],
                   [(socket.AF_INET, socket.SOCK_STREAM, 6, '', ('127.0.0.1', 443))]]
        with patch.dict(os.environ, {}, clear=True), patch.object(socket, 'getaddrinfo', side_effect=answers) as dns:
            target = broker.resolve_and_pin('https://rebind.test/')
            connect = AsyncMock(return_value=(object(), object()))
            with patch('asyncio.open_connection', connect):
                await PinningProxy()._dial(target, 443)
            connect.assert_awaited_once_with('93.184.216.34', 443)
            self.assertEqual(dns.call_count, 1)
            with self.assertRaises(broker.EgressBlocked):
                broker.resolve_and_pin('https://rebind.test/')

    async def test_browser_redirect_and_subresources_cannot_reach_private_server(self):
        hits = []

        async def origin(reader, writer):
            try:
                request = await reader.readuntil(b'\r\n\r\n')
                path = request.split(b' ')[1].decode()
                hits.append(path)
                if path == '/redirect':
                    response = f'HTTP/1.1 302 Found\r\nLocation: http://127.0.0.1:{port}/private-redirect\r\nContent-Length: 0\r\nConnection: close\r\n\r\n'.encode()
                else:
                    body = (f'<html><body>fixture<script>document.body.dataset.rendered="yes";</script>'
                            f'<img src="http://127.0.0.1:{port}/private-image">'
                            f'<iframe src="http://fixture.test:{port}/redirect"></iframe></body></html>').encode()
                    response = f'HTTP/1.1 200 OK\r\nContent-Type: text/html\r\nContent-Length: {len(body)}\r\nConnection: close\r\n\r\n'.encode() + body
                writer.write(response)
                await writer.drain()
            finally:
                writer.close()
                await writer.wait_closed()

        server = await asyncio.start_server(origin, '127.0.0.1', 0)
        port = server.sockets[0].getsockname()[1]
        real_resolve = broker.resolve_and_pin

        def fixture_resolve(url):
            if url == f'http://fixture.test:{port}':
                return broker.PinnedTarget('http', 'fixture.test', port, '127.0.0.1')
            return real_resolve(url)

        proxy = PinningProxy()
        try:
            with patch.dict(os.environ, {}, clear=True), patch('egress_proxy.resolve_and_pin', side_effect=fixture_resolve):
                await proxy.start()
                async with async_playwright() as playwright:
                    browser = await playwright.chromium.launch(headless=True, proxy={'server': proxy.url},
                                                               args=['--no-sandbox', '--disable-dev-shm-usage'])
                    try:
                        page = await browser.new_page()
                        await page.goto(f'http://fixture.test:{port}/page', wait_until='networkidle', timeout=15000)
                        self.assertEqual(await page.get_attribute('body', 'data-rendered'), 'yes')
                        self.assertIn('/page', hits)
                        self.assertIn('/redirect', hits)
                        self.assertFalse(any(path.startswith('/private') for path in hits), hits)
                    finally:
                        await browser.close()
        finally:
            await proxy.stop()
            server.close()
            await server.wait_closed()


if __name__ == '__main__':
    unittest.main(verbosity=2)
