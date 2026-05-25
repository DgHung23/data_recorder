import argparse
import os
import pickle
import time
from datetime import datetime
from pathlib import Path


SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_WATCH_FOLDER = SCRIPT_DIR / "output"
DEFAULT_TOKEN_PATH = SCRIPT_DIR / "token.pickle"
DEFAULT_CHECK_INTERVAL_SECONDS = 15
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".heic"}

# Optional in-code defaults. CLI arguments still take priority, then environment
# variables, then these values.
HARDCODED_DRIVE_FOLDER_ID = ""
HARDCODED_CREDENTIALS_PATH = ""

# Use full Drive scope so uploads can target an existing folder by ID. If this
# scope changes, the cached token must be recreated.
SCOPES = ["https://www.googleapis.com/auth/drive"]


def now_text():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def parse_args():
    parser = argparse.ArgumentParser(
        description="Watch a folder and upload new image files to Google Drive."
    )
    parser.add_argument(
        "--watch-folder",
        type=Path,
        default=DEFAULT_WATCH_FOLDER,
        help=f"Folder to watch. Default: {DEFAULT_WATCH_FOLDER}",
    )
    parser.add_argument(
        "--drive-folder-id",
        default=None,
        help=(
            "Google Drive destination folder ID. Priority: CLI argument, "
            "DRIVE_FOLDER_ID environment variable, HARDCODED_DRIVE_FOLDER_ID."
        ),
    )
    parser.add_argument(
        "--credentials",
        type=Path,
        default=None,
        help=(
            "OAuth client secret JSON file. Priority: CLI argument, "
            "GOOGLE_DRIVE_CREDENTIALS environment variable, "
            "HARDCODED_CREDENTIALS_PATH, first client_secret*.json in this project."
        ),
    )
    parser.add_argument(
        "--token",
        type=Path,
        default=DEFAULT_TOKEN_PATH,
        help=f"OAuth token cache path. Default: {DEFAULT_TOKEN_PATH}",
    )
    parser.add_argument(
        "--interval",
        type=int,
        default=DEFAULT_CHECK_INTERVAL_SECONDS,
        help=f"Folder check interval in seconds. Default: {DEFAULT_CHECK_INTERVAL_SECONDS}.",
    )
    parser.add_argument(
        "--upload-existing",
        action="store_true",
        help="Upload existing images on startup. By default only new images are uploaded.",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Check once and exit. Useful for testing or scheduled jobs.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print files that would be uploaded without authenticating or calling Google Drive.",
    )

    return parser.parse_args()


def first_non_empty(*values):
    for value in values:
        if value:
            return value

    return None


def resolve_drive_folder_id(cli_folder_id):
    return first_non_empty(
        cli_folder_id,
        os.getenv("DRIVE_FOLDER_ID"),
        HARDCODED_DRIVE_FOLDER_ID,
    )


def resolve_credentials_path(credentials_path):
    credentials_path = first_non_empty(
        credentials_path,
        os.getenv("GOOGLE_DRIVE_CREDENTIALS"),
        HARDCODED_CREDENTIALS_PATH,
    )

    if credentials_path:
        path = Path(credentials_path).expanduser().resolve()
        if not path.exists():
            raise SystemExit(f"Credentials file not found: {path}")
        return path

    candidates = sorted(SCRIPT_DIR.glob("client_secret*.json"))
    if not candidates:
        raise SystemExit(
            "No OAuth client secret JSON found. Put client_secret*.json in this project "
            "or pass --credentials PATH."
        )

    return candidates[0]


def token_has_required_scopes(creds):
    token_scopes = set(
        getattr(creds, "scopes", None) or getattr(creds, "granted_scopes", None) or []
    )
    required_scopes = set(SCOPES)

    return required_scopes.issubset(token_scopes)


def build_drive_service(credentials_path, token_path):
    try:
        from google.auth.exceptions import RefreshError
        from google.auth.transport.requests import Request
        from google_auth_oauthlib.flow import InstalledAppFlow
        from googleapiclient.discovery import build
    except ImportError as exc:
        raise SystemExit(
            "Missing Google Drive dependencies. Install them with: pip install -r requirements.txt"
        ) from exc

    token_path = token_path.expanduser().resolve()
    creds = None

    if token_path.exists():
        with token_path.open("rb") as token:
            creds = pickle.load(token)

        if not token_has_required_scopes(creds):
            print(
                f"[{now_text()}] Cached token does not include required Drive scope. "
                "Starting a new Google login."
            )
            creds = None

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            try:
                creds.refresh(Request())
            except RefreshError:
                print(
                    f"[{now_text()}] Cached token could not be refreshed. "
                    "Starting a new Google login."
                )
                creds = None

        if not creds or not creds.valid:
            credentials_path = resolve_credentials_path(credentials_path)
            flow = InstalledAppFlow.from_client_secrets_file(
                str(credentials_path),
                SCOPES,
            )
            creds = flow.run_local_server(port=0)

        token_path.parent.mkdir(parents=True, exist_ok=True)
        with token_path.open("wb") as token:
            pickle.dump(creds, token)

    return build("drive", "v3", credentials=creds)


def get_image_files(folder):
    if not folder.exists():
        return set()

    files = set()
    for file_path in folder.iterdir():
        if file_path.is_file() and file_path.suffix.lower() in IMAGE_EXTENSIONS:
            files.add(file_path.resolve())

    return files


def wait_until_file_is_stable(file_path, checks=3, delay_seconds=0.5):
    last_size = None

    for _ in range(checks):
        current_size = file_path.stat().st_size
        if last_size is not None and current_size == last_size:
            return

        last_size = current_size
        time.sleep(delay_seconds)


def upload_to_drive(service, file_path, drive_folder_id):
    from googleapiclient.http import MediaFileUpload

    file_metadata = {
        "name": file_path.name,
        "parents": [drive_folder_id],
    }
    media = MediaFileUpload(str(file_path), resumable=True)
    uploaded_file = (
        service.files()
        .create(body=file_metadata, media_body=media, fields="id")
        .execute()
    )

    print(f"[{now_text()}] Uploaded: {file_path.name}")
    print(f"[{now_text()}] File ID: {uploaded_file.get('id')}")


def check_once(service, watch_folder, drive_folder_id, known_files, dry_run):
    print(f"\n[{now_text()}] Checking for new images...")
    current_files = get_image_files(watch_folder)
    new_files = sorted(current_files - known_files)

    if not new_files:
        print(f"[{now_text()}] No new images found.")
        return current_files

    print(f"[{now_text()}] Found {len(new_files)} new image(s).")
    successful_files = set()

    for file_path in new_files:
        print(f"[{now_text()}] New image detected: {file_path}")

        try:
            wait_until_file_is_stable(file_path)
            if dry_run:
                print(f"[{now_text()}] Dry run: would upload {file_path.name}")
            else:
                upload_to_drive(service, file_path, drive_folder_id)
            successful_files.add(file_path)
        except Exception as exc:
            print(f"[{now_text()}] Upload failed for {file_path.name}: {exc}")

    return known_files | successful_files


def main():
    args = parse_args()
    drive_folder_id = resolve_drive_folder_id(args.drive_folder_id)

    if args.interval <= 0:
        raise SystemExit("--interval must be greater than 0.")

    watch_folder = args.watch_folder.expanduser().resolve()
    watch_folder.mkdir(parents=True, exist_ok=True)

    if not args.dry_run and not drive_folder_id:
        raise SystemExit(
            "Missing Google Drive folder ID. Pass --drive-folder-id, set DRIVE_FOLDER_ID, "
            "or set HARDCODED_DRIVE_FOLDER_ID in upload.py."
        )

    service = None
    if not args.dry_run:
        service = build_drive_service(args.credentials, args.token)

    print(f"[{now_text()}] Started watching folder: {watch_folder}")
    if args.dry_run:
        print(f"[{now_text()}] Dry run mode is enabled.")

    known_files = set() if args.upload_existing else get_image_files(watch_folder)

    while True:
        known_files = check_once(
            service=service,
            watch_folder=watch_folder,
            drive_folder_id=drive_folder_id,
            known_files=known_files,
            dry_run=args.dry_run,
        )

        if args.once:
            return

        print(f"[{now_text()}] Waiting {args.interval} seconds...\n")
        time.sleep(args.interval)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print(f"\n[{now_text()}] Stopped.")
