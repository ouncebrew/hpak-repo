#!/usr/bin/env python3
"""Sync package files with their latest upstream releases.

For each core/*.json and community/*.json: look at the latest GitHub or
GitLab release; if its package.json carries a different version (GitLab
falls back to the tag), update the metadata in place. `source` links are
left exactly as written (a pinned tag URL or a releases/latest link —
publish.py pins those at build time). Anything else uses `versionSource`:
regex the page and bump the version, rebuilding `source` from
`sourceTemplate` when present. Packages with `"manual": true` are left
alone entirely (hand-vetted entries); they are still validated.
Run: python3 tools/sync_upstream.py [--check]
--check validates the package files without touching the network.
"""
import argparse
import datetime
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import publish

USER_AGENT = publish.USER_AGENT
DATE_FORMAT = "%d/%m/%Y %H:%M"
ICON_NAMES = ("icon.png", "icon.jpg", "icon.jpeg")
REPOS = ("core", "community")

FIELDS = ["title", "author", "description", "details", "license", "changelog"]
LIST_FIELDS = ["requiredDeletion", "userDeletion"]


def _headers():
    headers = {"User-Agent": USER_AGENT, "Accept": "application/vnd.github+json"}
    token = os.environ.get("GITHUB_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def fetch_bytes(url):
    request = urllib.request.Request(url, headers=_headers())
    with urllib.request.urlopen(request, timeout=120) as response:
        return response.read()


def fetch_json_or_none(url):
    try:
        return json.loads(fetch_bytes(url).decode("utf-8"))
    except urllib.error.HTTPError as error:
        if error.code == 404:
            return None
        raise


def version_parts(s):
    """1-4 leading numeric segments (optional 'v', trailing suffix ignored)."""
    s = s.strip()
    if s[:1].lower() == "v":
        s = s[1:]
    segs = []
    for part in s.split("."):
        j = 0
        while j < len(part) and part[j].isdigit():
            j += 1
        if j == 0:
            break
        if j > 9:
            return None
        segs.append(int(part[:j]))
        if len(segs) > 4:
            return None
        if j < len(part):
            break
    if not segs:
        return None
    while len(segs) < 4:
        segs.append(0)
    return segs


def versions_equal(a, b):
    sa, sb = version_parts(a or ""), version_parts(b or "")
    if sa is not None and sb is not None:
        return sa == sb
    return a == b


def check_date(value):
    try:
        datetime.datetime.strptime(value.strip(), DATE_FORMAT)
        return value.strip()
    except (ValueError, AttributeError):
        return None


def apply_release(repo, package, now, assets, remote, new_version, updated_hint):
    """Sync metadata, version and icon from a release. Returns True if changed.
    `assets` maps asset names to download URLs; `remote` is a parsed
    package.json or None; `new_version`/`updated_hint` may be None."""
    changed = False
    name = package["name"]
    if isinstance(remote, dict):
        for field in FIELDS:
            value = remote.get(field)
            if isinstance(value, str) and value != package.get(field):
                print(f"[update] {name}.{field}")
                package[field] = value
                changed = True
        for field in LIST_FIELDS:
            value = remote.get(field)
            if isinstance(value, list) and all(isinstance(x, str) for x in value):
                if value != package.get(field):
                    print(f"[update] {name}.{field}")
                    package[field] = value
                    changed = True
        deps = remote.get("dependencies")
        if isinstance(deps, dict) and all(isinstance(k, str) and isinstance(v, str) for k, v in deps.items()):
            if deps != package.get("dependencies"):
                print(f"[update] {name}.dependencies")
                package["dependencies"] = deps
                changed = True
        darr = remote.get("depends")
        if isinstance(darr, list) and all(isinstance(x, str) for x in darr):
            if darr != package.get("depends"):
                print(f"[update] {name}.depends")
                package["depends"] = darr
                changed = True
    elif remote is not None:
        print(f"[skip] {name}: no usable package.json in latest release")

    if isinstance(new_version, str) and new_version != package.get("version"):
        if versions_equal(new_version, package.get("version") or ""):
            pass  # same version, different spelling: leave the file alone
        else:
            # The file asset must exist in the new release, but `source`
            # stays as written (a latest link or a pinned URL) — publish
            # resolves it at build time.
            if package["file"] not in assets:
                print(f"[skip] {name}: new version {new_version} but no {package['file']} asset")
                return changed
            print(f"[update] {name}.version ({new_version})")
            package["version"] = new_version
            package["updated"] = updated_hint or now
            print(f"[update] {name}.updated")
            changed = True

    # Icons ride along with version bumps (publish only ships them then),
    # or when the repo has none yet.
    has_icon = any(
        os.path.isfile(os.path.join(publish.ROOT, repo, "icons", name + e))
        for e in (".png", ".jpg", ".jpeg")
    )
    if changed or not has_icon:
        for icon_name in ICON_NAMES:
            if icon_name in assets:
                data = fetch_bytes(assets[icon_name])
                ext = os.path.splitext(icon_name)[1]
                dest = os.path.join(publish.ROOT, repo, "icons", name + ext)
                for old in (os.path.join(publish.ROOT, repo, "icons", name + e) for e in (".png", ".jpg", ".jpeg")):
                    if old != dest and os.path.isfile(old):
                        os.remove(old)
                with open(dest, "wb") as f:
                    f.write(data)
                print(f"[icon] {name}{ext}")
                break

    return changed


def sync_github(repo, package, now):
    """Sync one package with its latest GitHub release. Returns True if changed."""
    name = package["name"]
    match = re.match(r"https://github\.com/([^/]+)/([^/]+?)(?:\.git)?/?$", package.get("url", ""))
    if not match:
        print(f"[skip] {name}: url is not a GitHub repo page")
        return False
    owner, repo_name = match.groups()

    release = fetch_json_or_none(f"https://api.github.com/repos/{owner}/{repo_name}/releases/latest")
    if not release:
        print(f"[skip] {name}: no GitHub releases yet")
        return False
    assets = {a["name"]: a["browser_download_url"] for a in release.get("assets", [])}

    remote = None
    if "package.json" in assets:
        try:
            remote = json.loads(fetch_bytes(assets["package.json"]).decode("utf-8"))
        except Exception as error:
            print(f"[skip] {name}: package.json unreadable ({error})")

    new_version = date_hint = None
    if isinstance(remote, dict):
        new_version = remote.get("version")
        date_hint = check_date(remote.get("date") or "")
    return apply_release(repo, package, now, assets, remote, new_version, date_hint or now)


def tag_version(tag):
    """Best-effort numeric version from a tag like v1.1.0 or ounce-3.4.0."""
    match = re.search(r"\d", tag or "")
    if not match:
        return None
    candidate = tag[match.start():]
    return candidate if version_parts(candidate) is not None else None


def parse_gitlab_released_at(value):
    """2026-11-16T09:17:06.855Z -> DD/MM/YYYY HH:MM, else None."""
    try:
        dt = datetime.datetime.strptime((value or "")[:16], "%Y-%m-%dT%H:%M")
        return dt.strftime(DATE_FORMAT)
    except (ValueError, TypeError):
        return None


def sync_gitlab(repo, package, now):
    """Sync one package with its latest GitLab release. Returns True if changed."""
    name = package["name"]
    match = re.match(r"https://gitlab\.com/(.+?)(?:\.git)?/?$", package.get("url", ""))
    if not match:
        print(f"[skip] {name}: url is not a GitLab project page")
        return False
    project = urllib.parse.quote(match.group(1), safe="")

    request = urllib.request.Request(
        f"https://gitlab.com/api/v4/projects/{project}/releases?per_page=5",
        headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
    token = os.environ.get("GITLAB_TOKEN")
    if token:
        request.add_header("PRIVATE-TOKEN", token)
    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            releases = json.loads(response.read().decode("utf-8"))
    except Exception as error:
        print(f"[skip] {name}: GitLab releases unreadable ({error})")
        return False
    if not isinstance(releases, list) or not releases:
        print(f"[skip] {name}: no GitLab releases yet")
        return False
    release = next((r for r in releases if isinstance(r, dict) and not r.get("upcoming_release")),
                   releases[0])
    if not isinstance(release, dict):
        print(f"[skip] {name}: no GitLab releases yet")
        return False

    assets = {}
    for link in (release.get("assets") or {}).get("links", []):
        if isinstance(link, dict) and link.get("name"):
            assets[link["name"]] = link.get("direct_asset_url") or link.get("url")

    remote = None
    if "package.json" in assets:
        try:
            remote = json.loads(fetch_bytes(assets["package.json"]).decode("utf-8"))
        except Exception as error:
            print(f"[skip] {name}: package.json unreadable ({error})")

    new_version = tag_version(release.get("tag_name", ""))
    if new_version is None:
        print(f"[skip] {name}: cannot parse a version from tag {release.get('tag_name')!r}")
    return apply_release(repo, package, now, assets, remote if isinstance(remote, dict) else None,
                         new_version, parse_gitlab_released_at(release.get("released_at")) or now)


def sync_version_source(package, now):
    """Sync a non-GitHub version (e.g. curl.se revision table)."""
    vs = package.get("versionSource")
    if not isinstance(vs, dict) or not isinstance(vs.get("url"), str) or not isinstance(vs.get("pattern"), str):
        return False
    name = package["name"]
    text = fetch_bytes(vs["url"]).decode("utf-8", "ignore")
    match = re.search(vs["pattern"], text)
    if not match or not match.groups():
        print(f"[skip] {name}: versionSource pattern found nothing")
        return False
    version = ".".join(match.groups())
    if version_parts(version) is None:
        print(f"[skip] {name}: versionSource produced invalid version {version!r}")
        return False
    if versions_equal(version, package.get("version") or ""):
        return False
    print(f"[update] {name}.version ({version} from versionSource)")
    package["version"] = version
    template = vs.get("sourceTemplate")
    if isinstance(template, str) and template:
        try:
            package["source"] = template.format(*match.groups())
        except (IndexError, KeyError) as error:
            print(f"[skip] {name}: bad sourceTemplate ({error})")
            return False
    package["updated"] = now
    print(f"[update] {name}.updated")
    return True


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true",
                        help="validate the package files without touching the network")
    arguments = parser.parse_args()
    now = datetime.datetime.now(datetime.timezone.utc).strftime(DATE_FORMAT)

    changed_files = []
    for repo in REPOS:
        folder = os.path.join(publish.ROOT, repo)
        if not os.path.isdir(folder):
            continue
        for file_name in sorted(os.listdir(folder)):
            if not file_name.endswith(".json"):
                continue
            try:
                package = publish.load_package(repo, file_name, allow_latest=True)
            except publish.PackageError as error:
                sys.exit(f"sync: {error}")
            if arguments.check:
                continue
            if package.get("manual") is True:
                print(f"[skip] {package.get('name', file_name)}: manually vetted, left alone")
                continue
            try:
                changed = sync_version_source(package, now)
                url = package.get("url", "")
                if "://github.com/" in url:
                    changed = sync_github(repo, package, now) or changed
                elif "://gitlab.com/" in url:
                    changed = sync_gitlab(repo, package, now) or changed
                elif "versionSource" not in package:
                    print(f"[skip] {package.get('name', file_name)}: no supported host in url")
            except Exception as error:
                print(f"[skip] {package.get('name', file_name)}: {error}")
                continue
            if not check_date(package.get("updated") or ""):
                package["updated"] = now
                print(f"[update] {package.get('name', file_name)}.updated")
                changed = True
            if changed:
                path = os.path.join(folder, file_name)
                with open(path, "w", encoding="utf-8") as f:
                    json.dump(package, f, indent=2, ensure_ascii=False)
                    f.write("\n")
                changed_files.append(f"{repo}/{file_name}")

    if arguments.check:
        print("sync: all package files are valid")
        return
    if changed_files:
        print("sync: updated " + ", ".join(changed_files))
    else:
        print("sync: everything already up to date")


if __name__ == "__main__":
    main()
