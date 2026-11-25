# mailbox

Mailbox manager: Python 3 backend (standard library only, keep it Python 3.8 compatible) + Vue 3 / TypeScript dashboard. Mailboxes come from our own catch-all domains or from external sources (addy.io, Duck) whose aliases forward to a collector address on one of our domains.

## Commands

- Backend tests: `cd backend && python3 -m unittest test_server`
- Backend run: `cd backend && MAIL_DOMAINS=a.com,b.com API_KEY=... SMTP_PORT=25 HTTP_PORT=8080 python3 server.py`
- Frontend install: `cd frontend && yarn`
- Frontend dev (proxies `/api` to `127.0.0.1:8080`): `yarn dev`
- Frontend typecheck + build: `yarn build` (output in `frontend/dist`, served by the backend at `/`)

## Notes

- Storage is SQLite (`backend/data/mail.db`). Schema changes must upgrade existing databases in `Store.__init__` (`add_column`). Databases from before `SCHEMA_VERSION` 2 are renamed aside at startup, not converted.
- Backend modules import each other by plain name (`import store`), so run and test from `backend/`.
- Tests never call addy.io or Duck: `fake_sources` in `test_server.py` stands in for both.
- `MAIL_DOMAINS` only seeds the domain table on first start; afterwards domains are managed in the dashboard.

- Dependencies are pinned to exact versions; add new ones with `yarn add --exact`.
- The backend has no third-party dependencies; do not add any.
