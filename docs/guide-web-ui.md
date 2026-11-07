# Scribe: the admin web UI

With the service's admin password configured, open `http://127.0.0.1:29401/admin/` and sign in to manage status, model backends and settings. Public request authentication remains separate from that admin password.

## Connect to this service

The **Connect to this service** card lists the addresses other services can use: the address you opened the page with, the same PC (`127.0.0.1`), another Docker container on the same network, and a Docker container calling this PC (`host.docker.internal`), then every endpoint with its method and a **Copy** button. Calls need the internal token in `X-Internal-Token`; HTTP routes also need `X-Requester-Username`.

## Try it: send a real request

1. Sign in to `/admin/` and find **Try it**.
2. Paste a service token into the token field. This is the public client token, not the admin password. Use this tool service's configured internal API token and supply a requester username; do not use a model-backend token.
3. Choose an operation, complete its labeled fields and send it. Requests go to this service's public route, with the same token scope, rate/admission limits and cache as a separate client.
4. Read the HTTP status, elapsed time and response. NDJSON/SSE text appears while it arrives; JSON is formatted. HTTP errors, including 401, 422, 429 and 503, keep their response body visible. Use **Cancel** to abort an active request.
5. Copy the curl equivalent to repeat the request elsewhere. The token is always a placeholder; fill it locally. For an upload, provide the indicated input file. Multi-step operations show their follow-up request too.

Authentication choice is **(b), a pasted public token held only in memory**. The playground does not persist it in browser storage, put it in a URL, log it or include it in curl. Sign out or reset the panel to clear it. The existing admin sign-in is separate. A playground run consumes normal service quotas and can generate retained files where the public API normally does so; cancelling the browser fetch does not promise to undo work already accepted by a backend.

The browser caps uploads at **10 MiB**, displayed text at **256 KiB**, and binary audio/image responses at **32 MiB**. Server limits still apply and may be lower. Media uses bounded Blob URLs, revoked when replaced or cleared. Text is rendered as text, never executed as HTML. Audio players become available after the bounded audio download finishes. No new environment setting is needed.

### This service's request

Enter the requester username and upload encoded audio. The panel posts its bytes to `POST /api/transcribe` with `X-Requester-Username` and `X-Filename`, then displays the JSON transcript. Scribe keeps its existing ffmpeg decoding, duration/byte caps, per-user quota, cached transcription and retention behavior. This upload playground does not start a live dictation WebSocket.

The public token is sent only in `X-Internal-Token`. The requester username is sent in `X-Requester-Username` and determines ownership and quota. Admin requests continue to use `X-Admin-Password`. The shared tool page learns its service-specific playground descriptor from authenticated admin status metadata. Deploy the complete `web/` directory, including `playground.js`.
