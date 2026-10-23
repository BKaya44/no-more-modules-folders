#!/usr/bin/env python3
r"""
node_modules Cleaner

Scans a folder for node_modules directories and shows the outermost ones in a
checkable list. Nested node_modules folders are not listed separately because
they are removed together with their parent (this also avoids double work and
"file not found" errors).

Deletion is Windows-friendly:
  * long paths (>260 chars) are handled via the \\?\ prefix
  * read-only attributes are cleared before retrying deletion
  * junctions / symlinks are removed as links, never followed
  * failures on individual folders are reported without stopping the rest
"""

import os
import queue
import shutil
import stat
import sys
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

IS_WINDOWS = os.name == "nt"
LONG_PREFIX = "\\\\?\\"
REPARSE_FLAG = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)


# --------------------------------------------------------------------------- #
#  Filesystem helpers
# --------------------------------------------------------------------------- #
def long_path(path):
    r"""Return an absolute path with the Windows \\?\ prefix (needed for >260)."""
    path = os.fspath(path)
    if not IS_WINDOWS or path.startswith(LONG_PREFIX):
        return path
    path = os.path.abspath(path)
    if path.startswith("\\\\"):
        return LONG_PREFIX + "UNC\\" + path[2:]
    return LONG_PREFIX + path


def is_reparse_point(st):
    """True for symlinks / junctions on Windows."""
    return bool(REPARSE_FLAG and (getattr(st, "st_file_attributes", 0) & REPARSE_FLAG))


def find_top_node_modules(root, on_error=None, cancel=None):
    """
    Return every node_modules folder that is NOT inside another node_modules.
    The walk never descends into a node_modules (or into junctions/symlinks),
    so nested ones are implicitly covered by their parent.
    """
    root = os.path.abspath(os.path.expanduser(root))
    root_stat = os.lstat(long_path(root))
    if stat.S_ISLNK(root_stat.st_mode) or is_reparse_point(root_stat):
        raise ValueError("Choose a real folder, not a symbolic link or junction.")
    if not stat.S_ISDIR(root_stat.st_mode):
        raise NotADirectoryError(root)
    root_name = os.path.basename(root)
    if root_name == "node_modules" or (
            IS_WINDOWS and root_name.casefold() == "node_modules"):
        return [root]
    found = []
    stack = [root]

    while stack:
        if cancel is not None and cancel.is_set():
            return []
        current = stack.pop()
        try:
            with os.scandir(long_path(current)) as it:
                entries = list(it)
        except OSError as exc:
            if on_error is not None:
                on_error(current, exc)
            continue

        for entry in entries:
            if cancel is not None and cancel.is_set():
                return []
            try:
                st = entry.stat(follow_symlinks=False)
            except OSError as exc:
                if on_error is not None:
                    on_error(os.path.join(current, entry.name), exc)
                continue

            is_dir = stat.S_ISDIR(st.st_mode)
            is_link = stat.S_ISLNK(st.st_mode) or is_reparse_point(st)
            if not (is_dir or is_link):
                continue

            if entry.name == "node_modules" or (
                    IS_WINDOWS and entry.name.casefold() == "node_modules"):
                found.append(os.path.join(current, entry.name))
                continue                      # do NOT descend -> parent covers children

            if is_link:
                continue                      # never follow junctions / symlinks

            stack.append(os.path.join(current, entry.name))

    found.sort(key=str.lower)
    return found


def delete_tree(path):
    r"""
    Recursively delete a file/folder.

    Junctions and symlinks are removed as links (their target is untouched).
    Read-only entries are cleared and retried. Paths are used with the
    Windows \\?\ prefix so deep node_modules trees do not hit MAX_PATH.
    """
    lp = long_path(path)
    try:
        st = os.lstat(lp)
    except FileNotFoundError:
        return

    # Remove links themselves without changing permissions on their targets.
    if is_reparse_point(st) or stat.S_ISLNK(st.st_mode):
        try:
            if IS_WINDOWS and stat.S_ISDIR(st.st_mode):
                os.rmdir(lp)
            else:
                os.unlink(lp)
        except FileNotFoundError:
            pass
        return

    def retry_readonly(function, filename, exc_info):
        """Retry Windows read-only entries; do not mask unrelated errors."""
        error = exc_info[1]
        if isinstance(error, FileNotFoundError):
            return
        if not IS_WINDOWS or not isinstance(error, PermissionError):
            raise error
        current = os.lstat(filename)
        if stat.S_ISLNK(current.st_mode) or is_reparse_point(current):
            raise error
        if current.st_mode & stat.S_IWRITE:
            raise error
        os.chmod(filename, current.st_mode | stat.S_IWRITE)
        function(filename)

    # shutil uses descriptor-based traversal where supported and handles
    # Windows junctions without deleting their contents.
    try:
        if stat.S_ISDIR(st.st_mode):
            shutil.rmtree(lp, onerror=retry_readonly)
        else:
            try:
                os.unlink(lp)
            except PermissionError:
                retry_readonly(os.unlink, lp, sys.exc_info())
    except FileNotFoundError:
        pass
    if os.path.lexists(lp):
        raise OSError(f"Deletion incomplete: {path}")


def validate_delete_path(path, root):
    """Reject stale results whose parent path now contains a link."""
    path = os.path.abspath(path)
    root = os.path.abspath(root)
    if os.path.commonpath((root, path)) != root:
        raise ValueError("The selected folder is outside the scanned root.")
    current = os.path.dirname(path)
    while True:
        st = os.lstat(long_path(current))
        if stat.S_ISLNK(st.st_mode) or is_reparse_point(st):
            raise ValueError("A parent folder is now a link. Scan again.")
        parent = os.path.dirname(current)
        if parent == current:
            break
        current = parent


def format_errors(errors):
    """Keep error dialogs readable for large scans."""
    lines = "\n".join(f"{path}\n    {error}" for path, error in errors[:12])
    if len(errors) > 12:
        lines += f"\n... and {len(errors) - 12} more."
    return lines


# --------------------------------------------------------------------------- #
#  GUI
# --------------------------------------------------------------------------- #
class App:
    def __init__(self, root):
        self.root = root
        self.queue = queue.Queue()
        self.items = []          # [(BooleanVar, absolute_path), ...]
        self.busy = False
        self.deleting = False
        self.scan_root = None
        self.cancel = threading.Event()

        root.title("node_modules Cleaner")
        root.geometry("880x620")
        root.minsize(640, 420)

        self.folder_var = tk.StringVar(value=os.getcwd())
        self.status_var = tk.StringVar(value="Pick a folder and click Scan.")

        self._build_ui()
        self.folder_var.trace_add("write", self._folder_changed)
        root.protocol("WM_DELETE_WINDOW", self.close)
        self.poll_id = root.after(100, self._poll)

    # ---------------- layout --------------------------------------------- #
    def _build_ui(self):
        top = ttk.Frame(self.root)
        top.pack(fill="x", padx=10, pady=(10, 6))
        ttk.Label(top, text="Root folder:").pack(side="left")
        self.folder_entry = ttk.Entry(top, textvariable=self.folder_var)
        self.folder_entry.pack(
            side="left", fill="x", expand=True, padx=6)
        self.browse_btn = ttk.Button(top, text="Browse...", command=self.browse)
        self.browse_btn.pack(side="left")
        self.scan_btn = ttk.Button(top, text="Scan", command=self.scan)
        self.scan_btn.pack(side="left", padx=(6, 0))

        list_frame = ttk.LabelFrame(
            self.root, text="node_modules folders (checked = will be deleted)")
        list_frame.pack(fill="both", expand=True, padx=10, pady=(0, 6))

        self.canvas = tk.Canvas(list_frame, highlightthickness=0, borderwidth=0)
        vsb = ttk.Scrollbar(list_frame, orient="vertical", command=self.canvas.yview)
        self.canvas.configure(yscrollcommand=vsb.set)
        vsb.pack(side="right", fill="y")
        self.canvas.pack(side="left", fill="both", expand=True, padx=2, pady=2)

        self.inner = ttk.Frame(self.canvas)
        self.inner_id = self.canvas.create_window((0, 0), window=self.inner, anchor="nw")
        self.inner.bind(
            "<Configure>",
            lambda e: self.canvas.configure(scrollregion=self.canvas.bbox("all")))
        self.canvas.bind(
            "<Configure>",
            lambda e: self.canvas.itemconfigure(self.inner_id, width=e.width))
        self.canvas.bind_all("<MouseWheel>", self._scroll)
        self.canvas.bind_all("<Button-4>", self._scroll)
        self.canvas.bind_all("<Button-5>", self._scroll)

        row = ttk.Frame(self.root)
        row.pack(fill="x", padx=10)
        self.select_btn = ttk.Button(
            row, text="Select All", command=lambda: self._check_all(True))
        self.select_btn.pack(side="left")
        self.deselect_btn = ttk.Button(
            row, text="Deselect All", command=lambda: self._check_all(False))
        self.deselect_btn.pack(side="left", padx=(6, 0))

        ttk.Label(self.root, textvariable=self.status_var, anchor="w").pack(
            fill="x", padx=12, pady=(6, 2))

        bottom = ttk.Frame(self.root)
        bottom.pack(fill="x", padx=10, pady=(0, 10))
        self.progress = ttk.Progressbar(bottom, mode="determinate")
        self.progress.pack(side="left", fill="x", expand=True, padx=(0, 8))
        self.delete_btn = ttk.Button(bottom, text="Delete Selected",
                                     command=self.delete_selected)
        self.delete_btn.pack(side="right")
        self.delete_btn.configure(state="disabled")

    # ---------------- helpers -------------------------------------------- #
    def _set_busy(self, busy):
        self.busy = busy
        state = "disabled" if busy else "normal"
        for btn in (self.scan_btn, self.browse_btn, self.folder_entry,
                    self.select_btn, self.deselect_btn):
            btn.configure(state=state)
        self.delete_btn.configure(
            state="normal" if not busy and self.items else "disabled")
        for child in self.inner.winfo_children():
            if isinstance(child, ttk.Checkbutton):
                child.configure(state=state)

    def _scroll(self, event):
        if event.num == 4:
            units = -1
        elif event.num == 5:
            units = 1
        elif event.delta:
            units = -1 if event.delta > 0 else 1
            if abs(event.delta) >= 120:
                units = -int(event.delta / 120)
        else:
            return
        self.canvas.yview_scroll(units, "units")

    def _folder_changed(self, *_args):
        if not self.busy:
            self.items = []
            self.scan_root = None
            self._rebuild_list()
            self.delete_btn.configure(state="disabled")
            self.status_var.set("Folder changed. Click Scan to refresh the list.")

    def close(self):
        if self.deleting:
            messagebox.showinfo(
                "Deletion in progress", "Wait for deletion to finish before closing.")
            return
        self.cancel.set()
        self.root.after_cancel(self.poll_id)
        self.root.destroy()

    def _check_all(self, value):
        if self.busy:
            return
        for var, _path in self.items:
            var.set(value)

    def _rebuild_list(self):
        for child in self.inner.winfo_children():
            child.destroy()
        if not self.items:
            ttk.Label(self.inner, text="No folders listed. Scan to refresh.").pack(
                anchor="w", padx=4, pady=4)
            return
        root_dir = self.scan_root or self.folder_var.get()
        for var, path in self.items:
            try:
                shown = os.path.relpath(path, root_dir)
            except ValueError:
                shown = path
            ttk.Checkbutton(self.inner, text=shown, variable=var).pack(
                anchor="w", fill="x")

    def _reset_progress(self):
        self.progress.stop()
        self.progress.configure(mode="determinate", value=0, maximum=100)

    # ---------------- actions -------------------------------------------- #
    def browse(self):
        folder = filedialog.askdirectory(
            initialdir=self.folder_var.get() or os.getcwd(),
            title="Select root folder")
        if folder:
            self.folder_var.set(os.path.normpath(folder))
            self.scan()

    def scan(self):
        if self.busy:
            return
        folder = self.folder_var.get().strip()
        if not folder or not os.path.isdir(long_path(os.path.expanduser(folder))):
            messagebox.showerror("Invalid folder",
                                 "Please choose a valid folder to scan.")
            return
        folder = os.path.abspath(os.path.expanduser(folder))
        self.folder_var.set(folder)
        self.scan_root = folder
        self.cancel.clear()
        self._set_busy(True)
        self.status_var.set("Scanning... this may take a moment.")
        self.progress.configure(mode="indeterminate")
        self.progress.start(12)
        threading.Thread(target=self._scan_worker, args=(folder,), daemon=True).start()

    def _scan_worker(self, folder):
        try:
            errors = []
            paths = find_top_node_modules(
                folder, lambda path, exc: errors.append((path, str(exc))), self.cancel)
            self.queue.put(("scan_done", paths, errors))
        except Exception as exc:                       # noqa: BLE001
            self.queue.put(("scan_error", str(exc)))

    def delete_selected(self):
        if self.busy:
            return
        selected = [p for var, p in self.items if var.get()]
        if not selected:
            messagebox.showinfo("Nothing selected",
                                "No folders are checked for deletion.")
            return
        if not messagebox.askyesno(
                "Confirm deletion",
                f"Permanently delete {len(selected)} node_modules folder(s)?\n\n"
                f"Scanned root: {self.scan_root}\n\n"
                "This bypasses the Recycle Bin or Trash and cannot be undone."):
            return

        self.deleting = True
        self._set_busy(True)
        self.progress.stop()
        self.progress.configure(mode="determinate",
                                maximum=len(selected), value=0)
        self.status_var.set("Deleting...")
        threading.Thread(target=self._delete_worker,
                         args=(selected,), daemon=False).start()

    def _delete_worker(self, paths):
        deleted, failed = [], []
        total = len(paths)
        for i, path in enumerate(paths, 1):
            try:
                validate_delete_path(path, self.scan_root)
                delete_tree(path)
                deleted.append(path)
            except Exception as exc:                   # noqa: BLE001
                failed.append((path, str(exc)))
            self.queue.put(("progress", i, total, path))
        self.queue.put(("delete_done", deleted, failed))

    # ---------------- thread -> UI pump ---------------------------------- #
    def _poll(self):
        try:
            while True:
                self._handle(self.queue.get_nowait())
        except queue.Empty:
            pass
        self.poll_id = self.root.after(100, self._poll)

    def _handle(self, msg):
        kind = msg[0]

        if kind == "scan_done":
            self._reset_progress()
            _, paths, errors = msg
            self.items = [(tk.BooleanVar(value=True), p) for p in paths]
            self._rebuild_list()
            self._set_busy(False)
            self.status_var.set(
                f"Found {len(paths)} node_modules folder(s)."
                + (f" Scan incomplete: {len(errors)} access error(s)." if errors else ""))
            if errors:
                messagebox.showwarning("Scan incomplete", format_errors(errors))

        elif kind == "scan_error":
            self._reset_progress()
            self.items = []
            self._rebuild_list()
            self._set_busy(False)
            self.status_var.set("Scan failed.")
            messagebox.showerror("Scan failed", msg[1])

        elif kind == "progress":
            _, i, total, path = msg
            self.progress.configure(maximum=total, value=i)
            self.status_var.set(f"Processed {i}/{total}: {path}")

        elif kind == "delete_done":
            _, deleted, failed = msg
            self.deleting = False
            self.progress.configure(value=0)

            gone = set(deleted)
            self.items = [(v, p) for (v, p) in self.items if p not in gone]
            self._rebuild_list()
            self._set_busy(False)

            self.status_var.set(
                f"Deleted {len(deleted)} folder(s)."
                + (f" {len(failed)} failed." if failed else ""))

            if failed:
                messagebox.showwarning("Some deletions failed", format_errors(failed))
            else:
                messagebox.showinfo(
                    "Done", f"Deleted {len(deleted)} node_modules folder(s).")


def main():
    if IS_WINDOWS:
        try:  # crisp UI on high-DPI screens
            from ctypes import windll
            windll.shcore.SetProcessDpiAwareness(1)
        except Exception:                              # noqa: BLE001
            pass

    root = tk.Tk()
    try:
        ttk.Style().theme_use("vista")
    except tk.TclError:
        pass
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
