"""
overlay.py  -  OPTIONAL PHASE: built-in transparent drawing layer
=================================================================

Press F10 (or the "Drawing overlay" button). A see-through layer covers your
screen. Now "index + middle" draws on THIS layer instead of pressing the real
mouse button, so you can write over anything (slides, PDFs, browser) without
another annotation app.

* The layer lets real clicks pass through, so pinch-click still works on the
  apps underneath (and on the small "Pen" toolbar).
* Toolbar: colors, pen size, eraser, undo, clear, hide.

How the see-through trick works: the whole window is painted magenta, and we
tell Windows "treat magenta as invisible" (-transparentcolor). Then we add the
WS_EX_TRANSPARENT style so the mouse goes straight through the window.
"""

import ctypes
import logging
import queue
import sys
import tkinter as tk

log = logging.getLogger(__name__)

KEY_COLOR = "#ff00ff"       # this exact color becomes invisible
PEN_COLORS = [("#ff3b30", "Red"), ("#1e6bff", "Blue"), ("#16a34a", "Green"),
              ("#ffd60a", "Yellow"), ("#ffffff", "White"), ("#111111", "Black")]
PEN_SIZES = [2, 4, 7, 12]

WS_EX_TRANSPARENT = 0x00000020   # mouse passes through
WS_EX_TOOLWINDOW = 0x00000080    # no taskbar button
WS_EX_LAYERED = 0x00080000
WS_EX_NOACTIVATE = 0x08000000    # clicking it does not steal focus from PowerPoint


def add_window_style(window, extra_style):
    """Add Windows 'extended styles' to a Tk window."""
    if sys.platform != "win32":
        return
    from ctypes import wintypes
    user32 = ctypes.windll.user32
    user32.GetParent.argtypes = (wintypes.HWND,)
    user32.GetParent.restype = wintypes.HWND
    user32.GetWindowLongW.argtypes = (wintypes.HWND, ctypes.c_int)
    user32.GetWindowLongW.restype = ctypes.c_long
    user32.SetWindowLongW.argtypes = (wintypes.HWND, ctypes.c_int, ctypes.c_long)
    window.update_idletasks()
    hwnd = user32.GetParent(window.winfo_id()) or window.winfo_id()
    GWL_EXSTYLE = -20
    style = user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
    user32.SetWindowLongW(hwnd, GWL_EXSTYLE, style | extra_style)


class Overlay:
    def __init__(self, root, engine, cfg):
        self.root = root
        self.engine = engine
        self.cfg = cfg
        self.visible = False
        self.color = cfg["overlay_color"]
        self.size = cfg["overlay_pen_width"]
        self.eraser = False
        self.undo_stack = []         # ("stroke", item_id) or ("erase", [saved lines])
        self.current_item = None     # the line being drawn now
        self.current_points = []
        self.erased_now = None       # lines removed during the current eraser stroke
        self.offset = (0, 0)

        # --- the see-through drawing layer ---
        self.win = tk.Toplevel(root)
        self.win.withdraw()
        self.win.overrideredirect(True)                 # no title bar
        self.win.attributes("-topmost", True)
        self.win.attributes("-transparentcolor", KEY_COLOR)
        self.canvas = tk.Canvas(self.win, bg=KEY_COLOR, highlightthickness=0)
        self.canvas.pack(fill="both", expand=True)
        self._styled = False

        # --- the small toolbar ---
        self.bar = tk.Toplevel(root)
        self.bar.withdraw()
        self.bar.title("Pen")
        self.bar.attributes("-topmost", True)
        self.bar.attributes("-toolwindow", True)
        self.bar.resizable(False, False)
        self.bar.protocol("WM_DELETE_WINDOW", self._hide_from_toolbar)
        self._build_toolbar()

    # ---------------- toolbar ----------------
    def _build_toolbar(self):
        f = tk.Frame(self.bar, padx=4, pady=4)
        f.pack()
        big = ("Segoe UI", 11)
        self.color_buttons = []
        for color, name in PEN_COLORS:
            b = tk.Button(f, bg=color, activebackground=color, width=3, height=1, relief="raised",
                          command=lambda c=color: self.set_color(c))
            b.pack(side="left", padx=2)
            self.color_buttons.append((color, b))
        self.size_btn = tk.Button(f, text=f"Size {self.size}", font=big, command=self.next_size)
        self.size_btn.pack(side="left", padx=(8, 2))
        self.eraser_btn = tk.Button(f, text="Eraser", font=big, command=self.toggle_eraser)
        self.eraser_btn.pack(side="left", padx=2)
        tk.Button(f, text="Undo", font=big, command=self.undo).pack(side="left", padx=2)
        tk.Button(f, text="Clear", font=big, command=self.clear).pack(side="left", padx=2)
        tk.Button(f, text="Hide", font=big, command=self._hide_from_toolbar).pack(side="left", padx=(8, 2))
        self._mark_color()

    def _mark_color(self):
        for color, button in self.color_buttons:
            button.configure(relief="sunken" if (color == self.color and not self.eraser) else "raised",
                             bd=4 if color == self.color else 2)

    def set_color(self, color):
        self.color = color
        self.eraser = False
        self.eraser_btn.configure(relief="raised")
        self._mark_color()

    def next_size(self):
        i = PEN_SIZES.index(self.size) if self.size in PEN_SIZES else 0
        self.size = PEN_SIZES[(i + 1) % len(PEN_SIZES)]
        self.size_btn.configure(text=f"Size {self.size}")

    def toggle_eraser(self):
        self.eraser = not self.eraser
        self.eraser_btn.configure(relief="sunken" if self.eraser else "raised")
        self._mark_color()

    def _hide_from_toolbar(self):
        self.engine.overlay_on = False      # the control panel will hide us

    # ---------------- show / hide ----------------
    def show(self):
        left, top, width, height = self.engine.screen_rect
        self.offset = (left, top)
        self.win.geometry(f"{width}x{height}+{left}+{top}")
        self.win.deiconify()
        if not self._styled:
            add_window_style(self.win, WS_EX_LAYERED | WS_EX_TRANSPARENT | WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE)
            self._styled = True
        self.win.attributes("-topmost", True)
        self.win.lift()

        self.bar.deiconify()
        self.bar.update_idletasks()
        bar_w = self.bar.winfo_reqwidth()
        self.bar.geometry(f"+{left + (width - bar_w) // 2}+{top + 12}")
        add_window_style(self.bar, WS_EX_NOACTIVATE)
        self.bar.attributes("-topmost", True)
        self.bar.lift()

        self.visible = True
        self._drain(discard=True)            # forget anything from before
        self._pump()

    def hide(self):
        self._finish_stroke()
        self.win.withdraw()
        self.bar.withdraw()
        self.visible = False

    # ---------------- drawing events from the engine ----------------
    def _drain(self, discard=False):
        q = self.engine.overlay_queue
        for _ in range(1000):
            try:
                event = q.get_nowait()
            except queue.Empty:
                return
            if not discard:
                self._handle(event)

    def _pump(self):
        if not self.visible:
            return
        self._drain()
        self.win.after(15, self._pump)       # about 60 times a second

    def _to_canvas(self, x, y):
        return x - self.offset[0], y - self.offset[1]

    def _handle(self, event):
        kind = event[0]
        if kind == "start":
            x, y = self._to_canvas(event[1], event[2])
            if self.eraser:
                self.erased_now = []
                self._erase_at(x, y)
            else:
                self.current_points = [x, y, x + 0.1, y + 0.1]
                self.current_item = self.canvas.create_line(
                    *self.current_points, fill=self.color, width=self.size,
                    capstyle="round", joinstyle="round", tags="stroke")
        elif kind == "move":
            x, y = self._to_canvas(event[1], event[2])
            if self.erased_now is not None:
                self._erase_at(x, y)
            elif self.current_item is not None:
                self.current_points += [x, y]
                self.canvas.coords(self.current_item, *self.current_points)
        elif kind == "end":
            self._finish_stroke()

    def _finish_stroke(self):
        if self.current_item is not None:
            self.undo_stack.append(("stroke", self.current_item))
        if self.erased_now:
            self.undo_stack.append(("erase", self.erased_now))
        self.current_item = None
        self.current_points = []
        self.erased_now = None

    def _erase_at(self, x, y):
        r = max(14, self.size * 3)
        for item in self.canvas.find_overlapping(x - r, y - r, x + r, y + r):
            if "stroke" in self.canvas.gettags(item):
                self.erased_now.append((self.canvas.coords(item),
                                        self.canvas.itemcget(item, "fill"),
                                        self.canvas.itemcget(item, "width")))
                self.canvas.delete(item)

    # ---------------- undo / clear ----------------
    def undo(self):
        if not self.undo_stack:
            return
        kind, data = self.undo_stack.pop()
        if kind == "stroke":
            self.canvas.delete(data)
        else:                                   # bring erased lines back
            for coords, fill, width in data:
                self.canvas.create_line(*coords, fill=fill, width=width, capstyle="round",
                                        joinstyle="round", tags="stroke")

    def clear(self):
        saved = [(self.canvas.coords(i), self.canvas.itemcget(i, "fill"), self.canvas.itemcget(i, "width"))
                 for i in self.canvas.find_withtag("stroke")]
        if saved:
            self.canvas.delete("stroke")
            self.undo_stack.append(("erase", saved))   # so Undo can bring it all back
