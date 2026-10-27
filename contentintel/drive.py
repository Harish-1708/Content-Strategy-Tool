"""Pictures, videos and other files in a Google Shared Drive, one folder for every Idea Bank item.

Layout (the root is the folder named by the IDEA_BANK_FOLDER_ID secret, inside a Shared Drive):

    <root>/
        <Brand name>/
            IB-0001 - comfort hook demo/        the files of one Idea Bank item (pictures, videos)
            IB-0002 - gift guide reel/
            Briefs/                             the briefs made for the brand (Word files)

Only the files themselves live here. Links, text, scores and summaries stay in the Google Sheet, and the sheet row holds
the link to its folder. A service account cannot store files in a normal My Drive (it has no storage of its own), which is
why the folder must be inside a Shared Drive that the service account is a member of.
"""
import json
import re
import time
import uuid

SCOPE = "https://www.googleapis.com/auth/drive"
API = "https://www.googleapis.com/drive/v3/files"
UPLOAD = "https://www.googleapis.com/upload/drive/v3/files"
FOLDER = "application/vnd.google-apps.folder"
DOCX = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
TEST_FOLDER = "connection test (safe to delete)"
SMALL_UPLOAD = 4 * 1024 * 1024          # bigger files are sent in the resumable way
NAME_LIMIT = 80
_RETRY = (429, 500, 502, 503, 504)
_SLEEP = time.sleep


class DriveError(Exception):
    """A problem a person can fix (sharing, a missing switch, a wrong folder), worded as the fix."""


def parse_folder_id(text):
    """The folder id from its address or from the bare id; None when blank. Raises DriveError for anything else."""
    text = (text or "").strip()
    if not text:
        return None
    found = re.search(r"/folders/([A-Za-z0-9_-]{15,})", text)
    if found:
        return found.group(1)
    if re.fullmatch(r"[A-Za-z0-9_-]{15,}", text):
        return text
    raise DriveError("IDEA_BANK_FOLDER_ID is not a Google Drive folder address or id. Open the folder in your Shared Drive and paste "
                     "its address (https://drive.google.com/drive/folders/...) or just the long id after /folders/.")


def connect(key_json):
    """Return (session, service_account_email). Never echoes the key."""
    try:
        info = json.loads(key_json)
    except (TypeError, ValueError) as exc:
        raise DriveError("GOOGLE_SERVICE_ACCOUNT_JSON is not valid JSON. Paste the whole key file into the secret.") from exc
    if not isinstance(info, dict) or not info.get("client_email") or not info.get("private_key"):
        raise DriveError("GOOGLE_SERVICE_ACCOUNT_JSON is not a service account key (it needs client_email and private_key).")
    from google.auth.transport.requests import AuthorizedSession
    from google.oauth2 import service_account
    creds = service_account.Credentials.from_service_account_info(info, scopes=[SCOPE])
    return AuthorizedSession(creds), info["client_email"]


def safe_name(text, limit=NAME_LIMIT):
    """A folder or file name Drive accepts and a person can read: no slashes or control characters, one line, not too long."""
    name = re.sub(r"[\x00-\x1f\\/:*?\"<>|]", " ", str(text or ""))
    name = re.sub(r"\s+", " ", name).strip(" .")
    if len(name) > limit:
        name = name[:limit - 1].rstrip() + "…"
    return name or "untitled"


def item_folder_name(idea_id, topic):
    return safe_name(f"{idea_id} - {topic}" if str(topic or "").strip() else idea_id)


def _q(text):
    return str(text).replace("\\", "\\\\").replace("'", "\\'")


def folder_url(folder_id):
    return f"https://drive.google.com/drive/folders/{folder_id}"


def file_url(file_id):
    return f"https://drive.google.com/file/d/{file_id}/view"


def _explain(code, payload, email):
    error = payload.get("error") if isinstance(payload, dict) else None
    error = error if isinstance(error, dict) else {}
    message = str(error.get("message") or "").strip()
    blob = message + " " + json.dumps(error.get("details") or []) + " " + json.dumps(error.get("errors") or [])
    if code == 403 and ("SERVICE_DISABLED" in blob or "has not been used" in message or "is disabled" in message):
        found = re.search(r"https://console\.developers\.google\.com/apis/api/drive\.googleapis\.com[^\s\"'\\]*", blob)
        link = found.group(0) if found else "https://console.cloud.google.com/apis/library/drive.googleapis.com"
        return f"The Google Drive API is not switched on for the Google Cloud project of your service account. Open {link} and press Enable, then run this again."
    if code == 403 and "storageQuotaExceeded" in blob:
        return ("The folder is in a personal My Drive, where a service account has no storage to put files in. Use a folder inside a "
                "Shared Drive that the service account is a member of.")
    if code == 403:
        return (f"The folder is not shared with {email}. Add that address to the Shared Drive as a Content manager (or share the folder "
                "with it as an Editor), and run this again.")
    if code == 404:
        return ("No Drive folder was found for IDEA_BANK_FOLDER_ID. Check that it is the id of a folder (not a file) and that "
                f"{email} can see it.")
    if code == 401:
        return "Google did not accept the service account key. Create a new key and paste it into GOOGLE_SERVICE_ACCOUNT_JSON."
    return f"Google Drive answered {code}" + (f": {message.rstrip('.')}" if message else "") + "."


def _plain(code, payload):
    """Google's own words for a failure, with no guess about the cause."""
    error = payload.get("error") if isinstance(payload, dict) else None
    message = str((error or {}).get("message") or "").strip() if isinstance(error, dict) else ""
    message = message.rstrip(".")                              # Google's messages often end with a full stop already
    return f"Google Drive answered {code}" + (f": {message}" if message else "") + "."


class DriveClient:
    def __init__(self, session, root_id, email, sleep=None):
        self.session, self.root_id, self.email = session, root_id, email
        self.sleep = sleep or _SLEEP

    def _send(self, method, url, plain=False, **kw):
        for attempt in range(3):
            resp = self.session.request(method, url, timeout=120, **kw)
            if resp.status_code in _RETRY and attempt < 2:
                self.sleep(2 * (attempt + 1))
                continue
            if resp.status_code >= 400:
                try:
                    payload = resp.json()
                except ValueError:
                    payload = {}
                raise DriveError(_plain(resp.status_code, payload) if plain else _explain(resp.status_code, payload, self.email))
            return resp
        raise DriveError("Google Drive kept answering with errors; try again later.")      # pragma: no cover

    def _json(self, method, url, **kw):
        return self._send(method, url, **kw).json()

    # ---- folders -----------------------------------------------------------------------------------------------------
    def find_folder(self, parent_id, name):
        q = f"name = '{_q(name)}' and '{_q(parent_id)}' in parents and mimeType = '{FOLDER}' and trashed = false"
        body = self._json("GET", API, params={"q": q, "fields": "files(id,name)", "supportsAllDrives": "true",
                                              "includeItemsFromAllDrives": "true", "corpora": "allDrives", "pageSize": "10"})
        files = body.get("files") or []
        return files[0]["id"] if files else None

    def ensure_folder(self, parent_id, name):
        """The id of the folder called `name` inside `parent_id`, creating it only if it is not there."""
        name = safe_name(name)
        found = self.find_folder(parent_id, name)
        if found:
            return found
        made = self._json("POST", API, params={"supportsAllDrives": "true", "fields": "id"},
                          json={"name": name, "mimeType": FOLDER, "parents": [parent_id]})
        return made["id"]

    def brand_folder(self, brand_name):
        return self.ensure_folder(self.root_id, brand_name)

    def item_folder(self, brand_folder_id, idea_id, topic):
        return self.ensure_folder(brand_folder_id, item_folder_name(idea_id, topic))

    def briefs_folder(self, brand_folder_id):
        return self.ensure_folder(brand_folder_id, "Briefs")

    def list_files(self, folder_id):
        """{file name: id} for the files in a folder."""
        out, token = {}, None
        while True:
            params = {"q": f"'{_q(folder_id)}' in parents and trashed = false", "fields": "nextPageToken,files(id,name)",
                      "supportsAllDrives": "true", "includeItemsFromAllDrives": "true", "corpora": "allDrives", "pageSize": "200"}
            if token:
                params["pageToken"] = token
            body = self._json("GET", API, params=params)
            for f in body.get("files") or []:
                out[f["name"]] = f["id"]
            token = body.get("nextPageToken")
            if not token:
                return out

    # ---- files -------------------------------------------------------------------------------------------------------
    def upload(self, parent_id, name, data, mime):
        """Put `data` (bytes) in a folder as `name`. Returns the file's id."""
        name = safe_name(name, limit=120)
        meta = {"name": name, "parents": [parent_id]}
        if len(data) <= SMALL_UPLOAD:
            boundary = uuid.uuid4().hex
            body = (f"--{boundary}\r\nContent-Type: application/json; charset=UTF-8\r\n\r\n{json.dumps(meta)}\r\n"
                    f"--{boundary}\r\nContent-Type: {mime}\r\n\r\n").encode("utf-8") + data + f"\r\n--{boundary}--".encode("utf-8")
            made = self._json("POST", UPLOAD, params={"uploadType": "multipart", "supportsAllDrives": "true", "fields": "id"},
                              data=body, headers={"Content-Type": f"multipart/related; boundary={boundary}"})
            return made["id"]
        start = self._send("POST", UPLOAD, params={"uploadType": "resumable", "supportsAllDrives": "true", "fields": "id"},
                           json=meta, headers={"X-Upload-Content-Type": mime, "X-Upload-Content-Length": str(len(data))})
        location = (start.headers or {}).get("Location")
        if not location:
            raise DriveError("Google Drive did not say where to send a large file; nothing was saved.")
        done = self._send("PUT", location, data=data, headers={"Content-Type": mime})
        return done.json()["id"]

    def download(self, file_id):
        return self._send("GET", f"{API}/{file_id}", params={"alt": "media", "supportsAllDrives": "true"}).content

    def delete(self, file_id):
        self._send("DELETE", f"{API}/{file_id}", plain=True, params={"supportsAllDrives": "true"})

    def check(self):
        """Prove the path the tool really uses: read the root folder and make a folder in it. Then try to tidy up. The tool never deletes
        anything itself, so a service account that may add files but not remove them is fine: that is only reported, with Google's own reason."""
        root = self._json("GET", f"{API}/{self.root_id}", params={"fields": "id,name,mimeType,driveId", "supportsAllDrives": "true"})
        if root.get("mimeType") != FOLDER:
            raise DriveError("IDEA_BANK_FOLDER_ID is a file, not a folder. Use the id of the folder in your Shared Drive.")
        if not root.get("driveId"):
            raise DriveError("The folder is in a personal My Drive, where a service account has no storage to put files in. Use a "
                             "folder inside a Shared Drive that the service account is a member of.")
        test = self.ensure_folder(self.root_id, TEST_FOLDER)
        left_behind = ""
        try:
            self.delete(test)
        except DriveError as exc:
            left_behind = str(exc)
        return {"name": root.get("name", ""), "left_behind": left_behind}
