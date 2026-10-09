# SourceFerry third-party notices

SourceFerry's original code and documentation are licensed under the [MIT License](LICENSE). That license does not replace the licenses of the third-party software described here.

The deployment uses the official SearXNG and Crawl4AI container images pinned by digest in `.env.example`. Their source code is available from the projects below. The license copies in `third_party/` reproduce the upstream license files for these pinned versions without changes.

## SearXNG

Credit: SearXNG contributors.

- Project: https://github.com/searxng/searxng
- Image release: `2026.10.7-6671d89be`
- Source revision: [6671d89bede8c9fc108b17bb98916170f5657650](https://github.com/searxng/searxng/tree/6671d89bede8c9fc108b17bb98916170f5657650)
- License: GNU Affero General Public License v3.0 or later (`AGPL-3.0-or-later`, as declared in the upstream README's SPDX identifier).
- License copy: [third_party/SEARXNG-LICENSE.txt](third_party/SEARXNG-LICENSE.txt)
- [Upstream license](https://github.com/searxng/searxng/blob/6671d89bede8c9fc108b17bb98916170f5657650/LICENSE)

## Crawl4AI

Credit: UncleCode and Crawl4AI contributors.

- Project: https://github.com/unclecode/crawl4ai
- Image/source release: [v0.9.2](https://github.com/unclecode/crawl4ai/tree/v0.9.2)
- License: Apache License 2.0 with the attribution requirement appended in the upstream `LICENSE` file. The complete file, including that requirement, is retained here.
- License copy: [third_party/CRAWL4AI-LICENSE.txt](third_party/CRAWL4AI-LICENSE.txt)
- [Upstream license](https://github.com/unclecode/crawl4ai/blob/v0.9.2/LICENSE)

Required upstream attribution:

This product includes software developed by UncleCode (https://x.com/unclecode) as part of the Crawl4AI project (https://github.com/unclecode/crawl4ai).

These notices describe the named upstream projects. Dependencies inside their images retain their respective notices and licenses.
