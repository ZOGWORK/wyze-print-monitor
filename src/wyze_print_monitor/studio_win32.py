"""Find and track eufyMake Studio's Device tab (Windows only).

eufyMake Studio (a PrusaSlicer fork) is a wxWidgets app, so every panel on its
Device tab — the "My Printer" list, Monitor, Progress, Control, Others — is a
native child window. This module locates those windows by their header
labels so :mod:`ui.studio_dock` can place our own panels exactly over the
Monitor and Others cards.

Only read-only window queries plus two style/z-order tweaks are used, and
the ways we act on Studio — pressing its print Pause/Stop buttons for the
print watcher, and its Slice tab / Print / "Choose a Printer" buttons for
scheduled prints — *post* mouse messages (never send), so a busy Studio
can never stall our UI thread through these calls. Everything degrades to
"not found" off Windows.

The pure helpers at the top (no ctypes) hold the decision logic and are unit
tested; the Win32 calls below them are thin wrappers.
"""
from __future__ import annotations

import sys
from dataclasses import dataclass, field

STUDIO_TITLE_MARKERS = ("eufyMake Studio", "AnkerMake Studio")
MONITOR_LABEL = "Monitor"
OTHERS_LABEL = "Others"
PRINTER_LIST_LABEL = "My Printer"

Rect = tuple[int, int, int, int]  # left, top, width, height (physical px)

PROGRESS_LABEL = "Progress"
SLICE_TAB_LABEL = "Slice"
DEVICE_TAB_LABEL = "Device"
CHOOSE_PRINTER_TITLE = "Choose a Printer"  # the dialog Studio's Print opens

# Studio's print status label (Progress card's bottom bar: "<file> | Printing").
STATUS_RUNNING = (
    "printing", "heating", "preheating", "leveling", "levelling", "calibrating",
    "resuming", "starting", "transferring",
)
STATUS_PAUSED = ("paused", "pausing")
STATUS_IDLE = ("idle", "completed", "complete", "finished", "cancelled", "canceled", "stopped", "failed", "ready", "offline")

# Labels of print-control buttons, for Studio builds that label them
# (the current eufyMake Studio uses unlabeled icon buttons instead).
CONTROL_LABELS = {
    "pause": ("pause", "pause print", "pause printing"),
    "resume": ("resume", "resume print", "resume printing", "continue"),
    "stop": ("stop", "stop print", "stop printing", "abort", "cancel print"),
}


# --------------------------------------------------------------------- pure logic
def is_studio_title(title: str) -> bool:
    return any(marker in title for marker in STUDIO_TITLE_MARKERS)


def project_name(title: str) -> str:
    """The project in Studio's title ("*benchy - eufyMake Studio 3D ..."),
    or "" for an unsaved "Untitled" one."""
    name = title.split(" - ", 1)[0].lstrip("*").strip() if " - " in title else ""
    return "" if name.lower() == "untitled" else name


def classify_control(text: str) -> str | None:
    """"pause" / "resume" / "stop" for a print-control button's label."""
    label = " ".join(text.replace("&", "").split()).strip().lower()
    for kind, labels in CONTROL_LABELS.items():
        if label in labels:
            return kind
    return None


BLANK_CARD = "N/A"  # what Studio's Progress card shows with no print info


def classify_print_status(text: str) -> str | None:
    """"running" / "paused" / "idle" for Studio's status label, "blank" for
    its empty "N/A" card (a blink while it reconnects, or no print any
    more); None when it's something we don't know (watch, press nothing)."""
    words = " ".join(text.split()).strip().lower()
    if not words:
        return None
    if words == BLANK_CARD.lower():
        return "blank"
    for kind, names in (("paused", STATUS_PAUSED), ("running", STATUS_RUNNING), ("idle", STATUS_IDLE)):
        if any(words == name or words.startswith(name + " ") for name in names):
            return kind
    return None


def status_from_bar(statics: list[tuple[int, str]]) -> str:
    """The status text from the bottom bar's labels ((x, text) pairs):
    "<file name> | <status>" — the label after the "|" separator."""
    texts = [text for _x, text in sorted(statics)]
    if "|" in texts:
        after = texts[texts.index("|") + 1 :]
        return after[0] if after else ""
    if texts == [BLANK_CARD]:
        return BLANK_CARD  # the empty card: no file, no status
    return texts[-1] if len(texts) >= 2 else ""


def controls_from_bar(status: str | None, buttons: list[tuple[int, int, bool]]) -> dict[str, int]:
    """Studio's unlabeled icon buttons -> {"stop", "pause" | "resume": hwnd}.

    ``buttons`` are all of the bar's icon buttons, shown or not, as (x,
    hwnd, usable). They sit in two slots: Stop on the left; Pause and
    Resume share the right-hand slot (Studio shows one at a time, and e.g.
    no Pause while still heating). Which one the right slot holds is only
    trusted from a *known* status: a wrong guess could resume a paused
    print, so an unknown status exposes nothing.
    """
    xs = sorted({x for x, _hwnd, _usable in buttons})
    if status not in ("running", "paused") or len(xs) < 2:
        return {}
    found: dict[str, int] = {}
    for x, hwnd, usable in sorted(buttons):
        if not usable:
            continue
        if x == xs[0]:
            found.setdefault("stop", hwnd)
        elif x == xs[-1]:
            found.setdefault("pause" if status == "running" else "resume", hwnd)
    return found


def is_selected_row_color(rgb: tuple[int, int, int]) -> bool:
    """Studio paints the selected printer row green-tinted (≈ #354138) and the
    others neutral grey (≈ #292a2d). Green dominance tells them apart."""
    r, g, b = rgb
    return g - max(r, b) >= 6


def selected_row_index(colors: list[tuple[int, int, int] | None]) -> int | None:
    """Index of the one selected row, or None when unknown/ambiguous
    (a row was covered by another window, or no row looks selected)."""
    if not colors or any(c is None for c in colors):
        return None
    hits = [i for i, c in enumerate(colors) if c is not None and is_selected_row_color(c)]
    return hits[0] if len(hits) == 1 else None


def camera_printer_selected(
    selected: int | None, configured_row: int, previous: bool | None
) -> bool | None:
    """Should the camera panels show? ``configured_row`` is 1-based, 0 = any
    printer. Returns None while that's still unknown.

    An unknown selection (sidebar covered, rows not found) keeps the previous
    *confirmed* decision so a briefly covered sidebar doesn't make the panels
    flicker — but never invents one: until the selected printer has actually
    been read, the answer stays None and the panels stay hidden.
    """
    if configured_row <= 0:
        return True
    if selected is None:
        return previous
    return selected == configured_row - 1


def may_control_print(selected_ok: bool | None, configured_row: int, row_count: int) -> bool:
    """May we use (or trust) the print controls Studio shows right now?

    Only when they're the camera's printer's: it's the selected one. With
    "any printer" configured that's only certain if there's a single
    printer — otherwise we can't know which printer the camera watches.
    """
    if selected_ok is not True:
        return False
    return configured_row > 0 or row_count <= 1


def pick_printer_rows(
    candidates: list[tuple[int, Rect]], list_rect: Rect, header_bottom: int
) -> list[int]:
    """From visible windows inside the sidebar, keep the printer rows: nearly
    full sidebar width, short (a row, not the list), below the header. Sorted
    top to bottom."""
    _lx, _ly, lw, lh = list_rect
    rows = [
        (rect[1], hwnd)
        for hwnd, rect in candidates
        if rect[2] >= 0.8 * lw
        and 0.02 * lh <= rect[3] <= 0.2 * lh
        and rect[1] >= header_bottom
    ]
    return [hwnd for _top, hwnd in sorted(rows)]


def pick_print_button(groups: list[list[tuple[int, Rect]]], window: Rect) -> int | None:
    """Studio's Slice-tab Print button, which is custom-drawn with no label.

    ``groups`` are the custom controls of each parent window as (hwnd,
    rect). Print sits in a bar of exactly two same-size wide buttons side by
    side, Export then Print, at the bottom of the right-hand settings panel:
    the right one of the only such pair in the window's lower right. Any
    doubt (no pair, or several) is None.
    """
    wx, wy, ww, wh = window
    hits = []
    for controls in groups:
        if len(controls) != 2:
            continue
        (_left, r1), (right, r2) = sorted(controls, key=lambda c: c[1][0])
        x, y, w, h = r2
        if r1[1:] != r2[1:] or r1[0] + r1[2] > x:
            continue  # not the same size and row, or overlapping
        if not (20 <= h <= 80 and w >= 3 * h):
            continue
        if x < wx + 0.55 * ww or y < wy + 0.7 * wh:
            continue
        hits.append(right)
    return hits[0] if len(hits) == 1 else None


@dataclass
class ChoosePrinter:
    """Studio's "Choose a Printer" dialog: one row per printer (in the "My
    Printer" order), then Cancel and Print."""

    dialog: int
    rows: list[int]
    cancel: int
    print: int


def split_choose_printer(
    dialog: int, rows: list[tuple[int, Rect]], buttons: list[tuple[int, Rect]]
) -> ChoosePrinter | None:
    """Rows are the list's entries; the two wide buttons below the list are
    Cancel (left) and Print (right). Anything else isn't the dialog we know."""
    if not rows:
        return None
    list_bottom = max(r[1] + r[3] for _h, r in rows)
    below = sorted((r[0], h) for h, r in buttons if r[1] >= list_bottom and r[2] >= 80 and 20 <= r[3] <= 80)
    if len(below) != 2:
        return None
    ordered = [h for _y, h in sorted((r[1], h) for h, r in rows)]
    return ChoosePrinter(dialog, ordered, below[0][1], below[1][1])


@dataclass
class StudioLayout:
    """The Studio windows we care about (all HWNDs as ints)."""

    top: int
    content: int  # common parent of the Monitor and Others cards
    monitor_card: int
    others_card: int
    printer_rows: list[int] = field(default_factory=list)
    # Where our panels are parented: the window *above* ``content``. Studio
    # hides and re-shows ``content`` for a few dozen ms every ~3 s while it
    # refreshes the Device tab; panels inside it blinked along with it.
    host: int = 0
    sidebar: int = 0  # the "My Printer" column


# --------------------------------------------------------------------- Win32
if sys.platform == "win32":
    import ctypes
    import ctypes.wintypes as wt

    _user32 = ctypes.WinDLL("user32", use_last_error=True)
    _gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)
    _ENUMPROC = ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)

    def _sig(fn, restype, *argtypes):
        fn.restype = restype
        fn.argtypes = list(argtypes)
        return fn

    _EnumWindows = _sig(_user32.EnumWindows, wt.BOOL, _ENUMPROC, wt.LPARAM)
    _EnumChildWindows = _sig(_user32.EnumChildWindows, wt.BOOL, wt.HWND, _ENUMPROC, wt.LPARAM)
    _GetWindowTextLengthW = _sig(_user32.GetWindowTextLengthW, ctypes.c_int, wt.HWND)
    _GetWindowTextW = _sig(_user32.GetWindowTextW, ctypes.c_int, wt.HWND, wt.LPWSTR, ctypes.c_int)
    _IsWindowVisible = _sig(_user32.IsWindowVisible, wt.BOOL, wt.HWND)
    _IsWindow = _sig(_user32.IsWindow, wt.BOOL, wt.HWND)
    _IsIconic = _sig(_user32.IsIconic, wt.BOOL, wt.HWND)
    _GetParent = _sig(_user32.GetParent, wt.HWND, wt.HWND)
    _GetWindowRect = _sig(_user32.GetWindowRect, wt.BOOL, wt.HWND, ctypes.POINTER(wt.RECT))
    _ScreenToClient = _sig(_user32.ScreenToClient, wt.BOOL, wt.HWND, ctypes.POINTER(wt.POINT))
    _WindowFromPoint = _sig(_user32.WindowFromPoint, wt.HWND, wt.POINT)
    _GetAncestor = _sig(_user32.GetAncestor, wt.HWND, wt.HWND, wt.UINT)
    _GetWindowLongPtrW = _sig(_user32.GetWindowLongPtrW, ctypes.c_ssize_t, wt.HWND, ctypes.c_int)
    _SetWindowLongPtrW = _sig(_user32.SetWindowLongPtrW, ctypes.c_ssize_t, wt.HWND, ctypes.c_int, ctypes.c_ssize_t)
    _SetWindowPos = _sig(
        _user32.SetWindowPos, wt.BOOL, wt.HWND, wt.HWND,
        ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, wt.UINT,
    )
    _GetWindow = _sig(_user32.GetWindow, wt.HWND, wt.HWND, wt.UINT)
    _GetDpiForWindow = _sig(_user32.GetDpiForWindow, wt.UINT, wt.HWND)
    _GetDC = _sig(_user32.GetDC, wt.HDC, wt.HWND)
    _ReleaseDC = _sig(_user32.ReleaseDC, ctypes.c_int, wt.HWND, wt.HDC)
    _GetPixel = _sig(_gdi32.GetPixel, wt.DWORD, wt.HDC, ctypes.c_int, ctypes.c_int)

    _GWL_STYLE = -16
    _WS_CLIPSIBLINGS = 0x04000000
    _GA_ROOT = 2
    _GW_HWNDPREV = 3
    _SWP_NOSIZE, _SWP_NOMOVE, _SWP_NOZORDER, _SWP_NOACTIVATE = 0x1, 0x2, 0x4, 0x10
    _CLR_INVALID = 0xFFFFFFFF

    def _text(hwnd: int) -> str:
        n = _GetWindowTextLengthW(hwnd)
        if n <= 0:
            return ""
        buf = ctypes.create_unicode_buffer(n + 1)
        _GetWindowTextW(hwnd, buf, n + 1)
        return buf.value

    def window_rect(hwnd: int) -> Rect:
        r = wt.RECT()
        _GetWindowRect(hwnd, ctypes.byref(r))
        return (r.left, r.top, r.right - r.left, r.bottom - r.top)

    def rect_in_parent(hwnd: int, parent: int) -> Rect:
        """``hwnd``'s rect in ``parent``'s client coordinates (physical px)."""
        x, y, w, h = window_rect(hwnd)
        p = wt.POINT(x, y)
        _ScreenToClient(parent, ctypes.byref(p))
        return (p.x, p.y, w, h)

    def is_window(hwnd: int | None) -> bool:
        return bool(hwnd) and bool(_IsWindow(hwnd))

    def is_visible(hwnd: int) -> bool:
        """True only if the window and all its ancestors are shown (so the
        Device tab's cards report False while Studio shows the Slice tab)."""
        return bool(_IsWindowVisible(hwnd))

    def is_minimized(hwnd: int) -> bool:
        return bool(_IsIconic(hwnd))

    def dpi_scale(hwnd: int) -> float:
        dpi = _GetDpiForWindow(hwnd)
        return dpi / 96.0 if dpi else 1.0

    def find_studio() -> int | None:
        found: list[int] = []

        def cb(hwnd, _lp):
            if _IsWindowVisible(hwnd) and is_studio_title(_text(hwnd)):
                found.append(hwnd)
                return False
            return True

        _EnumWindows(_ENUMPROC(cb), 0)
        return found[0] if found else None

    def _children(parent: int) -> list[int]:
        out: list[int] = []
        _EnumChildWindows(parent, _ENUMPROC(lambda h, _lp: out.append(h) or True), 0)
        return out

    def _ancestors(hwnd: int) -> list[int]:
        chain = []
        while hwnd:
            chain.append(hwnd)
            hwnd = _GetParent(hwnd)
        return chain

    def locate_layout(top: int) -> StudioLayout | None:
        """Find the Device tab's cards inside Studio's main window.

        The Monitor and Others cards are the children of the deepest window
        containing both header labels. Returns None while the Device tab has
        never been built (e.g. Studio still starting) or the UI isn't English.
        """
        labels: dict[str, int] = {}
        for hwnd in _children(top):
            text = _text(hwnd)
            if text in (MONITOR_LABEL, OTHERS_LABEL, PRINTER_LIST_LABEL) and _IsWindowVisible(hwnd):
                labels.setdefault(text, hwnd)
        if MONITOR_LABEL not in labels or OTHERS_LABEL not in labels:
            return None
        mon_chain = _ancestors(labels[MONITOR_LABEL])
        oth_chain = _ancestors(labels[OTHERS_LABEL])
        oth_set = set(oth_chain)
        content = next((h for h in mon_chain if h in oth_set), None)
        if not content or content == top:
            return None
        monitor_card = mon_chain[mon_chain.index(content) - 1]
        others_card = oth_chain[oth_chain.index(content) - 1]
        rows: list[int] = []
        sidebar = 0
        if PRINTER_LIST_LABEL in labels:
            sidebar, rows = _locate_printer_rows(labels[PRINTER_LIST_LABEL])
        host = _GetParent(content) or content
        return StudioLayout(top, content, monitor_card, others_card, rows, host, sidebar)

    def _locate_printer_rows(list_label: int) -> tuple[int, list[int]]:
        # Walk up from the "My Printer" label to the whole sidebar column.
        label_rect = window_rect(list_label)
        sidebar = list_label
        while True:
            parent = _GetParent(sidebar)
            if not parent:
                break
            sidebar = parent
            if window_rect(sidebar)[3] > 6 * label_rect[3]:
                break
        sidebar_rect = window_rect(sidebar)
        candidates = [
            (h, window_rect(h)) for h in _children(sidebar) if _IsWindowVisible(h)
        ]
        return sidebar, pick_printer_rows(candidates, sidebar_rect, label_rect[1] + label_rect[3])

    def row_color(row: int) -> tuple[int, int, int] | None:
        """Background color of a printer row, read from the screen — or None
        if another window covers that spot (we only trust Studio's pixels)."""
        x, y, _w, h = window_rect(row)
        px, py = x + 4, y + h // 2
        under = _WindowFromPoint(wt.POINT(px, py))
        if not under or _GetAncestor(under, _GA_ROOT) != _GetAncestor(row, _GA_ROOT):
            return None
        hdc = _GetDC(None)
        try:
            value = _GetPixel(hdc, px, py)
        finally:
            _ReleaseDC(None, hdc)
        if value == _CLR_INVALID:
            return None
        return (value & 0xFF, (value >> 8) & 0xFF, (value >> 16) & 0xFF)

    _PostMessageW = _sig(_user32.PostMessageW, wt.BOOL, wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM)
    _IsWindowEnabled = _sig(_user32.IsWindowEnabled, wt.BOOL, wt.HWND)
    _WM_MOUSEMOVE, _WM_LBUTTONDOWN, _WM_LBUTTONUP, _MK_LBUTTON = 0x0200, 0x0201, 0x0202, 0x0001

    _GetClassNameW = _sig(_user32.GetClassNameW, ctypes.c_int, wt.HWND, wt.LPWSTR, ctypes.c_int)
    _BM_CLICK = 0x00F5

    def _class(hwnd: int) -> str:
        buf = ctypes.create_unicode_buffer(64)
        _GetClassNameW(hwnd, buf, 64)
        return buf.value

    def read_print_bar(root: int) -> tuple[str | None, dict[str, int]] | None:
        """The Progress card's bottom bar: (status kind, controls), or None
        when there's no Progress card on screen.

        Layout (eufyMake Studio): a "Progress" header, and a bar holding
        "<file name>", "|", "<status>" and two unlabeled icon Buttons.
        """
        header = next(
            (h for h in _children(root) if _text(h) == PROGRESS_LABEL and _IsWindowVisible(h)), None
        )
        if header is None:
            return None
        card = _GetParent(_GetParent(header) or header) or header
        # All the bar's icon buttons, hidden ones too: they mark the slots.
        bars: dict[int, list[tuple[int, int, bool]]] = {}
        for hwnd in _children(card):
            if _class(hwnd) == "Button" and not _text(hwnd):
                x, _y, w, h = window_rect(hwnd)
                if 0 < w <= 64 and 0 < h <= 64:
                    usable = bool(_IsWindowVisible(hwnd) and _IsWindowEnabled(hwnd))
                    bars.setdefault(_GetParent(hwnd), []).append((x, hwnd, usable))
        bar = next((p for p, buttons in bars.items() if len({b[0] for b in buttons}) >= 2), None)
        if bar is None:
            return None, {}
        statics = [
            (window_rect(h)[0], _text(h))
            for h in _children(bar)
            if _class(h) == "Static" and _IsWindowVisible(h) and _text(h)
        ]
        status = classify_print_status(status_from_bar(statics))
        return status, controls_from_bar(status, bars[bar])

    def read_print_state(top: int) -> tuple[str | None, dict[str, int]]:
        """(status kind or None, controls) for the print Studio shows."""
        bar = read_print_bar(top)
        status, controls = bar if bar is not None else (None, {})
        if not controls:
            controls = find_labeled_controls(top)
            if status is None and controls:
                # A labeled build without a readable status: the buttons say it.
                status = "paused" if "resume" in controls else "running" if "pause" in controls else None
        return status, controls

    def find_print_controls(top: int) -> dict[str, int]:
        """Studio's print-control buttons: {"pause"|"resume"|"stop": hwnd}."""
        return read_print_state(top)[1]

    def find_labeled_controls(top: int) -> dict[str, int]:
        """Studio builds with labeled print buttons."""
        found: dict[str, int] = {}
        for hwnd in _children(top):
            kind = classify_control(_text(hwnd))
            if kind and kind not in found and _IsWindowVisible(hwnd) and _IsWindowEnabled(hwnd):
                _x, _y, w, h = window_rect(hwnd)
                if w > 0 and h > 0:
                    found[kind] = hwnd
        return found

    def press(hwnd: int) -> bool:
        """Click a Studio button without touching the real mouse: post the
        clicks to it (asynchronous, so a hung Studio can't block us)."""
        if not is_window(hwnd):
            return False
        if _class(hwnd) == "Button":
            # A native button: BM_CLICK is its own "clicked" (posted, not sent).
            return bool(_PostMessageW(hwnd, _BM_CLICK, 0, 0))
        _x, _y, w, h = window_rect(hwnd)
        point = ((h // 2) << 16) | (w // 2 & 0xFFFF)
        ok = _PostMessageW(hwnd, _WM_MOUSEMOVE, 0, point)
        ok = _PostMessageW(hwnd, _WM_LBUTTONDOWN, _MK_LBUTTON, point) and ok
        ok = _PostMessageW(hwnd, _WM_LBUTTONUP, 0, point) and ok
        return bool(ok)

    _GetWindowThreadProcessId = _sig(
        _user32.GetWindowThreadProcessId, wt.DWORD, wt.HWND, ctypes.POINTER(wt.DWORD)
    )
    _ShowWindow = _sig(_user32.ShowWindow, wt.BOOL, wt.HWND, ctypes.c_int)
    _SetForegroundWindow = _sig(_user32.SetForegroundWindow, wt.BOOL, wt.HWND)
    _PrintWindow = _sig(_user32.PrintWindow, wt.BOOL, wt.HWND, wt.HDC, wt.UINT)
    _CreateCompatibleDC = _sig(_gdi32.CreateCompatibleDC, wt.HDC, wt.HDC)
    _CreateCompatibleBitmap = _sig(_gdi32.CreateCompatibleBitmap, wt.HBITMAP, wt.HDC, ctypes.c_int, ctypes.c_int)
    _SelectObject = _sig(_gdi32.SelectObject, wt.HGDIOBJ, wt.HDC, wt.HGDIOBJ)
    _DeleteObject = _sig(_gdi32.DeleteObject, wt.BOOL, wt.HGDIOBJ)
    _DeleteDC = _sig(_gdi32.DeleteDC, wt.BOOL, wt.HDC)
    _SW_RESTORE, _SW_SHOWNOACTIVATE = 9, 4
    _PW_RENDERFULLCONTENT = 2
    _WS_CLIPCHILDREN = 0x02000000

    _GetCursorPos = _sig(_user32.GetCursorPos, wt.BOOL, ctypes.POINTER(wt.POINT))

    def is_enabled(hwnd: int) -> bool:
        return bool(_IsWindowEnabled(hwnd))

    def title(hwnd: int) -> str:
        return _text(hwnd)

    def parent(hwnd: int) -> int:
        return _GetParent(hwnd) or 0

    def cursor_pos() -> tuple[int, int]:
        """The mouse position in physical screen pixels."""
        p = wt.POINT()
        _GetCursorPos(ctypes.byref(p))
        return p.x, p.y

    def find_tab(top: int, label: str) -> int | None:
        """One of Studio's top tabs ("Slice", "Device"): a Button whose
        caption is a child label."""
        for hwnd in _children(top):
            if _class(hwnd) == "Static" and _text(hwnd) == label:
                parent = _GetParent(hwnd)
                if parent and _class(parent) == "Button":
                    return parent
        return None

    def find_print_button(top: int) -> int | None:
        """The Slice tab's Print button, while it's on screen. The bar
        holds a second pair in the same spot (shown before slicing), so
        only the visible pair counts."""
        shown: dict[int, list[tuple[int, Rect]]] = {}
        for hwnd in _children(top):
            if _class(hwnd) != "wxWindowNR" or _text(hwnd) != "control" or not _IsWindowVisible(hwnd):
                continue
            rect = window_rect(hwnd)
            if rect[2] > 0 and rect[3] > 0:
                shown.setdefault(_GetParent(hwnd), []).append((hwnd, rect))
        return pick_print_button(list(shown.values()), window_rect(top))

    def _process_id(hwnd: int) -> int:
        pid = wt.DWORD()
        _GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        return pid.value

    def process_windows(top: int) -> list[int]:
        """Studio's visible top-level windows (main window and dialogs)."""
        pid = _process_id(top)
        found: list[int] = []

        def cb(hwnd, _lp):
            if _IsWindowVisible(hwnd) and _process_id(hwnd) == pid:
                found.append(hwnd)
            return True

        _EnumWindows(_ENUMPROC(cb), 0)
        return found

    def find_choose_printer(top: int) -> int | None:
        return next((h for h in process_windows(top) if _text(h) == CHOOSE_PRINTER_TITLE), None)

    def read_choose_printer(dialog: int) -> ChoosePrinter | None:
        rows: list[tuple[int, Rect]] = []
        buttons: list[tuple[int, Rect]] = []
        for hwnd in _children(dialog):
            if _class(hwnd) != "wxWindowNR" or not _IsWindowVisible(hwnd):
                continue
            parent = _GetParent(hwnd)
            if parent and _class(parent) == "ListBox":
                rows.append((hwnd, window_rect(hwnd)))
            elif _text(hwnd) == "control":
                buttons.append((hwnd, window_rect(hwnd)))
        return split_choose_printer(dialog, rows, buttons)

    def window_colors(hwnd: int, points: list[tuple[int, int]]) -> list[tuple[int, int, int]] | None:
        """Colors of a top-level window's own pixels at screen ``points``,
        drawn by the window itself (PrintWindow), so they're right even
        while another window covers it."""
        x0, y0, w, h = window_rect(hwnd)
        if w <= 0 or h <= 0:
            return None
        screen = _GetDC(None)
        mem = _CreateCompatibleDC(screen)
        bitmap = _CreateCompatibleBitmap(screen, w, h)
        old = _SelectObject(mem, bitmap)
        try:
            if not _PrintWindow(hwnd, mem, _PW_RENDERFULLCONTENT):
                return None
            colors = []
            for px, py in points:
                value = _GetPixel(mem, px - x0, py - y0)
                if value == _CLR_INVALID:
                    return None
                colors.append((value & 0xFF, (value >> 8) & 0xFF, (value >> 16) & 0xFF))
            return colors
        finally:
            _SelectObject(mem, old)
            _DeleteObject(bitmap)
            _DeleteDC(mem)
            _ReleaseDC(None, screen)

    def selected_rows(rows: list[int]) -> list[bool] | None:
        """Which of the dialog's printer rows is selected (green-tinted)."""
        if not rows:
            return None
        root = _GetAncestor(rows[0], _GA_ROOT)
        points = []
        for row in rows:
            x, y, _w, h = window_rect(row)
            points.append((x + 8, y + h // 2))
        colors = window_colors(root, points)
        return None if colors is None else [is_selected_row_color(c) for c in colors]

    def restore(top: int) -> None:
        """Un-minimize without stealing focus (a minimized Studio doesn't
        lay out its tabs)."""
        if _IsIconic(top):
            _ShowWindow(top, _SW_SHOWNOACTIVATE)

    def bring_to_front(top: int) -> None:
        if _IsIconic(top):
            _ShowWindow(top, _SW_RESTORE)
        _SetForegroundWindow(top)

    def ensure_clips_children(hwnd: int) -> None:
        """Stop a Studio window from painting over our panel inside it."""
        style = _GetWindowLongPtrW(hwnd, _GWL_STYLE)
        if not style & _WS_CLIPCHILDREN:
            _SetWindowLongPtrW(hwnd, _GWL_STYLE, style | _WS_CLIPCHILDREN)

    def ensure_clips_siblings(hwnd: int) -> None:
        """Stop a Studio card from painting over the panel we put above it.

        wx doesn't set WS_CLIPSIBLINGS, so without it every Studio repaint of
        the card would draw straight over our sibling window.
        """
        style = _GetWindowLongPtrW(hwnd, _GWL_STYLE)
        if not style & _WS_CLIPSIBLINGS:
            _SetWindowLongPtrW(hwnd, _GWL_STYLE, style | _WS_CLIPSIBLINGS)

    def is_above(ours: int, theirs: int) -> bool:
        """True if sibling ``ours`` is higher in z-order than ``theirs``."""
        h = _GetWindow(theirs, _GW_HWNDPREV)
        while h:
            if h == ours:
                return True
            h = _GetWindow(h, _GW_HWNDPREV)
        return False

    def place(hwnd: int, rect: Rect, raise_to_top: bool) -> None:
        """Move/resize a child window in exact physical pixels."""
        x, y, w, h = rect
        flags = _SWP_NOACTIVATE | (0 if raise_to_top else _SWP_NOZORDER)
        _SetWindowPos(hwnd, None, x, y, w, h, flags)  # HWND_TOP == NULL

else:  # pragma: no cover - non-Windows stubs

    def find_studio() -> int | None:
        return None

    def locate_layout(top: int) -> StudioLayout | None:
        return None

    def is_window(hwnd: int | None) -> bool:
        return False

    def find_print_controls(top: int) -> dict[str, int]:
        return {}

    def read_print_state(top: int) -> tuple[str | None, dict[str, int]]:
        return None, {}

    def press(hwnd: int) -> bool:
        return False

    def find_tab(top: int, label: str) -> int | None:
        return None

    def find_print_button(top: int) -> int | None:
        return None

    def find_choose_printer(top: int) -> int | None:
        return None

    def read_choose_printer(dialog: int) -> ChoosePrinter | None:
        return None

    def selected_rows(rows: list[int]) -> list[bool] | None:
        return None

    def is_visible(hwnd: int) -> bool:
        return False

    def is_enabled(hwnd: int) -> bool:
        return False

    def restore(top: int) -> None:
        pass

    def title(hwnd: int) -> str:
        return ""

    def parent(hwnd: int) -> int:
        return 0

    def bring_to_front(top: int) -> None:
        pass
