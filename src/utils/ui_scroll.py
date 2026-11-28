"""
Shared Windows-first Tkinter Scrolling & Responsive Layout Helpers
------------------------------------------------------------------
Provides leak-free, cross-window-safe scrolling, screen-clamped window sizing,
responsive Korean text wrapping, and pinned footer bars for all dialogs and
panels in Blue Archive Voice Companion.

Design guarantees:
- NEVER calls destructive global `unbind_all("<MouseWheel>")`.
- Installs at most ONE idempotent root-level mousewheel/keyboard router per
  `tk.Tk` root, tracking registered scrollable containers cleanly and
  unregistering automatically on `<Destroy>`.
- Hit-test-based routing works over every child widget (Label, Frame,
  LabelFrame, Checkbutton, Radiobutton, Entry, Button, empty space) without
  clobbering sibling `Toplevel` windows.
- Protects closed `ttk.Combobox` (and unfocused `ttk.Spinbox`) inside
  `ScrollableFrame` from accidental value mutations on hover-scroll on Windows,
  while still allowing open Combobox dropdowns and nested `Text`, `Listbox`,
  `Treeview`, and scrollable `Canvas` widgets to scroll naturally (falling
  through to the outer `ScrollableFrame` only when not scrollable or already
  at their scroll boundary).
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes
import os
import threading
import tkinter as tk
from tkinter import ttk
from typing import Any, Callable


_INNER_SCROLLABLE_CLASSES: frozenset[str] = frozenset({"Text", "Listbox", "Treeview"})
_GUARDED_WHEEL_CLASSES: frozenset[str] = frozenset({"TCombobox", "TSpinbox", "Spinbox"})
_FOCUSED_CONTROL_CLASSES: frozenset[str] = frozenset({"Scale", "TScale", "Spinbox", "TSpinbox"})
_KEYBOARD_BLOCK_CLASSES: frozenset[str] = frozenset(
    {
        "Entry",
        "TEntry",
        "Text",
        "Spinbox",
        "TSpinbox",
        "TCombobox",
        "Listbox",
        "Treeview",
        "Scale",
        "TScale",
    }
)


# ---------------------------------------------------------------------------
# Internal Tk hierarchy / event helpers
# ---------------------------------------------------------------------------

def _get_tk_root(widget: tk.Misc) -> tk.Misc:
    """Return the root `tk.Tk` interpreter widget for any Tk descendant."""
    try:
        return widget.nametowidget(".")
    except Exception:
        cur: Any = widget
        while getattr(cur, "master", None) is not None:
            cur = cur.master
        return cur


def _widget_exists(widget: Any) -> bool:
    if widget is None:
        return False
    try:
        return bool(widget.winfo_exists())
    except Exception:
        return False


def _widget_class(widget: Any) -> str:
    try:
        return str(widget.winfo_class() or "")
    except Exception:
        return ""


def _widget_toplevel(widget: Any) -> Any:
    try:
        return widget.winfo_toplevel()
    except Exception:
        return None


def _is_descendant(widget: Any, ancestor: Any) -> bool:
    """Return True if `widget` is `ancestor` or a descendant of `ancestor`."""
    if widget is None or ancestor is None:
        return False
    cur = widget
    guard = 0
    while cur is not None and guard < 128:
        if cur is ancestor or str(cur) == str(ancestor):
            return True
        cur = getattr(cur, "master", None)
        guard += 1
    return False


def _is_combobox_popdown(widget: Any) -> bool:
    """Return True if `widget` belongs to an open `ttk.Combobox` dropdown popdown."""
    try:
        path = str(widget)
        if ".popdown" in path.lower():
            return True
        top = _widget_toplevel(widget)
        if top is not None:
            top_cls = _widget_class(top)
            top_path = str(top).lower()
            if top_cls == "ComboboxPopdown" or ".popdown" in top_path:
                return True
    except Exception:
        pass
    return False


def _is_combobox_popdown_open(cb: Any) -> bool:
    """Return True iff `ttk.Combobox` `cb` currently has its dropdown list mapped."""
    try:
        popdown_name = cb.tk.call("ttk::combobox::PopdownWindow", str(cb))
        if popdown_name:
            return bool(int(cb.tk.call("winfo", "ismapped", popdown_name)))
    except Exception:
        pass
    return False


def _safe_event_int(event: Any, attr: str, default: int = 0) -> int:
    val = getattr(event, attr, default)
    if isinstance(val, bool):
        return default
    if isinstance(val, int):
        return val
    try:
        return int(val)
    except (TypeError, ValueError):
        return default


def _wheel_steps_from_event(event: Any, units: int = 3) -> int:
    """Convert a Windows `<MouseWheel>` or X11 `<Button-4>/<Button-5>` event into scroll units."""
    num = _safe_event_int(event, "num", 0)
    if num == 4:
        return -max(1, int(units))
    if num == 5:
        return max(1, int(units))
    delta = _safe_event_int(event, "delta", 0)
    if delta == 0:
        return 0
    raw = -(delta / 120.0) * max(1, int(units))
    steps = int(round(raw))
    if steps == 0:
        steps = -1 if delta > 0 else 1
    return steps


def _get_yview_pair(widget: Any) -> tuple[float, float] | None:
    try:
        yv = widget.yview()
        if isinstance(yv, (tuple, list)) and len(yv) >= 2:
            return (float(yv[0]), float(yv[1]))
    except Exception:
        pass
    return None


def _canvas_has_scroll_overflow(canvas: Any) -> bool:
    yv = _get_yview_pair(canvas)
    if yv is None:
        return False
    first, last = yv
    return not (first <= 1e-4 and last >= 1.0 - 1e-4)


def _is_independent_scrollable_canvas(widget: Any, owner_canvas: Any) -> bool:
    """Return True if `widget` is a nested scrollable Canvas distinct from `owner_canvas`."""
    if widget is owner_canvas or _widget_class(widget) != "Canvas":
        return False
    try:
        sr = str(widget.cget("scrollregion") or "").strip()
        if not sr:
            return False
        return _canvas_has_scroll_overflow(widget)
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Root-scoped MouseWheel & Keyboard Scroll Router
# ---------------------------------------------------------------------------

class _ScrollTargetEntry:
    __slots__ = ("target", "container", "yview_widget", "units", "on_scroll", "enable_keyboard")

    def __init__(
        self,
        target: Any,
        container: tk.Misc,
        yview_widget: Any,
        units: int,
        on_scroll: Callable[[int], bool] | None,
        enable_keyboard: bool,
    ) -> None:
        self.target = target
        self.container = container
        self.yview_widget = yview_widget
        self.units = max(1, int(units))
        self.on_scroll = on_scroll
        self.enable_keyboard = bool(enable_keyboard)


class _MouseWheelRouter:
    """Idempotent per-Tk-root scroll dispatcher (never calls `unbind_all`)."""

    def __init__(self, root: tk.Misc) -> None:
        self.root = root
        self._entries: dict[str, _ScrollTargetEntry] = {}
        self._focus_scroll_targets: dict[str, Any] = {}
        self._combobox_guarded: bool = False
        self._spinbox_guarded: bool = False
        self._install_bindings()

    def _install_bindings(self) -> None:
        # Install once per root with add="+"; never unbind_all.
        self.root.bind_all("<MouseWheel>", self._on_mousewheel, add="+")
        self.root.bind_all("<Button-4>", self._on_mousewheel, add="+")
        self.root.bind_all("<Button-5>", self._on_mousewheel, add="+")
        self.root.bind_all("<Prior>", self._on_key_scroll, add="+")
        self.root.bind_all("<Next>", self._on_key_scroll, add="+")
        self.root.bind_all("<Home>", self._on_key_scroll, add="+")
        self.root.bind_all("<End>", self._on_key_scroll, add="+")
        self.root.bind_all("<FocusIn>", self._on_focus_in, add="+")
        self._install_combobox_and_spinbox_guards()

    def _install_combobox_and_spinbox_guards(self) -> None:
        """Prevent closed `ttk.Combobox` / unfocused `ttk.Spinbox` inside a registered
        scrollable from mutating values on hover-scroll on Windows."""
        if not self._combobox_guarded:
            self._combobox_guarded = True
            self.root.bind_class("TCombobox", "<MouseWheel>", self._on_tcombobox_wheel)
            self.root.bind_class("TCombobox", "<Button-4>", self._on_tcombobox_wheel)
            self.root.bind_class("TCombobox", "<Button-5>", self._on_tcombobox_wheel)
        if not self._spinbox_guarded:
            self._spinbox_guarded = True
            self.root.bind_class("TSpinbox", "<MouseWheel>", self._on_tspinbox_wheel)
            self.root.bind_class("TSpinbox", "<Button-4>", self._on_tspinbox_wheel)
            self.root.bind_class("TSpinbox", "<Button-5>", self._on_tspinbox_wheel)

    def _on_tcombobox_wheel(self, event: Any) -> str | None:
        cb = getattr(event, "widget", None)
        if not _widget_exists(cb):
            return None
        # If dropdown is currently open, let the popdown handle scrolling.
        if _is_combobox_popdown_open(cb):
            return "break"
        # On Windows, <MouseWheel> fires with event.widget = focused widget. If this
        # Combobox holds focus while the pointer hovers elsewhere, route to the pointer target!
        ptr = self._resolve_pointer_widget(event)
        if ptr is not None and ptr is not cb:
            if not _is_combobox_popdown(ptr):
                self._on_mousewheel(event)
            return "break"
        entry = self._find_enclosing_entry(cb)
        if entry is not None:
            # Inside a registered ScrollableFrame: NEVER mutate a closed Combobox on wheel;
            # scroll the outer scrollable container instead!
            self._dispatch_scroll_for_entry(entry, event)
            return "break"
        # Outside any ScrollableFrame: keep default Tk TCombobox wheel behavior.
        delta = _safe_event_int(event, "delta", 0)
        num = _safe_event_int(event, "num", 0)
        if num == 4:
            step = -1
        elif num == 5:
            step = 1
        elif delta != 0:
            step = -1 if delta > 0 else 1
        else:
            step = 0
        if step != 0:
            try:
                cb.tk.call("ttk::combobox::Scroll", str(cb), step)
            except Exception:
                pass
        return "break"

    def _on_tspinbox_wheel(self, event: Any) -> str | None:
        sb = getattr(event, "widget", None)
        if not _widget_exists(sb):
            return None
        # If this Spinbox holds focus while the pointer hovers elsewhere, route to pointer target.
        ptr = self._resolve_pointer_widget(event)
        if ptr is not None and ptr is not sb:
            if not _is_combobox_popdown(ptr):
                self._on_mousewheel(event)
            return "break"
        entry = self._find_enclosing_entry(sb)
        focused = None
        try:
            focused = self.root.focus_get()
        except Exception:
            focused = None
        if entry is not None and focused is not sb:
            # Unfocused spinbox inside a scrollable form -> scroll the page, don't mutate number.
            self._dispatch_scroll_for_entry(entry, event)
            return "break"
        delta = _safe_event_int(event, "delta", 0)
        num = _safe_event_int(event, "num", 0)
        if num == 4:
            step = -1
        elif num == 5:
            step = 1
        elif delta != 0:
            step = -1 if delta > 0 else 1
        else:
            step = 0
        if step != 0:
            try:
                sb.tk.call("ttk::spinbox::MouseWheel", str(sb), step)
            except Exception:
                pass
        return "break"

    def register(
        self,
        target: Any,
        container: tk.Misc,
        yview_widget: Any,
        *,
        units: int = 3,
        on_scroll: Callable[[int], bool] | None = None,
        enable_keyboard: bool = True,
        auto_scroll_on_focus: bool = False,
    ) -> str:
        key = str(container)
        self._entries[key] = _ScrollTargetEntry(
            target=target,
            container=container,
            yview_widget=yview_widget,
            units=units,
            on_scroll=on_scroll,
            enable_keyboard=enable_keyboard,
        )
        if auto_scroll_on_focus:
            self._focus_scroll_targets[key] = target

        def _cleanup(ev: Any) -> None:
            if getattr(ev, "widget", None) is container:
                self.unregister(key)

        try:
            container.bind("<Destroy>", _cleanup, add="+")
        except Exception:
            pass
        return key

    def set_auto_scroll_on_focus(self, container: tk.Misc, target: Any, enabled: bool) -> None:
        key = str(container)
        if enabled:
            self._focus_scroll_targets[key] = target
        else:
            self._focus_scroll_targets.pop(key, None)

    def unregister(self, key: str) -> None:
        self._entries.pop(key, None)
        self._focus_scroll_targets.pop(key, None)

    def _prune_dead(self) -> None:
        dead = [k for k, e in self._entries.items() if not _widget_exists(e.container)]
        for k in dead:
            self._entries.pop(k, None)
            self._focus_scroll_targets.pop(k, None)

    def _find_enclosing_entry(self, widget: Any) -> _ScrollTargetEntry | None:
        """Walk up `widget`'s ancestor chain and return the nearest registered entry."""
        if not self._entries or not _widget_exists(widget):
            return None
        cur: Any = widget
        guard = 0
        while cur is not None and guard < 128:
            entry = self._entries.get(str(cur))
            if entry is not None and _widget_exists(entry.container):
                return entry
            cur = getattr(cur, "master", None)
            guard += 1
        return None

    def _resolve_pointer_widget(self, event: Any) -> Any:
        """Resolve the widget under the pointer when coordinates are real, else `event.widget`."""
        ev_widget = getattr(event, "widget", None)
        x_root = getattr(event, "x_root", None)
        y_root = getattr(event, "y_root", None)
        if isinstance(x_root, int) and isinstance(y_root, int) and not isinstance(x_root, bool) and not isinstance(y_root, bool):
            try:
                under = self.root.winfo_containing(x_root, y_root)
            except Exception:
                under = None
            if _widget_exists(under):
                # Respect Tk modal grabs: if another window holds a grab, only route inside it.
                try:
                    grab = self.root.grab_current()
                except Exception:
                    grab = None
                if grab is not None and _widget_exists(grab):
                    if _is_descendant(under, grab) or _is_combobox_popdown(under):
                        return under
                else:
                    return under
        return ev_widget if _widget_exists(ev_widget) else None

    def _find_inner_scrollable(self, widget: Any, entry: _ScrollTargetEntry) -> Any:
        """Find any nested scrollable widget strictly inside `entry.container` above `widget`."""
        cur: Any = widget
        guard = 0
        owner_canvas = entry.yview_widget
        while cur is not None and guard < 128:
            if cur is entry.container or str(cur) == str(entry.container):
                return None
            cls = _widget_class(cur)
            if cls in _INNER_SCROLLABLE_CLASSES:
                return cur
            if _is_independent_scrollable_canvas(cur, owner_canvas):
                return cur
            cur = getattr(cur, "master", None)
            guard += 1
        return None

    def _should_inner_consume_wheel(self, inner: Any, event: Any, steps: int) -> bool:
        """Return True if the nested scrollable widget should consume this wheel tick."""
        yv = _get_yview_pair(inner)
        if yv is None:
            return False
        first, last = yv

        # Inner widget has no vertical overflow at all -> let outer scroll cleanly.
        if first <= 1e-4 and last >= 1.0 - 1e-4:
            return False

        ev_widget = getattr(event, "widget", None)
        # Check if inner widget has room in the requested scroll direction.
        has_room = (steps < 0 and first > 1e-4) or (steps > 0 and last < 1.0 - 1e-4)
        if has_room:
            # If Windows routed `<MouseWheel>` to a different focused widget while hovering
            # over this inner Text/Listbox/Treeview, scroll the inner widget directly!
            if ev_widget is not inner and _widget_class(inner) in _INNER_SCROLLABLE_CLASSES:
                try:
                    inner.yview_scroll(steps, "units")
                except Exception:
                    pass
            return True

        return False

    def _dispatch_scroll_for_entry(self, entry: _ScrollTargetEntry, event: Any) -> bool:
        steps = _wheel_steps_from_event(event, entry.units)
        if steps == 0:
            return False
        if entry.on_scroll is not None:
            try:
                return bool(entry.on_scroll(steps))
            except Exception:
                return False
        yv_widget = entry.yview_widget
        if not _widget_exists(yv_widget):
            return False
        yv = _get_yview_pair(yv_widget)
        if yv is None:
            return False
        first, last = yv
        if first <= 1e-4 and last >= 1.0 - 1e-4:
            return False
        if (steps < 0 and first <= 1e-4) or (steps > 0 and last >= 1.0 - 1e-4):
            return False
        try:
            yv_widget.yview_scroll(steps, "units")
            return True
        except Exception:
            return False

    def _on_mousewheel(self, event: Any) -> None:
        if not self._entries:
            return
        self._prune_dead()
        target_widget = self._resolve_pointer_widget(event)
        if target_widget is None:
            return

        # Never scroll an outer ScrollableFrame while scrolling inside a Combobox dropdown popdown.
        if _is_combobox_popdown(target_widget):
            return

        # Respect focused Spinbox / Scale controls if wheel is aimed directly at them.
        t_cls = _widget_class(target_widget)
        if t_cls in _FOCUSED_CONTROL_CLASSES:
            try:
                if self.root.focus_get() is target_widget:
                    return
            except Exception:
                pass

        entry = self._find_enclosing_entry(target_widget)
        if entry is None:
            return

        # Check nested scrollable widgets (Text, Listbox, Treeview, nested scrollable Canvas).
        inner = self._find_inner_scrollable(target_widget, entry)
        if inner is not None:
            steps = _wheel_steps_from_event(event, entry.units)
            if steps != 0 and self._should_inner_consume_wheel(inner, event, steps):
                return

        self._dispatch_scroll_for_entry(entry, event)

    def _on_key_scroll(self, event: Any) -> None:
        if not self._entries:
            return
        self._prune_dead()
        ev_widget = getattr(event, "widget", None)
        if not _widget_exists(ev_widget):
            return
        if _widget_class(ev_widget) in _KEYBOARD_BLOCK_CLASSES:
            return

        # 1) Prefer the scrollable container enclosing the focused widget.
        entry = self._find_enclosing_entry(ev_widget)
        # 2) Otherwise pick the mapped scrollable inside the same Toplevel.
        if entry is None:
            ev_top = _widget_toplevel(ev_widget)
            if ev_top is None:
                return
            candidates = [
                e
                for e in self._entries.values()
                if e.enable_keyboard
                and _widget_exists(e.container)
                and _widget_toplevel(e.container) is ev_top
                and bool(e.container.winfo_ismapped())
            ]
            if len(candidates) == 1:
                entry = candidates[0]
            elif len(candidates) > 1:
                # Prefer the candidate currently under the mouse pointer, else first mapped.
                try:
                    px, py = self.root.winfo_pointerxy()
                    under = self.root.winfo_containing(px, py)
                    ptr_entry = self._find_enclosing_entry(under) if under is not None else None
                except Exception:
                    ptr_entry = None
                entry = ptr_entry if ptr_entry in candidates else candidates[0]

        if entry is None or not entry.enable_keyboard:
            return
        yv_widget = entry.yview_widget
        if not _widget_exists(yv_widget):
            return
        yv = _get_yview_pair(yv_widget)
        if yv is None or (yv[0] <= 1e-4 and yv[1] >= 1.0 - 1e-4):
            return

        keysym = str(getattr(event, "keysym", "") or "")
        try:
            if keysym == "Prior":
                yv_widget.yview_scroll(-1, "pages")
            elif keysym == "Next":
                yv_widget.yview_scroll(1, "pages")
            elif keysym == "Home":
                yv_widget.yview_moveto(0.0)
            elif keysym == "End":
                yv_widget.yview_moveto(1.0)
        except Exception:
            pass

    def _on_focus_in(self, event: Any) -> None:
        if not self._focus_scroll_targets:
            return
        w = getattr(event, "widget", None)
        if not _widget_exists(w):
            return
        entry = self._find_enclosing_entry(w)
        if entry is None:
            return
        target = self._focus_scroll_targets.get(str(entry.container))
        if target is None or not _widget_exists(target):
            return
        # Only scroll to widgets inside `target.inner`, not the canvas/frame itself.
        inner = getattr(target, "inner", None)
        if inner is not None and w is not inner and _is_descendant(w, inner):
            scroll_fn = getattr(target, "scroll_to_widget", None)
            if callable(scroll_fn):
                try:
                    scroll_fn(w)
                except Exception:
                    pass


def bind_mousewheel_router(widget: tk.Misc) -> _MouseWheelRouter:
    """Install (idempotently) and return the per-root `_MouseWheelRouter`."""
    root = _get_tk_root(widget)
    router = getattr(root, "_ui_scroll_wheel_router", None)
    if not isinstance(router, _MouseWheelRouter):
        router = _MouseWheelRouter(root)
        setattr(root, "_ui_scroll_wheel_router", router)
    return router


def attach_scrollable_mousewheel(
    target: Any,
    *,
    container: tk.Misc | None = None,
    units: int = 3,
    on_scroll: Callable[[int], bool] | None = None,
    yview_widget: Any | None = None,
    enable_keyboard: bool = True,
    auto_scroll_on_focus: bool = False,
) -> Callable[[], None]:
    """Attach hit-test-based `<MouseWheel>` (and keyboard) scrolling to any scrollable widget.

    Works safely across multiple open windows/dialogs without ever calling `unbind_all`.
    Automatically cleans up when `container` is destroyed. Returns a `detach()` callable.
    """
    cont = container if container is not None else target
    yv_w = yview_widget if yview_widget is not None else getattr(target, "canvas", target)
    router = bind_mousewheel_router(cont)
    key = router.register(
        target=target,
        container=cont,
        yview_widget=yv_w,
        units=units,
        on_scroll=on_scroll,
        enable_keyboard=enable_keyboard,
        auto_scroll_on_focus=auto_scroll_on_focus,
    )
    return lambda: router.unregister(key)


# ---------------------------------------------------------------------------
# ScrollableFrame
# ---------------------------------------------------------------------------

class ScrollableFrame(tk.Frame):
    """Reusable vertically scrollable container (`Canvas` + `ttk.Scrollbar` + `inner` Frame).

    Usage::
        sf = ScrollableFrame(tab, bg="#F3F5F8", padx=6, pady=6, bottom_pad=28)
        sf.pack(fill="both", expand=True)
        tk.Label(sf.inner, text="...").pack(anchor="w")
    """

    def __init__(
        self,
        parent: tk.Misc,
        *,
        bg: str = "#F3F5F8",
        padx: int = 0,
        pady: int = 0,
        bottom_pad: int = 24,
        autohide_scrollbar: bool = False,
        yscrollincrement: int = 24,
        wheel_units: int = 3,
        enable_keyboard: bool = True,
        auto_scroll_on_focus: bool = False,
        **frame_kwargs: Any,
    ) -> None:
        frame_kwargs.setdefault("bg", bg)
        super().__init__(parent, **frame_kwargs)

        self._bg = bg
        self._padx = max(0, int(padx))
        self._pady = max(0, int(pady))
        self._bottom_pad = max(0, int(bottom_pad))
        self._autohide_scrollbar = bool(autohide_scrollbar)
        self._scrollbar_visible = True
        self._scroll_content_height: int = 1
        self._last_canvas_width: int = 0
        self._last_scrollregion: tuple[int, int, int, int] = (0, 0, 0, 0)
        self._refresh_idle_id: str | None = None

        self.canvas = tk.Canvas(
            self,
            bg=bg,
            highlightthickness=0,
            bd=0,
            yscrollincrement=max(1, int(yscrollincrement)),
        )
        self.scrollbar = ttk.Scrollbar(self, orient="vertical", command=self.canvas.yview)
        self.canvas.configure(yscrollcommand=self._on_canvas_yscroll)

        self.scrollbar.pack(side="right", fill="y")
        self.canvas.pack(side="left", fill="both", expand=True)

        # Host frame sits inside the canvas window at natural requested height so Tk
        # ALWAYS propagates dynamic child show/hide/reflow changes via <Configure>.
        self._host = tk.Frame(self.canvas, bg=bg)
        self.inner = tk.Frame(self._host, bg=bg)
        self.inner.pack(
            fill="both",
            expand=True,
            padx=self._padx,
            pady=(self._pady, self._bottom_pad),
        )
        self._window_id = self.canvas.create_window((0, 0), window=self._host, anchor="nw")

        self.canvas.bind("<Configure>", self._on_canvas_configure, add="+")
        self._host.bind("<Configure>", self._on_content_configure, add="+")
        self.inner.bind("<Configure>", self._on_content_configure, add="+")
        self.bind("<Map>", self._on_map, add="+")
        self.bind("<Destroy>", self._on_destroy_event, add="+")

        self._detach_wheel = attach_scrollable_mousewheel(
            self,
            container=self,
            yview_widget=self.canvas,
            units=wheel_units,
            enable_keyboard=enable_keyboard,
            auto_scroll_on_focus=auto_scroll_on_focus,
        )

    @property
    def content(self) -> tk.Frame:
        """Alias for `self.inner`."""
        return self.inner

    @property
    def body(self) -> tk.Frame:
        """Alias for `self.inner`."""
        return self.inner

    def set_bg(self, color: str) -> None:
        self._bg = color
        for w in (self, self.canvas, self._host, self.inner):
            try:
                w.configure(bg=color)
            except Exception:
                pass

    def _on_canvas_yscroll(self, first: str, last: str) -> None:
        try:
            self.scrollbar.set(first, last)
        except Exception:
            pass
        if self._autohide_scrollbar:
            try:
                f_val = float(first)
                l_val = float(last)
                need_bar = not (f_val <= 1e-4 and l_val >= 1.0 - 1e-4)
                if need_bar and not self._scrollbar_visible:
                    self.scrollbar.pack(side="right", fill="y", before=self.canvas)
                    self._scrollbar_visible = True
                elif not need_bar and self._scrollbar_visible:
                    self.scrollbar.pack_forget()
                    self._scrollbar_visible = False
            except Exception:
                pass

    def _on_canvas_configure(self, event: Any) -> None:
        cw = max(1, int(getattr(event, "width", 0) or self.canvas.winfo_width() or 1))
        if cw != self._last_canvas_width:
            self._last_canvas_width = cw
            try:
                self.canvas.itemconfigure(self._window_id, width=cw)
            except Exception:
                pass
        self.refresh_scrollregion(defer=False)
        self._schedule_idle_refresh()

    def _on_content_configure(self, _event: Any = None) -> None:
        self.refresh_scrollregion(defer=False)
        self._schedule_idle_refresh()

    def _on_map(self, _event: Any = None) -> None:
        self.refresh_scrollregion(defer=False)
        self._schedule_idle_refresh()

    def _schedule_idle_refresh(self) -> None:
        if not _widget_exists(self):
            return
        if self._refresh_idle_id is not None:
            try:
                self.after_cancel(self._refresh_idle_id)
            except Exception:
                pass
            self._refresh_idle_id = None

        def _run() -> None:
            self._refresh_idle_id = None
            if _widget_exists(self):
                self.refresh_scrollregion(defer=False)

        try:
            self._refresh_idle_id = self.after_idle(_run)
        except Exception:
            self._refresh_idle_id = None

    def refresh_scrollregion(self, *, defer: bool = False) -> tuple[int, int, int, int]:
        """Recompute and apply the Canvas `scrollregion` & inner width accurately."""
        if defer:
            self._schedule_idle_refresh()
            return self._last_scrollregion
        if not _widget_exists(self) or not _widget_exists(self.canvas) or not _widget_exists(self._host):
            return self._last_scrollregion

        cw = int(self.canvas.winfo_width() or 0)
        ch = int(self.canvas.winfo_height() or 0)
        if cw <= 1:
            cw = max(1, int(self._host.winfo_reqwidth() or 1))
        if cw != self._last_canvas_width and cw > 1:
            self._last_canvas_width = cw
            try:
                self.canvas.itemconfigure(self._window_id, width=cw)
            except Exception:
                pass

        host_req_h = max(1, int(self._host.winfo_reqheight() or 1))
        inner_req_h = max(1, int(self.inner.winfo_reqheight() or 1)) + self._pady + self._bottom_pad
        bbox_h = 0
        try:
            bbox = self.canvas.bbox(self._window_id)
            if bbox is not None and len(bbox) == 4:
                bbox_h = max(0, int(bbox[3] - bbox[1]))
        except Exception:
            bbox_h = 0

        content_h = max(host_req_h, inner_req_h, bbox_h)
        region_h = max(content_h, ch if ch > 1 else 1)
        self._scroll_content_height = region_h
        region = (0, 0, max(1, cw), region_h)

        if region != self._last_scrollregion:
            self._last_scrollregion = region
            try:
                self.canvas.configure(scrollregion=region)
            except Exception:
                pass

        # If content shrunk so it now fits completely, snap back to top cleanly.
        if ch > 1 and content_h <= ch:
            try:
                first, _last = self.canvas.yview()
                if first > 0.0:
                    self.canvas.yview_moveto(0.0)
            except Exception:
                pass
        return region

    def is_scrollable(self) -> bool:
        """Return True iff content currently overflows the visible canvas viewport."""
        return _canvas_has_scroll_overflow(self.canvas)

    def scroll_to_top(self) -> None:
        if _widget_exists(self.canvas):
            try:
                self.canvas.yview_moveto(0.0)
            except Exception:
                pass

    def scroll_to_bottom(self) -> None:
        if _widget_exists(self.canvas):
            self.refresh_scrollregion(defer=False)
            try:
                self.canvas.yview_moveto(1.0)
            except Exception:
                pass

    def scroll_by_units(self, units: int) -> bool:
        if not _widget_exists(self.canvas) or units == 0 or not self.is_scrollable():
            return False
        try:
            before = self.canvas.yview()
            self.canvas.yview_scroll(int(units), "units")
            return self.canvas.yview() != before
        except Exception:
            return False

    def scroll_to_widget(self, widget: tk.Misc, *, pad: int = 16) -> bool:
        """Scroll smoothly so `widget` (any descendant of `self.inner`) is fully visible."""
        if not _widget_exists(self) or not _widget_exists(widget) or not _widget_exists(self._host):
            return False
        if not _is_descendant(widget, self._host):
            return False
        try:
            self.update_idletasks()
            self.refresh_scrollregion(defer=False)
            host_top = int(self._host.winfo_rooty())
            w_top = int(widget.winfo_rooty()) - host_top
            w_h = max(1, int(widget.winfo_height() or widget.winfo_reqheight() or 1))
            w_bot = w_top + w_h
            canvas_h = max(1, int(self.canvas.winfo_height() or 1))
            total_h = max(1, int(self._scroll_content_height or 1))
            if total_h <= canvas_h:
                self.canvas.yview_moveto(0.0)
                return False
            first, last = self.canvas.yview()
            view_top = first * total_h
            view_bot = last * total_h
            if w_top - pad < view_top:
                target_y = max(0.0, float(w_top - pad))
                self.canvas.yview_moveto(target_y / float(total_h))
                return True
            if w_bot + pad > view_bot:
                target_y = min(float(total_h - canvas_h), max(0.0, float(w_bot + pad - canvas_h)))
                self.canvas.yview_moveto(target_y / float(total_h))
                return True
        except Exception:
            pass
        return False

    def _cleanup_resources(self) -> None:
        if self._refresh_idle_id is not None:
            try:
                self.after_cancel(self._refresh_idle_id)
            except Exception:
                pass
            self._refresh_idle_id = None
        try:
            self._detach_wheel()
        except Exception:
            pass

    def _on_destroy_event(self, event: Any) -> None:
        if getattr(event, "widget", None) is self:
            self._cleanup_resources()

    def destroy(self) -> None:
        self._cleanup_resources()
        super().destroy()


# ---------------------------------------------------------------------------
# Screen Work-Area & Window Geometry Clamping
# ---------------------------------------------------------------------------

class _MONITORINFO(ctypes.Structure):
    _fields_ = [
        ("cbSize", ctypes.wintypes.DWORD),
        ("rcMonitor", ctypes.wintypes.RECT),
        ("rcWork", ctypes.wintypes.RECT),
        ("dwFlags", ctypes.wintypes.DWORD),
    ]


_UI_USER32_LOCK = threading.Lock()
_UI_PRIVATE_USER32: Any = None


def _get_private_user32() -> Any:
    """Return a private 64-bit-safe `user32` WinDLL handle without mutating global `ctypes.windll.user32`."""
    global _UI_PRIVATE_USER32
    if _UI_PRIVATE_USER32 is not None:
        return _UI_PRIVATE_USER32
    with _UI_USER32_LOCK:
        if _UI_PRIVATE_USER32 is None:
            u32 = ctypes.WinDLL("user32", use_last_error=True)
            u32.MonitorFromWindow.argtypes = [ctypes.wintypes.HWND, ctypes.wintypes.DWORD]
            u32.MonitorFromWindow.restype = ctypes.wintypes.HMONITOR
            u32.GetMonitorInfoW.argtypes = [ctypes.wintypes.HMONITOR, ctypes.POINTER(_MONITORINFO)]
            u32.GetMonitorInfoW.restype = ctypes.wintypes.BOOL
            u32.SystemParametersInfoW.argtypes = [
                ctypes.wintypes.UINT,
                ctypes.wintypes.UINT,
                ctypes.c_void_p,
                ctypes.wintypes.UINT,
            ]
            u32.SystemParametersInfoW.restype = ctypes.wintypes.BOOL
            _UI_PRIVATE_USER32 = u32
    return _UI_PRIVATE_USER32


def get_work_area(win: tk.Misc | None = None) -> tuple[int, int, int, int]:
    """Return usable desktop work area `(left, top, width, height)` excluding the taskbar.

    Respects multi-monitor layouts on Windows and stays compatible with unit tests
    that mock `win.winfo_screenwidth()` / `win.winfo_screenheight()`.
    """
    tk_sw = 1280
    tk_sh = 720
    if win is not None:
        try:
            tk_sw = max(320, int(win.winfo_screenwidth() or 1280))
            tk_sh = max(240, int(win.winfo_screenheight() or 720))
        except Exception:
            pass

    if os.name == "nt":
        try:
            raw_u32 = ctypes.windll.user32
            user32 = _get_private_user32() if isinstance(raw_u32, ctypes.WinDLL) else raw_u32
            # 1) Try monitor containing `win` (or its parent) first
            hwnd = 0
            if win is not None:
                try:
                    top = _widget_toplevel(win)
                    if top is not None:
                        hwnd = int(top.winfo_id() or 0)
                except Exception:
                    hwnd = 0
            if hwnd:
                hmon = user32.MonitorFromWindow(ctypes.wintypes.HWND(hwnd), 2)  # MONITOR_DEFAULTTONEAREST
                if hmon:
                    mi = _MONITORINFO()
                    mi.cbSize = ctypes.sizeof(_MONITORINFO)
                    if user32.GetMonitorInfoW(hmon, ctypes.byref(mi)):
                        work_w = int(mi.rcWork.right - mi.rcWork.left)
                        work_h = int(mi.rcWork.bottom - mi.rcWork.top)
                        # Verify Win32 monitor coordinates share Tk's DPI coordinate space by comparing
                        # against the PRIMARY monitor (Tk winfo_screenwidth/height always report primary),
                        # so heterogeneous secondary monitors (different resolution or negative virtual
                        # coordinates) are accepted while mocked screen sizes in tests still take effect.
                        if mi.dwFlags & 1:  # MONITORINFOF_PRIMARY
                            prim_w = int(mi.rcMonitor.right - mi.rcMonitor.left)
                            prim_h = int(mi.rcMonitor.bottom - mi.rcMonitor.top)
                            dpi_space_matches = (prim_w == tk_sw and prim_h == tk_sh)
                        else:
                            dpi_space_matches = False
                            hmon_primary = user32.MonitorFromWindow(ctypes.wintypes.HWND(0), 1)  # MONITOR_DEFAULTTOPRIMARY
                            if hmon_primary:
                                mi_primary = _MONITORINFO()
                                mi_primary.cbSize = ctypes.sizeof(_MONITORINFO)
                                if user32.GetMonitorInfoW(hmon_primary, ctypes.byref(mi_primary)):
                                    prim_w = int(mi_primary.rcMonitor.right - mi_primary.rcMonitor.left)
                                    prim_h = int(mi_primary.rcMonitor.bottom - mi_primary.rcMonitor.top)
                                    dpi_space_matches = (prim_w == tk_sw and prim_h == tk_sh)
                        if dpi_space_matches and work_w >= 320 and work_h >= 240:
                            return (int(mi.rcWork.left), int(mi.rcWork.top), work_w, work_h)

            # 2) Fallback: primary monitor SPI_GETWORKAREA
            rect = ctypes.wintypes.RECT()
            if user32.SystemParametersInfoW(0x0030, 0, ctypes.byref(rect), 0):
                work_w = int(rect.right - rect.left)
                work_h = int(rect.bottom - rect.top)
                # Ensure SPI_GETWORKAREA matches Tk's reported screen coordinate space
                if (
                    work_w >= 320
                    and work_h >= 240
                    and work_w <= tk_sw
                    and work_h <= tk_sh
                    and (tk_sw - work_w) <= 160
                    and (tk_sh - work_h) <= 160
                ):
                    return (int(rect.left), int(rect.top), work_w, work_h)
        except Exception:
            pass

    taskbar_pad = 48 if tk_sh >= 480 else 24
    return (0, 0, tk_sw, max(240, tk_sh - taskbar_pad))


def clamp_window_position(
    win: tk.Misc,
    x: int,
    y: int,
    width: int | None = None,
    height: int | None = None,
    *,
    margin: int = 8,
    work_area: tuple[int, int, int, int] | None = None,
) -> tuple[int, int]:
    """Clamp `(x, y)` so a window of `(width, height)` remains completely inside the work area."""
    wl, wt, ww, wh = work_area if work_area is not None else get_work_area(win)
    try:
        w = int(width) if width is not None else max(1, int(win.winfo_width() or win.winfo_reqwidth() or 320))
        h = int(height) if height is not None else max(1, int(win.winfo_height() or win.winfo_reqheight() or 200))
    except Exception:
        w = int(width or 320)
        h = int(height or 200)
    m = max(0, int(margin))
    min_x = wl + m
    min_y = wt + m
    max_x = max(min_x, wl + ww - w - m)
    max_y = max(min_y, wt + wh - h - m)
    return (max(min_x, min(int(x), max_x)), max(min_y, min(int(y), max_y)))


def fit_window_to_screen(
    win: tk.Wm,
    preferred_w: int,
    preferred_h: int,
    min_w: int | None = None,
    min_h: int | None = None,
    *,
    max_screen_ratio: float = 0.88,
    center_on: tk.Misc | None = None,
    margin: int = 16,
    apply_minsize: bool = True,
    work_area: tuple[int, int, int, int] | None = None,
) -> tuple[int, int, int, int]:
    """Clamp window dimensions & position cleanly inside the usable desktop work area.

    Prevents bottom buttons/text from ever being clipped behind the taskbar or off-screen
    on 1366x768, 1024x600, or 1080p @ 125%/150% DPI scaling.

    Returns `(width, height, x, y)`.
    """
    ref_win: tk.Misc = center_on if (center_on is not None and _widget_exists(center_on)) else win  # type: ignore[assignment]
    wl, wt, ww, wh = work_area if work_area is not None else get_work_area(ref_win)
    ratio = max(0.35, min(1.0, float(max_screen_ratio)))
    m = max(4, int(margin))

    avail_w = max(320, ww - m * 2)
    avail_h = max(240, wh - m * 2)
    cap_w = max(320, min(avail_w, int(ww * ratio)))
    cap_h = max(240, min(avail_h, int(wh * ratio)))

    w = max(320, min(int(preferred_w), cap_w))
    h = max(240, min(int(preferred_h), cap_h))

    if min_w is not None:
        eff_min_w = max(240, min(int(min_w), w))
    else:
        eff_min_w = min(480, w)
    if min_h is not None:
        eff_min_h = max(180, min(int(min_h), h))
    else:
        eff_min_h = min(360, h)

    cx = wl + (ww - w) // 2
    cy = wt + (wh - h) // 2
    if center_on is not None and _widget_exists(center_on):
        try:
            pw = int(center_on.winfo_width() or 0)
            ph = int(center_on.winfo_height() or 0)
            if pw > 1 and ph > 1:
                cx = int(center_on.winfo_rootx()) + (pw - w) // 2
                cy = int(center_on.winfo_rooty()) + (ph - h) // 2
        except Exception:
            pass

    x, y = clamp_window_position(win, cx, cy, w, h, margin=8, work_area=(wl, wt, ww, wh))  # type: ignore[arg-type]
    try:
        win.geometry(f"{w}x{h}+{x}+{y}")
    except Exception:
        pass
    if apply_minsize:
        try:
            win.minsize(eff_min_w, eff_min_h)
        except Exception:
            pass
    return (w, h, x, y)


# ---------------------------------------------------------------------------
# Responsive Text Wrapping & Pinned Footer Helpers
# ---------------------------------------------------------------------------

_WRAPPABLE_CLASSES: frozenset[str] = frozenset({"Label", "TLabel", "Checkbutton", "Radiobutton"})
_CONTAINER_CLASSES: frozenset[str] = frozenset({"Frame", "TFrame", "Labelframe", "TLabelframe", "Toplevel", "Tk"})


def _find_parent_scrollable(widget: Any) -> ScrollableFrame | None:
    cur = getattr(widget, "master", None)
    guard = 0
    while cur is not None and guard < 128:
        if isinstance(cur, ScrollableFrame):
            return cur
        cur = getattr(cur, "master", None)
        guard += 1
    return None


def bind_responsive_wrap(
    label_or_container: tk.Misc,
    pad: int = 32,
    min_wrap: int = 200,
    *,
    container: tk.Misc | None = None,
    max_wrap: int | None = None,
    on_reflow: Callable[[], None] | None = None,
) -> Callable[[], None]:
    """Dynamically sync Label `wraplength` on `<Configure>` so Korean help text reflows cleanly.

    Accepts either:
    - A single `Label` / `Checkbutton` / `Radiobutton` widget (watches `container` or `widget.master`), OR
    - A container (`Frame`, `LabelFrame`, `ScrollableFrame`), automatically managing all descendant
      `Label` widgets inside it that already have a `wraplength` or multi-line/long text.

    Automatically triggers any enclosing `ScrollableFrame.refresh_scrollregion()` when wrapping changes.
    Returns a `refresh()` callable.
    """
    pad_px = max(0, int(pad))
    min_px = max(80, int(min_wrap))
    max_px = int(max_wrap) if max_wrap is not None else None

    is_container_mode = isinstance(label_or_container, ScrollableFrame) or (
        _widget_class(label_or_container) in _CONTAINER_CLASSES
    )

    if isinstance(label_or_container, ScrollableFrame):
        watch: tk.Misc = label_or_container.inner
        target_root: tk.Misc = label_or_container.inner
    elif is_container_mode:
        watch = container if container is not None else label_or_container
        target_root = label_or_container
    else:
        watch = container if container is not None else (getattr(label_or_container, "master", None) or label_or_container)
        target_root = label_or_container

    last_wraps: dict[str, int] = {}
    in_progress = [False]

    def _collect_targets() -> list[tk.Misc]:
        if not is_container_mode:
            return [target_root] if _widget_exists(target_root) else []
        out: list[tk.Misc] = []
        stack: list[Any] = [target_root]
        while stack:
            cur = stack.pop()
            if not _widget_exists(cur):
                continue
            for ch in cur.winfo_children():
                cls = _widget_class(ch)
                if cls in ("Label", "TLabel"):
                    try:
                        cur_wl = int(float(ch.cget("wraplength") or 0))
                    except Exception:
                        cur_wl = 0
                    try:
                        txt = str(ch.cget("text") or "")
                    except Exception:
                        txt = ""
                    if cur_wl > 0 or "\n" in txt or len(txt) >= 36:
                        out.append(ch)
                elif cls in _CONTAINER_CLASSES:
                    stack.append(ch)
        return out

    def _apply(event: Any = None) -> None:
        if in_progress[0] or not _widget_exists(watch):
            return
        in_progress[0] = True
        changed = False
        try:
            avail = int(getattr(event, "width", 0) or watch.winfo_width() or 0)
            if avail <= 1:
                return
            desired = max(min_px, avail - pad_px)
            if max_px is not None:
                desired = min(desired, max(min_px, max_px))
            for lbl in _collect_targets():
                if not _widget_exists(lbl):
                    continue
                # Account for nested card padding when in container mode
                w_target = desired
                if is_container_mode and lbl.master is not watch and _widget_exists(lbl.master):
                    mw = int(lbl.master.winfo_width() or 0)
                    if mw > 1:
                        w_target = max(min_px, min(desired, mw - pad_px))
                        if max_px is not None:
                            w_target = min(w_target, max(min_px, max_px))
                key = str(lbl)
                prev = last_wraps.get(key)
                if prev is not None and abs(prev - w_target) < 4:
                    continue
                try:
                    lbl.configure(wraplength=w_target)
                    last_wraps[key] = w_target
                    changed = True
                except Exception:
                    pass
        finally:
            in_progress[0] = False

        if changed:
            if on_reflow is not None:
                try:
                    on_reflow()
                except Exception:
                    pass
            sf = _find_parent_scrollable(watch)
            if sf is not None:
                sf.refresh_scrollregion(defer=True)

    idle_id: list[str | None] = [None]

    def _run_idle() -> None:
        idle_id[0] = None
        if _widget_exists(watch):
            _apply()

    def _on_watch_destroy(event: Any) -> None:
        if getattr(event, "widget", None) is watch and idle_id[0] is not None:
            try:
                watch.after_cancel(idle_id[0])
            except Exception:
                pass
            idle_id[0] = None

    try:
        watch.bind("<Configure>", _apply, add="+")
        watch.bind("<Destroy>", _on_watch_destroy, add="+")
    except Exception:
        pass
    try:
        idle_id[0] = watch.after_idle(_run_idle)
    except Exception:
        idle_id[0] = None
    return _apply


def make_pinned_footer(
    parent: tk.Misc,
    *,
    bg: str = "#F3F5F8",
    border_color: str = "#E2E8F0",
    padx: int = 14,
    pady: int | tuple[int, int] = (8, 10),
    separator: bool = True,
) -> tk.Frame:
    """Create a bottom action bar packed FIRST (`side="bottom"`) outside any scroll area.

    Pack buttons directly into the returned `tk.Frame` so Save / Apply / Cancel
    actions ALWAYS remain reachable regardless of scroll position or window height.
    """
    outer = tk.Frame(parent, bg=bg)
    outer.pack(side="bottom", fill="x")

    sep: tk.Frame | None = None
    if separator:
        sep = tk.Frame(outer, bg=border_color, height=1)
        sep.pack(side="top", fill="x")

    if isinstance(pady, tuple):
        py_top, py_bot = int(pady[0]), int(pady[1])
    else:
        py_top = py_bot = int(pady)

    footer = tk.Frame(outer, bg=bg)
    footer.pack(side="top", fill="x", padx=padx, pady=(py_top, py_bot))
    # Attach references for optional styling/inspection
    footer.wrapper = outer  # type: ignore[attr-defined]
    footer.separator = sep  # type: ignore[attr-defined]
    return footer


__all__ = [
    "ScrollableFrame",
    "attach_scrollable_mousewheel",
    "bind_mousewheel_router",
    "bind_responsive_wrap",
    "clamp_window_position",
    "fit_window_to_screen",
    "get_work_area",
    "make_pinned_footer",
]
