# admin-ui

The admin web UI for quota management on the Bedrock pilot
account.

`admin-ui/web/` is a Vite/React SPA served by the FastAPI api
container at `/`. Admins reach it over the browser via Cognito
login or OIDC/Okta.

> **Note (#576):** the Python `tg-admin` shiv desktop binary
> (`admin-ui/src/`, `build.sh`, `pyproject.toml`) was **removed**.
> The desktop SigV4 client is no longer a supported entry path
> (#574 park → #576 delete); the web login is the admin entry.
> The archived desktop code lives at the `desktop-admin-archived`
> git tag.

## Run from source (dev)

```bash
cd admin-ui/web
npm install
npm run dev   # vite dev server on :5173, proxies /api to the api
```

## Build the SPA

```bash
cd admin-ui/web
npm run build   # emits dist/ — copied into container/static/
```

The built bundle is served by the api container (see
`container/static/`). The two-surface refresh (api container +
its baked copy) is documented where the SPA is consumed.
