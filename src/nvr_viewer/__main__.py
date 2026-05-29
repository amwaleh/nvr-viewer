"""CLI entry point for NVR Viewer."""
import argparse
import logging
import sys

from .app import NVRApp
from .core.rtsp_client import CameraConfig
from .network.scanner import NetworkScanner
from .storage.credentials import CredentialStore


def setup_logging(verbose: bool = False):
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )


def cmd_view(args):
    """View camera streams."""
    app = NVRApp(
        enable_detection=not args.no_detection,
        enable_recording=args.record,
    )

    if args.discover:
        discovered = app.auto_discover(args.subnet)
        if not discovered:
            print("No cameras found on network.")
            return

        print(f"\nFound {len(discovered)} camera(s):")
        for cam in discovered:
            print(f"  {cam['host']}:{cam.get('port', 554)} "
                  f"paths={cam.get('paths', [])} "
                  f"realm={cam.get('realm', '?')}")

        # Need credentials for discovered cameras
        creds = CredentialStore()
        for cam in discovered:
            host = cam["host"]
            stored = creds.get(host)
            if not stored:
                print(f"\nCredentials needed for {host}:")
                user = input(f"  Username [{args.user}]: ").strip() or args.user
                pwd = input(f"  Password: ").strip()
                creds.set(host, user, pwd)
                stored = {"username": user, "password": pwd}

            config = CameraConfig(
                host=host,
                port=cam.get("port", 554),
                path=cam.get("paths", ["/onvif1"])[0],
                username=stored["username"],
                password=stored["password"],
                name=cam.get("name", f"Camera_{host.split('.')[-1]}"),
            )
            app.add_camera(config)
    else:
        # Use specified cameras
        if not args.cameras:
            print("Specify cameras with --camera HOST or use --discover")
            return

        creds = CredentialStore()
        for i, host in enumerate(args.cameras):
            stored = creds.get(host)
            config = CameraConfig(
                host=host,
                port=args.port,
                path=args.path,
                username=stored["username"] if stored else args.user,
                password=stored["password"] if stored else args.password,
                name=f"Camera {i + 1}",
            )
            app.add_camera(config)

    if args.detect_objects:
        app.enable_object_detection(args.model)
    if args.detect_faces:
        app.enable_face_detection()

    app.start()
    app.run_viewer()


def cmd_scan(args):
    """Scan network for cameras."""
    scanner = NetworkScanner()
    print(f"Scanning network...")

    cameras = scanner.discover_cameras(args.subnet)

    if not cameras:
        print("No cameras found.")
        return

    print(f"\nFound {len(cameras)} camera(s):\n")
    for cam in cameras:
        print(f"  Host: {cam['host']}:{cam.get('port', 554)}")
        print(f"  Server: {cam.get('server', 'unknown')}")
        print(f"  Realm: {cam.get('realm', 'unknown')}")
        print(f"  Paths: {', '.join(cam.get('paths', []))}")
        print()


def cmd_creds(args):
    """Manage stored credentials."""
    store = CredentialStore()

    if args.action == "list":
        hosts = store.list_hosts()
        if not hosts:
            print("No stored credentials.")
        else:
            print("Stored credentials:")
            for host in hosts:
                cred = store.get(host)
                print(f"  {host}: user={cred['username']}")

    elif args.action == "set":
        pwd = args.password or input("Password: ").strip()
        store.set(args.host, args.user, pwd)
        print(f"Saved credentials for {args.host}")

    elif args.action == "delete":
        if store.delete(args.host):
            print(f"Deleted credentials for {args.host}")
        else:
            print(f"No credentials found for {args.host}")


def cmd_events(args):
    """Query detection events."""
    from .storage.database import Database
    db = Database()
    events = db.get_events(
        detection_type=args.type,
        limit=args.limit,
    )

    if not events:
        print("No detection events found.")
        return

    print(f"{'Time':<20} {'Type':<10} {'Label':<15} {'Confidence':<12} {'Camera'}")
    print("-" * 75)
    for e in events:
        print(f"{e['timestamp']:<20} {e['detection_type']:<10} "
              f"{e.get('label', ''):<15} {e.get('confidence', 0):>8.1%}     "
              f"cam_id={e['camera_id']}")

    db.close()


def cmd_web(args):
    """Start the web UI and API server."""
    from .web.server import run
    run(host=args.host, port=args.port, reload=args.reload)


def main():
    parser = argparse.ArgumentParser(
        prog="nvr-viewer",
        description="Network Video Recorder — camera viewer with AI detection",
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="Debug logging")
    sub = parser.add_subparsers(dest="command")

    # view
    p_view = sub.add_parser("view", help="View camera streams")
    p_view.add_argument("-c", "--camera", dest="cameras", action="append",
                        help="Camera host IP (repeatable)")
    p_view.add_argument("--discover", action="store_true",
                        help="Auto-discover cameras on network")
    p_view.add_argument("--subnet", help="Subnet to scan (e.g., 192.168.1.0/24)")
    p_view.add_argument("--port", type=int, default=554, help="RTSP port")
    p_view.add_argument("--path", default="/onvif1", help="RTSP path")
    p_view.add_argument("-u", "--user", default="admin", help="Username")
    p_view.add_argument("-p", "--password", default="", help="Password")
    p_view.add_argument("--record", action="store_true", help="Start recording immediately")
    p_view.add_argument("--no-detection", action="store_true", help="Disable all detection")
    p_view.add_argument("--detect-objects", action="store_true",
                        help="Enable YOLO object detection")
    p_view.add_argument("--detect-faces", action="store_true",
                        help="Enable face detection")
    p_view.add_argument("--model", default="yolov8n.pt", help="YOLO model name")
    p_view.set_defaults(func=cmd_view)

    # scan
    p_scan = sub.add_parser("scan", help="Scan network for cameras")
    p_scan.add_argument("--subnet", help="Subnet to scan (e.g., 192.168.1.0/24)")
    p_scan.set_defaults(func=cmd_scan)

    # creds
    p_cred = sub.add_parser("creds", help="Manage camera credentials")
    p_cred.add_argument("action", choices=["list", "set", "delete"])
    p_cred.add_argument("--host", help="Camera host IP")
    p_cred.add_argument("-u", "--user", default="admin")
    p_cred.add_argument("-p", "--password", default="")
    p_cred.set_defaults(func=cmd_creds)

    # events
    p_events = sub.add_parser("events", help="Query detection events")
    p_events.add_argument("--type", help="Filter by detection type")
    p_events.add_argument("--limit", type=int, default=50, help="Max results")
    p_events.set_defaults(func=cmd_events)

    # web server
    p_web = sub.add_parser("web", help="Start web UI and API server")
    p_web.add_argument("--host", default="0.0.0.0", help="Bind host")
    p_web.add_argument("--port", type=int, default=8080, help="Bind port")
    p_web.add_argument("--reload", action="store_true", help="Auto-reload on changes")
    p_web.set_defaults(func=cmd_web)

    args = parser.parse_args()
    setup_logging(args.verbose)

    if not args.command:
        parser.print_help()
        return

    args.func(args)


if __name__ == "__main__":
    main()
