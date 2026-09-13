# Frontend

The student-facing application is a React 19 and Leaflet client built with Vite. It consumes the FastAPI `/api` contract and contains no scraper or database credentials.

From this directory:

```powershell
npm ci
npm run dev
```

During development, Vite proxies `/api` to `http://127.0.0.1:8000`.

Available checks:

```powershell
npm test
npm run lint
npm run build
```

Runtime display configuration is read from optional `VITE_*` variables in `src/config.js`. Keep secrets server-side; Vite variables are bundled into browser code.
