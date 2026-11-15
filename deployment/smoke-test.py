#!/usr/bin/env python3
"""Check an isolated platform installation using Python's standard library.

The default checks pages and authentication without changing business data.
--write additionally creates, uploads, downloads, verifies and deletes its own
temporary dataset. --allow-password-change explicitly acknowledges the initial
password requirement using the configured password in a disposable test setup.
Never pass passwords on the command line or run the write check against a
production installation unless those temporary test records are intended.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import getpass
from html.parser import HTMLParser
from http.cookiejar import CookieJar
import json
import os
from pathlib import Path
import re
import shlex
import sys
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin, urlsplit
from urllib.request import HTTPCookieProcessor, HTTPRedirectHandler, Request, build_opener
import uuid


class CheckFailed(Exception):
    pass


def read_env(path: Path) -> dict[str, str]:
    """Read literal dotenv assignments, including shell-style single quotes.

    No shell is invoked and variables/command substitutions are never expanded.
    """
    result: dict[str, str] = {}
    if not path.is_file():
        return result
    for number, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        key, separator, value = line.partition("=")
        if not separator or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key.strip()):
            raise CheckFailed(f"Invalid private environment assignment on line {number}.")
        try:
            tokens = shlex.split(value.strip(), comments=True, posix=True)
        except ValueError:
            raise CheckFailed(f"Invalid private environment quoting on line {number}.") from None
        if len(tokens) > 1:
            raise CheckFailed(f"Private environment values containing spaces must be quoted (line {number}).")
        result[key.strip()] = tokens[0] if tokens else ""
    return result


class AssetReferences(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.paths: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = dict(attrs)
        candidate = values.get("src") if tag == "script" else (
            values.get("href") if tag == "link" and "stylesheet" in (values.get("rel") or "") else None
        )
        if candidate:
            self.paths.append(candidate)


class SameOriginRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, response, code, message, headers, new_url):
        current, target = urlsplit(request.full_url), urlsplit(new_url)
        if (current.scheme, current.netloc) != (target.scheme, target.netloc):
            raise CheckFailed("Unexpected redirect outside this deployment.")
        return super().redirect_request(request, response, code, message, headers, new_url)


class Platform:
    def __init__(self, base: str, timeout: float) -> None:
        parsed = urlsplit(base)
        if (parsed.scheme not in ("http", "https") or not parsed.netloc or parsed.username
                or parsed.password or parsed.query or parsed.fragment or parsed.path not in ("", "/")):
            raise CheckFailed("--base-url must be a plain HTTP(S) origin without credentials or a path.")
        self.base = base.rstrip("/")
        self.origin = urlsplit(self.base)
        self.timeout = timeout
        self.opener = build_opener(SameOriginRedirect(), HTTPCookieProcessor(CookieJar()))
        self.csrf: str | None = None

    def request(self, path: str, method: str = "GET", value=None, *, raw: bytes | None = None,
                headers: dict[str, str] | None = None, decode: bool = True):
        url = urljoin(self.base + "/", path)
        parsed = urlsplit(url)
        if (parsed.scheme, parsed.netloc) != (self.origin.scheme, self.origin.netloc):
            raise CheckFailed("A response referenced a download or asset outside this deployment.")
        outgoing = {"Accept": "application/json" if decode else "*/*", "Origin": self.base}
        if self.csrf:
            outgoing["X-CSRF-Token"] = self.csrf
        if value is not None:
            raw = json.dumps(value, ensure_ascii=False).encode("utf-8")
            outgoing["Content-Type"] = "application/json"
        outgoing.update(headers or {})
        request = Request(url, data=raw, headers=outgoing, method=method)
        try:
            with self.opener.open(request, timeout=min(self.timeout, 45)) as response:
                body = response.read()
                final = urlsplit(response.url)
                if (final.scheme, final.netloc) != (self.origin.scheme, self.origin.netloc):
                    raise CheckFailed("Unexpected redirect outside this deployment.")
                if not decode:
                    return body
                if response.status == 204 or not body:
                    return None
                try:
                    return json.loads(body)
                except (ValueError, UnicodeError):
                    raise CheckFailed(f"Non-JSON response from {parsed.path}.") from None
        except HTTPError as error:
            # Do not print response bodies, credentials, CSRF or signed URLs.
            raise CheckFailed(f"HTTP {error.code}: {method} {parsed.path}") from None
        except (URLError, TimeoutError, OSError):
            raise CheckFailed(f"Connection failed: {method} {parsed.path}") from None

    def api(self, path: str, method: str = "GET", value=None, **kwargs):
        return self.request("/dbprog/api/" + path.lstrip("/"), method, value, **kwargs)

    def warehouse_login(self, email: str, password: str):
        result = self.api("v1/auth/login", "POST", {"email": email, "password": password})
        if not isinstance(result, dict) or not isinstance(result.get("user"), dict) or not result.get("csrfToken"):
            raise CheckFailed("Warehouse login did not return a user and CSRF token.")
        self.csrf = result["csrfToken"]
        return result["user"]

    def page(self, path: str) -> None:
        body = self.request(path, decode=False)
        if not re.search(br"<(?:!doctype\s+html|html)\b", body, re.I):
            raise CheckFailed(f"Page did not return HTML: {path}")
        parser = AssetReferences()
        parser.feed(body.decode("utf-8"))
        for asset in dict.fromkeys(parser.paths):
            resolved = urljoin(self.base + path, asset)
            if not self.request(resolved, decode=False):
                raise CheckFailed(f"Empty page asset referenced from {path}.")
        print(f"PASS page and script/style assets: {path}")

    def wait_job(self, job_id: str) -> dict:
        deadline = time.monotonic() + self.timeout
        while time.monotonic() < deadline:
            job = self.api(f"v1/jobs/{job_id}")
            if job.get("status") == "SUCCEEDED":
                return job
            if job.get("status") in ("FAILED", "CANCELLED"):
                raise CheckFailed(f"Worker job {job_id} ended with {job['status']}.")
            time.sleep(1)
        raise CheckFailed(f"Worker job {job_id} did not complete within the timeout.")

    def write_check(self, keep: bool) -> None:
        projects = self.api("warehouse/projects")
        if not isinstance(projects, list) or not projects:
            raise CheckFailed("No warehouse project is available; check bootstrap.")
        name = "DeploymentSmoke_" + uuid.uuid4().hex
        dataset = self.api("v1/datasets", "POST", {"name": name, "projectId": projects[0]["id"]})
        dataset_id = dataset["id"]
        print("PASS create temporary dataset")
        success = False
        try:
            defaults = self.api("v1/uploads/defaults", "POST", {"datasetId": dataset_id})
            content = json.dumps({"test": "isolated-deployment-smoke", "nonce": uuid.uuid4().hex,
                                  "data": [1, 2, 3]}, sort_keys=True).encode("utf-8")
            checksum = hashlib.sha256(content).hexdigest()
            upload = self.api("v1/uploads", "POST", {
                "datasetId": dataset_id, "folderPath": "", "robotId": defaults["robotId"],
                "originalName": "smoke.json", "mimeType": "application/json", "fileSize": len(content),
                "checksum": checksum, "recognitionType": defaults["recognitionType"],
                "schemaVersion": defaults["schemaVersion"],
                "capturedAt": datetime.now(timezone.utc).isoformat(),
            })
            upload_id = upload["id"]
            self.api(f"v1/uploads/{upload_id}/parts/1", "PUT", raw=content, headers={
                "Content-Type": "application/octet-stream",
                "Content-Range": f"bytes 0-{len(content) - 1}/{len(content)}", "X-Part-SHA256": checksum,
            })
            job = self.api(f"v1/uploads/{upload_id}/complete", "POST", {})
            if not job.get("id"):
                raise CheckFailed("Upload completion did not enqueue a worker job.")
            self.wait_job(job["id"])
            finished = self.api(f"v1/uploads/{upload_id}")
            if finished.get("status") != "COMPLETED" or not finished.get("robotFileId"):
                raise CheckFailed("The upload worker did not register the completed file.")
            print("PASS chunk upload, object storage and worker finalization")
            file_id = finished["robotFileId"]
            link = self.api(f"v1/files/{file_id}/download")
            downloaded = self.request(link["url"], decode=False)
            if downloaded != content or hashlib.sha256(downloaded).hexdigest() != checksum:
                raise CheckFailed("Downloaded file differs from the original upload.")
            contents = self.api(f"v1/datasets/{dataset_id}/contents")
            if not any(item.get("id") == file_id for item in contents.get("files", [])):
                raise CheckFailed("The uploaded file is absent from the dataset browser.")
            print("PASS authenticated download, SHA-256 and dataset browser")
            success = True
        finally:
            if success and not keep:
                deletion = self.api(f"v1/datasets/{dataset_id}", "DELETE", {"confirmationName": name})
                self.wait_job(deletion["id"])
                print("PASS temporary dataset and object cleanup")
            else:
                print(f"Temporary dataset retained for inspection: {dataset_id}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--base-url", default="http://localhost:18080")
    parser.add_argument("--env-file", type=Path,
                        default=Path(__file__).resolve().parents[1] / "runtime-data" / "deployment.env")
    parser.add_argument("--write", action="store_true", help="Create and clean up a temporary dataset; verify the upload worker.")
    parser.add_argument("--ask-password", action="store_true",
                        help="Read current observer and warehouse passwords privately; do not modify saved configuration.")
    parser.add_argument("--allow-password-change", action="store_true",
                        help="In an isolated test, acknowledge the first-login change using the configured password.")
    parser.add_argument("--keep-test-data", action="store_true", help="Keep this script's temporary test dataset for inspection.")
    parser.add_argument("--timeout", type=float, default=180, help="Worker timeout in seconds.")
    args = parser.parse_args()
    if args.timeout <= 0:
        raise CheckFailed("--timeout must be positive.")
    configured = read_env(args.env_file)
    if args.ask_password:
        if not sys.stdin.isatty():
            raise CheckFailed("--ask-password requires an interactive terminal; password input must remain hidden.")
        for key, prompt in (("VIEWER_PASSWORD_PLAIN", "Current observer password: "),
                            ("BOOTSTRAP_ADMIN_PASSWORD", "Current warehouse password: ")):
            try:
                value = getpass.getpass(prompt)
            except (EOFError, KeyboardInterrupt):
                raise CheckFailed("Private password entry was cancelled.") from None
            if not value:
                raise CheckFailed("The current password cannot be empty.")
            configured[key] = value

    def credential(key: str, fallback: str | None = None) -> str:
        value = configured.get(key, os.environ.get(key)) or fallback
        if not value:
            raise CheckFailed(f"Missing {key}; provide it through the private environment file or process environment.")
        return value

    platform = Platform(args.base_url, args.timeout)
    health = platform.request("/readyz")
    if not isinstance(health, dict):
        raise CheckFailed("Observer readiness did not return JSON.")
    print("PASS observer readiness")
    observer = platform.request("/api/login", "POST", {
        "username": credential("VIEWER_USERNAME", "observer"), "password": credential("VIEWER_PASSWORD_PLAIN"),
    })
    if not observer.get("authenticated") or not observer.get("approved"):
        raise CheckFailed("Observer administrator login was not approved.")
    session = platform.request("/api/session")
    if not session.get("authenticated"):
        raise CheckFailed("Observer login cookie was not retained.")
    platform.request("/api/robots")
    print("PASS observer login, session cookie and robot listing")
    for path in ("/", "/dbprog/", "/coverage", "/sensor-library/"):
        platform.page(path)
    email = credential("BOOTSTRAP_ADMIN_EMAIL")
    password = credential("BOOTSTRAP_ADMIN_PASSWORD")
    user = platform.warehouse_login(email, password)
    print("PASS warehouse login and CSRF token")
    if user.get("mustChangePassword"):
        if not args.allow_password_change:
            if args.write:
                raise CheckFailed("Initial password change is required; complete it in the UI, or use --allow-password-change only in an isolated test.")
            print("PASS initial administrator must change password before using business APIs")
            print("Read-only smoke checks passed. Upload/download checks require --write after the initial password step.")
            return
        platform.api("v1/auth/change-password", "POST", {"currentPassword": password, "newPassword": password})
        platform.csrf = None
        user = platform.warehouse_login(email, password)
        if user.get("mustChangePassword"):
            raise CheckFailed("The explicit initial-password acknowledgement did not complete.")
        print("PASS explicit initial-password test step and reauthentication")
    platform.api("warehouse/projects")
    platform.api("warehouse/robots")
    platform.api("v1/datasets")
    print("PASS warehouse projects, robots and dataset listing")
    if args.write:
        platform.write_check(args.keep_test_data)
    print("All requested smoke checks passed.")


if __name__ == "__main__":
    try:
        main()
    except (CheckFailed, KeyError, TypeError, ValueError) as error:
        # Unexpected API structures can include private values in representations.
        message = str(error) if isinstance(error, CheckFailed) else "Unexpected configuration or API response structure."
        print("FAIL " + message, file=sys.stderr)
        sys.exit(1)
