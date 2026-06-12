# CLSS Download CLI

Small Python CLI for resolving CLSS tiles and downloading products from the official public asset portal at `https://clss.si/`.

The tool reads the official tile index from `data/clss_mreza_2325.gpkg` and can:

- resolve tiles from an AOI vector file
- resolve tiles from one tile name or a list of tile names
- download CLSS products with the required HTTP headers

## Files in This Repo

- `clss_download.py`: the CLI
- `.clss_headers.env.example`: sample local header config
- `environment.yml`: conda/mamba environment
- `data/clss_mreza_2325.gpkg`: official CLSS tile grid / tile metadata
- `data/Prispevna_povrsina_Cerknisko.geojson`: example AOI

## Quick Start

Create the environment:

```bash
mamba env create -f environment.yml
conda activate clss-download
```

Create a local headers file:

```powershell
Copy-Item .clss_headers.env.example .clss_headers.env
```

Edit `.clss_headers.env` and fill in:

```dotenv
CLSS_REFERER=...
CLSS_USER_AGENT=...
```

Check which tiles intersect the example AOI:

```bash
python clss_download.py tiles --area data/Prispevna_povrsina_Cerknisko.geojson
```

Preview a download without downloading:

```bash
python clss_download.py download --tile 451_111 --product dmr --dry-run --semi-flat
```

Run a real download:

```bash
python clss_download.py download --tile 451_111 --product dmr
```

During real downloads, the CLI shows:

- a `Files` progress bar for completed files
- a `Bytes` progress bar for transferred download volume
- if your terminal does not render `tqdm` well, use `--progress plain`

## Requirements and Setup

- Python 3.10+
- `requests`
- `tqdm`

Preferred setup with `mamba`:

```bash
mamba env create -f environment.yml
conda activate clss-download
```

If you already created this environment before `tqdm` was added, update it with:

```bash
mamba env update -f environment.yml --prune
```

With `conda` only:

```bash
conda env create -f environment.yml
conda activate clss-download
```

For an existing conda environment:

```bash
conda env update -f environment.yml --prune
```

Minimal `pip` setup:

```bash
pip install requests tqdm
```

## CLSS Source Notes

Official sources:

- `https://clss.si/`
- `https://assets.flycom.si/clss/navodila_pregledovalnik_clss.pdf`

The downloader requires request headers, but they are not hardcoded in the repository or code.

You can provide them in any of these ways:

- CLI flags: `--referer` and `--user-agent`
- environment variables: `CLSS_REFERER` and `CLSS_USER_AGENT`
- local file: `.clss_headers.env`

Precedence is:

- CLI flags
- environment variables
- `.clss_headers.env`

Recommended option: keep them in a local untracked file named `.clss_headers.env`.

Example:

```dotenv
CLSS_REFERER=...
CLSS_USER_AGENT=...
```

The real `.clss_headers.env` file is ignored by git. A template is included as `.clss_headers.env.example`.

If you prefer environment variables in PowerShell:

```powershell
$env:CLSS_REFERER="..."
$env:CLSS_USER_AGENT="..."
```

## Supported Inputs

### AOI / area file

Supported vector inputs:

- GeoJSON: `.geojson`, `.json`
- GeoPackage: `.gpkg`

Only `Polygon` and `MultiPolygon` geometries are supported.

The CLSS tile index is in `EPSG:3794`, so the AOI must also be in `EPSG:3794`.

If a GeoJSON file does not declare its CRS, use:

```bash
--assume-srid 3794
```

### Tile names

Examples:

- `450_124`
- `451_124`
- `450_123`
- `451_123`

You can pass tiles:

- with repeated `--tile`
- as a comma-separated list
- from a text file with `--tiles-file`

## Common Workflows

Resolve tiles from an AOI:

```bash
python clss_download.py tiles --area data/Prispevna_povrsina_Cerknisko.geojson
```

Resolve tiles from explicit tile names:

```bash
python clss_download.py tiles --tile 450_124 --tile 451_124 --tile 450_123,451_123
```

Download GKOT for a few tiles:

```bash
python clss_download.py download --tile 450_124 --tile 451_124 --tile 450_123 --tile 451_123 --product gkot
```

Download multiple products for an AOI:

```bash
python clss_download.py download --area data/Prispevna_povrsina_Cerknisko.geojson --product dmr,dmp --out-dir downloads
```

Download from a text file of tile names:

```bash
python clss_download.py download --tiles-file my_tiles.txt --product gkot
```

## Usage

Show help:

```bash
python clss_download.py --help
```

List tiles from an AOI:

```bash
python clss_download.py tiles --area data/Prispevna_povrsina_Cerknisko.geojson
```

List tiles as JSON:

```bash
python clss_download.py tiles --area data/Prispevna_povrsina_Cerknisko.geojson --json
```

List explicit tile names:

```bash
python clss_download.py tiles --tile 450_124 --tile 451_124 --tile 450_123,451_123
```

Download one product for explicit tiles:

```bash
python clss_download.py download --tile 450_124 --tile 451_124 --tile 450_123 --tile 451_123 --product gkot
```

Download multiple products for an AOI:

```bash
python clss_download.py download --area data/Prispevna_povrsina_Cerknisko.geojson --product dmr,dmp --out-dir downloads
```

Preview the download plan without downloading:

```bash
python clss_download.py download --tile 451_111 --product dmr --dry-run --flat
```

Read tile names from a text file:

```bash
python clss_download.py download --tiles-file my_tiles.txt --product gkot
```

Use a local untracked headers file instead of flags:

```powershell
Copy-Item .clss_headers.env.example .clss_headers.env
python clss_download.py download --tile 451_111 --product dmr
```

Use environment variables in the current shell:

```powershell
$env:CLSS_REFERER="..."
$env:CLSS_USER_AGENT="..."
python clss_download.py download --tile 451_111 --product dmr
```

Pass headers explicitly for one command:

```bash
python clss_download.py download --tile 451_111 --product dmr --referer "..." --user-agent "..."
```

Force plain progress output for IDE terminals:

```bash
python clss_download.py download --tile 451_111 --product dmr --progress plain
```

Store files by product folder only:

```bash
python clss_download.py download --tile 450_124,451_124 --product gkot --semi-flat
```

## Products

Supported product names:

- `gkot`
- `dmr`
- `dmp`
- `ndmp`
- `pas`
- `pof`
- `pofi`
- `all`

If `--product` is omitted, the default is `gkot`.

## Output Layout

By default, downloads preserve the CLSS folder structure under the output directory.

Example:

```text
downloads/05-ljubljana/zls/gkot/GKOT_450_124.laz
```

Use `--semi-flat` to keep only product subfolders:

```text
downloads/gkot/GKOT_450_124.laz
downloads/dmr/DMR_450_124.laz
```

Use `--flat` to store only the filenames directly in the output directory.

## Behavior Notes

- Real downloads require both `Referer` and `User-Agent`.
- `--dry-run` does not require headers.
- The AOI must already be in `EPSG:3794`; the tool does not reproject it.
- Real downloads use `--progress auto` by default.
- `auto` uses `tqdm` in normal TTY terminals and falls back to plain ASCII progress in less capable consoles.
- If `tqdm` is not installed, the CLI will say so and fall back to plain progress.
- `Ctrl+C` stops queued and active downloads as cleanly as possible and removes partial `.part` files.
- Use `--no-progress` if you want completion logs without progress updates.

## Useful Options

- `--out-dir downloads`: output directory
- `--semi-flat`: store files in `out-dir/<product>/filename`
- `--flat`: do not preserve CLSS folder structure
- `--overwrite`: replace existing files
- `--dry-run`: print the plan only
- `--workers 2`: concurrent downloads
- `--timeout 120`: request timeout in seconds
- `--layer my_layer`: GeoPackage layer name for AOI input
- `--trust-env-proxy`: use proxy settings from the environment
- `--headers-file .clss_headers.env`: env-style file with `CLSS_REFERER` and `CLSS_USER_AGENT`
- `--referer ...`: request header override
- `--user-agent ...`: request header override
- `--progress auto|tqdm|plain|off`: choose progress style
- `--no-progress`: disable terminal progress bars

## Notes and Limits

The public CLSS instructions mention:

- up to 10 blocks at once in the web bulk-download workflow
- up to 20 downloads per day in the viewer workflow

This CLI warns when your requested batch exceeds those counts, but it does not enforce them.

## Author

Krištof Oštir, UL FGG

## Copyright

Copyright 2026 Krištof Oštir, UL FGG
