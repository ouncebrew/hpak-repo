# hpak repositories

The package repositories of [hpak](https://github.com/ouncebrew/hpak), served at
`https://hpak.ouncebrew.org`:

- `https://hpak.ouncebrew.org/core`: select apps, utilities and libraries.
- `https://hpak.ouncebrew.org/community`: community apps.

`https://hpak.ouncebrew.org/repos.json` lists both.

## Packages

Each package is one file, `<repository>/<name>.json`, with its icon in
`<repository>/icons/<name>.png` (or `.jpg`):

```json
{
  "name": "example",
  "version": "1.0.0",
  "title": "Example",
  "author": "someone",
  "description": "One line.",
  "details": "More text.",
  "license": "MIT",
  "changelog": "1.0.0: First release.",
  "category": "Tools",
  "url": "https://github.com/someone/example",
  "source": "https://github.com/someone/example/releases/download/v1.0.0/example.nro",
  "file": "example.nro",
  "updated": "25/11/2026 12:00"
}
```

- `source` is one fixed file: a release asset of a specific tag, never a
  `releases/latest` link. What the repository serves changes only when this
  file changes.
- `path` is where hpak installs the file (default `sdmc:/switch/`). A `.zip`
  is extracted to the SD card's root.
- `sha256` is optional. When it is there, the download must match it.
- `requiredDeletion` and `userDeletion` list what hpak removes with the
  package. Each entry is a file or a package's own folder: not a top-level
  folder, and nothing under `sdmc:/Nintendo`, `sdmc:/kaihen/bin` or
  `sdmc:/kaihen/preload`.

## Publishing

A push to `main` runs `.github/workflows/publish.yml`:
`tools/publish.py` downloads each new package once, records its SHA-256 and
size in `repo.json`, and uploads it to `<repository>/packages/<name>/<version>/`.
A published version never changes: to change a package, raise its version.
Pull requests are checked the same way without uploading.

To build locally:

```sh
python3 tools/publish.py --out out           # what would be uploaded
python3 tools/publish.py --check --out out   # validate only
```

