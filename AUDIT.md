# Production audit — 2026-09-07

## Findings before changes

| Area | Finding | Severity |
|---|---|---|
| Payments | Quickpay URL could be opened, but no YooKassa API/webhook flow, remote payment lookup or provider payment ID existed. | Critical |
| Payments | `success` was a non-YooKassa final status; no `waiting_for_capture` or `canceled` state. | Critical |
| Payments | Legacy handler accepted an amount greater than or equal to the price, rather than the order's exact amount. | High |
| Database | No migration history, WAL/busy timeout, indexes, subscription start date, or provider/currency fields. | High |
| Security | No YooKassa webhook source verification or server-side re-query of a payment. | Critical |
| Security | API JSON had no typed body validation; Telegram `auth_date` could be arbitrarily in the future. | Medium |
| Bot | Proxy was hard-coded, no dispatcher error handler, and web server was exposed on all interfaces. | High |
| Deployment | No nginx/systemd/SSL configuration, health endpoint, restart policy, or secret-file guidance. | High |
| UX | Payment screen claimed automatic access but did not say that checkout return itself is not confirmation; no payment-method guidance. | Medium |
| Performance | SQLite had no WAL, timeout, or indexes for common payment/subscription reads. | Medium |

## Change record (old → new)

The complete line-level old/new diff is available with `git diff f5a4088..HEAD` after committing, or `git diff f5a4088` now.

| File | Old | New / reason |
|---|---|---|
| `main.py` | `Quickpay(...)` was the only `buy` flow; proxy was `socks5://127.0.0.1:10808`; uvicorn used `0.0.0.0`. | `buy` reserves an order, creates a YooKassa API payment and stores its provider ID; proxy is `PROXY_URL`; server binds `127.0.0.1`; structured logging and a dispatcher error handler were added. `buy_legacy` remains only behind `ENABLE_LEGACY_YOOMONEY`. |
| `app/yookassa.py` | Absent. | New async HTTPS client for official YooKassa v3: Basic auth, idempotence key, `redirect` confirmation, `capture=true`, and `GET /payments/{id}` verification. |
| `app/web.py` | Only `/api/yoomoney/notification`; payment status was trusted from its form payload. | New `POST /api/payment/webhook`: source allowlist, event consistency check, server-side YooKassa re-fetch, exact amount/currency/user/order check, then idempotent activation. Typed Mini App requests, safe error responses and `/healthz` added. |
| `app/database.py` | `payments(id, user_id, amount, status, operation_id)` and a non-idempotent-for-YooKassa model. | Migration-compatible columns for provider/payment ID/currency and subscription start; WAL, timeout and indexes; status transition set; atomic `BEGIN IMMEDIATE` activation. An active subscription extends from its end, otherwise starts now. |
| `app/security.py` | Checked Telegram signature and only an old YooMoney signature. | Rejects implausible future Telegram auth dates and adds the official YooKassa IP networks. YooKassa data is also verified by API lookup because webhook requests have no shared secret signature. |
| `app/config.py`, `.env.example` | Required legacy wallet variables and allowed any public URL. | Requires HTTPS public URL and YooKassa shop credentials; adds price/days/proxy/trusted-proxy/legacy migration flags. |
| `requirements.txt` | No HTTP client for provider API. | Adds `httpx`. |
| `deploy/nginx.conf`, `deploy/pathology-bot.service` | Absent. | New TLS reverse proxy and hardened, auto-restarting systemd service. |
| `README.md`, `.gitignore` | Legacy-only operational instructions. | YooKassa setup, installation, deployment and acceptance checklist; ignores venv and SQLite WAL sidecars. |

## Validation performed

`python3 -m compileall -q main.py app` and `git diff --check` pass. Runtime/integration tests could not run: the host has neither project packages nor `pip`, and `python3 -m venv` fails because the Ubuntu `python3.14-venv` package is missing. Installation needs an interactive `sudo` password.

## Mini App CMS update — 2026-09-12

Implemented the requested category → material → text/photo gallery workflow. SQLite migration 2 adds only content tables and indexes; each connection enables foreign keys for cascading content deletion. Admin IDs come exclusively from ENV, avoiding a second permissions source. The YooKassa provider client and payment activation logic are retained.

Added authenticated content CRUD, bounded image uploads, image decoding/normalization with metadata removal, private disk storage, server-rendered personalized watermarks, protected Canvas delivery, per-user image rate limiting, and no-store/security response headers. Deleting content revokes API access and removes its image files; failed image DB inserts roll back the saved file. Administrators can preview, edit and delete content without paying for a subscription.

Updated `/start`, added `/menu`, `/id`, course information and Telegram menu configuration, and renamed the checkout button to “Перейти к оплате”. The Mini App displays configured pricing, subscription expiry, and empty/error states. It hides on loss of activity and revalidates access on return; editor drafts survive focus changes. Nginx now accepts image uploads and permits Telegram Web embedding. The backup service has its missing working directory set; full media backup requirements are documented separately because the existing timer backs up only SQLite.

Security boundary: subscription checks and permissions are enforced by the server. Canvas, selection/printing restrictions and inactivity hiding only deter casual copying. Telegram Mini Apps provide no documented native screenshot prohibition. Authorized clients can still capture received data. Image watermarks are baked into each authenticated response, not removable DOM overlays.

Validation: runtime dependencies installed in local `.venv`; API/database tests cover migration preservation, authorization, content CRUD, image limits/cleanup/watermarks, subscription revocation and repeated YooKassa webhooks. A browser acceptance test uses the existing Firefox/geckodriver with a local test server and signed test Telegram data. No production database, Telegram account or YooKassa shop is touched. Device-specific Telegram behavior and a real provider payment remain deployment acceptance checks.

Final results: **22 tests passed**, including Firefox acceptance and bot menu/checkout tests; Python compilation, JavaScript syntax check and `git diff --check` passed. Two upstream Starlette test-client deprecation warnings remain; they do not affect the test results.
