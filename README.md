# Gilad Markets

A public dashboard of realized returns on Gilad’s Polymarket trading account. The latest-method view follows the highest numbered `SuccessfulN` Git tag, resolves its commit through GitHub, and matches the exact source commit against deployment intervals in the live ledger. The static site is served from `docs/` on GitHub Pages.

Each row groups the outcomes held in a single market. Markets with any remaining outcome of 0.01 shares or more are excluded. Dust-only markets count as closed. P&L is the sum of the provider’s realized amounts across all held outcomes. All-time account figures include manual activity. Entry attribution uses confirmed purchases from the bot ledger and each outcome’s lifetime purchase size. The latest release also requires the originating pairs and position entries to start after activation.

The browser receives a snapshot of public market names, closure times and results. The host-side exporter opens SQLite read-only and reads both public position views. The publisher checks every 15 minutes, updates only the snapshot when results change, and refreshes its timestamp at least every six hours while the host and data services are available. GitHub Pages may take a few minutes to serve a new version.
