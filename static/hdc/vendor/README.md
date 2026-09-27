# Self-hosted browser dependencies

Pinned npm packages, unmodified runtime assets, original licenses alongside:
- bootstrap 5.3.2 (MIT)
- chart.js 4.4.0 (MIT)
- @fortawesome/fontawesome-free 6.4.0 (see LICENSE)
- @fontsource/plus-jakarta-sans 5.2.8 (OFL), Latin normal weights 400–800

The public CDNs were unavailable during real-browser QA, breaking charts and
Bootstrap interactions. Local delivery removes that runtime/network dependency.
No build step or npm dependency is required for production. Update versions
intentionally and rerun offline asset and browser tests. Latin-extended glyphs
and Urdu use the existing system-font fallback.
