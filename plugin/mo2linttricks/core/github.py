"""GitHub access: OAuth device flow, repo creation, and snapshot commits.

Commits go through the Git Data API (trees/commits/refs), so no git binary is
needed inside Wine. One sync = one commit, and only when something changed.
"""

from __future__ import annotations

import base64
import hashlib
import json
import time
import urllib.parse
from collections.abc import Callable
from dataclasses import dataclass

from .http import Response, Transport
from .snapshot import Snapshot, is_managed

API = "https://api.github.com"
DEVICE_CODE_URL = "https://github.com/login/device/code"
TOKEN_URL = "https://github.com/login/oauth/access_token"
SCOPE = "repo"  # needed to create and write private repositories
BRANCH = "main"


class GitHubError(RuntimeError):
    def __init__(self, msg: str, status: int = 0):
        super().__init__(msg)
        self.status = status


def blob_sha(data: bytes) -> str:
    return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()


# --- device flow --------------------------------------------------------------


@dataclass
class DeviceCode:
    device_code: str
    user_code: str
    verification_uri: str
    interval: int
    expires_in: int


def start_device_flow(client_id: str, transport: Transport) -> DeviceCode:
    r = transport(
        "POST",
        DEVICE_CODE_URL,
        {"Accept": "application/json", "Content-Type": "application/x-www-form-urlencoded"},
        urllib.parse.urlencode({"client_id": client_id, "scope": SCOPE}).encode(),
    )
    d = r.json() if r.body else {}
    if r.status != 200 or not isinstance(d, dict) or "device_code" not in d:
        raise GitHubError(f"device code request failed ({r.status}): {d}", r.status)
    return DeviceCode(d["device_code"], d["user_code"], d["verification_uri"], int(d.get("interval", 5)), int(d.get("expires_in", 900)))


def poll_device_flow(
    client_id: str,
    code: DeviceCode,
    transport: Transport,
    cancelled: Callable[[], bool] = lambda: False,
    sleep: Callable[[float], None] = time.sleep,
) -> str:
    """Block until the user approves; returns the access token."""
    interval = code.interval
    deadline = time.monotonic() + code.expires_in
    while time.monotonic() < deadline:
        sleep(interval)
        if cancelled():
            raise GitHubError("sign-in cancelled")
        r = transport(
            "POST",
            TOKEN_URL,
            {"Accept": "application/json", "Content-Type": "application/x-www-form-urlencoded"},
            urllib.parse.urlencode(
                {
                    "client_id": client_id,
                    "device_code": code.device_code,
                    "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
                }
            ).encode(),
        )
        d = r.json() if r.body else {}
        if not isinstance(d, dict):
            continue
        if d.get("access_token"):
            return d["access_token"]
        err = d.get("error")
        if err == "authorization_pending":
            continue
        if err == "slow_down":
            interval = int(d.get("interval", interval + 5))
            continue
        raise GitHubError(f"sign-in failed: {d.get('error_description') or err or r.status}")
    raise GitHubError("sign-in code expired")


# --- REST client --------------------------------------------------------------


class GitHub:
    def __init__(self, token: str, transport: Transport | None = None):
        self.token = token
        self.t = transport or Transport()

    def _req(self, method: str, path: str, body: object = None, ok=(200, 201)) -> object:
        r: Response = self.t(
            method,
            path if path.startswith("http") else API + path,
            {
                "Accept": "application/vnd.github+json",
                "Authorization": f"Bearer {self.token}",
                "X-GitHub-Api-Version": "2022-11-28",
                **({"Content-Type": "application/json"} if body is not None else {}),
            },
            json.dumps(body).encode() if body is not None else None,
        )
        if r.status not in ok:
            try:
                d = r.json()
                msg = d.get("message", "") if isinstance(d, dict) else ""
            except Exception:
                msg = r.body[:200].decode(errors="replace")
            raise GitHubError(f"{method} {path}: {r.status} {msg}", r.status)
        return r.json() if r.body else None

    def user(self) -> str:
        return self._req("GET", "/user")["login"]  # type: ignore[index]

    def ensure_repo(self, name: str, description: str) -> tuple[str, bool]:
        """Returns (full_name, created). Creates a private repo with an initial commit."""
        owner = self.user()
        try:
            r = self._req("GET", f"/repos/{owner}/{name}")
            return r["full_name"], False  # type: ignore[index]
        except GitHubError as e:
            if e.status != 404:
                raise
        r = self._req(
            "POST",
            "/user/repos",
            {"name": name, "private": True, "auto_init": True, "description": description},
        )
        full = r["full_name"]  # type: ignore[index]
        # auto_init creates the branch asynchronously; wait until the ref exists
        for _ in range(20):
            try:
                self._head(full)
                break
            except GitHubError:
                time.sleep(0.5)
        return full, True

    def _head(self, repo: str) -> str:
        return self._req("GET", f"/repos/{repo}/git/ref/heads/{BRANCH}")["object"]["sha"]  # type: ignore[index]

    def remote_tree(self, repo: str) -> tuple[str, str, dict[str, str]]:
        """(head commit sha, tree sha, {path: blob sha}) for the branch."""
        head = self._head(repo)
        commit = self._req("GET", f"/repos/{repo}/git/commits/{head}")
        tree_sha = commit["tree"]["sha"]  # type: ignore[index]
        tree = self._req("GET", f"/repos/{repo}/git/trees/{tree_sha}?recursive=1")
        if isinstance(tree, dict) and tree.get("truncated"):
            raise GitHubError("repository tree too large to diff")
        blobs = {e["path"]: e["sha"] for e in tree["tree"] if e["type"] == "blob"}  # type: ignore[index]
        return head, tree_sha, blobs

    def get_blob(self, repo: str, sha: str) -> bytes:
        b = self._req("GET", f"/repos/{repo}/git/blobs/{sha}")
        return base64.b64decode(b["content"])  # type: ignore[index]

    def get_file(self, repo: str, path: str) -> bytes | None:
        quoted = urllib.parse.quote(path)
        try:
            d = self._req("GET", f"/repos/{repo}/contents/{quoted}?ref={BRANCH}")
        except GitHubError as e:
            if e.status == 404:
                return None
            raise
        if isinstance(d, dict) and d.get("encoding") == "base64":
            return base64.b64decode(d["content"])
        if isinstance(d, dict) and d.get("sha"):  # >1MB files come back without content
            b = self._req("GET", f"/repos/{repo}/git/blobs/{d['sha']}")
            return base64.b64decode(b["content"])  # type: ignore[index]
        return None

    def baseline(self, repo: str, snap: Snapshot) -> dict[str, bytes]:
        """Approximate the remote's managed files for commit messages, cheaply.

        Only mods.json and profile modlist.json are downloaded; other files are
        represented by the local bytes when the blob hashes match, else a marker.
        """
        _, _, remote = self.remote_tree(repo)
        out: dict[str, bytes] = {}
        for path, sha in remote.items():
            if not is_managed(path):
                continue
            if path == "mods.json" or path.endswith("/modlist.json"):
                out[path] = self.get_blob(repo, sha)
            elif path in snap.files and blob_sha(snap.files[path]) == sha:
                out[path] = snap.files[path]
            else:
                out[path] = b"\0remote:" + sha.encode()
        return out

    def push_snapshot(self, repo: str, snap: Snapshot, message: str) -> str | None:
        """Make the managed part of the repo equal to the snapshot. Returns the new commit sha, or None if unchanged."""
        for attempt in range(3):
            head, tree_sha, remote = self.remote_tree(repo)
            entries: list[dict[str, object]] = []
            for path, data in sorted(snap.files.items()):
                if remote.get(path) == blob_sha(data):
                    continue
                entry: dict[str, object] = {"path": path, "mode": "100644", "type": "blob"}
                try:
                    entry["content"] = data.decode("utf-8")
                except UnicodeDecodeError:
                    b = self._req(
                        "POST",
                        f"/repos/{repo}/git/blobs",
                        {"content": base64.b64encode(data).decode(), "encoding": "base64"},
                    )
                    entry["sha"] = b["sha"]  # type: ignore[index]
                entries.append(entry)
            for path in sorted(remote):
                if is_managed(path) and path not in snap.files:
                    entries.append({"path": path, "mode": "100644", "type": "blob", "sha": None})
            if not entries:
                return None
            tree = self._req("POST", f"/repos/{repo}/git/trees", {"base_tree": tree_sha, "tree": entries})
            commit = self._req(
                "POST",
                f"/repos/{repo}/git/commits",
                {"message": message, "tree": tree["sha"], "parents": [head]},  # type: ignore[index]
            )
            try:
                self._req("PATCH", f"/repos/{repo}/git/refs/heads/{BRANCH}", {"sha": commit["sha"], "force": False})  # type: ignore[index]
                return commit["sha"]  # type: ignore[index]
            except GitHubError as e:
                if e.status != 422 or attempt == 2:  # 422: branch moved underneath us; rebuild on new head
                    raise
        return None


def describe_changes(old: dict[str, bytes], new: dict[str, bytes]) -> str:
    """Human commit message from two snapshots' modlist/mods.json contents."""

    def load(files: dict[str, bytes], path: str):
        try:
            return json.loads(files.get(path, b"null")) or {}
        except ValueError:
            return {}

    lines: list[str] = []
    old_mods, new_mods = load(old, "mods.json"), load(new, "mods.json")
    added = sorted(set(new_mods) - set(old_mods))
    removed = sorted(set(old_mods) - set(new_mods))
    updated = sorted(m for m in set(new_mods) & set(old_mods) if old_mods[m].get("version") != new_mods[m].get("version"))
    for m in added:
        lines.append(f"+ installed {m} {new_mods[m].get('version', '')}".rstrip())
    for m in removed:
        lines.append(f"- removed {m}")
    for m in updated:
        lines.append(f"~ updated {m} {old_mods[m].get('version', '')} -> {new_mods[m].get('version', '')}")

    profiles = sorted({p.split("/")[1] for p in list(old) + list(new) if p.startswith("profiles/") and p.endswith("/modlist.json")})
    for prof in profiles:
        path = f"profiles/{prof}/modlist.json"
        o, n = load(old, path) or [], load(new, path) or []
        if o == n:
            continue
        o_en = {e["name"]: e["enabled"] for e in o}
        toggled = [e["name"] for e in n if e["name"] in o_en and o_en[e["name"]] != e["enabled"]]
        moved = [e["name"] for e in n if e["name"] in o_en] != [e["name"] for e in o if e["name"] in {x["name"] for x in n}]
        bits = []
        if toggled:
            bits.append(f"toggled {len(toggled)}")
        if moved:
            bits.append("reordered")
        lines.append(f"profile {prof}: {', '.join(bits) or 'changed'}")

    old_dl = {p for p in old if p.startswith("downloads/")}
    new_dl = {p for p in new if p.startswith("downloads/")}
    for p in sorted(new_dl - old_dl):
        lines.append(f"+ downloaded {p[len('downloads/'):-len('.meta')]}")
    for p in sorted(old_dl - new_dl):
        lines.append(f"- deleted download {p[len('downloads/'):-len('.meta')]}")

    changed_other = sorted(
        p for p in set(old) | set(new)
        if old.get(p) != new.get(p) and not p.endswith("modlist.json") and p != "mods.json" and not p.startswith("mods/") and not p.startswith("downloads/")
    )
    if changed_other and not lines:
        lines.append(f"updated {len(changed_other)} file(s)")
    if not lines:
        return "Sync modlist"
    summary = lines[0] if len(lines) == 1 else f"Sync modlist: {len(lines)} changes"
    return summary + ("\n\n" + "\n".join(lines) if len(lines) > 1 else "")
