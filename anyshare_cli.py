#!/usr/bin/env python3
import argparse
import os
import re
import sys
import time
from urllib.parse import urlparse, quote

try:
    import requests
except Exception:
    print("This script requires the 'requests' package. Install with: python3 -m pip install requests")
    sys.exit(1)

DEFAULT_API_TIMEOUT = 30
DEFAULT_CONNECT_TIMEOUT = 10
DEFAULT_TRANSFER_TIMEOUT = 3600


class AnyshareError(RuntimeError):
    pass


def extract_link_id(link_url):
    match = re.search(r"/link/([A-Za-z0-9]{30,64})", link_url)
    if not match:
        match = re.search(r"([A-Za-z0-9]{30,64})", link_url)
    if not match:
        raise AnyshareError("Could not find link id in the URL.")
    return match.group(1)


def base_url_from_link(link_url):
    parsed = urlparse(link_url)
    if not parsed.scheme or not parsed.netloc:
        raise AnyshareError("Link must include scheme and host, e.g. https://example.com/link/...")
    return f"{parsed.scheme}://{parsed.netloc}"


def get_link_token(session, link_id):
    cookie_name = f"link_token:{link_id}"
    for cookie in session.cookies:
        if cookie.name == cookie_name:
            return cookie.value
    return None


def ensure_link_token(session, link_url, base_url, link_id):
    session.get(link_url, allow_redirects=True, timeout=DEFAULT_API_TIMEOUT)
    token = get_link_token(session, link_id)
    if token:
        return token
    alt_url = f"{base_url}/anyshare/zh-cn/link/{link_id}"
    session.get(alt_url, allow_redirects=True, timeout=DEFAULT_API_TIMEOUT)
    token = get_link_token(session, link_id)
    if not token:
        raise AnyshareError("Failed to obtain link token. The link may be invalid or expired.")
    return token


def check_share_info(session, base_url, link_id):
    url = f"{base_url}/api/shared-link/v1/links/{link_id}"
    resp = session.get(url, timeout=DEFAULT_API_TIMEOUT)
    if not resp.ok:
        return
    data = resp.json()
    if data.get("password_required"):
        raise AnyshareError("This share link requires a password and is not supported by this script.")


def api_headers(token):
    return {
        "Authorization": f"Bearer {token}",
        "X-Requested-With": "XMLHttpRequest",
    }


def api_url(base_url, path):
    if path.startswith("/"):
        path = path[1:]
    return f"{base_url}/api/{path}"


def raise_for_status(resp, action):
    if resp.status_code < 400:
        return
    message = resp.text
    try:
        data = resp.json()
        if isinstance(data, dict) and data.get("message"):
            message = data["message"]
    except Exception:
        pass
    raise AnyshareError(f"{action} failed: HTTP {resp.status_code} {message}")


def api_get_json(session, base_url, token, path, params=None, action="GET"):
    resp = session.get(
        api_url(base_url, path),
        params=params,
        headers=api_headers(token),
        timeout=DEFAULT_API_TIMEOUT,
    )
    raise_for_status(resp, action)
    return resp.json()


def api_post_json(session, base_url, token, path, payload, action="POST"):
    resp = session.post(
        api_url(base_url, path),
        json=payload,
        headers=api_headers(token),
        timeout=DEFAULT_API_TIMEOUT,
    )
    raise_for_status(resp, action)
    return resp.json()


def item_docid(item):
    if isinstance(item, dict):
        return item.get("docid") or item.get("id")
    return None


def get_entry_item(session, base_url, token):
    data = api_get_json(session, base_url, token, "/efast/v1/entry-item", action="entry-item")
    if not isinstance(data, list) or not data:
        raise AnyshareError("Entry item not found in share link.")
    return data[0]


def list_folder(session, base_url, token, folder_docid):
    if not folder_docid:
        raise AnyshareError("Folder id is empty.")
    dirs = []
    files = []
    marker = ""
    while True:
        params = {
            "limit": 100,
            "sort": "name",
            "direction": "asc",
            "permission_attributes_required": "false",
        }
        if marker:
            params["marker"] = marker
        encoded = quote(folder_docid, safe="")
        path = f"/efast/v1/folders/{encoded}/sub_objects"
        data = api_get_json(session, base_url, token, path, params=params, action="list folder")
        dirs.extend(data.get("dirs", []) or [])
        files.extend(data.get("files", []) or [])
        marker = data.get("next_marker") or ""
        if not marker:
            break
    return dirs, files


def resolve_remote_path(session, base_url, token, root_docid, remote_path):
    if not remote_path:
        return root_docid
    path = remote_path.replace("\\", "/")
    parts = [p for p in path.split("/") if p]
    current = root_docid
    for part in parts:
        dirs, _ = list_folder(session, base_url, token, current)
        match = None
        for item in dirs:
            if item.get("name") == part:
                match = item
                break
        if not match:
            raise AnyshareError(f"Remote folder not found: {part}")
        current = item_docid(match)
    return current


def human_size(size):
    if size is None:
        return "-"
    units = ["B", "KB", "MB", "GB", "TB"]
    value = float(size)
    for unit in units:
        if value < 1024 or unit == units[-1]:
            if unit == "B":
                return f"{int(value)} {unit}"
            return f"{value:.2f} {unit}"
        value /= 1024
    return f"{value:.2f} TB"


def build_transfer_timeout(seconds):
    if seconds is None:
        seconds = DEFAULT_TRANSFER_TIMEOUT
    if seconds <= 0:
        return None
    return (DEFAULT_CONNECT_TIMEOUT, seconds)


class ProgressPrinter:
    def __init__(self, total_bytes, label="Uploading", enabled=True):
        self.total = total_bytes
        self.enabled = bool(enabled and total_bytes and sys.stderr.isatty())
        self.label = label
        self.start = time.monotonic()
        self.last_print = 0.0
        self.transferred = 0
        self.last_len = 0

    def update(self, delta):
        if not self.enabled:
            return
        self.transferred += delta
        now = time.monotonic()
        if now - self.last_print < 0.2 and self.transferred < self.total:
            return
        self.last_print = now
        percent = self.transferred / self.total * 100
        elapsed = max(now - self.start, 0.001)
        speed = self.transferred / elapsed
        message = (
            f"\r{self.label}: {percent:6.2f}% "
            f"({human_size(self.transferred)}/{human_size(self.total)}) "
            f"{human_size(speed)}/s"
        )
        pad = max(0, self.last_len - len(message))
        self.last_len = len(message)
        sys.stderr.write(message + (" " * pad))
        sys.stderr.flush()

    def finish(self):
        if not self.enabled:
            return
        self.update(0)
        sys.stderr.write("\n")
        sys.stderr.flush()

    def abort(self):
        if not self.enabled:
            return
        sys.stderr.write("\n")
        sys.stderr.flush()


class ProgressFile:
    def __init__(self, fp, total_bytes, progress):
        self._fp = fp
        self._total = total_bytes
        self._progress = progress

    def read(self, size=-1):
        data = self._fp.read(size)
        if data:
            self._progress.update(len(data))
        return data

    def __getattr__(self, name):
        return getattr(self._fp, name)

    def __len__(self):
        return self._total


def _escape_multipart_value(value):
    return str(value).replace("\\", "\\\\").replace('"', '\\"')


def _multipart_field_bytes(boundary, name, value):
    header = (
        f"--{boundary}\r\n"
        f"Content-Disposition: form-data; name=\"{_escape_multipart_value(name)}\"\r\n\r\n"
        f"{value}\r\n"
    )
    return header.encode("utf-8")


def _multipart_file_header_bytes(boundary, field_name, file_name, content_type):
    header = (
        f"--{boundary}\r\n"
        f"Content-Disposition: form-data; name=\"{_escape_multipart_value(field_name)}\"; "
        f"filename=\"{_escape_multipart_value(file_name)}\"\r\n"
        f"Content-Type: {content_type}\r\n\r\n"
    )
    return header.encode("utf-8")


def _multipart_content_length(fields, boundary, file_field, file_name, content_type, file_size):
    length = 0
    for name, value in fields.items():
        length += len(_multipart_field_bytes(boundary, name, value))
    length += len(_multipart_file_header_bytes(boundary, file_field, file_name, content_type))
    length += file_size
    length += len(b"\r\n")
    length += len(f"--{boundary}--\r\n".encode("utf-8"))
    return length


def _iter_multipart(fields, file_field, file_path, file_name, content_type, boundary, progress, chunk_size=1024 * 1024):
    for name, value in fields.items():
        yield _multipart_field_bytes(boundary, name, value)
    yield _multipart_file_header_bytes(boundary, file_field, file_name, content_type)
    with open(file_path, "rb") as handle:
        while True:
            chunk = handle.read(chunk_size)
            if not chunk:
                break
            progress.update(len(chunk))
            yield chunk
    yield b"\r\n"
    yield f"--{boundary}--\r\n".encode("utf-8")


def print_listing(dirs, files):
    if not dirs and not files:
        print("Empty folder.")
        return
    if dirs:
        print("Directories:")
        for idx, item in enumerate(dirs, 1):
            print(f"  [D{idx:02d}] {item.get('name', '-')}")
    if files:
        print("Files:")
        for idx, item in enumerate(files, 1):
            size = human_size(item.get("size"))
            print(f"  [F{idx:02d}] {item.get('name', '-')} ({size})")


def choose_file(files, name=None, index=None):
    if not files:
        raise AnyshareError("No files in this folder.")
    if name:
        matches = [f for f in files if f.get("name") == name]
        if not matches:
            raise AnyshareError(f"File not found: {name}")
        if len(matches) > 1:
            raise AnyshareError(f"Multiple files share the name: {name}")
        return matches[0]
    if index is None:
        for idx, item in enumerate(files, 1):
            size = human_size(item.get("size"))
            print(f"{idx:02d}. {item.get('name', '-')} ({size})")
        raw = input("Select a file index to download: ").strip()
        if not raw.isdigit():
            raise AnyshareError("Invalid index input.")
        index = int(raw)
    if index < 1 or index > len(files):
        raise AnyshareError("File index out of range.")
    return files[index - 1]


def get_download_url(session, base_url, token, docid, savename, authtype, usehttps, rev=None):
    payload = {
        "docid": docid,
        "authtype": str(authtype),
        "savename": savename,
        "usehttps": bool(usehttps),
    }
    if rev:
        payload["rev"] = rev
    data = api_post_json(session, base_url, token, "/efast/v1/file/osdownload", payload, action="osdownload")
    authrequest = data.get("authrequest")
    if not isinstance(authrequest, list) or len(authrequest) < 2:
        raise AnyshareError("Unexpected download authrequest format.")
    method = authrequest[0] or "GET"
    url = authrequest[1]
    return method, url


def download_file(session, method, url, out_path, timeout):
    resp = session.request(method, url, stream=True, timeout=timeout)
    resp.raise_for_status()
    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
    with open(out_path, "wb") as f:
        for chunk in resp.iter_content(chunk_size=1024 * 1024):
            if chunk:
                f.write(chunk)


def begin_upload(session, base_url, token, folder_docid, file_path, file_name, ondup):
    stat = os.stat(file_path)
    payload = {
        "client_mtime": int(stat.st_mtime * 1000),
        "docid": folder_docid,
        "length": stat.st_size,
        "name": file_name,
        "ondup": int(ondup),
        "reqmethod": "POST",
    }
    data = api_post_json(session, base_url, token, "/efast/v1/file/osbeginupload", payload, action="osbeginupload")
    authrequest = data.get("authrequest")
    if not isinstance(authrequest, list) or len(authrequest) < 2:
        raise AnyshareError("Unexpected upload authrequest format.")
    method = authrequest[0]
    url = authrequest[1]
    fields = {}
    for entry in authrequest[2:]:
        if ": " in entry:
            key, value = entry.split(": ", 1)
            fields[key] = value
    return data, method, url, fields


def finish_upload(session, base_url, token, docid, rev, csflevel):
    payload = {
        "docid": docid,
        "rev": rev,
        "csflevel": int(csflevel),
    }
    api_post_json(session, base_url, token, "/efast/v1/file/osendupload", payload, action="osendupload")


def run_list(args, session, base_url, token, root_docid):
    target_docid = resolve_remote_path(session, base_url, token, root_docid, args.path)
    dirs, files = list_folder(session, base_url, token, target_docid)
    print_listing(dirs, files)


def run_download(args, session, base_url, token, root_docid):
    target_docid = resolve_remote_path(session, base_url, token, root_docid, args.path)
    _, files = list_folder(session, base_url, token, target_docid)
    file_item = choose_file(files, name=args.name, index=args.index)
    docid = item_docid(file_item)
    if not docid:
        raise AnyshareError("Selected file has no docid.")
    method, url = get_download_url(
        session,
        base_url,
        token,
        docid,
        file_item.get("name") or "download",
        args.authtype,
        True,
        file_item.get("rev"),
    )
    out_path = args.out or file_item.get("name") or "download"
    if os.path.isdir(out_path):
        out_path = os.path.join(out_path, file_item.get("name") or "download")
    if os.path.exists(out_path) and not args.overwrite:
        raise AnyshareError("Output file already exists. Use --overwrite to replace it.")
    download_file(session, method, url, out_path, build_transfer_timeout(args.timeout))
    print(f"Downloaded to: {out_path}")


def run_upload(args, session, base_url, token, root_docid):
    if not os.path.isfile(args.file):
        raise AnyshareError(f"Local file not found: {args.file}")
    target_docid = resolve_remote_path(session, base_url, token, root_docid, args.path)
    file_name = args.name or os.path.basename(args.file)
    data, method, url, fields = begin_upload(
        session, base_url, token, target_docid, args.file, file_name, args.ondup
    )
    file_size = os.path.getsize(args.file)
    progress = ProgressPrinter(file_size, enabled=not args.no_progress)
    content_type = fields.get("Content-Type") or "application/octet-stream"
    boundary = f"----anysharecli{int(time.time() * 1000)}"
    headers = {
        "Content-Type": f"multipart/form-data; boundary={boundary}",
        "Content-Length": str(
            _multipart_content_length(fields, boundary, "file", file_name, content_type, file_size)
        ),
    }
    body = _iter_multipart(fields, "file", args.file, file_name, content_type, boundary, progress)
    try:
        resp = session.request(
            method,
            url,
            data=body,
            headers=headers,
            timeout=build_transfer_timeout(args.timeout),
        )
        resp.raise_for_status()
    except KeyboardInterrupt:
        progress.abort()
        raise AnyshareError("Upload interrupted by user.")
    progress.finish()
    finish_upload(session, base_url, token, data.get("docid"), data.get("rev"), 0)
    print(f"Upload finished: {file_name}")


def build_parser():
    parser = argparse.ArgumentParser(description="Anyshare share-link upload/download helper")
    parser.add_argument("--link", required=True, help="Anyshare share link URL")
    sub = parser.add_subparsers(dest="command")

    p_list = sub.add_parser("list", help="List folder contents")
    p_list.add_argument("--path", default="", help="Remote subfolder path, like sub/dir")

    p_download = sub.add_parser("download", help="Download a file from the share")
    p_download.add_argument("--path", default="", help="Remote subfolder path, like sub/dir")
    p_download.add_argument("--name", default="", help="Exact file name to download")
    p_download.add_argument("--index", type=int, help="File index (from list output)")
    p_download.add_argument("--out", default="", help="Output file or directory path")
    p_download.add_argument("--overwrite", action="store_true", help="Overwrite output file if exists")
    p_download.add_argument("--authtype", default="1", help="Auth type string for osdownload (default: 1)")
    p_download.add_argument(
        "--timeout",
        type=int,
        default=DEFAULT_TRANSFER_TIMEOUT,
        help="Transfer timeout seconds (default: 3600, 0 to disable)",
    )

    p_upload = sub.add_parser("upload", help="Upload a local file to the share")
    p_upload.add_argument("--file", required=True, help="Local file path")
    p_upload.add_argument("--path", default="", help="Remote subfolder path, like sub/dir")
    p_upload.add_argument("--name", default="", help="Remote file name override")
    p_upload.add_argument("--ondup", type=int, default=1, help="Duplicate policy (default: 1)")
    p_upload.add_argument(
        "--timeout",
        type=int,
        default=DEFAULT_TRANSFER_TIMEOUT,
        help="Transfer timeout seconds (default: 3600, 0 to disable)",
    )
    p_upload.add_argument("--no-progress", action="store_true", help="Disable upload progress display")

    return parser


def main():
    parser = build_parser()
    args = parser.parse_args()
    if not args.command:
        parser.print_help()
        return 2

    session = requests.Session()
    session.headers.update({
        "User-Agent": "anyshare-cli/1.0",
        "Accept": "application/json, text/plain, */*",
    })

    link_id = extract_link_id(args.link)
    base_url = base_url_from_link(args.link)

    check_share_info(session, base_url, link_id)
    token = ensure_link_token(session, args.link, base_url, link_id)

    entry = get_entry_item(session, base_url, token)
    if entry.get("type") != "folder":
        raise AnyshareError("The shared item is not a folder. Upload/list require a folder link.")
    root_docid = item_docid(entry)
    if not root_docid:
        raise AnyshareError("Root folder docid not found.")

    if args.command == "list":
        run_list(args, session, base_url, token, root_docid)
    elif args.command == "download":
        run_download(args, session, base_url, token, root_docid)
    elif args.command == "upload":
        run_upload(args, session, base_url, token, root_docid)
    else:
        parser.print_help()
        return 2
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except AnyshareError as exc:
        print(f"Error: {exc}")
        sys.exit(1)
    except requests.RequestException as exc:
        print(f"Network error: {exc}")
        sys.exit(1)
