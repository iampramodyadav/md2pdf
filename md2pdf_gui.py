#!/usr/bin/env python3
"""
MD2PDF - tiny drag & drop Markdown / HTML -> PDF app (Mermaid diagrams, offline).

* Drop .md or .html files and/or folders onto the window (folders are scanned recursively)
* Pick an output folder (empty = save the PDF next to each .md)
* Double-click a file (or press Preview) to read it rendered in your browser
* Uses the Microsoft Edge / Chrome already installed on the PC -> no browser bundled
* mermaid.min.js is bundled into the exe -> no internet needed

Needs md2pdf.py in the same folder. Build the exe with build_exe.bat.
"""
import os
import queue
import sys
import tempfile
import threading
import webbrowser
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

try:
    from tkinterdnd2 import DND_FILES, TkinterDnD

    BaseTk = TkinterDnD.Tk
    HAS_DND = True
except Exception:  # drag & drop optional; buttons still work
    BaseTk = tk.Tk
    HAS_DND = False

import md2pdf

MD_EXT = {".md", ".markdown"}
HTML_EXT = md2pdf.HTML_EXT


def resource_path(name: str) -> Path:
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
    return base / name


_mj = resource_path("mermaid.min.js")
MERMAID_SRC = _mj.as_uri() if _mj.exists() else md2pdf.MERMAID_CDN


def collect_files(paths, include_html_in_folders=False):
    """Dropped files are accepted if .md/.html; folders yield .md (and .html if asked)."""
    folder_ext = MD_EXT | (HTML_EXT if include_html_in_folders else set())
    found = []
    for p in map(Path, paths):
        if p.is_dir():
            found += sorted(q for q in p.rglob("*") if q.is_file() and q.suffix.lower() in folder_ext)
        elif p.is_file() and p.suffix.lower() in MD_EXT | HTML_EXT:
            found.append(p)
    return found


def make_html(md_path: Path, opts: dict):
    opts = {**opts, "stem": md_path.stem, "toc_title": "Contents"}
    doc, info = md2pdf.build_html(md_path.read_text(encoding="utf-8"), opts, MERMAID_SRC)
    base = md_path.parent.resolve().as_uri() + "/"  # so relative images still resolve
    doc = doc.replace("<head>", f'<head><base href="{base}">', 1)
    return doc, info


def write_temp_html(doc: str) -> Path:
    fd, name = tempfile.mkstemp(suffix=".html")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(doc)
    return Path(name)


class App(BaseTk):
    def __init__(self):
        super().__init__()
        self.title("MD → PDF")
        self.geometry("740x640")
        self.minsize(580, 500)
        self.files: list[Path] = []
        self.q: queue.Queue = queue.Queue()
        self.busy = False
        self.out_var = tk.StringVar()
        self.author_var = tk.StringVar()
        self.version_var = tk.StringVar()
        self.toc_var = tk.BooleanVar(value=False)
        self.hf_var = tk.BooleanVar(value=True)
        self.inc_html_var = tk.BooleanVar(value=False)
        self._build()
        self.after(100, self._poll)

    # ---------------------------------------------------------------- UI
    def _build(self):
        pad = {"padx": 8, "pady": 4}
        top = ttk.Frame(self)
        top.pack(fill="both", expand=True, **pad)

        hint = "Drop .md / .html files or folders here" if HAS_DND else "Use Add files / Add folder"
        ttk.Label(top, text=hint + "   (double-click a file to preview)").pack(anchor="w")

        lf = ttk.Frame(top)
        lf.pack(fill="both", expand=True)
        self.lb = tk.Listbox(lf, selectmode="extended", height=10, activestyle="none")
        sb = ttk.Scrollbar(lf, command=self.lb.yview)
        self.lb.config(yscrollcommand=sb.set)
        self.lb.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        self.lb.bind("<Double-Button-1>", lambda e: self.preview())
        if HAS_DND:
            for w in (self, self.lb):
                w.drop_target_register(DND_FILES)
                w.dnd_bind("<<Drop>>", self._on_drop)

        row = ttk.Frame(top)
        row.pack(fill="x", pady=(6, 0))
        for text, cmd in [
            ("Add files…", self.add_files),
            ("Add folder…", self.add_folder),
            ("Remove selected", self.remove_selected),
            ("Clear", self.clear),
        ]:
            ttk.Button(row, text=text, command=cmd).pack(side="left", padx=(0, 6))

        opt = ttk.LabelFrame(self, text="Options")
        opt.pack(fill="x", **pad)
        opt.columnconfigure(1, weight=1)
        ttk.Label(opt, text="Output folder").grid(row=0, column=0, sticky="w", padx=6, pady=3)
        ttk.Entry(opt, textvariable=self.out_var).grid(row=0, column=1, sticky="ew", padx=6)
        ttk.Button(opt, text="Browse…", command=self.pick_out).grid(row=0, column=2, padx=6)
        ttk.Label(opt, text="(empty = next to each .md)").grid(row=1, column=1, sticky="w", padx=6)
        ttk.Label(opt, text="Author").grid(row=2, column=0, sticky="w", padx=6, pady=3)
        ttk.Entry(opt, textvariable=self.author_var).grid(row=2, column=1, sticky="ew", padx=6)
        ttk.Label(opt, text="Version").grid(row=3, column=0, sticky="w", padx=6, pady=3)
        ttk.Entry(opt, textvariable=self.version_var, width=14).grid(row=3, column=1, sticky="w", padx=6)
        flags = ttk.Frame(opt)
        flags.grid(row=4, column=0, columnspan=3, sticky="w", padx=6, pady=4)
        ttk.Checkbutton(flags, text="Table of contents", variable=self.toc_var).pack(side="left", padx=(0, 14))
        ttk.Checkbutton(flags, text="Header / page numbers", variable=self.hf_var).pack(side="left", padx=(0, 14))
        ttk.Checkbutton(flags, text="Include .html when adding folders",
                        variable=self.inc_html_var).pack(side="left")

        act = ttk.Frame(self)
        act.pack(fill="x", **pad)
        self.btn_convert = ttk.Button(act, text="Convert all to PDF", command=self.convert)
        self.btn_convert.pack(side="left", padx=(0, 6))
        ttk.Button(act, text="Preview selected", command=self.preview).pack(side="left")
        self.bar = ttk.Progressbar(act, mode="determinate")
        self.bar.pack(side="left", fill="x", expand=True, padx=10)

        lg = ttk.Frame(self)
        lg.pack(fill="both", expand=True, padx=8, pady=(0, 8))
        self.log = tk.Text(lg, height=8, state="disabled", wrap="word")
        lsb = ttk.Scrollbar(lg, command=self.log.yview)
        self.log.config(yscrollcommand=lsb.set)
        self.log.pack(side="left", fill="both", expand=True)
        lsb.pack(side="right", fill="y")

    # ------------------------------------------------------- file handling
    def _on_drop(self, event):
        self.add_paths(self.tk.splitlist(event.data))

    def add_paths(self, paths):
        added = 0
        for p in collect_files(paths, self.inc_html_var.get()):
            if p not in self.files:
                self.files.append(p)
                self.lb.insert("end", f"{p.name}    —    {p.parent}")
                added += 1
        self._log(f"Added {added} file(s). Total: {len(self.files)}")

    def add_files(self):
        self.add_paths(filedialog.askopenfilenames(
            filetypes=[("Markdown / HTML", "*.md *.markdown *.html *.htm"), ("All files", "*.*")]))

    def add_folder(self):
        d = filedialog.askdirectory()
        if d:
            self.add_paths([d])

    def remove_selected(self):
        for i in reversed(self.lb.curselection()):
            self.lb.delete(i)
            del self.files[i]

    def clear(self):
        self.lb.delete(0, "end")
        self.files.clear()

    def pick_out(self):
        d = filedialog.askdirectory()
        if d:
            self.out_var.set(d)

    # ------------------------------------------------------------- actions
    def _opts(self):
        return {
            "author": self.author_var.get().strip(),
            "version": self.version_var.get().strip(),
            "toc": self.toc_var.get(),
        }

    def preview(self):
        sel = self.lb.curselection()
        if sel:
            path = self.files[sel[0]]
        elif len(self.files) == 1:
            path = self.files[0]
        else:
            messagebox.showinfo("Preview", "Select a file in the list first.")
            return
        try:
            if path.suffix.lower() in HTML_EXT:
                webbrowser.open(path.resolve().as_uri())
                return
            doc, _ = make_html(path, self._opts())
            webbrowser.open(write_temp_html(doc).as_uri())
        except Exception as e:
            messagebox.showerror("Preview failed", str(e))

    def convert(self):
        if self.busy:
            return
        if not self.files:
            messagebox.showinfo("Convert", "Add some .md files first.")
            return
        out_dir = Path(self.out_var.get().strip()) if self.out_var.get().strip() else None
        if out_dir:
            out_dir.mkdir(parents=True, exist_ok=True)
        self.busy = True
        self.btn_convert.state(["disabled"])
        self.bar.config(maximum=len(self.files), value=0)
        threading.Thread(
            target=self._worker,
            args=(list(self.files), out_dir, self._opts(), self.hf_var.get()),
            daemon=True,
        ).start()

    def _worker(self, files, out_dir, opts, use_hf):
        q = self.q
        ok = 0
        used = set()
        try:
            from playwright.sync_api import sync_playwright

            with sync_playwright() as p:
                browser = md2pdf.launch_browser(p)
                for n, f in enumerate(files, 1):
                    tmp = None
                    try:
                        target_dir = out_dir or f.parent
                        out = target_dir / f"{f.stem}.pdf"
                        i = 1
                        while out in used:  # same name from different folders / a.md + a.html
                            out = target_dir / f"{f.stem}_{i}.pdf"
                            i += 1
                        used.add(out)

                        is_html = f.suffix.lower() in HTML_EXT
                        if is_html:
                            url = f.resolve().as_uri()
                            info = md2pdf.html_info(
                                f.read_text(encoding="utf-8", errors="replace"), opts, f.stem)
                        else:
                            doc, info = make_html(f, opts)
                            tmp = write_temp_html(doc)
                            url = tmp.as_uri()
                        page = browser.new_page()
                        page.goto(url, wait_until="load")
                        if is_html:
                            try:
                                page.wait_for_load_state("networkidle", timeout=5_000)
                            except Exception:
                                pass
                        else:
                            try:
                                page.wait_for_function("window.mermaidDone === true", timeout=30_000)
                            except Exception:
                                q.put(("log", f"  ! diagrams may be incomplete in {f.name}"))
                        page.pdf(path=str(out), **md2pdf.pdf_options(info, use_hf))
                        page.close()
                        ok += 1
                        q.put(("log", f"✔ {f.name} → {out}"))
                    except Exception as e:
                        q.put(("log", f"✘ {f.name}: {e}"))
                    finally:
                        if tmp:
                            tmp.unlink(missing_ok=True)
                        q.put(("progress", n))
                browser.close()
        except Exception as e:
            q.put(("log", f"Could not start browser (need Microsoft Edge or Chrome installed): {e}"))
        q.put(("log", f"Done: {ok}/{len(files)} converted."))
        q.put(("done", None))

    # -------------------------------------------------------------- plumbing
    def _log(self, msg):
        self.log.config(state="normal")
        self.log.insert("end", msg + "\n")
        self.log.see("end")
        self.log.config(state="disabled")

    def _poll(self):
        try:
            while True:
                kind, val = self.q.get_nowait()
                if kind == "log":
                    self._log(val)
                elif kind == "progress":
                    self.bar.config(value=val)
                elif kind == "done":
                    self.busy = False
                    self.btn_convert.state(["!disabled"])
        except queue.Empty:
            pass
        self.after(100, self._poll)


if __name__ == "__main__":
    App().mainloop()
