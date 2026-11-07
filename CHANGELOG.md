# Changelog

## Unreleased

- Admin page: **Connect to this service** card with the addresses other services can use (this page's address, same PC, Docker network, `host.docker.internal`) and every endpoint, each with a Copy button.
- Admin **Try it** panel sends real public API requests with a requester username, readable status/errors, latency, cancellation, media/text results and redacted curl. Auth choice (b): the public token stays in memory, separate from admin auth; existing quotas, ownership and caches apply.
