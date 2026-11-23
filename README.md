# Flower Measurement Explorer

A local-first botanical research application with both a guided Python terminal
interface and an authenticated, responsive web workspace.

## Launch the web workspace

Requires Python 3.10 or newer.

```powershell
python -m pip install -r requirements.txt
$env:FLOWER_SECRET_KEY = python -c "import secrets; print(secrets.token_hex(32))"
python -m flower_web.app
```

Open <http://127.0.0.1:5000>. Registration creates an account; records and
account data are stored in `data/flower_explorer.sqlite3`. Set a stable
`FLOWER_SECRET_KEY` before launch to preserve sessions across restarts. The app
binds to localhost by default.

Password reset email requires SMTP configuration through
`FLOWER_SMTP_HOST`, `FLOWER_SMTP_PORT`, `FLOWER_SMTP_FROM`, and optionally
`FLOWER_SMTP_USERNAME` / `FLOWER_SMTP_PASSWORD`. Without SMTP, password resets
are not sent; change a password from Settings while signed in.

## Launch the terminal interface

```powershell
python flower_explorer.py
```

The CLI stores its 36 reproducible demonstration observations in
`data/flowers.json` and `data/flowers.csv`. It retains the required
menu-driven workflow, strict validation, persistence, search, summaries,
CSV import/export, report generation, and optional Matplotlib charts.

## Web workspace

- SQLite-backed account registration, salted scrypt password hashes,
  CSRF-protected forms, signed sessions, and user-scoped edits.
- Dashboard metrics use stored values; records support add, edit, delete,
  search, filtering, sorting, and pagination.
- Interactive Plotly charts with PNG downloads; Matplotlib generates images.
- Plotly's client bundle and licensed notice are included locally, so chart
  interactions do not require a CDN connection.
- K-means clustering, PCA, nearest-measurement similarity, and outlier review.
  Species labels are not used to cluster records or infer a classification.
  If a platform blocks scikit-learn's native extensions, deterministic NumPy
  clustering/PCA and robust-distance review keep exploration available and are
  identified as the fallback method in the interface.
- CSV/JSON upload preview and row-level validation, with CSV/JSON and
  Markdown/TXT executive report exports.
- Account-specific JSON backup, dataset/import history, data quality checks,
  and responsive desktop/mobile layouts.

The web database starts with the 150-record Fisher Iris dataset in
`data/iris.csv` and `data/iris.json` (UCI Machine Learning Repository,
[dataset 53](https://archive.ics.uci.edu/dataset/53/iris)). The original CLI
demo files are kept separate. The web app shares measurement validation with
the CLI and imports the same record schema; the separate SQLite store provides
user-specific access controls.

The web interface uses locally stored botanical photographs, including Iris,
orchid, sunflower, tulip, and lavender images. Image credits and licenses are
listed in `static/assets/ATTRIBUTION.txt`. Full-screen, page-specific
photographic backgrounds are bundled in `static/assets/backgrounds/`; flower
photographs are in `static/assets/flowers/`. The images are decorative;
measurement analytics use only stored flower data. The bundled Plotly.js
license notice is in `static/vendor/PLOTLY-LICENSE.txt`.

## Tests

```powershell
python -m unittest discover -s tests -v
```
