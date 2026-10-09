# tgobjects3 Console

Admin console SPA for the tgobjects3 S3-compatible gateway. React + TypeScript +
Vite, with TanStack Query for data fetching and Radix UI primitives.

## Development

```bash
npm install
npm run dev        # Vite dev server on :5173, proxies /admin → gateway :8080
```

Point the proxy at a non-default gateway with `ADMIN_API_URL`:

```bash
ADMIN_API_URL=http://localhost:18080 npm run dev
```

## Scripts

| Script              | Purpose                                        |
| ------------------- | ---------------------------------------------- |
| `npm run dev`       | Start the Vite dev server with the API proxy   |
| `npm run build`     | Type-check then build static assets to `dist/` |
| `npm run preview`   | Preview the production build locally           |
| `npm run lint`      | ESLint + Prettier check                        |
| `npm run typecheck` | `tsc --noEmit` strict type check               |

## Build output

`npm run build` emits static files into `dist/`, served by nginx in `deploy/`
(referenced by `deploy/docker-compose.yml` as `deploy/Dockerfile.console`).

The actual screens (Buckets, Objects, Jobs, Recovery, Settings) are stubs here
and are implemented in task 44.
