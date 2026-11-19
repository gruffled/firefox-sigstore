#!/usr/bin/env python3
"""Sign the extension as an unlisted add-on through the addons.mozilla.org API.

Usage:
    amo_sign.py validate <unsigned.zip>
        Upload and run AMO's validator without creating a version.
    amo_sign.py sign <unsigned.zip> <signed.xpi>
        Upload, create an unlisted version, wait for Mozilla to sign it and
        download the signed file. If AMO already has that version, it skips
        the upload and downloads the signed file, so a re-run is safe.

Reads the AMO API key and secret from WEB_EXT_API_KEY and WEB_EXT_API_SECRET
(https://addons.mozilla.org/developers/addon/api/key/), the same variables
`web-ext sign` uses. Uses only the standard library, so the job holding those
secrets runs no third-party code.
"""
import base64
import hashlib
import hmac
import json
import os
import pathlib
import secrets
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile

API = "https://addons.mozilla.org/api/v5"
POLL_SECONDS = 10
TIMEOUT_SECONDS = 30 * 60


class ApiError(Exception):
    def __init__(self, method, url, code, detail):
        super().__init__(f"{method} {url} failed: HTTP {code}\n{detail}")
        self.code = code


class _SameHostAuthRedirect(urllib.request.HTTPRedirectHandler):
    """Drops the Authorization header when a redirect leaves the original host."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        new = super().redirect_request(req, fp, code, msg, headers, newurl)
        if new is not None and urllib.parse.urlsplit(newurl).hostname != urllib.parse.urlsplit(req.full_url).hostname:
            new.remove_header("Authorization")
        return new


_opener = urllib.request.build_opener(_SameHostAuthRedirect)


def _b64url(raw):
    return base64.urlsafe_b64encode(raw).rstrip(b"=")


def _auth_header():
    key = os.environ.get("WEB_EXT_API_KEY")
    secret = os.environ.get("WEB_EXT_API_SECRET")
    if not key or not secret:
        sys.exit("WEB_EXT_API_KEY and WEB_EXT_API_SECRET must be set")
    now = int(time.time())
    claims = {"iss": key, "jti": secrets.token_hex(16), "iat": now, "exp": now + 60}
    signing_input = b".".join(
        _b64url(json.dumps(part, separators=(",", ":")).encode())
        for part in ({"alg": "HS256", "typ": "JWT"}, claims)
    )
    signature = _b64url(hmac.new(secret.encode(), signing_input, hashlib.sha256).digest())
    return "JWT " + (signing_input + b"." + signature).decode()


def _call(method, path_or_url, data=None, content_type=None, raw=False):
    url = path_or_url if path_or_url.startswith("https://") else API + path_or_url
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Authorization", _auth_header())
    req.add_header("User-Agent", "firefox-sigstore-release")
    if content_type:
        req.add_header("Content-Type", content_type)
    try:
        with _opener.open(req, timeout=120) as resp:
            body = resp.read()
    except urllib.error.HTTPError as err:
        raise ApiError(method, url, err.code, err.read().decode(errors="replace")[:2000]) from None
    return body if raw else json.loads(body)


def _poll(path):
    """GET that retries on network errors and 5xx responses."""
    for attempt in range(5):
        try:
            return _call("GET", path)
        except ApiError as err:
            if err.code < 500:
                raise
            last = err
        except urllib.error.URLError as err:
            last = err
        time.sleep(POLL_SECONDS * (attempt + 1))
    raise last


def _wait(what, fetch, done):
    deadline = time.monotonic() + TIMEOUT_SECONDS
    while True:
        result = fetch()
        if done(result):
            return result
        if time.monotonic() > deadline:
            sys.exit(f"timed out after {TIMEOUT_SECONDS // 60} minutes waiting for {what}; re-run to pick it up")
        time.sleep(POLL_SECONDS)


def _addon_id(manifest):
    return manifest["browser_specific_settings"]["gecko"]["id"]


def _read_manifest(zip_path):
    with zipfile.ZipFile(zip_path) as z:
        return json.loads(z.read("manifest.json"))


def _versions_path(guid):
    return f"/addons/addon/{urllib.parse.quote(guid, safe='@')}/versions/"


def upload(zip_path):
    """Uploads the zip and waits for AMO's validator. Returns the upload UUID."""
    zip_path = pathlib.Path(zip_path)
    boundary = secrets.token_hex(16)
    body = b"".join([
        f'--{boundary}\r\nContent-Disposition: form-data; name="channel"\r\n\r\nunlisted\r\n'.encode(),
        f'--{boundary}\r\nContent-Disposition: form-data; name="upload"; filename="{zip_path.name}"\r\n'
        "Content-Type: application/zip\r\n\r\n".encode(),
        zip_path.read_bytes(),
        f"\r\n--{boundary}--\r\n".encode(),
    ])
    up = _call("POST", "/addons/upload/", data=body, content_type=f"multipart/form-data; boundary={boundary}")
    print(f"uploaded {zip_path.name} as {up['uuid']}, waiting for validation")
    up = _wait("validation", lambda: _poll(f"/addons/upload/{up['uuid']}/"), lambda u: u["processed"])

    validation = up.get("validation") or {}
    print(
        f"validation: {validation.get('errors', '?')} errors, "
        f"{validation.get('warnings', '?')} warnings, {validation.get('notices', '?')} notices"
    )
    for message in validation.get("messages", []):
        if message.get("type") in ("error", "warning"):
            where = f" ({message['file']})" if message.get("file") else ""
            print(f"  {message['type']}: {message.get('message')}{where}")
    if not up["valid"]:
        sys.exit("AMO validation failed")
    return up["uuid"]


def _existing_version(guid, version):
    try:
        return _call("GET", f"{_versions_path(guid)}v{version}/")
    except ApiError as err:
        if err.code == 404:
            return None
        raise


def sign(zip_path, out_path):
    manifest = _read_manifest(zip_path)
    guid, version = _addon_id(manifest), manifest["version"]

    existing = _existing_version(guid, version)
    if existing:
        print(f"AMO already has {guid} {version} (id {existing['id']}), downloading it")
        version_id = existing["id"]
    else:
        upload_uuid = upload(zip_path)
        created = _call(
            "POST",
            _versions_path(guid),
            data=json.dumps({"upload": upload_uuid}).encode(),
            content_type="application/json",
        )
        version_id = created["id"]
        print(f"created {guid} {created['version']} (id {version_id}), waiting for Mozilla to sign it")

    def file_status(v):
        status = (v.get("file") or {}).get("status")
        if status == "disabled":
            sys.exit(f"AMO rejected or disabled {guid} {version}")
        return status == "public"

    signed = _wait("signing", lambda: _poll(f"{_versions_path(guid)}{version_id}/"), file_status)
    data = _call("GET", signed["file"]["url"], raw=True)

    out_path = pathlib.Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_bytes(data)
    with zipfile.ZipFile(out_path) as z:
        if "META-INF/mozilla.rsa" not in z.namelist():
            sys.exit(f"{out_path} has no Mozilla signature")
    print(f"signed: {out_path} sha256={hashlib.sha256(data).hexdigest()}")


def main(argv):
    try:
        if len(argv) == 2 and argv[0] == "validate":
            upload(argv[1])
        elif len(argv) == 3 and argv[0] == "sign":
            sign(argv[1], argv[2])
        else:
            sys.exit(__doc__)
    except ApiError as err:
        sys.exit(str(err))


if __name__ == "__main__":
    main(sys.argv[1:])
