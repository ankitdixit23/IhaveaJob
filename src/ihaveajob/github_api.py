"""Minimal GitHub REST client used by the web app to edit config/companies.yaml,
read the job database from the ``data`` branch, and start workflow runs.

Needs a fine-grained personal access token limited to this one repository
with: Contents (read and write), Actions (read and write), Metadata (read).
"""

from __future__ import annotations

import base64
from dataclasses import dataclass
from typing import Any

import requests

API = "https://api.github.com"


class GitHubError(Exception):
    pass


@dataclass
class GitHubRepo:
    token: str
    repo: str  # "owner/name"
    branch: str = "main"
    timeout: float = 30.0

    def _request(self, method: str, path: str, *, accept: str = "application/vnd.github+json", **kw: Any):
        headers = {"Authorization": f"Bearer {self.token}", "Accept": accept, "X-GitHub-Api-Version": "2022-11-28"}
        try:
            resp = requests.request(method, f"{API}/repos/{self.repo}{path}", headers=headers, timeout=self.timeout, **kw)
        except requests.RequestException as exc:
            raise GitHubError(f"Could not reach GitHub: {exc}") from exc
        if resp.status_code == 401:
            raise GitHubError("GitHub rejected the token (expired or wrong). Create a new one and update GITHUB_TOKEN.")
        if resp.status_code == 403:
            raise GitHubError("The token lacks permission. It needs Contents: read & write and Actions: read & write "
                              f"for {self.repo}.")
        if resp.status_code == 404:
            raise GitHubError(f"Not found on GitHub: {path} (check GITHUB_REPO and token repository access).")
        if resp.status_code == 409:
            raise GitHubError("The file changed on GitHub at the same time. Reload the page and try again.")
        if resp.status_code >= 400:
            raise GitHubError(f"GitHub error {resp.status_code}: {resp.text[:300]}")
        return resp

    def get_text(self, path: str, ref: str | None = None) -> tuple[str, str]:
        """Return (text, sha) of a file."""
        data = self._request("GET", f"/contents/{path}", params={"ref": ref or self.branch}).json()
        return base64.b64decode(data["content"]).decode("utf-8"), data["sha"]

    def put_text(self, path: str, text: str, sha: str, message: str) -> str:
        body = {"message": message, "content": base64.b64encode(text.encode("utf-8")).decode(), "sha": sha,
                "branch": self.branch}
        data = self._request("PUT", f"/contents/{path}", json=body).json()
        return data["commit"]["html_url"]

    def get_bytes(self, path: str, ref: str) -> bytes | None:
        try:
            return self._request("GET", f"/contents/{path}", params={"ref": ref},
                                 accept="application/vnd.github.raw+json").content
        except GitHubError as exc:
            if "Not found" in str(exc):
                return None
            raise

    def run_workflow(self, workflow_file: str, inputs: dict[str, Any] | None = None) -> None:
        self._request("POST", f"/actions/workflows/{workflow_file}/dispatches",
                      json={"ref": self.branch, "inputs": inputs or {}})

    def recent_runs(self, workflow_file: str, limit: int = 5) -> list[dict]:
        data = self._request("GET", f"/actions/workflows/{workflow_file}/runs", params={"per_page": limit}).json()
        return [{"status": r["status"], "conclusion": r.get("conclusion"), "created_at": r["created_at"],
                 "event": r["event"], "url": r["html_url"]} for r in data.get("workflow_runs", [])]
