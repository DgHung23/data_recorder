import argparse
import contextlib
import os
import platform
import subprocess
import time
from datetime import datetime
from pathlib import Path


DEFAULT_INTERVAL_SECONDS = 15
DEFAULT_OUTPUT_DIR = Path("output")
DEFAULT_MAX_CAMERAS = 10
DEFAULT_DIAGNOSTIC_FRAMES = 30
DEFAULT_BLACK_FRAME_THRESHOLD = 3.0
DEFAULT_CAMERA_WIDTH = 1920
DEFAULT_CAMERA_HEIGHT = 1080
DEFAULT_CAMERA_FOURCC = "MJPG"


def load_cv2():
    try:
        import cv2
    except ImportError as exc:
        raise SystemExit(
            "Missing dependency: opencv-python\n"
            "Install it with: pip install -r requirements.txt"
        ) from exc

    return cv2


def backend_options(cv2):
    options = {"any": cv2.CAP_ANY}

    if hasattr(cv2, "CAP_DSHOW"):
        options["dshow"] = cv2.CAP_DSHOW

    if hasattr(cv2, "CAP_MSMF"):
        options["msmf"] = cv2.CAP_MSMF

    return options


def preferred_backend_name():
    if platform.system().lower() == "windows":
        return "dshow"

    return "any"


def resolve_backend_name(backend_name):
    if backend_name == "auto":
        return preferred_backend_name()

    return backend_name


def get_backend(cv2, backend_name):
    backends = backend_options(cv2)
    resolved_name = resolve_backend_name(backend_name)

    if resolved_name not in backends:
        available = ", ".join(["auto", *sorted(backends)])
        raise SystemExit(f"Unknown backend '{backend_name}'. Available: {available}")

    return backends[resolved_name]


def normalize_fourcc(fourcc):
    if fourcc is None:
        return None

    normalized = fourcc.strip().upper()
    if normalized in {"", "NONE", "AUTO"}:
        return None

    if len(normalized) != 4:
        raise SystemExit("--fourcc must be 4 characters, or 'none' to disable it.")

    return normalized


def apply_capture_settings(cv2, capture, width=None, height=None, fps=None, fourcc=None):
    fourcc = normalize_fourcc(fourcc)

    if fourcc:
        capture.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*fourcc))

    if width:
        capture.set(cv2.CAP_PROP_FRAME_WIDTH, width)

    if height:
        capture.set(cv2.CAP_PROP_FRAME_HEIGHT, height)

    if fps:
        capture.set(cv2.CAP_PROP_FPS, fps)


def capture_properties(cv2, capture):
    fourcc_value = int(capture.get(cv2.CAP_PROP_FOURCC))
    fourcc_chars = [chr((fourcc_value >> 8 * i) & 0xFF) for i in range(4)]
    fourcc = "".join(fourcc_chars).strip("\x00").strip()
    if len(fourcc) != 4 or not all(32 <= ord(char) <= 126 for char in fourcc):
        fourcc = None

    fps = capture.get(cv2.CAP_PROP_FPS)

    return {
        "width": int(capture.get(cv2.CAP_PROP_FRAME_WIDTH)),
        "height": int(capture.get(cv2.CAP_PROP_FRAME_HEIGHT)),
        "fps": fps if fps and fps > 0 else None,
        "fourcc": fourcc,
    }


def scan_backend_names(cv2, backend_name):
    if backend_name != "auto":
        return [resolve_backend_name(backend_name)]

    if platform.system().lower() == "windows":
        preferred = preferred_backend_name()
        candidates = [preferred, "msmf", "any"]
    else:
        candidates = [preferred_backend_name(), "any"]

    available_backends = backend_options(cv2)
    scan_order = []
    for candidate in candidates:
        if candidate in available_backends and candidate not in scan_order:
            scan_order.append(candidate)

    return scan_order


@contextlib.contextmanager
def suppress_native_stderr():
    stderr_fd = 2
    saved_stderr_fd = os.dup(stderr_fd)
    null_fd = os.open(os.devnull, os.O_WRONLY)

    try:
        os.dup2(null_fd, stderr_fd)
        yield
    finally:
        os.dup2(saved_stderr_fd, stderr_fd)
        os.close(saved_stderr_fd)
        os.close(null_fd)


def now_text():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def image_filename():
    return datetime.now().strftime("webcam_%Y-%m-%d_%H-%M-%S.jpg")


def get_windows_camera_names():
    command = [
        "powershell",
        "-NoProfile",
        "-Command",
        (
            "Get-CimInstance Win32_PnPEntity | "
            "Where-Object { "
            "($_.PNPClass -in @('Camera','Image') -or "
            "$_.Name -match 'camera|webcam|video|capture|ivcam|droidcam|camo') "
            "-and $_.Name -notmatch 'audio|microphone' "
            "} | "
            "Select-Object -ExpandProperty Name"
        ),
    ]

    try:
        result = subprocess.run(
            command,
            capture_output=True,
            check=False,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return []

    if result.returncode != 0:
        return []

    names = []
    seen = set()
    for line in result.stdout.splitlines():
        name = line.strip()
        if name and name not in seen:
            names.append(name)
            seen.add(name)

    return names


def get_linux_camera_names():
    command = ["v4l2-ctl", "--list-devices"]

    try:
        result = subprocess.run(
            command,
            capture_output=True,
            check=False,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return []

    if result.returncode != 0:
        return []

    names = []
    for line in result.stdout.splitlines():
        if line and not line.startswith(("\t", " ")):
            names.append(line.rstrip(":"))

    return names


def get_camera_names():
    system_name = platform.system().lower()

    if system_name == "windows":
        return get_windows_camera_names()

    if system_name == "linux":
        return get_linux_camera_names()

    return []


def probe_camera(
    cv2,
    index,
    name=None,
    backend_name="auto",
    width=None,
    height=None,
    fps=None,
    fourcc=None,
):
    with suppress_native_stderr():
        capture = cv2.VideoCapture(index, get_backend(cv2, backend_name))

    try:
        if not capture.isOpened():
            return None

        apply_capture_settings(cv2, capture, width, height, fps, fourcc)

        ok, _ = capture.read()
        if not ok:
            return None

        properties = capture_properties(cv2, capture)

        return {
            "index": index,
            "name": name or f"Camera {index}",
            "backend": resolve_backend_name(backend_name),
            "width": properties["width"],
            "height": properties["height"],
            "fps": properties["fps"],
            "fourcc": properties["fourcc"],
        }
    finally:
        capture.release()


def list_cameras(
    cv2,
    max_cameras,
    backend_name="auto",
    width=None,
    height=None,
    fps=None,
    fourcc=None,
):
    cameras = []
    camera_names = get_camera_names()
    backend_names = scan_backend_names(cv2, backend_name)

    print(
        f"[{now_text()}] Scanning webcam indexes 0-{max_cameras - 1} "
        f"with backend(s): {', '.join(backend_names)}..."
    )

    found_pairs = set()
    for current_backend_name in backend_names:
        for index in range(max_cameras):
            name = camera_names[index] if index < len(camera_names) else None
            camera = probe_camera(
                cv2,
                index,
                name,
                current_backend_name,
                width,
                height,
                fps,
                fourcc,
            )
            if camera:
                pair = (camera["index"], camera["backend"])
                if pair not in found_pairs:
                    cameras.append(camera)
                    found_pairs.add(pair)

    return cameras


def print_cameras(cameras, detected_names=None):
    if not cameras:
        print("No webcam found.")
    else:
        print("\nAvailable webcams:")
        for camera in cameras:
            fps_text = f", {camera['fps']:.1f} FPS" if camera["fps"] else ""
            fourcc_text = f", {camera['fourcc']}" if camera.get("fourcc") else ""
            print(
                f"  [{camera['index']}] {camera['name']} "
                f"backend={camera['backend']} "
                f"({camera['width']}x{camera['height']}{fps_text}{fourcc_text})"
            )

    if detected_names:
        available_names = {camera["name"] for camera in cameras}
        unopened_names = [
            name for name in detected_names if name and name not in available_names
        ]

        if unopened_names:
            print("\nDetected by Windows but not opened by OpenCV in this scan:")
            for name in unopened_names:
                print(f"  - {name}")
            print("Try --backend msmf, --backend dshow, or a higher --max-cameras value.")


def ask_for_camera_index(cameras):
    available_indexes = {camera["index"] for camera in cameras}

    while True:
        raw_value = input("\nSelect webcam index: ").strip()

        try:
            selected_index = int(raw_value)
        except ValueError:
            print("Please enter a valid number.")
            continue

        if selected_index in available_indexes:
            return selected_index

        print(f"Camera {selected_index} is not in the available webcam list.")


def open_camera(
    cv2,
    camera_index,
    backend_name,
    width=None,
    height=None,
    fps=None,
    fourcc=None,
):
    capture = cv2.VideoCapture(camera_index, get_backend(cv2, backend_name))

    if not capture.isOpened():
        resolved_backend = resolve_backend_name(backend_name)
        raise RuntimeError(
            f"Cannot open webcam index {camera_index} with backend '{resolved_backend}'."
        )

    apply_capture_settings(cv2, capture, width, height, fps, fourcc)

    return capture


def warm_up_camera(capture, frame_count=5):
    frame = None

    for _ in range(frame_count):
        ok, frame = capture.read()
        if not ok:
            frame = None
        time.sleep(0.1)

    return frame


def capture_image(cv2, capture, output_dir):
    frame = warm_up_camera(capture)

    if frame is None:
        ok, frame = capture.read()
        if not ok:
            raise RuntimeError(
                "Could not read an image from the selected webcam. "
                "Try --fourcc none or a lower --width/--height value."
            )

    output_dir.mkdir(parents=True, exist_ok=True)
    file_path = output_dir / image_filename()

    if not cv2.imwrite(str(file_path), frame):
        raise RuntimeError(f"Could not save image to {file_path}.")

    return file_path, frame_stats(frame)


def frame_stats(frame):
    return {
        "width": frame.shape[1],
        "height": frame.shape[0],
        "mean": float(frame.mean()),
        "min": int(frame.min()),
        "max": int(frame.max()),
        "nonzero_percent": float((frame > 0).mean() * 100),
    }


def properties_text(properties):
    fps_text = f", {properties['fps']:.1f} FPS" if properties.get("fps") else ""
    fourcc_text = f", {properties['fourcc']}" if properties.get("fourcc") else ""
    return f"{properties['width']}x{properties['height']}{fps_text}{fourcc_text}"


def diagnostic_filename(camera_index, backend_name, frame_number):
    timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    return f"diagnose_cam{camera_index}_{backend_name}_frame{frame_number}_{timestamp}.jpg"


def diagnose_camera(
    cv2,
    camera_index,
    output_dir,
    frames_to_try,
    width=None,
    height=None,
    fps=None,
    fourcc=None,
):
    diagnostic_dir = output_dir / "diagnostics"
    diagnostic_dir.mkdir(parents=True, exist_ok=True)

    print(f"\n[{now_text()}] Diagnosing webcam index {camera_index}...")
    print(f"[{now_text()}] Diagnostic samples will be saved to: {diagnostic_dir.resolve()}")

    for backend_name in sorted(backend_options(cv2)):
        print(f"\nBackend: {backend_name}")

        try:
            with suppress_native_stderr():
                capture = cv2.VideoCapture(camera_index, get_backend(cv2, backend_name))
        except Exception as exc:
            print(f"  open: failed ({exc})")
            continue

        try:
            if not capture.isOpened():
                print("  open: failed")
                continue

            apply_capture_settings(cv2, capture, width, height, fps, fourcc)

            best_frame = None
            best_stats = None
            successful_reads = 0

            for frame_number in range(1, frames_to_try + 1):
                ok, frame = capture.read()
                if not ok or frame is None:
                    time.sleep(0.1)
                    continue

                successful_reads += 1
                stats = frame_stats(frame)

                if best_stats is None or stats["mean"] > best_stats["mean"]:
                    best_frame = frame
                    best_stats = stats

                time.sleep(0.1)

            if best_frame is None or best_stats is None:
                print(f"  read: failed after {frames_to_try} attempts")
                continue

            sample_path = diagnostic_dir / diagnostic_filename(
                camera_index, backend_name, successful_reads
            )
            cv2.imwrite(str(sample_path), best_frame)

            black_text = (
                "looks black"
                if best_stats["mean"] < DEFAULT_BLACK_FRAME_THRESHOLD
                else "has visible data"
            )
            print(f"  read: {successful_reads}/{frames_to_try} frames")
            active_settings = properties_text(capture_properties(cv2, capture))
            print(f"  active settings: {active_settings}")
            print(
                "  best frame: "
                f"{best_stats['width']}x{best_stats['height']}, "
                f"mean={best_stats['mean']:.2f}, "
                f"min={best_stats['min']}, "
                f"max={best_stats['max']}, "
                f"nonzero={best_stats['nonzero_percent']:.2f}% "
                f"({black_text})"
            )
            print(f"  saved: {sample_path}")
        finally:
            capture.release()


def sleep_with_countdown(interval_seconds):
    target_time = time.monotonic() + interval_seconds

    while True:
        remaining_seconds = int(target_time - time.monotonic())
        if remaining_seconds <= 0:
            return

        time.sleep(min(remaining_seconds, 60))


def run_capture_loop(
    cv2,
    camera_index,
    output_dir,
    interval_seconds,
    capture_now,
    backend_name,
    once,
    width=None,
    height=None,
    fps=None,
    fourcc=None,
):
    capture = open_camera(cv2, camera_index, backend_name, width, height, fps, fourcc)
    resolved_backend = resolve_backend_name(backend_name)

    try:
        print(f"[{now_text()}] Using webcam index {camera_index}.")
        print(f"[{now_text()}] Backend: {resolved_backend}.")
        active_settings = properties_text(capture_properties(cv2, capture))
        print(f"[{now_text()}] Active camera settings: {active_settings}.")
        print(f"[{now_text()}] Images will be saved to: {output_dir.resolve()}")
        print(f"[{now_text()}] Capture interval: {interval_seconds} seconds.")
        print("Press Ctrl+C to stop.\n")

        if not capture_now:
            print(f"[{now_text()}] Waiting before first capture...")
            sleep_with_countdown(interval_seconds)

        while True:
            try:
                file_path, stats = capture_image(cv2, capture, output_dir)
                print(
                    f"[{now_text()}] Saved: {file_path} "
                    f"({stats['width']}x{stats['height']})"
                )
            except Exception as exc:
                print(f"[{now_text()}] Capture failed: {exc}")

            if once:
                return

            print(f"[{now_text()}] Waiting {interval_seconds} seconds...\n")
            sleep_with_countdown(interval_seconds)
    finally:
        capture.release()


def parse_args():
    parser = argparse.ArgumentParser(
        description=(
            "List available webcams, select one, then capture one image every "
            "15 seconds into ./output."
        )
    )
    parser.add_argument(
        "--camera",
        type=int,
        help="Webcam index to use. If omitted, the program shows a selection menu.",
    )
    parser.add_argument(
        "--list",
        action="store_true",
        help="Only list available webcams and exit.",
    )
    parser.add_argument(
        "--backend",
        choices=["auto", "any", "dshow", "msmf"],
        default="auto",
        help="OpenCV backend to use. On Windows, 'auto' scans dshow, msmf, and any.",
    )
    parser.add_argument(
        "--diagnose",
        action="store_true",
        help="Test the selected webcam with all available OpenCV backends and save samples.",
    )
    parser.add_argument(
        "--diagnostic-frames",
        type=int,
        default=DEFAULT_DIAGNOSTIC_FRAMES,
        help=f"Frames to test per backend during diagnosis. Default: {DEFAULT_DIAGNOSTIC_FRAMES}.",
    )
    parser.add_argument(
        "--max-cameras",
        type=int,
        default=DEFAULT_MAX_CAMERAS,
        help=f"Number of webcam indexes to scan. Default: {DEFAULT_MAX_CAMERAS}.",
    )
    parser.add_argument(
        "--width",
        type=int,
        default=DEFAULT_CAMERA_WIDTH,
        help=f"Requested camera width. Default: {DEFAULT_CAMERA_WIDTH}.",
    )
    parser.add_argument(
        "--height",
        type=int,
        default=DEFAULT_CAMERA_HEIGHT,
        help=f"Requested camera height. Default: {DEFAULT_CAMERA_HEIGHT}.",
    )
    parser.add_argument(
        "--fps",
        type=float,
        default=None,
        help="Requested camera FPS. If omitted, the driver default is used.",
    )
    parser.add_argument(
        "--fourcc",
        default=DEFAULT_CAMERA_FOURCC,
        help=(
            "Requested camera pixel format. Use 'none' to disable. "
            f"Default: {DEFAULT_CAMERA_FOURCC}."
        ),
    )
    parser.add_argument(
        "--interval",
        type=int,
        default=DEFAULT_INTERVAL_SECONDS,
        help=f"Capture interval in seconds. Default: {DEFAULT_INTERVAL_SECONDS}.",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=DEFAULT_OUTPUT_DIR,
        help=f"Output folder. Default: {DEFAULT_OUTPUT_DIR}.",
    )
    parser.add_argument(
        "--no-initial-capture",
        action="store_true",
        help="Wait one interval before taking the first image.",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Capture one image and exit.",
    )

    return parser.parse_args()


def main():
    args = parse_args()
    
    if args.interval <= 0:
        raise SystemExit("--interval must be greater than 0.")

    if args.max_cameras <= 0:
        raise SystemExit("--max-cameras must be greater than 0.")

    if args.diagnostic_frames <= 0:
        raise SystemExit("--diagnostic-frames must be greater than 0.")

    if args.width <= 0:
        raise SystemExit("--width must be greater than 0.")

    if args.height <= 0:
        raise SystemExit("--height must be greater than 0.")

    if args.fps is not None and args.fps <= 0:
        raise SystemExit("--fps must be greater than 0.")

    args.fourcc = normalize_fourcc(args.fourcc)

    cv2 = load_cv2()
    cameras = list_cameras(
        cv2,
        args.max_cameras,
        args.backend,
        args.width,
        args.height,
        args.fps,
        args.fourcc,
    )
    camera_names = get_camera_names()
    print_cameras(cameras, camera_names)

    if args.list:
        return

    if not cameras:
        raise SystemExit("No webcam available. Connect a webcam and try again.")

    camera_index = args.camera
    if camera_index is None:
        camera_index = ask_for_camera_index(cameras)
    elif camera_index not in {camera["index"] for camera in cameras}:
        raise SystemExit(f"Camera {camera_index} was not found.")

    selected_backend_name = args.backend
    if args.backend == "auto":
        selected_camera = next(
            camera for camera in cameras if camera["index"] == camera_index
        )
        selected_backend_name = selected_camera["backend"]

    if args.diagnose:
        diagnose_camera(
            cv2,
            camera_index,
            args.output,
            args.diagnostic_frames,
            args.width,
            args.height,
            args.fps,
            args.fourcc,
        )
        return

    try:
        run_capture_loop(
            cv2=cv2,
            camera_index=camera_index,
            output_dir=args.output,
            interval_seconds=args.interval,
            capture_now=not args.no_initial_capture,
            backend_name=selected_backend_name,
            once=args.once,
            width=args.width,
            height=args.height,
            fps=args.fps,
            fourcc=args.fourcc,
        )
    except KeyboardInterrupt:
        print(f"\n[{now_text()}] Stopped.")


if __name__ == "__main__":
    main()
