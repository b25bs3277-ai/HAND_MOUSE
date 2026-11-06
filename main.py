"""
main.py  -  HandMouse: control the mouse with your hand through a webcam
=======================================================================

Run:   python main.py          (or double-click run.bat)

What starts here:
  1. DPI awareness (so screen sizes are real pixels)
  2. settings.json
  3. the Engine thread (camera -> hand -> gesture -> mouse)
  4. global hotkeys (F8 start/stop, F9 preview, F10 overlay, Esc emergency exit)
  5. the control panel window (Tkinter, must run in the main thread)
"""

import logging
import os
import sys
import threading

from mouse_control import emergency_release, set_dpi_awareness


def setup_logging():
    """Write a log file next to the app. Very useful when the .exe has no console."""
    from config import data_dir
    log_path = os.path.join(data_dir(), "handmouse.log")
    file_handler = logging.FileHandler(log_path, mode="w", encoding="utf-8")
    handlers = [file_handler]
    # A --noconsole .exe has no stdout/stderr; send them to the log so nothing crashes.
    if sys.stdout is None:
        sys.stdout = file_handler.stream
    else:
        handlers.append(logging.StreamHandler(sys.stdout))
    if sys.stderr is None:
        sys.stderr = file_handler.stream
    logging.basicConfig(level=logging.INFO, handlers=handlers,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    def thread_error(args):
        logging.getLogger("thread").error("Error in thread %s", args.thread.name,
                                          exc_info=(args.exc_type, args.exc_value, args.exc_traceback))
    threading.excepthook = thread_error
    return log_path


def to_pynput(name):
    """'f8' -> '<f8>',  'ctrl+shift+q' -> '<ctrl>+<shift>+q'  (pynput's hotkey format)."""
    parts = [p.strip().lower() for p in name.split("+") if p.strip()]
    return "+".join(p if len(p) == 1 else f"<{p}>" for p in parts)


def start_hotkeys(cfg, engine):
    log = logging.getLogger("hotkeys")
    try:
        from pynput import keyboard
    except Exception as e:
        log.warning("pynput not available, hotkeys disabled: %s", e)
        return None

    def emergency_exit():
        emergency_release()                  # let go of the buttons FIRST
        engine.request_exit()
        # If something hangs, force the app to close after 3 seconds.
        timer = threading.Timer(3.0, lambda: os._exit(0))
        timer.daemon = True
        timer.start()

    actions = {
        "hotkey_start_stop": engine.toggle_running,
        "hotkey_preview": engine.toggle_preview,
        "hotkey_overlay": engine.toggle_overlay,
        "hotkey_exit": emergency_exit,
    }
    from config import DEFAULTS
    mapping = {}
    for setting, action in actions.items():
        for text in (cfg[setting], DEFAULTS[setting]):   # fall back to the default if invalid
            try:
                combo = to_pynput(text)
                keyboard.HotKey.parse(combo)
                mapping[combo] = action
                break
            except ValueError:
                log.warning("Hotkey '%s' is not valid, trying the default", text)
    listener = keyboard.GlobalHotKeys(mapping)
    listener.daemon = True
    listener.start()
    log.info("Hotkeys: %s", ", ".join(mapping))
    return listener


def main():
    set_dpi_awareness()                      # must be first, before any window opens
    log_path = setup_logging()
    log = logging.getLogger("main")
    log.info("HandMouse starting (log: %s)", log_path)

    from config import Config
    from engine import Engine
    from ui import App

    cfg = Config.load()
    engine = Engine(cfg)
    import atexit
    atexit.register(emergency_release)       # whatever happens, never leave a button stuck

    hotkeys = start_hotkeys(cfg, engine)
    app = App(cfg, engine)

    worker = threading.Thread(target=engine.run, name="engine", daemon=True)
    worker.start()
    try:
        app.run()                            # blocks until the panel closes
    finally:
        engine.request_exit()
        worker.join(timeout=3.0)
        emergency_release()
        if hotkeys is not None:
            hotkeys.stop()
        cfg.save()
        log.info("HandMouse closed")


if __name__ == "__main__":
    main()
