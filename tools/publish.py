#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Builds the hpak repositories served at https://hpak.ouncebrew.org.

Each repository is a folder of this git repository (core/, community/) with
one JSON file per package and its icon in icons/. A package file names one
fixed upstream file (`source`): a release asset of a specific tag, never a
"latest" link. This script downloads each new package once, records its
SHA-256 and size, and writes:

  OUT/repos.json                                     the repositories
  OUT/<repo>/repo.json                               the package index
  OUT/<repo>/icons.zip                               <name>/icon.<ext>
  OUT/<repo>/packages/<name>/<version>/<file>        the package
  OUT/<repo>/packages/<name>/<version>/icon.<ext>

A published version never changes: a package whose version is already in the
live repo.json is not downloaded again, and if its source changed the build
fails, so that new content needs a new version. OUT/upload.txt lists what to
upload, packages first and the indexes last, so an index never names a file
that is not there yet.

  publish.py --out DIR [--base-url URL] [--check]

--check validates and downloads without writing OUT, for pull requests.
"""
import argparse
import hashlib
import json
import os
import re
import shutil
import sys
import tempfile
import urllib.error
import urllib.request
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
USER_AGENT = "ouncebrew-hpak-repo"

# The fields of a package file that go to repo.json as they are.
COPIED_FIELDS = ["title", "author", "description", "details", "license", "changelog",
                 "category", "url", "path", "dependencies", "depends",
                 "requiredDeletion", "userDeletion"]
ICON_EXTENSIONS = (".png", ".jpg", ".jpeg")
NAME_PATTERN = re.compile(r"^[a-z0-9][a-z0-9._-]*$")
VERSION_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]*$")
FILE_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]*$")
DATE_PATTERN = re.compile(r"^\d{2}/\d{2}/\d{4} \d{2}:\d{2}$")

# Top-level SD-card folders that a package may never delete as a whole or
# below: the system's own data and the files of kaiHEN itself.
PROTECTED_DELETION_ROOTS = ("sdmc:/Nintendo", "sdmc:/kaihen/bin", "sdmc:/kaihen/preload")


class PackageError(Exception):
    pass


def fetch(url):
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=120) as response:
        return response.read()


def fetch_json_or_none(url):
    """The JSON at url, or None when the server has no such file yet."""
    try:
        return json.loads(fetch(url))
    except urllib.error.HTTPError as error:
        if error.code == 404:
            return None
        raise


def check_sd_path(name, field, path):
    if not path.startswith("sdmc:/") or ".." in path or "\\" in path:
        raise PackageError(f"{name}: {field} entry {path!r} must be an sdmc:/ path without '..'")


def check_deletion_path(name, field, path):
    check_sd_path(name, field, path)
    trimmed = path.rstrip("/")
    # At least two levels below the root: a package folder, never a top-level folder.
    if trimmed.count("/") < 2:
        raise PackageError(f"{name}: {field} entry {path!r} is a top-level folder")
    for protected in PROTECTED_DELETION_ROOTS:
        if trimmed == protected or trimmed.startswith(protected + "/"):
            raise PackageError(f"{name}: {field} entry {path!r} is under {protected}")


def load_package(repo, file_name):
    """Reads and checks one package file."""
    path = os.path.join(ROOT, repo, file_name)
    with open(path, encoding="utf-8") as f:
        package = json.load(f)

    name = package.get("name", "")
    if file_name != name + ".json" or not NAME_PATTERN.match(name):
        raise PackageError(f"{repo}/{file_name}: the name must match the file name, in lowercase")
    for field, pattern in (("version", VERSION_PATTERN), ("file", FILE_PATTERN)):
        if not isinstance(package.get(field), str) or not pattern.match(package[field]):
            raise PackageError(f"{name}: {field} is missing or has characters that a path cannot have")
    source = package.get("source", "")
    if not source.startswith("https://") or "/releases/latest/" in source:
        raise PackageError(f"{name}: source must be an https URL of one fixed file, not a latest link")
    if package.get("updated") and not DATE_PATTERN.match(package["updated"]):
        raise PackageError(f"{name}: updated must be DD/MM/YYYY HH:MM")
    if "path" in package:
        check_sd_path(name, "path", package["path"])
    for field in ("requiredDeletion", "userDeletion"):
        for entry in package.get(field, []):
            check_deletion_path(name, field, entry)
    if "sha256" in package and not re.match(r"^[0-9a-f]{64}$", package["sha256"]):
        raise PackageError(f"{name}: sha256 must be 64 lowercase hex digits")
    return package


def find_icon(repo, name):
    for extension in ICON_EXTENSIONS:
        path = os.path.join(ROOT, repo, "icons", name + extension)
        if os.path.isfile(path):
            return path
    return None


def build_repository(repo, title, base_url, out, check_only, uploads):
    """Builds one repository; returns its repo.json."""
    repo_url = f"{base_url}/{repo}"
    live = fetch_json_or_none(f"{repo_url}/repo.json") or {"packages": []}
    live_packages = {p["name"]: p for p in live.get("packages", [])}

    entries = []
    for file_name in sorted(os.listdir(os.path.join(ROOT, repo))):
        if not file_name.endswith(".json"):
            continue
        package = load_package(repo, file_name)
        name, version, file = package["name"], package["version"], package["file"]
        folder = f"{repo}/packages/{name}/{version}"
        previous = live_packages.get(name)

        if previous and previous.get("version") == version:
            # Published already: it stays as it is.
            if previous.get("source") != package["source"]:
                raise PackageError(f"{name} {version} is published from {previous.get('source')}; "
                                   "a new source needs a new version")
            sha256, size = previous["sha256"], previous["size"]
            print(f"[keep] {repo}/{name} {version}")
        else:
            print(f"[new]  {repo}/{name} {version} from {package['source']}")
            data = fetch(package["source"])
            sha256, size = hashlib.sha256(data).hexdigest(), len(data)
            if package.get("sha256") and package["sha256"] != sha256:
                raise PackageError(f"{name}: the download's SHA-256 is {sha256}, "
                                   f"not the pinned {package['sha256']}")
            if not check_only:
                target = os.path.join(out, folder, file)
                os.makedirs(os.path.dirname(target), exist_ok=True)
                with open(target, "wb") as f:
                    f.write(data)
                uploads.append(("immutable", f"{folder}/{file}"))
                icon = find_icon(repo, name)
                if icon:
                    icon_key = f"{folder}/icon{os.path.splitext(icon)[1]}"
                    shutil.copy(icon, os.path.join(out, icon_key))
                    uploads.append(("immutable", icon_key))

        entry = {"name": name}
        entry.update({k: package[k] for k in COPIED_FIELDS if k in package})
        entry.update({
            "version": version,
            "file": file,
            "direct_url": f"{repo_url}/packages/{name}/{version}",
            "source": package["source"],
            "sha256": sha256,
            "size": size,
        })
        if package.get("updated"):
            entry["UpdateDateTime"] = package["updated"]
        entries.append(entry)

    index = {"name": title, "packages": entries}
    if not check_only:
        os.makedirs(os.path.join(out, repo), exist_ok=True)
        with open(os.path.join(out, repo, "repo.json"), "w", encoding="utf-8") as f:
            json.dump(index, f, indent=2, ensure_ascii=False)
            f.write("\n")
        with zipfile.ZipFile(os.path.join(out, repo, "icons.zip"), "w", zipfile.ZIP_STORED) as icons:
            for entry in entries:
                icon = find_icon(repo, entry["name"])
                if icon:
                    icons.write(icon, f"{entry['name']}/icon{os.path.splitext(icon)[1]}")
    return index


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--out", required=True)
    parser.add_argument("--base-url", default="https://hpak.ouncebrew.org")
    parser.add_argument("--check", action="store_true")
    arguments = parser.parse_args()
    base_url = arguments.base_url.rstrip("/")
    out = arguments.out

    with open(os.path.join(ROOT, "repos.json"), encoding="utf-8") as f:
        repositories = json.load(f)["repos"]

    if not arguments.check:
        if os.path.exists(out) and os.listdir(out):
            sys.exit(f"publish: {out} is not empty")
        os.makedirs(out, exist_ok=True)

    uploads = []
    try:
        for repository in repositories:
            build_repository(repository["folder"], repository["name"], base_url, out,
                             arguments.check, uploads)
    except PackageError as error:
        sys.exit(f"publish: {error}")

    if arguments.check:
        print("publish: all packages are valid")
        return

    # The repositories, in the format of hpak's own repos.json.
    listing = {"repos": [{"name": r["name"], "url": f"{base_url}/{r['folder']}",
                          "enabled": True, "type": "get"} for r in repositories]}
    with open(os.path.join(out, "repos.json"), "w", encoding="utf-8") as f:
        json.dump(listing, f, indent=2)
        f.write("\n")

    # Packages first, then the indexes that name them.
    for repository in repositories:
        uploads.append(("index", f"{repository['folder']}/icons.zip"))
        uploads.append(("index", f"{repository['folder']}/repo.json"))
    uploads.append(("index", "repos.json"))
    with open(os.path.join(out, "upload.txt"), "w") as f:
        for kind, key in uploads:
            f.write(f"{kind} {key}\n")
    print(f"publish: {sum(1 for k, _ in uploads if k == 'immutable')} new files, "
          f"{sum(1 for k, _ in uploads if k == 'index')} indexes in {out}")


if __name__ == "__main__":
    main()
