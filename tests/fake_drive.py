"""A Google Drive that behaves like the real one, as far as this tool uses it: folders found by name and parent, files kept with
their bytes, multipart and resumable uploads, downloads, deletes, and the errors Google sends. `script` is a queue of
(status, body) answers that override the real behaviour, one per call, for testing failures."""
import hashlib
import json
import re

ROOT = "ROOTFOLDER0123456789abcdef"
EMAIL = "robot@project.iam.gserviceaccount.com"


class Resp:
    def __init__(self, status=200, body=None, headers=None, content=b""):
        self.status_code, self._body, self.headers, self.content = status, body, headers or {}, content

    def json(self):
        if self._body is None:
            raise ValueError("no json")
        return self._body


def unescape(text):
    return re.sub(r"\\(.)", r"\1", text)


class FakeDrive:
    def __init__(self, *, shared=True, root_is_folder=True, script=None, delete_error=None, trash_error=None, mangle=None):
        self.files = {ROOT: {"id": ROOT, "name": "Idea Bank", "parents": [], "mime": "application/vnd.google-apps.folder",
                             "data": b"", "driveId": "SHAREDDRIVE1" if shared else None}}
        if not root_is_folder:
            self.files[ROOT]["mime"] = "text/plain"
        self.script, self.calls, self.counter, self.resumable = list(script or []), [], 0, {}
        self.mangle = mangle
        self.trash_error = trash_error                                         # (status, body) to answer every trash request with
        self.delete_error = delete_error                                       # (status, body) to answer every DELETE with

    # ---- the API -----------------------------------------------------------------------------------------------------
    def request(self, method, url, json=None, params=None, data=None, headers=None, timeout=None):
        self.calls.append({"method": method, "url": url, "json": json, "params": params or {}, "headers": headers or {}, "data": data})
        assert timeout == 120
        if self.script:
            status, body = self.script.pop(0)
            return Resp(status, body)
        params = params or {}
        if url.startswith("https://www.googleapis.com/upload/drive/v3/files"):
            return self._after_upload(self._upload(method, url, json, params, data, headers or {}))
        if url.startswith("https://upload.example/"):                                  # the resumable session address
            return self._after_upload(self._finish_resumable(url, data, headers or {}))
        assert url.startswith("https://www.googleapis.com/drive/v3/files"), url
        tail = url[len("https://www.googleapis.com/drive/v3/files"):].lstrip("/")
        if method == "GET" and not tail:
            return self._query(params)
        if method == "POST" and not tail:
            return self._create(json)
        if method == "GET" and params.get("alt") != "media" and "md5Checksum" in str(params.get("fields", "")):
            entry = self.files.get(tail)
            if not entry:
                return Resp(404, {"error": {"message": "File not found: " + tail}})
            body = entry.get("data") or b""
            return Resp(200, {"id": tail, "name": entry["name"], "size": str(len(body)), "md5Checksum": hashlib.md5(body).hexdigest(), "mimeType": entry["mime"]})
        if method == "GET":
            return self._get(tail, params)
        if method == "PATCH":
            if tail not in self.files:
                return Resp(404, {"error": {"message": "File not found: " + tail}})
            if self.trash_error:
                return Resp(*self.trash_error)
            if json.get("name"):
                self.files[tail]["name"] = json["name"]
            if json.get("trashed") is False:
                self.files[tail]["trashed"] = False
            if json.get("trashed"):
                self.files[tail]["trashed"] = True
            return Resp(200, {"id": tail})
        if method == "DELETE":
            if self.delete_error:
                return Resp(*self.delete_error)
            if tail not in self.files:
                return Resp(404, {"error": {"message": "File not found"}})
            del self.files[tail]
            return Resp(204, {})
        raise AssertionError((method, url))

    def _after_upload(self, response):
        """`mangle` (a function from bytes to bytes) damages every uploaded file, to prove that a bad copy is noticed."""
        if self.mangle and self.counter:
            last = self.files.get(f"FILE{self.counter:04d}xxxxxxxxxxxxxxxx")
            if last and last.get("data") is not None:
                last["data"] = self.mangle(last["data"])
        return response

    def _new_id(self):
        self.counter += 1
        return f"FILE{self.counter:04d}xxxxxxxxxxxxxxxx"

    def _query(self, params):
        assert params.get("supportsAllDrives") == "true" and params.get("includeItemsFromAllDrives") == "true"
        q = params["q"]
        parent = re.search(r"'((?:[^'\\]|\\.)*)' in parents", q)
        name = re.search(r"name = '((?:[^'\\]|\\.)*)'", q)
        folder_only = "mimeType = 'application/vnd.google-apps.folder'" in q
        found = [f for f in self.files.values() if not f.get("trashed") and parent and unescape(parent.group(1)) in f["parents"]
                 and (not name or f["name"] == unescape(name.group(1)))
                 and (not folder_only or f["mime"] == "application/vnd.google-apps.folder")]
        found.sort(key=lambda f: f["id"])
        size = int(params.get("pageSize", 100))
        start = int(params.get("pageToken", 0))
        page = found[start:start + size]
        body = {"files": [{"id": f["id"], "name": f["name"]} for f in page]}
        if start + size < len(found):
            body["nextPageToken"] = str(start + size)
        return Resp(200, body)

    def _create(self, meta):
        fid = self._new_id()
        self.files[fid] = {"id": fid, "name": meta["name"], "parents": list(meta["parents"]), "mime": meta["mimeType"], "data": b"",
                           "driveId": "SHAREDDRIVE1"}
        return Resp(200, {"id": fid})

    def _get(self, file_id, params):
        f = self.files.get(file_id)
        if f is None:
            return Resp(404, {"error": {"message": "File not found: " + file_id}})
        if params.get("alt") == "media":
            return Resp(200, None, content=f["data"])
        return Resp(200, {k: f[k] for k in ("id", "name") if k in f} | {"mimeType": f["mime"], "driveId": f["driveId"]})

    def _upload(self, method, url, meta, params, data, headers):
        assert params.get("supportsAllDrives") == "true"
        if params["uploadType"] == "multipart":
            boundary = headers["Content-Type"].split("boundary=")[1]
            parts = data.split(f"--{boundary}".encode())
            head_json = parts[1].split(b"\r\n\r\n", 1)[1].rsplit(b"\r\n", 1)[0]
            metadata = json.loads(head_json)
            mime_line, payload = parts[2].lstrip(b"\r\n").split(b"\r\n\r\n", 1)
            payload = payload[:-2] if payload.endswith(b"\r\n") else payload
            fid = self._new_id()
            self.files[fid] = {"id": fid, "name": metadata["name"], "parents": metadata["parents"], "mime": mime_line.split(b": ")[1].decode(),
                               "data": payload, "driveId": "SHAREDDRIVE1"}
            return Resp(200, {"id": fid})
        assert params["uploadType"] == "resumable"
        token = f"https://upload.example/session/{len(self.resumable)}"
        self.resumable[token] = {"meta": meta, "mime": headers["X-Upload-Content-Type"], "length": int(headers["X-Upload-Content-Length"])}
        return Resp(200, {}, headers={"Location": token})

    def _finish_resumable(self, url, data, headers):
        session = self.resumable[url]
        assert len(data) == session["length"] and headers["Content-Type"] == session["mime"]
        fid = self._new_id()
        self.files[fid] = {"id": fid, "name": session["meta"]["name"], "parents": session["meta"]["parents"], "mime": session["mime"],
                           "data": data, "driveId": "SHAREDDRIVE1"}
        return Resp(200, {"id": fid})

    # ---- helpers for assertions --------------------------------------------------------------------------------------
    def children(self, parent_id):
        return sorted((f["name"] for f in self.files.values() if parent_id in f["parents"] and not f.get("trashed")))

    def folder_named(self, parent_id, name):
        for f in self.files.values():
            if parent_id in f["parents"] and f["name"] == name and f["mime"] == "application/vnd.google-apps.folder" and not f.get("trashed"):
                return f["id"]
        return None

    def tree(self, folder_id=ROOT, depth=0):
        out = []
        for f in sorted((f for f in self.files.values() if folder_id in f["parents"] and not f.get("trashed")), key=lambda f: f["name"]):
            out.append("  " * depth + f["name"] + ("/" if f["mime"] == "application/vnd.google-apps.folder" else ""))
            if f["mime"] == "application/vnd.google-apps.folder":
                out += self.tree(f["id"], depth + 1)
        return out
