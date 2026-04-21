import struct
import json
import os
import copy
import tkinter as tk
from tkinter import filedialog, messagebox, ttk, colorchooser
from PIL import Image, ImageTk, ImageDraw, ImageOps

try:
    from ttkthemes import ThemedTk
except ImportError:
    ThemedTk = None

# Global mapping for Fallout 2 Directions
DIR_NAMES = [
    "0 (Northeast)", 
    "1 (East)", 
    "2 (Southeast)", 
    "3 (Southwest)", 
    "4 (West)", 
    "5 (Northwest)"
]

# =====================================================================
# CORE CONVERTER & APPLICATION
# =====================================================================

class FalloutConverter:
    def __init__(self, palette_path=None):
        self.palette = self._load_palette(palette_path) if palette_path else None

    def _load_palette(self, path):
        try:
            with open(path, 'rb') as f:
                raw_data = f.read(768)
                return [c * 4 for c in raw_data]
        except: return None

    def _read_frm(self, path):
        with open(path, 'rb') as f:
            header = f.read(62)
            version, fps, action_frame, frames_per_dir = struct.unpack('>IHHH', header[:10])
            x_shifts, y_shifts = struct.unpack('>6h', header[10:22]), struct.unpack('>6h', header[22:34])
            dir_offsets = struct.unpack('>6I', header[34:58])
            
            images = []
            metadata = {
                "original_version": version, "fps": fps, "action_frame": action_frame, 
                "frames_per_dir": frames_per_dir, "x_shifts": list(x_shifts), "y_shifts": list(y_shifts), "frame_details": []
            }

            for d in range(6):
                if d > 0 and dir_offsets[d] == 0: continue
                f.seek(62 + dir_offsets[d])
                for i in range(frames_per_dir):
                    f_hdr = f.read(12)
                    if len(f_hdr) < 12: break
                    w, h, size, ox, oy = struct.unpack('>HHIhh', f_hdr)
                    pixels = f.read(size)
                    
                    if version == 5:
                        img = Image.frombytes('RGBA', (w, h), pixels)
                        b, g, r, a = img.split()
                        img = Image.merge('RGBA', (r, g, b, a))
                    else:
                        if not self.palette: raise Exception("8-bit FRM requires color.pal")
                        img_l = Image.frombytes('L', (w, h), pixels)
                        indices = list(img_l.getdata())
                        
                        img_rgba = img_l.copy()
                        img_rgba.putpalette(self.palette)
                        img_rgba = img_rgba.convert("RGBA")
                        rgba_data = list(img_rgba.getdata())
                        
                        new_data = [(rgba_data[j][0], rgba_data[j][1], rgba_data[j][2], 0) if indices[j] == 0 else (rgba_data[j][0], rgba_data[j][1], rgba_data[j][2], 255) for j in range(len(indices))]
                        img = Image.new("RGBA", (w, h))
                        img.putdata(new_data)
                        
                    images.append(img)
                    metadata["frame_details"].append({
                        "dir": d, "frame_index": i, "ox": ox, "oy": oy, 
                        "orig_ox": ox, "orig_oy": oy, "width": w, "height": h,
                        "orig_id": f"Frame {i}"
                    })
            return metadata, images

    def _write_frm(self, path, metadata, images, target_version):
        encoded_frames = []
        if target_version == 5:
            for img in images:
                r, g, b, a = img.split()
                encoded_frames.append(Image.merge('RGBA', (b, g, r, a)).tobytes())
        else:
            if not self.palette: raise Exception("8-bit FRM requires color.pal")
            pal_img = Image.new('P', (1, 1))
            pal_img.putpalette(self.palette)
            for img in images:
                img = img.convert('RGBA')
                r, g, b, alpha = img.split()
                
                rgb_frame = Image.merge('RGB', (r, g, b))
                indexed_frame = rgb_frame.quantize(palette=pal_img, dither=0)
                
                pixels = list(indexed_frame.getdata())
                alpha_data = list(alpha.getdata())
                
                final_pixels = bytearray()
                for i in range(len(pixels)):
                    if alpha_data[i] < 255:
                        final_pixels.append(0)
                    else:
                        idx = pixels[i]
                        if idx == 0: idx = 1
                        final_pixels.append(idx)
                        
                encoded_frames.append(bytes(final_pixels))

        with open(path, 'wb') as f:
            f.write(struct.pack('>IHHH', target_version, metadata["fps"], metadata["action_frame"], metadata["frames_per_dir"]))
            f.write(struct.pack('>6h', *metadata["x_shifts"]))
            f.write(struct.pack('>6h', *metadata["y_shifts"]))
            dir_offsets, current_ptr = [0] * 6, 0
            for d in range(6):
                if any(det["dir"] == d for det in metadata["frame_details"]):
                    dir_offsets[d] = current_ptr
                    for idx, det in enumerate(metadata["frame_details"]):
                        if det["dir"] == d: current_ptr += 12 + len(encoded_frames[idx])
            f.write(struct.pack('>6I', *dir_offsets))
            f.write(struct.pack('>I', current_ptr))
            for idx, det in enumerate(metadata["frame_details"]):
                data = encoded_frames[idx]
                f.write(struct.pack('>HHihh', det["width"], det["height"], len(data), det["ox"], det["oy"]))
                f.write(data)

    def frm_to_png(self, frm_path, output_name, add_padding=False):
        meta, images = self._read_frm(frm_path)
        max_w = max(det["width"] for det in meta["frame_details"])
        max_h = max(det["height"] for det in meta["frame_details"])
        padding = 30 if add_padding else 0
        sheet_w = (max_w * meta["frames_per_dir"]) + padding
        sheet_h = max_h * 6
        spritesheet = Image.new('RGBA', (sheet_w, sheet_h), (0,0,0,0))
        for idx, img in enumerate(images):
            det = meta["frame_details"][idx]
            spritesheet.paste(img, (det["frame_index"] * max_w, det["dir"] * max_h))
        meta["grid_cell_w"], meta["grid_cell_h"] = max_w, max_h
        meta["has_padding"] = add_padding
        spritesheet.save(f"{output_name}.png")
        with open(f"{output_name}.json", 'w') as j:
            json.dump(meta, j, indent=4)
        return True

# =====================================================================
# UI TOOLKIT & TABS
# =====================================================================

def create_scroll_panel(parent, width=280):
    """Creates a robust scrollable side panel for toolbars."""
    outer = tk.Frame(parent, width=width)
    outer.pack(side="left", fill="y")
    outer.pack_propagate(False)
    
    sb = ttk.Scrollbar(outer, orient="vertical")
    sb.pack(side="right", fill="y")
    
    canvas = tk.Canvas(outer, highlightthickness=0, yscrollcommand=sb.set)
    canvas.pack(side="left", fill="both", expand=True)
    sb.config(command=canvas.yview)
    
    inner = tk.Frame(canvas)
    cw = canvas.create_window((0,0), window=inner, anchor="nw")
    
    def configure_inner(event):
        canvas.configure(scrollregion=canvas.bbox("all"))
    inner.bind("<Configure>", configure_inner)
    
    def configure_canvas(event):
        canvas.itemconfig(cw, width=event.width)
    canvas.bind("<Configure>", configure_canvas)
    
    def _on_mousewheel(event):
        if event.num == 4 or event.delta > 0:
            canvas.yview_scroll(-1, "units")
        elif event.num == 5 or event.delta < 0:
            canvas.yview_scroll(1, "units")
            
    def bind_wheel(widget):
        widget.bind("<MouseWheel>", _on_mousewheel)
        widget.bind("<Button-4>", _on_mousewheel)
        widget.bind("<Button-5>", _on_mousewheel)
        for child in widget.winfo_children():
            bind_wheel(child)
            
    inner.bind("<Enter>", lambda e: bind_wheel(inner))
    return inner

class GridSlicerWindow(tk.Toplevel):
    def __init__(self, parent, builder):
        super().__init__(parent)
        self.title("Advanced Grid Slicer & Builder")
        self.geometry("1100x700")
        self.builder = builder
        self.app = builder.app
        self.img = builder.img.convert("RGBA")
        self.f_count = max(1, builder.f_var.get())
        self.d_count = max(1, builder.rows_var.get())
        self.pad = builder.chk_pad.get()
        self.work_w = self.img.width - 30 if self.pad else self.img.width

        self.y_lines = [int(i * self.img.height / self.d_count) for i in range(1, self.d_count)]
        self.x_lines = [[int(i * self.work_w / self.f_count) for i in range(1, self.f_count)] for _ in range(self.d_count)]

        self.scale = min(850 / self.img.width, 600 / self.img.height, 1.0)
        self.cam_x, self.cam_y = 0, 0
        self.tk_img = None

        top = tk.Frame(self)
        top.pack(fill="x", pady=10, padx=10)
        
        tk.Button(top, text="Auto-Detect Gaps", command=self.auto_detect, bg="#ddf" if not ThemedTk else None).pack(side="left", padx=5)
        tk.Button(top, text="Reset Grid", command=self.reset_grid).pack(side="left", padx=5)
        
        # Zoom controls
        tk.Label(top, text="Zoom:").pack(side="left", padx=(15,2))
        tk.Button(top, text=" - ", command=self.zoom_out, width=3).pack(side="left", padx=2)
        tk.Button(top, text=" + ", command=self.zoom_in, width=3).pack(side="left", padx=2)

        tk.Button(top, text="Build 32-bit FRM", command=lambda: self.build(5), bg="#cfc" if not ThemedTk else None).pack(side="right", padx=5)
        tk.Button(top, text="Build 8-bit FRM", command=lambda: self.build(4), bg="#cfc" if not ThemedTk else None).pack(side="right", padx=5)

        help_lbl = tk.Label(self, text="Middle-Click to Pan | Mousewheel to Zoom | Drag Red/Blue lines to adjust grid.", fg="#1AFF1A")
        help_lbl.pack()

        main_frame = tk.Frame(self)
        main_frame.pack(fill="both", expand=True)

        img_frame = tk.Frame(main_frame)
        img_frame.pack(side="left", fill="both", expand=True)

        self.canvas = tk.Canvas(img_frame, cursor="crosshair", bg="#111")
        self.canvas.pack(fill="both", expand=True, pady=10)

        # Mapping side
        map_frame = tk.Frame(main_frame, width=280)
        map_frame.pack(side="right", fill="y", padx=10, pady=10)
        map_frame.pack_propagate(False)

        tk.Label(map_frame, text="Row to Direction Mapping", font=("", 10, "bold"), fg="#1AFF1A").pack(pady=5)
        tk.Label(map_frame, text="Select which row corresponds\nto which Fallout direction.", justify="center").pack(pady=5)

        scroll_panel = create_scroll_panel(map_frame, width=270)

        self.row_mappings = []
        options = ["Ignore"] + DIR_NAMES
        for r in range(self.d_count):
            f = tk.Frame(scroll_panel)
            f.pack(fill="x", pady=2)
            tk.Label(f, text=f"Row {r+1}:", width=8, anchor="w", fg="#1AFF1A").pack(side="left")
            var = tk.StringVar(value=options[r+1] if r < 6 else "Ignore")
            cb = ttk.Combobox(f, textvariable=var, values=options, state="readonly", width=18)
            cb.pack(side="left", padx=5)
            self.row_mappings.append(var)

        self.active_line = None

        self.canvas.bind("<ButtonPress-1>", self.on_press)
        self.canvas.bind("<B1-Motion>", self.on_drag)
        self.canvas.bind("<ButtonRelease-1>", self.on_release)
        
        self.canvas.bind("<ButtonPress-2>", self.start_pan)
        self.canvas.bind("<B2-Motion>", self.pan)
        
        def _on_mousewheel(e):
            self.scale = max(0.1, min(10.0, self.scale * (1.1 if e.delta > 0 else 0.9)))
            self.redraw()
        self.canvas.bind("<MouseWheel>", _on_mousewheel)

        # Defer initial draw to allow canvas to update its winfo sizes
        self.after(100, self.redraw)

    def zoom_in(self):
        self.scale = max(0.1, min(10.0, self.scale * 1.1))
        self.redraw()
        
    def zoom_out(self):
        self.scale = max(0.1, min(10.0, self.scale * 0.9))
        self.redraw()

    def start_pan(self, e):
        self.lx, self.ly = e.x, e.y
        
    def pan(self, e):
        self.cam_x += e.x - self.lx
        self.cam_y += e.y - self.ly
        self.lx, self.ly = e.x, e.y
        self.redraw()

    def get_img_coords(self):
        cx = self.canvas.winfo_width() // 2 + self.cam_x
        cy = self.canvas.winfo_height() // 2 + self.cam_y
        cw = int(self.img.width * self.scale)
        ch = int(self.img.height * self.scale)
        return cx - cw // 2, cy - ch // 2, cw, ch

    def redraw(self):
        self.canvas.delete("all")
        ix, iy, cw, ch = self.get_img_coords()
        
        if cw > 0 and ch > 0:
            self.tk_img = ImageTk.PhotoImage(self.img.resize((cw, ch), Image.NEAREST))
            self.canvas.create_image(ix, iy, anchor="nw", image=self.tk_img)

        for r in range(self.d_count):
            y_top = iy + (self.y_lines[r-1] if r > 0 else 0) * self.scale
            y_bot = iy + (self.y_lines[r] if r < self.d_count - 1 else self.img.height) * self.scale
            for c, x in enumerate(self.x_lines[r]):
                sx = ix + x * self.scale
                self.canvas.create_line(sx, y_top, sx, y_bot, fill="#00aaff", width=2, dash=(4,4), tags=("line", f"v_{r}_{c}"))
                
        for i, y in enumerate(self.y_lines):
            sy = iy + y * self.scale
            self.canvas.create_line(ix, sy, ix + cw, sy, fill="#ff4444", width=3, tags=("line", f"h_{i}"))

    def on_press(self, e):
        ix, iy, cw, ch = self.get_img_coords()
        x, y = (e.x - ix) / self.scale, (e.y - iy) / self.scale
        threshold = 6 / self.scale

        for i, ly in enumerate(self.y_lines):
            if abs(y - ly) < threshold:
                self.active_line = ("h", i)
                return

        row = 0
        for i, ly in enumerate(self.y_lines):
            if y < ly: break
            row = i + 1

        if row < self.d_count:
            for c, lx in enumerate(self.x_lines[row]):
                if abs(x - lx) < threshold:
                    self.active_line = ("v", row, c)
                    return

    def on_drag(self, e):
        if not self.active_line: return
        ix, iy, cw, ch = self.get_img_coords()
        x, y = (e.x - ix) / self.scale, (e.y - iy) / self.scale

        if self.active_line[0] == "h":
            idx = self.active_line[1]
            min_y = self.y_lines[idx-1] + 5 if idx > 0 else 5
            max_y = self.y_lines[idx+1] - 5 if idx < len(self.y_lines)-1 else self.img.height - 5
            self.y_lines[idx] = max(min_y, min(max_y, int(y)))
        else:
            _, r, c = self.active_line
            min_x = self.x_lines[r][c-1] + 5 if c > 0 else 5
            max_x = self.x_lines[r][c+1] - 5 if c < len(self.x_lines[r])-1 else self.work_w - 5
            self.x_lines[r][c] = max(min_x, min(max_x, int(x)))

        self.redraw()

    def on_release(self, e):
        self.active_line = None

    def auto_detect(self):
        alpha = self.img.split()[3]
        w, h = alpha.size
        alpha_data = alpha.load()

        row_sums = [sum(alpha_data[x, y] for x in range(w)) for y in range(h)]
        for i in range(len(self.y_lines)):
            orig_y = self.y_lines[i]
            best_y = orig_y
            min_sum = row_sums[orig_y]
            search_range = int((h / self.d_count) * 0.4) 
            for dy in range(-search_range, search_range):
                ny = orig_y + dy
                if 0 <= ny < h and row_sums[ny] < min_sum:
                    min_sum = row_sums[ny]
                    best_y = ny
            self.y_lines[i] = best_y

        for r in range(self.d_count):
            y_top = self.y_lines[r-1] if r > 0 else 0
            y_bot = self.y_lines[r] if r < self.d_count - 1 else h
            col_sums = [sum(alpha_data[x, y] for y in range(y_top, y_bot)) for x in range(self.work_w)]
            for c in range(len(self.x_lines[r])):
                orig_x = self.x_lines[r][c]
                best_x = orig_x
                min_sum = col_sums[orig_x]
                search_range = int((self.work_w / self.f_count) * 0.4)
                for dx in range(-search_range, search_range):
                    nx = orig_x + dx
                    if 0 <= nx < self.work_w and col_sums[nx] < min_sum:
                        min_sum = col_sums[nx]
                        best_x = nx
                self.x_lines[r][c] = best_x
        self.redraw()

    def reset_grid(self):
        self.y_lines = [int(i * self.img.height / self.d_count) for i in range(1, self.d_count)]
        self.x_lines = [[int(i * self.work_w / self.f_count) for i in range(1, self.f_count)] for _ in range(self.d_count)]
        self.redraw()

    def build(self, ver):
        out = filedialog.asksaveasfilename(defaultextension=".frm", filetypes=[("FRM files", "*.frm *.FRM")])
        if not out: return

        meta = {"fps": self.builder.fps_var.get(), "action_frame": 0, "frames_per_dir": self.f_count, "x_shifts": [0]*6, "y_shifts": [0]*6, "frame_details": []}
        imgs = []

        for r in range(self.d_count):
            mapping = self.row_mappings[r].get()
            if mapping == "Ignore":
                continue
                
            d = int(mapping.split()[0])
            
            y_top = self.y_lines[r-1] if r > 0 else 0
            y_bot = self.y_lines[r] if r < self.d_count - 1 else self.img.height
            
            for f in range(self.f_count):
                x_left = self.x_lines[r][f-1] if f > 0 else 0
                x_right = self.x_lines[r][f] if f < self.f_count - 1 else self.work_w

                crop_w = x_right - x_left
                crop_h = y_bot - y_top
                
                imgs.append(self.img.crop((x_left, y_top, x_right, y_bot)))
                meta["frame_details"].append({"dir": d, "frame_index": f, "ox": 0, "oy": 0, "width": crop_w, "height": crop_h, "orig_id": f"Frame {f}"})
        
        combined = list(zip(meta["frame_details"], imgs))
        combined.sort(key=lambda x: (x[0]["dir"], x[0]["frame_index"]))
        meta["frame_details"] = [x[0] for x in combined]
        imgs = [x[1] for x in combined]

        self.app.converter._write_frm(out, meta, imgs, ver)
        
        if messagebox.askyesno("Success", f"FRM generated successfully!\n\nWould you like to instantly load it into the Editor workspace?"):
            self.app.load_workspace_from_path(out)
            
        self.destroy()
        self.builder.destroy()

class RawPNGBuilder(tk.Toplevel):
    def __init__(self, parent, png_path, app):
        super().__init__(parent)
        self.title("Raw Builder Configuration")
        self.geometry("350x400")
        self.app = app
        self.png_path = png_path
        self.img = Image.open(png_path).convert("RGBA")
        
        tk.Label(self, text=f"File: {os.path.basename(png_path)}", fg="#1AFF1A").pack(pady=10)
        
        tk.Label(self, text="Frames Per Row:").pack()
        self.f_var = tk.IntVar(value=1)
        tk.Entry(self, textvariable=self.f_var, justify="center").pack()
        
        tk.Label(self, text="Number of Rows on Spritesheet:").pack(pady=(10,0))
        self.rows_var = tk.IntVar(value=6)
        tk.Entry(self, textvariable=self.rows_var, justify="center").pack()

        tk.Label(self, text="Animation FPS:").pack(pady=(10,0))
        self.fps_var = tk.IntVar(value=10)
        tk.Entry(self, textvariable=self.fps_var, justify="center").pack()

        self.chk_pad = tk.BooleanVar(value=False)
        tk.Checkbutton(self, text="Ignore rightmost 30px (Notes Stripe)", variable=self.chk_pad).pack(pady=15)
        
        tk.Button(self, text="Open Advanced Slicer >>>", command=self.open_slicer, font=("", 10, "bold")).pack(pady=10, fill="x", padx=30)

    def open_slicer(self):
        GridSlicerWindow(self, self)

class EditorTab(ttk.Frame):
    def __init__(self, parent, app):
        super().__init__(parent)
        self.app = app
        self._syncing = False
        self.current_dir = 0
        self.cam_x, self.cam_y, self.zoom = 0, 0, 1.0
        self.show_hex = tk.BooleanVar(value=True)
        self.show_bg = tk.BooleanVar(value=True)
        self.show_ghost = tk.BooleanVar(value=True)
        self.ghost_mode = tk.StringVar(value="prev")
        self.ghost_alpha = tk.DoubleVar(value=0.5)
        self.is_playing = False
        self.bg_image = None
        self.anim_id = None
        self.ext_ghost_meta = None
        self.ext_ghost_imgs = []
        
        lp = create_scroll_panel(self, 280)
        
        tk.Button(lp, text="Load FRM to Workspace", command=self.app.load_workspace).pack(fill="x", pady=2)
        bg_btn = tk.Frame(lp)
        bg_btn.pack(fill="x", pady=2)
        tk.Button(bg_btn, text="Load Background", command=self.load_bg).pack(side="left", expand=True, fill="x")
        tk.Checkbutton(bg_btn, text="Show BG", variable=self.show_bg, command=self.redraw).pack(side="left")
        
        tk.Label(lp, text="Direction:").pack(pady=(5,0))
        self.dir_v = tk.StringVar(value=DIR_NAMES[0])
        cb_dir = ttk.Combobox(lp, textvariable=self.dir_v, values=DIR_NAMES, state="readonly")
        cb_dir.pack(fill="x")
        cb_dir.bind("<<ComboboxSelected>>", lambda e: self.change_dir())
        
        list_f = tk.Frame(lp)
        list_f.pack(fill="x", pady=2)
        sb = ttk.Scrollbar(list_f, orient="vertical")
        self.listbox = tk.Listbox(list_f, height=8, exportselection=False, yscrollcommand=sb.set)
        sb.config(command=self.listbox.yview)
        sb.pack(side="right", fill="y")
        self.listbox.pack(side="left", fill="both", expand=True)
        self.listbox.bind('<<ListboxSelect>>', self.on_select)
        
        zoom_f = tk.Frame(lp)
        zoom_f.pack(fill="x", pady=2)
        tk.Label(zoom_f, text="Zoom Level:").pack(side="left")
        tk.Button(zoom_f, text=" - ", command=self.zoom_out, width=3).pack(side="left", padx=2)
        tk.Button(zoom_f, text=" + ", command=self.zoom_in, width=3).pack(side="left", padx=2)
        
        f_btns = tk.Frame(lp)
        f_btns.pack(fill="x", pady=2)
        tk.Button(f_btns, text="Up", command=lambda: self.move_frame(-1)).pack(side="left", expand=True, fill="x")
        tk.Button(f_btns, text="Dn", command=lambda: self.move_frame(1)).pack(side="left", expand=True, fill="x")
        tk.Button(f_btns, text="Dup", command=self.duplicate_frame).pack(side="left", expand=True, fill="x")
        tk.Button(f_btns, text="Del", command=self.delete_frame).pack(side="left", expand=True, fill="x")

        f_io = tk.Frame(lp)
        f_io.pack(fill="x", pady=2)
        tk.Button(f_io, text="Import Frame", command=self.import_frame).pack(side="left", expand=True, fill="x")
        tk.Button(f_io, text="Export Frame", command=self.export_frame).pack(side="left", expand=True, fill="x")

        fps_f = tk.Frame(lp)
        fps_f.pack(fill="x", pady=4)
        tk.Label(fps_f, text="Anim FPS:").pack(side="left", padx=(0,5))
        self.fps_var = tk.IntVar(value=10)
        tk.Spinbox(fps_f, from_=1, to=120, textvariable=self.fps_var, command=self.update_fps, width=5).pack(side="left")
        
        self.btn_play = tk.Button(lp, text="Play Animation", command=self.toggle_play)
        self.btn_play.pack(fill="x", pady=2)
        
        off_f = tk.LabelFrame(lp, text="Offsets (ox, oy)")
        off_f.pack(fill="x", pady=2)
        off_top = tk.Frame(off_f)
        off_top.pack(fill="x")
        self.ox_v, self.oy_v = tk.IntVar(), tk.IntVar()
        tk.Entry(off_top, textvariable=self.ox_v, width=5).pack(side="left", padx=2)
        tk.Entry(off_top, textvariable=self.oy_v, width=5).pack(side="left", padx=2)
        tk.Button(off_top, text="Set", command=self.set_offs).pack(side="left")
        
        off_pad = tk.Frame(off_f)
        off_pad.pack(pady=2)
        tk.Button(off_pad, text="U", command=lambda: self.nudge_offset(0, -1), width=2).grid(row=0, column=1)
        tk.Button(off_pad, text="L", command=lambda: self.nudge_offset(-1, 0), width=2).grid(row=1, column=0)
        tk.Button(off_pad, text="D", command=lambda: self.nudge_offset(0, 1), width=2).grid(row=1, column=1)
        tk.Button(off_pad, text="R", command=lambda: self.nudge_offset(1, 0), width=2).grid(row=1, column=2)

        align_f = tk.Frame(lp)
        align_f.pack(fill="x", pady=1)
        tk.Button(align_f, text="Align to Hex", command=self.auto_align).pack(side="left", expand=True, fill="x", padx=(0,1))
        tk.Button(align_f, text="Align to Ghost", command=self.align_to_ghost).pack(side="left", expand=True, fill="x", padx=(1,0))
        
        res_f = tk.Frame(lp)
        res_f.pack(fill="x", pady=1)
        tk.Button(res_f, text="Reset Offs", command=self.reset_offs).pack(side="left", expand=True, fill="x", padx=(0,2))
        tk.Button(res_f, text="Reset Workspace", command=self.reset_workspace).pack(side="left", expand=True, fill="x")
        
        ghost_f = tk.LabelFrame(lp, text="Ghosting Overlay")
        ghost_f.pack(fill="x", pady=2)
        tk.Checkbutton(ghost_f, text="Enabled", variable=self.show_ghost, command=self.redraw).pack(anchor="w")
        tk.Radiobutton(ghost_f, text="Previous Frame", variable=self.ghost_mode, value="prev", command=self.redraw).pack(anchor="w")
        tk.Radiobutton(ghost_f, text="Frame 0 (Idle)", variable=self.ghost_mode, value="frame0", command=self.redraw).pack(anchor="w")
        tk.Radiobutton(ghost_f, text="External FRM", variable=self.ghost_mode, value="ext", command=self.redraw).pack(anchor="w")
        tk.Button(ghost_f, text="Load External Ghost...", command=self.load_ext_ghost).pack(fill="x", padx=5, pady=2)
        tk.Scale(ghost_f, from_=0.1, to=0.9, resolution=0.1, orient="horizontal", variable=self.ghost_alpha, command=lambda x: self.redraw()).pack(fill="x")
        
        tk.Checkbutton(lp, text="Show Hex Grid", variable=self.show_hex, command=self.redraw).pack(anchor="w", pady=5)
        
        tk.Button(lp, text="Save Workspace (8-bit)", command=lambda: self.app.save_workspace(4)).pack(fill="x")
        tk.Button(lp, text="Save Workspace (32-bit)", command=lambda: self.app.save_workspace(5)).pack(fill="x", pady=2)

        self.canvas = tk.Canvas(self, bg="#222")
        self.canvas.pack(side="right", fill="both", expand=True)
        self.canvas.bind("<B2-Motion>", self.pan)
        self.canvas.bind("<ButtonPress-2>", self.start_pan)
        self.canvas.bind("<MouseWheel>", self.do_zoom)
        self.tk_imgs = {}

    def zoom_in(self):
        self.zoom = max(0.5, min(15.0, self.zoom * 1.1))
        self.redraw()
        
    def zoom_out(self):
        self.zoom = max(0.5, min(15.0, self.zoom * 0.9))
        self.redraw()

    def load_bg(self):
        f = filedialog.askopenfilename(filetypes=[("PNG files", "*.png *.PNG")])
        if f:
            self.bg_image = Image.open(f).convert("RGBA")
            self.redraw()

    def load_ext_ghost(self):
        path = filedialog.askopenfilename(filetypes=[("FRM files", "*.frm *.FRM")])
        if path:
            try:
                self.ext_ghost_meta, self.ext_ghost_imgs = self.app.converter._read_frm(path)
                self.ghost_mode.set("ext")
                self.redraw()
            except Exception as e:
                messagebox.showerror("Error", f"Failed to load external ghost FRM:\n{e}")

    def update_fps(self):
        if self.app.wk_meta:
            try: self.app.wk_meta["fps"] = int(self.fps_var.get())
            except ValueError: pass

    def change_dir(self):
        self.current_dir = int(self.dir_v.get().split()[0])
        self.refresh_listbox(sync=True)

    def refresh_listbox(self, sync=False):
        old_sel = self.listbox.curselection()
        self.listbox.delete(0, tk.END)
        if not self.app.wk_meta: return
        
        self.fps_var.set(self.app.wk_meta.get("fps", 10))
        
        for i, d in enumerate(self.app.wk_meta["frame_details"]):
            if d["dir"] == self.current_dir:
                orig = d.get('orig_id', 'New')
                self.listbox.insert(tk.END, f"Frame {d['frame_index']} [Orig: {orig}]")
        
        if old_sel and old_sel[0] < self.listbox.size():
            self.listbox.selection_set(old_sel[0])
        elif self.listbox.size() > 0:
            self.listbox.selection_set(0)
            
        if sync:
            self.on_select(None)
        else:
            self.redraw()

    def get_idx(self):
        sel = self.listbox.curselection()
        if not sel: return None
        count = 0
        for i, d in enumerate(self.app.wk_meta["frame_details"]):
            if d["dir"] == self.current_dir:
                if count == sel[0]: return i
                count += 1
        return None

    def get_idx_for_frame(self, frame_index):
        for i, d in enumerate(self.app.wk_meta["frame_details"]):
            if d["dir"] == self.current_dir and d["frame_index"] == frame_index:
                return i
        return None

    def on_select(self, e):
        if self._syncing: return
        self._syncing = True
        idx = self.get_idx()
        if idx is not None:
            self.ox_v.set(self.app.wk_meta["frame_details"][idx]["ox"])
            self.oy_v.set(self.app.wk_meta["frame_details"][idx]["oy"])
            self.redraw()
            sel = self.listbox.curselection()
            if sel:
                self.app.t_pt.sync_selection(self.current_dir, sel[0])
        self._syncing = False

    def sync_selection(self, d_val, sel_idx):
        self._syncing = True
        self.dir_v.set(DIR_NAMES[d_val])
        self.current_dir = d_val
        self.listbox.delete(0, tk.END)
        if not self.app.wk_meta:
            self._syncing = False
            return
        for i, d in enumerate(self.app.wk_meta["frame_details"]):
            if d["dir"] == self.current_dir:
                orig = d.get('orig_id', 'New')
                self.listbox.insert(tk.END, f"Frame {d['frame_index']} [Orig: {orig}]")
        if sel_idx < self.listbox.size():
            self.listbox.selection_set(sel_idx)
            idx = self.get_idx()
            if idx is not None:
                self.ox_v.set(self.app.wk_meta["frame_details"][idx]["ox"])
                self.oy_v.set(self.app.wk_meta["frame_details"][idx]["oy"])
            self.redraw()
        self._syncing = False

    def move_frame(self, direction):
        idx = self.get_idx()
        if idx is None: return
        list_idx = self.listbox.curselection()[0]
        new_list_idx = list_idx + direction
        if new_list_idx < 0 or new_list_idx >= self.listbox.size(): return
        swap_idx = None
        count = 0
        for i, det in enumerate(self.app.wk_meta["frame_details"]):
            if det["dir"] == self.current_dir:
                if count == new_list_idx:
                    swap_idx = i
                    break
                count += 1
        if swap_idx is not None:
            self.app.wk_meta["frame_details"][idx], self.app.wk_meta["frame_details"][swap_idx] = self.app.wk_meta["frame_details"][swap_idx], self.app.wk_meta["frame_details"][idx]
            self.app.wk_imgs[idx], self.app.wk_imgs[swap_idx] = self.app.wk_imgs[swap_idx], self.app.wk_imgs[idx]
            self._reindex_frames()
            self.app.notify_workspace_update()
            self.listbox.selection_set(new_list_idx)
            self.on_select(None)

    def duplicate_frame(self):
        idx = self.get_idx()
        if idx is None: return
        new_det = self.app.wk_meta["frame_details"][idx].copy()
        new_det["orig_id"] = new_det.get("orig_id", "New") + " (Copy)"
        self.app.wk_meta["frame_details"].insert(idx + 1, new_det)
        self.app.wk_imgs.insert(idx + 1, self.app.wk_imgs[idx].copy())
        self._reindex_frames()
        self.app.notify_workspace_update()

    def delete_frame(self):
        idx = self.get_idx()
        if idx is None: return
        if self.listbox.size() <= 1:
            messagebox.showwarning("Warning", "Cannot delete last frame in direction.")
            return
        del self.app.wk_meta["frame_details"][idx]
        del self.app.wk_imgs[idx]
        self._reindex_frames()
        self.app.notify_workspace_update()
        self.listbox.selection_set(0)
        self.on_select(None)

    def import_frame(self):
        if not self.app.wk_meta: return
        f = filedialog.askopenfilename(filetypes=[("Images", "*.png *.PNG *.bmp *.BMP *.jpg *.JPG *.jpeg *.JPEG")])
        if not f: return
        img = Image.open(f).convert("RGBA")
        new_det = {"dir": self.current_dir, "frame_index": 0, "ox": 0, "oy": 0, "orig_ox": 0, "orig_oy": 0, "width": img.width, "height": img.height, "orig_id": "Imported"}
        self.app.wk_meta["frame_details"].append(new_det)
        self.app.wk_imgs.append(img)
        self._reindex_frames()
        self.app.notify_workspace_update()

    def export_frame(self):
        idx = self.get_idx()
        if idx is None: return
        f = filedialog.asksaveasfilename(defaultextension=".png", filetypes=[("PNG files", "*.png"), ("BMP files", "*.bmp"), ("JPEG files", "*.jpg *.jpeg")])
        if f:
            img_to_save = self.app.wk_imgs[idx]
            if f.lower().endswith(('.jpg', '.jpeg')):
                img_to_save = img_to_save.convert("RGB")
            img_to_save.save(f)
            messagebox.showinfo("Done", "Frame exported successfully.")

    def _reindex_frames(self):
        max_frames = 0
        for d in range(6):
            count = 0
            for det in self.app.wk_meta["frame_details"]:
                if det["dir"] == d:
                    det["frame_index"] = count
                    count += 1
            max_frames = max(max_frames, count)
        self.app.wk_meta["frames_per_dir"] = max_frames

    def set_offs(self):
        idx = self.get_idx()
        if idx is not None:
            self.app.wk_meta["frame_details"][idx]["ox"] = self.ox_v.get()
            self.app.wk_meta["frame_details"][idx]["oy"] = self.oy_v.get()
            self.redraw()

    def nudge_offset(self, dx, dy):
        idx = self.get_idx()
        if idx is not None:
            self.app.wk_meta["frame_details"][idx]["ox"] += dx
            self.app.wk_meta["frame_details"][idx]["oy"] += dy
            self.ox_v.set(self.app.wk_meta["frame_details"][idx]["ox"])
            self.oy_v.set(self.app.wk_meta["frame_details"][idx]["oy"])
            self.redraw()

    def reset_offs(self):
        idx = self.get_idx()
        if idx is not None:
            orig_ox = self.app.wk_meta["frame_details"][idx].get("orig_ox", 0)
            orig_oy = self.app.wk_meta["frame_details"][idx].get("orig_oy", 0)
            self.app.wk_meta["frame_details"][idx]["ox"] = orig_ox
            self.app.wk_meta["frame_details"][idx]["oy"] = orig_oy
            self.on_select(None)
            
    def reset_workspace(self):
        if hasattr(self.app, 'backup_meta') and self.app.backup_meta:
            self.app.wk_meta = copy.deepcopy(self.app.backup_meta)
            self.app.wk_imgs = [img.copy() for img in self.app.backup_imgs]
            self.app.notify_workspace_update()
            messagebox.showinfo("Workspace Reset", "Successfully restored workspace to original loaded state.")
        else:
            messagebox.showinfo("Info", "No original state backed up to restore.")

    def auto_align(self):
        idx = self.get_idx()
        if idx is not None:
            img = self.app.wk_imgs[idx]
            bbox = img.getbbox()
            if bbox:
                content_cx = bbox[0] + (bbox[2] - bbox[0]) / 2.0
                content_by = bbox[3]
                self.app.wk_meta["frame_details"][idx]["ox"] = int((img.width / 2.0) - content_cx)
                self.app.wk_meta["frame_details"][idx]["oy"] = int(img.height - content_by)
            self.on_select(None)

    def align_to_ghost(self):
        if not self.app.wk_meta: return
        idx = self.get_idx()
        if idx is None: return
        
        gx, gy = None, None
        if self.ghost_mode.get() == "prev" and self.listbox.curselection() and self.listbox.curselection()[0] > 0:
            target_f = self.listbox.curselection()[0] - 1
            g_idx = self.get_idx_for_frame(target_f)
            if g_idx is not None:
                gx, gy = self.get_accum_offsets(self.app.wk_meta, self.current_dir, target_f)
        elif self.ghost_mode.get() == "frame0":
            g_idx = self.get_idx_for_frame(0)
            if g_idx is not None:
                gx, gy = self.get_accum_offsets(self.app.wk_meta, self.current_dir, 0)
        elif self.ghost_mode.get() == "ext" and self.ext_ghost_meta:
            curr_idx = self.listbox.curselection()[0] if self.listbox.curselection() else 0
            ext_frames = [(i, m) for i, m in enumerate(self.ext_ghost_meta["frame_details"]) if m["dir"] == self.current_dir]
            if ext_frames:
                target_f = min(curr_idx, len(ext_frames) - 1)
                _, g_det = ext_frames[target_f]
                gx, gy = self.get_accum_offsets(self.ext_ghost_meta, self.current_dir, g_det["frame_index"])
        
        if gx is not None and gy is not None:
            cur_ax, cur_ay = self.get_accum_offsets(self.app.wk_meta, self.current_dir, self.app.wk_meta["frame_details"][idx]["frame_index"])
            dx = gx - cur_ax
            dy = gy - cur_ay
            self.app.wk_meta["frame_details"][idx]["ox"] += int(dx)
            self.app.wk_meta["frame_details"][idx]["oy"] += int(dy)
            self.on_select(None)
        else:
            messagebox.showinfo("Align to Ghost", "No active or valid Ghost frame to align to.")

    def get_accum_offsets(self, meta, d_val, target_idx):
        ax = meta["x_shifts"][d_val]
        ay = meta["y_shifts"][d_val]
        for d in meta["frame_details"]:
            if d["dir"] == d_val:
                ax += d["ox"]
                ay += d["oy"]
                if d["frame_index"] == target_idx: break
        return ax, ay

    def redraw(self):
        if not self.app.wk_meta: return
        self.canvas.delete("all")
        self.tk_imgs.clear()
        cx, cy = self.canvas.winfo_width()//2 + self.cam_x, self.canvas.winfo_height()//2 + self.cam_y
        
        if self.bg_image and self.show_bg.get():
            w, h = int(self.bg_image.width * self.zoom), int(self.bg_image.height * self.zoom)
            self.tk_imgs["bg"] = ImageTk.PhotoImage(self.bg_image.resize((w, h), Image.NEAREST))
            self.canvas.create_image(cx, cy, anchor="center", image=self.tk_imgs["bg"])
        
        if self.show_hex.get():
            w, h = 32 * self.zoom, 16 * self.zoom
            for r in range(-3, 4):
                for c in range(-3, 4):
                    px, py = cx + (c * w) + (r%2)*(w/2), cy + (r * h * 0.75)
                    pts = [px, py-h/2, px+w/2, py-h/4, px+w/2, py+h/4, px, py+h/2, px-w/2, py+h/4, px-w/2, py-h/4]
                    self.canvas.create_polygon(pts, outline="#444", fill="")

        idx = self.get_idx()
        if idx is None: return
        
        det = self.app.wk_meta["frame_details"][idx]
        accum_x, accum_y = self.get_accum_offsets(self.app.wk_meta, self.current_dir, det["frame_index"])

        if self.show_ghost.get():
            g_img = None
            gx, gy = 0, 0
            
            if self.ghost_mode.get() == "prev" and self.listbox.curselection() and self.listbox.curselection()[0] > 0:
                count = 0
                target_f = self.listbox.curselection()[0] - 1
                for i, d in enumerate(self.app.wk_meta["frame_details"]):
                    if d["dir"] == self.current_dir:
                        if count == target_f:
                            gx, gy = self.get_accum_offsets(self.app.wk_meta, self.current_dir, d["frame_index"])
                            g_img = self.app.wk_imgs[i].convert("RGBA")
                            break
                        count += 1
            elif self.ghost_mode.get() == "frame0":
                for i, d in enumerate(self.app.wk_meta["frame_details"]):
                    if d["dir"] == self.current_dir:
                        gx, gy = self.get_accum_offsets(self.app.wk_meta, self.current_dir, d["frame_index"])
                        g_img = self.app.wk_imgs[i].convert("RGBA")
                        break
            elif self.ghost_mode.get() == "ext" and self.ext_ghost_meta:
                curr_idx = self.listbox.curselection()[0] if self.listbox.curselection() else 0
                ext_frames = [(i, m) for i, m in enumerate(self.ext_ghost_meta["frame_details"]) if m["dir"] == self.current_dir]
                if ext_frames:
                    target_f = min(curr_idx, len(ext_frames) - 1)
                    g_idx, g_det = ext_frames[target_f]
                    gx, gy = self.get_accum_offsets(self.ext_ghost_meta, self.current_dir, g_det["frame_index"])
                    g_img = self.ext_ghost_imgs[g_idx].convert("RGBA")
            
            if g_img:
                is_same_internal = self.ghost_mode.get() in ["prev", "frame0"] and g_img is self.app.wk_imgs[idx]
                
                if not is_same_internal:
                    r, g, b, a = g_img.split()
                    r = r.point(lambda p: int(p * 0.4))
                    g = g.point(lambda p: int(p * 0.8))
                    b = b.point(lambda p: int(min(255, p * 1.5)))
                    a = a.point(lambda p: int(p * self.ghost_alpha.get()))
                    g_img = Image.merge("RGBA", (r, g, b, a))
                    
                    nw_g, nh_g = int(g_img.width*self.zoom), int(g_img.height*self.zoom)
                    self.tk_imgs["ghost"] = ImageTk.PhotoImage(g_img.resize((nw_g, nh_g), Image.NEAREST))
                    
                    gx_pos = cx + gx * self.zoom - (nw_g / 2.0)
                    gy_pos = cy + gy * self.zoom - nh_g
                    self.canvas.create_image(gx_pos, gy_pos, anchor="nw", image=self.tk_imgs["ghost"])

        img = self.app.wk_imgs[idx]
        nw, nh = int(img.width*self.zoom), int(img.height*self.zoom)
        self.tk_imgs["cur"] = ImageTk.PhotoImage(img.resize((nw, nh), Image.NEAREST))
        
        pos_x = cx + accum_x * self.zoom - (nw / 2.0)
        pos_y = cy + accum_y * self.zoom - nh
        self.canvas.create_image(pos_x, pos_y, anchor="nw", image=self.tk_imgs["cur"])
        
        self.canvas.create_line(cx-5, cy, cx+5, cy, fill="red")
        self.canvas.create_line(cx, cy-5, cx, cy+5, fill="red")

    def toggle_play(self):
        self.is_playing = not self.is_playing
        self.btn_play.config(text="Pause" if self.is_playing else "Play Animation")
        if self.is_playing: self.animate()
        else:
            if self.anim_id:
                self.after_cancel(self.anim_id)
                self.anim_id = None

    def animate(self):
        if not self.is_playing: return
        if self.listbox.size() > 0:
            cur = self.listbox.curselection()[0]
            nxt = (cur + 1) % self.listbox.size()
            self.listbox.selection_clear(0, tk.END)
            self.listbox.selection_set(nxt)
            self.on_select(None)
            
        fps = self.fps_var.get() if self.fps_var.get() > 0 else 10
        self.anim_id = self.after(int(1000/fps), self.animate)

    def pan(self, e):
        self.cam_x += e.x - self.lx
        self.cam_y += e.y - self.ly
        self.lx, self.ly = e.x, e.y
        self.redraw()
        
    def start_pan(self, e):
        self.lx, self.ly = e.x, e.y
        
    def do_zoom(self, e):
        self.zoom = max(0.2, min(8.0, self.zoom * (1.1 if e.delta > 0 else 0.9)))
        self.redraw()

class PaintTab(ttk.Frame):
    def __init__(self, parent, app):
        super().__init__(parent)
        self.app = app
        self._syncing = False
        self.current_dir = 0
        self.cam_x, self.cam_y, self.zoom = 0, 0, 3.0
        self.tool = tk.StringVar(value="brush")
        self.brush_size = tk.IntVar(value=1)
        self.brush_color = (255, 255, 255, 255)
        self.captured_color = None
        self.color_mode = tk.StringVar(value="pal")
        
        lp = create_scroll_panel(self, 280)

        tk.Button(lp, text="Load FRM to Workspace", command=self.app.load_workspace).pack(fill="x", pady=2)
        
        tk.Label(lp, text="Direction:").pack(pady=(5,0))
        self.dir_v = tk.StringVar(value=DIR_NAMES[0])
        cb_dir = ttk.Combobox(lp, textvariable=self.dir_v, values=DIR_NAMES, state="readonly")
        cb_dir.pack(fill="x")
        cb_dir.bind("<<ComboboxSelected>>", lambda e: self.change_dir())
        
        list_f = tk.Frame(lp)
        list_f.pack(fill="x", pady=2)
        sb = ttk.Scrollbar(list_f, orient="vertical")
        self.listbox = tk.Listbox(list_f, height=8, exportselection=False, yscrollcommand=sb.set)
        sb.config(command=self.listbox.yview)
        sb.pack(side="right", fill="y")
        self.listbox.pack(side="left", fill="both", expand=True)
        self.listbox.bind('<<ListboxSelect>>', self.on_select)
        
        zoom_f = tk.Frame(lp)
        zoom_f.pack(fill="x", pady=2)
        tk.Label(zoom_f, text="Zoom Level:").pack(side="left")
        tk.Button(zoom_f, text=" - ", command=self.zoom_out, width=3).pack(side="left", padx=2)
        tk.Button(zoom_f, text=" + ", command=self.zoom_in, width=3).pack(side="left", padx=2)

        tools_f = tk.LabelFrame(lp, text="Paint Tools")
        tools_f.pack(fill="x", pady=5)
        tk.Radiobutton(tools_f, text="Brush", variable=self.tool, value="brush").pack(anchor="w")
        tk.Radiobutton(tools_f, text="Eraser (Transparency)", variable=self.tool, value="eraser").pack(anchor="w")
        tk.Scale(tools_f, from_=1, to=10, variable=self.brush_size, orient="horizontal", label="Brush Size").pack(fill="x")

        pal_f = tk.LabelFrame(lp, text="Color Selection")
        pal_f.pack(fill="x", pady=5)
        
        tk.Label(pal_f, text="Palette Mode:").pack(anchor="w")
        tk.Radiobutton(pal_f, text="Fallout 8-bit Palette", variable=self.color_mode, value="pal").pack(anchor="w")
        tk.Radiobutton(pal_f, text="32-bit Truecolor", variable=self.color_mode, value="rgb").pack(anchor="w")
        
        tk.Button(pal_f, text="Pick 32-bit Color", command=self.pick_rgb_color).pack(fill="x", pady=2)
        
        self.pal_canvas = tk.Canvas(pal_f, width=160, height=160, bg="black")
        self.pal_canvas.pack(pady=2)
        self.pal_canvas.bind("<Button-1>", self.pick_pal_color)
        
        self.lbl_curr_col = tk.Label(pal_f, text="Current Color", bg="white", fg="black")
        self.lbl_curr_col.pack(fill="x")

        ops_f = tk.LabelFrame(lp, text="Frame Operations")
        ops_f.pack(fill="x", pady=5)
        tk.Button(ops_f, text="Trim Alpha Space", command=self.trim).pack(fill="x", pady=2)
        
        tk.Label(ops_f, text="Right-click canvas to capture", fg="#1AFF1A", font=("Arial",8)).pack()
        self.lbl_cap = tk.Label(ops_f, text="Captured: None", bg="gray", fg="black")
        self.lbl_cap.pack(fill="x", pady=2)
        tk.Button(ops_f, text="Swap Color Globally", command=self.apply_swap).pack(fill="x", pady=2)
        
        self.canvas = tk.Canvas(self, bg="#333", cursor="crosshair")
        self.canvas.pack(side="right", fill="both", expand=True)
        self.canvas.bind("<B1-Motion>", self.paint)
        self.canvas.bind("<ButtonPress-1>", self.paint)
        self.canvas.bind("<B2-Motion>", self.pan)
        self.canvas.bind("<ButtonPress-2>", self.start_pan)
        self.canvas.bind("<Button-3>", self.capture_color)
        
        def _on_mousewheel(event):
            self.zoom = max(0.5, min(15.0, self.zoom * (1.1 if event.delta > 0 else 0.9)))
            self.redraw()
        self.canvas.bind("<MouseWheel>", _on_mousewheel)
        self.tk_imgs = {}

    def zoom_in(self):
        self.zoom = max(0.5, min(15.0, self.zoom * 1.1))
        self.redraw()
        
    def zoom_out(self):
        self.zoom = max(0.5, min(15.0, self.zoom * 0.9))
        self.redraw()

    def draw_palette(self):
        if not self.app.converter.palette:
            self.pal_canvas.create_text(80, 80, text="No color.pal loaded", fill="white", justify="center", width=150)
            return
        self.pal_canvas.delete("all")
        sw, sh = 10, 10
        for i in range(256):
            r = self.app.converter.palette[i*3]
            g = self.app.converter.palette[i*3+1]
            b = self.app.converter.palette[i*3+2]
            c = '#%02x%02x%02x' % (r,g,b)
            x, y = (i % 16) * sw, (i // 16) * sh
            self.pal_canvas.create_rectangle(x, y, x+sw, y+sh, fill=c, outline="")

    def pick_pal_color(self, event):
        if not self.app.converter.palette: return
        self.color_mode.set("pal")
        sw, sh = 10, 10
        idx = (event.y // sh) * 16 + (event.x // sw)
        if 0 <= idx < 256:
            r = self.app.converter.palette[idx*3]
            g = self.app.converter.palette[idx*3+1]
            b = self.app.converter.palette[idx*3+2]
            self.brush_color = (r, g, b, 255)
            h = '#%02x%02x%02x' % (r,g,b)
            self.lbl_curr_col.config(bg=h, fg="black" if (r*0.299 + g*0.587 + b*0.114) > 186 else "white")

    def pick_rgb_color(self):
        c = colorchooser.askcolor()[0]
        if c:
            self.color_mode.set("rgb")
            r, g, b = int(c[0]), int(c[1]), int(c[2])
            self.brush_color = (r, g, b, 255)
            h = '#%02x%02x%02x' % (r,g,b)
            self.lbl_curr_col.config(bg=h, fg="black" if (r*0.299 + g*0.587 + b*0.114) > 186 else "white")

    def change_dir(self):
        self.current_dir = int(self.dir_v.get().split()[0])
        self.refresh_listbox(sync=True)

    def refresh_listbox(self, sync=False):
        old_sel = self.listbox.curselection()
        self.listbox.delete(0, tk.END)
        if not self.app.wk_meta: return
        for i, d in enumerate(self.app.wk_meta["frame_details"]):
            if d["dir"] == self.current_dir:
                orig = d.get('orig_id', 'New')
                self.listbox.insert(tk.END, f"Frame {d['frame_index']} [Orig: {orig}]")
        
        if old_sel and old_sel[0] < self.listbox.size():
            self.listbox.selection_set(old_sel[0])
        elif self.listbox.size() > 0:
            self.listbox.selection_set(0)
            
        if sync:
            self.on_select(None)
        else:
            self.redraw()

    def get_idx(self):
        sel = self.listbox.curselection()
        if not sel: return None
        count = 0
        for i, d in enumerate(self.app.wk_meta["frame_details"]):
            if d["dir"] == self.current_dir:
                if count == sel[0]: return i
                count += 1
        return None

    def on_select(self, e):
        if self._syncing: return
        self._syncing = True
        self.redraw()
        sel = self.listbox.curselection()
        if sel:
            self.app.t_ed.sync_selection(self.current_dir, sel[0])
        self._syncing = False

    def sync_selection(self, d_val, sel_idx):
        self._syncing = True
        self.dir_v.set(DIR_NAMES[d_val])
        self.current_dir = d_val
        self.listbox.delete(0, tk.END)
        if not self.app.wk_meta:
            self._syncing = False
            return
        for i, d in enumerate(self.app.wk_meta["frame_details"]):
            if d["dir"] == self.current_dir:
                orig = d.get('orig_id', 'New')
                self.listbox.insert(tk.END, f"Frame {d['frame_index']} [Orig: {orig}]")
        if sel_idx < self.listbox.size():
            self.listbox.selection_set(sel_idx)
            self.redraw()
        self._syncing = False

    def paint(self, event):
        idx = self.get_idx()
        if idx is None: return
        img = self.app.wk_imgs[idx]
        
        cx, cy = self.canvas.winfo_width()//2 + self.cam_x, self.canvas.winfo_height()//2 + self.cam_y
        ix = int((event.x - cx + (img.width * self.zoom) / 2.0) / self.zoom)
        iy = int((event.y - cy + (img.height * self.zoom) / 2.0) / self.zoom)
        
        if 0 <= ix < img.width and 0 <= iy < img.height:
            draw = ImageDraw.Draw(img)
            c = self.brush_color if self.tool.get() == "brush" else (0,0,0,0)
            r = self.brush_size.get() - 1
            draw.rectangle([ix-r, iy-r, ix+r, iy+r], fill=c)
            self.redraw()
            self.app.t_ed.redraw()

    def capture_color(self, event):
        idx = self.get_idx()
        if idx is None: return
        img = self.app.wk_imgs[idx]
        cx, cy = self.canvas.winfo_width()//2 + self.cam_x, self.canvas.winfo_height()//2 + self.cam_y
        ix = int((event.x - cx + (img.width * self.zoom) / 2.0) / self.zoom)
        iy = int((event.y - cy + (img.height * self.zoom) / 2.0) / self.zoom)
        
        if 0 <= ix < img.width and 0 <= iy < img.height:
            c = img.getpixel((ix, iy))
            if c[3] > 0:
                self.captured_color = c
                h = '#%02x%02x%02x' % c[:3]
                self.lbl_cap.config(text=f"Captured: {h}", bg=h, fg="black" if sum(c[:3])>382 else "white")

    def apply_swap(self):
        if not self.captured_color: return
        if self.tool.get() == "brush":
            target = self.brush_color
        else:
            c = colorchooser.askcolor()[0]
            if not c: return
            target = (int(c[0]), int(c[1]), int(c[2]), 255)
        
        for i in range(len(self.app.wk_imgs)):
            data = list(self.app.wk_imgs[i].getdata())
            self.app.wk_imgs[i].putdata([target if p == self.captured_color else p for p in data])
        
        self.app.notify_workspace_update()

    def trim(self):
        idx = self.get_idx()
        if idx is None: return
        img = self.app.wk_imgs[idx]
        
        bbox = img.getbbox()
        if bbox:
            self.app.wk_imgs[idx] = img.crop(bbox)
            self.app.wk_meta["frame_details"][idx]["ox"] += bbox[0]
            self.app.wk_meta["frame_details"][idx]["oy"] += bbox[1]
            self.app.wk_meta["frame_details"][idx]["width"] = self.app.wk_imgs[idx].width
            self.app.wk_meta["frame_details"][idx]["height"] = self.app.wk_imgs[idx].height
            self.app.notify_workspace_update()

    def redraw(self):
        if not self.app.wk_meta: return
        self.canvas.delete("all")
        
        idx = self.get_idx()
        if idx is None: return
        img = self.app.wk_imgs[idx]
        
        cx, cy = self.canvas.winfo_width()//2 + self.cam_x, self.canvas.winfo_height()//2 + self.cam_y
        nw, nh = int(img.width*self.zoom), int(img.height*self.zoom)
        self.tk_imgs["cur"] = ImageTk.PhotoImage(img.resize((nw, nh), Image.NEAREST))
        
        self.canvas.create_rectangle(cx - nw/2.0 - 1, cy - nh/2.0 - 1, cx + nw/2.0 + 1, cy + nh/2.0 + 1, outline="blue", dash=(2,2))
        self.canvas.create_image(cx, cy, anchor="center", image=self.tk_imgs["cur"])

    def pan(self, e):
        self.cam_x += e.x - self.lx
        self.cam_y += e.y - self.ly
        self.lx, self.ly = e.x, e.y
        self.redraw()
        
    def start_pan(self, e):
        self.lx, self.ly = e.x, e.y

class ViewerCell(tk.Frame):
    def __init__(self, parent, app, tab):
        super().__init__(parent, bd=2, relief="ridge")
        self.app = app
        self.tab = tab
        self.imgs = {}
        self.f_idx = 0
        self.fps = 10
        self.play = False
        self.z = 1.0
        self.anim_id = None
        
        self.lbl = tk.Label(self, text="Empty", fg="#1AFF1A", font=("Arial", 8))
        self.lbl.pack(side="top", fill="x")
        
        self.canvas = tk.Canvas(self, bg="#111", highlightthickness=0)
        self.canvas.pack(fill="both", expand=True)
        
        btn_f = tk.Frame(self)
        btn_f.pack(side="bottom", fill="x")
        self.btn_play = tk.Button(btn_f, text="Play", command=self.toggle)
        self.btn_play.pack(side="left", fill="x", expand=True)
        tk.Button(btn_f, text="Clear", command=self.clear).pack(side="left")
        
        self.bind("<Button-1>", self.select_cell)
        self.lbl.bind("<Button-1>", self.select_cell)
        self.canvas.bind("<Button-1>", self.select_cell)

    def select_cell(self, event=None):
        for c in self.tab.cells: 
            c.config(bd=2, relief="ridge")
        self.config(bd=2, relief="solid")
        self.tab.active_cell = self

    def load(self, path):
        self.lbl.config(text=os.path.basename(path))
        meta, flat = self.app.converter._read_frm(path)
        self.imgs = {d: [] for d in range(6)}
        for i, det in enumerate(meta["frame_details"]): 
            self.imgs[det["dir"]].append(flat[i])
        self.fps = meta["fps"] if meta["fps"] > 0 else 10
        self.f_idx = 0
        self.update_frame()

    def clear(self):
        self.imgs = {}
        self.lbl.config(text="Empty")
        self.canvas.delete("all")
        if self.play: self.toggle()

    def update_frame(self):
        if not self.imgs: return
        d = int(self.tab.dir_v.get().split()[0])
        frames = self.imgs.get(d, [])
        if not frames: 
            self.canvas.delete("all")
            return
            
        img = frames[self.f_idx % len(frames)]
        nw, nh = int(img.width * self.z), int(img.height * self.z)
        
        self.tk_img = ImageTk.PhotoImage(img.resize((nw, nh), Image.NEAREST))
        self.canvas.delete("all")
        
        w = self.canvas.winfo_width() if self.canvas.winfo_width() > 1 else 200
        h = self.canvas.winfo_height() if self.canvas.winfo_height() > 1 else 200
        self.canvas.create_image(w//2, h//2, image=self.tk_img)

    def toggle(self):
        self.play = not self.play
        self.btn_play.config(text="Pause" if self.play else "Play")
        if self.play:
            self.anim()
        else:
            if self.anim_id:
                self.after_cancel(self.anim_id)
                self.anim_id = None

    def anim(self):
        if not self.play: return
        self.f_idx += 1
        self.update_frame()
        fps = self.fps if self.fps > 0 else 10
        self.anim_id = self.after(int(1000/fps), self.anim)

class ViewerTab(ttk.Frame):
    def __init__(self, parent, app):
        super().__init__(parent)
        self.app = app
        self.files = []
        self.cur_folder = ""
        
        left = create_scroll_panel(self, 280)
        
        tk.Button(left, text="Open Folder", command=self.load_folder).pack(fill="x", pady=(5,2))
        
        list_f = tk.Frame(left)
        list_f.pack(fill="x", expand=True, pady=5)
        sb = ttk.Scrollbar(list_f, orient="vertical")
        self.listbox = tk.Listbox(list_f, height=15, yscrollcommand=sb.set)
        sb.config(command=self.listbox.yview)
        sb.pack(side="right", fill="y")
        self.listbox.pack(side="left", fill="both", expand=True)
        self.listbox.bind('<Double-1>', self.load_selected)
        
        ctrl = tk.Frame(left)
        ctrl.pack(fill="x", pady=2)
        tk.Label(ctrl, text="Dir:").pack(side="left")
        self.dir_v = tk.StringVar(value=DIR_NAMES[0])
        cb = ttk.Combobox(ctrl, textvariable=self.dir_v, values=DIR_NAMES, state="readonly", width=14)
        cb.pack(side="left", padx=5)
        cb.bind("<<ComboboxSelected>>", lambda e: self.update_all())
        
        tk.Button(left, text="Load to Active Slot", command=self.load_selected).pack(fill="x", pady=2)
        
        nav = tk.Frame(left)
        nav.pack(fill="x", pady=2)
        tk.Button(nav, text="<< Prev", command=lambda: self.cycle(-1)).pack(side="left", expand=True, fill="x")
        tk.Button(nav, text="Next >>", command=lambda: self.cycle(1)).pack(side="left", expand=True, fill="x")
        
        tk.Button(left, text="Load to Editor Workspace", command=self.load_to_editor, font=("", 9, "bold")).pack(fill="x", pady=(15,2))
        tk.Button(left, text="Load as Ghost in Editor", command=self.load_ghost_to_editor, fg="#1AFF1A").pack(fill="x", pady=(0,15))
        
        tk.Button(left, text="Play All slots", command=lambda: self.set_play_all(True)).pack(fill="x", pady=(5,0))
        tk.Button(left, text="Pause All slots", command=lambda: self.set_play_all(False)).pack(fill="x", pady=(1,10))

        self.grid_f = tk.Frame(self, bg="#111")
        self.grid_f.pack(side="right", fill="both", expand=True)
        
        for r in range(3):
            self.grid_f.rowconfigure(r, weight=1)
            self.grid_f.columnconfigure(r, weight=1)
            
        self.cells = []
        for r in range(3):
            for c in range(3):
                cell = ViewerCell(self.grid_f, app, self)
                cell.grid(row=r, column=c, sticky="nsew", padx=2, pady=2)
                self.cells.append(cell)
                
        self.active_cell = self.cells[0]
        self.active_cell.select_cell()

    def load_folder(self):
        folder = filedialog.askdirectory()
        if not folder: return
        self.cur_folder = folder
        self.files = sorted([f for f in os.listdir(folder) if f.lower().endswith(('.frm', '.FRM'))])
        self.listbox.delete(0, tk.END)
        for f in self.files: self.listbox.insert(tk.END, f)

    def load_selected(self, e=None):
        sel = self.listbox.curselection()
        if not sel: return
        path = os.path.join(self.cur_folder, self.files[sel[0]])
        self.active_cell.load(path)
        
    def load_to_editor(self):
        sel = self.listbox.curselection()
        if not sel: return
        path = os.path.join(self.cur_folder, self.files[sel[0]])
        self.app.load_workspace_from_path(path)

    def load_ghost_to_editor(self):
        sel = self.listbox.curselection()
        if not sel: return
        path = os.path.join(self.cur_folder, self.files[sel[0]])
        try:
            meta, imgs = self.app.converter._read_frm(path)
            self.app.t_ed.ext_ghost_meta = meta
            self.app.t_ed.ext_ghost_imgs = imgs
            self.app.t_ed.ghost_mode.set("ext")
            self.app.tabs.select(self.app.t_ed)
            self.app.t_ed.redraw()
            messagebox.showinfo("Ghost Loaded", f"Successfully loaded '{os.path.basename(path)}' as the External Ghost layer.")
        except Exception as e:
            messagebox.showerror("Error", f"Failed to load ghost:\n{e}")

    def cycle(self, d):
        if not self.files: return
        sel = self.listbox.curselection()
        idx = (sel[0] + d) % len(self.files) if sel else 0
        self.listbox.selection_clear(0, tk.END)
        self.listbox.selection_set(idx)
        self.listbox.see(idx)
        self.load_selected()

    def update_all(self):
        for c in self.cells: c.update_frame()

    def set_play_all(self, state):
        for c in self.cells:
            if c.imgs and c.play != state:
                c.toggle()


class GifExporterTab(ttk.Frame):
    def __init__(self, parent, app):
        super().__init__(parent)
        self.app = app
        
        left = create_scroll_panel(self, 280)
        
        tk.Label(left, text="GIF Engine Exporter", font=("Arial", 12, "bold")).pack(pady=10)
        tk.Label(left, text="Exports the exact in-engine \nrendered offsets to a shareable \nanimated .GIF file", justify="center").pack(pady=(0,15))
        
        tk.Button(left, text="Load FRM to Workspace", command=self.app.load_workspace).pack(fill="x", pady=2)
        
        tk.Label(left, text="Direction to Export:").pack(pady=(15,0))
        self.dir_v = tk.StringVar(value=DIR_NAMES[0])
        ttk.Combobox(left, textvariable=self.dir_v, values=DIR_NAMES, state="readonly").pack(fill="x")
        
        self.export_all_v = tk.BooleanVar(value=False)
        tk.Checkbutton(left, text="Export ALL Directions", variable=self.export_all_v).pack(pady=(5,0))
        
        tk.Label(left, text="Scale Factor:").pack(pady=(10,0))
        self.scale_v = tk.DoubleVar(value=2.0)
        tk.Spinbox(left, from_=1.0, to=10.0, increment=1.0, textvariable=self.scale_v).pack(fill="x")
        
        tk.Label(left, text="Background Color (Hex):").pack(pady=(10,0))
        self.bg_color = tk.StringVar(value="#333333")
        tk.Entry(left, textvariable=self.bg_color, justify="center").pack(fill="x")
        
        tk.Button(left, text="Pick Background Color", command=self.pick_color).pack(fill="x", pady=2)
        
        tk.Button(left, text="Export Workspace to GIF", command=self.export_gif, font=("", 10, "bold")).pack(fill="x", pady=25)

        right = tk.Frame(self, bg="#222")
        right.pack(side="right", fill="both", expand=True)
        tk.Label(right, text="Export Settings configured on the left panel.\n\nAll frame alignment processing occurs natively using\nWorkspace Meta shifts and origin points.", fg="white", bg="#222", justify="center").pack(expand=True)

    def pick_color(self):
        c = colorchooser.askcolor(title="Select Background Color")[1]
        if c: self.bg_color.set(c)

    def get_accum_offsets(self, meta, d_val, target_idx):
        ax = meta["x_shifts"][d_val]
        ay = meta["y_shifts"][d_val]
        for d in meta["frame_details"]:
            if d["dir"] == d_val:
                ax += d["ox"]
                ay += d["oy"]
                if d["frame_index"] == target_idx: break
        return ax, ay

    def export_gif(self):
        if not self.app.wk_meta:
            messagebox.showwarning("Warning", "Workspace is empty. Load an FRM first.")
            return
            
        out_path = filedialog.asksaveasfilename(defaultextension=".gif", filetypes=[("GIF files", "*.gif *.GIF")])
        if not out_path: return
        
        export_all = self.export_all_v.get()
        dirs_to_export = range(6) if export_all else [int(self.dir_v.get().split()[0])]
        
        frames_to_process = []
        min_x, min_y, max_x, max_y = 0, 0, 0, 0
        
        for d_val in dirs_to_export:
            for i, det in enumerate(self.app.wk_meta["frame_details"]):
                if det["dir"] == d_val:
                    img = self.app.wk_imgs[i]
                    ax, ay = self.get_accum_offsets(self.app.wk_meta, d_val, det["frame_index"])
                    px = ax - (img.width / 2.0)
                    py = ay - img.height
                    
                    frames_to_process.append((img, px, py))
                    
                    if px < min_x: min_x = px
                    if py < min_y: min_y = py
                    if px + img.width > max_x: max_x = px + img.width
                    if py + img.height > max_y: max_y = py + img.height
                
        if not frames_to_process:
            messagebox.showinfo("Error", "No frames found to export.")
            return
            
        canvas_w = int(max_x - min_x)
        canvas_h = int(max_y - min_y)
        
        gif_frames = []
        scale = self.scale_v.get()
        
        for img, px, py in frames_to_process:
            canvas = Image.new("RGBA", (canvas_w, canvas_h), self.bg_color.get())
            paste_x = int(px - min_x)
            paste_y = int(py - min_y)
            canvas.paste(img, (paste_x, paste_y), mask=img)
            
            if scale != 1.0:
                canvas = canvas.resize((int(canvas_w * scale), int(canvas_h * scale)), Image.NEAREST)
                
            gif_frames.append(canvas)
            
        fps = self.app.wk_meta.get("fps", 10)
        duration = int(1000 / fps) if fps > 0 else 100
        
        gif_frames[0].save(out_path, save_all=True, append_images=gif_frames[1:], optimize=False, duration=duration, loop=0)
        messagebox.showinfo("Success", f"GIF animation saved successfully:\n{out_path}")


class FrmScalingTab(ttk.Frame):
    def __init__(self, parent, app):
        super().__init__(parent)
        self.app = app
        self.orig_meta = None
        self.orig_imgs = []
        self.scaled_meta = None
        self.scaled_imgs = []
        self.f_idx = 0
        self.play = False
        self.anim_id = None
        
        left = create_scroll_panel(self, 280)
        
        tk.Label(left, text="FRM 2x Scaler", font=("Arial", 12, "bold")).pack(pady=10)
        tk.Label(left, text="Mathematically scale FRMs by 200%\nwithout breaking engine offsets.", justify="center").pack(pady=(0,15))
        tk.Button(left, text="Load FRM to Scale", command=self.load_frm).pack(fill="x", pady=2)
        tk.Label(left, text="Preview Direction:").pack(pady=(15,0))
        self.dir_v = tk.StringVar(value=DIR_NAMES[0])
        ttk.Combobox(left, textvariable=self.dir_v, values=DIR_NAMES, state="readonly").pack(fill="x")
        self.btn_play = tk.Button(left, text="Play Animation", command=self.toggle_play)
        self.btn_play.pack(fill="x", pady=10)
        
        ops_f = tk.LabelFrame(left, text="Single Save")
        ops_f.pack(fill="x", pady=10)
        tk.Button(ops_f, text="Save 2x Scaled (8-bit)", command=lambda: self.save_scaled(4)).pack(fill="x", pady=2, padx=5)
        tk.Button(ops_f, text="Save 2x Scaled (32-bit)", command=lambda: self.save_scaled(5)).pack(fill="x", pady=2, padx=5)
        
        batch_f = tk.LabelFrame(left, text="Batch Processing")
        batch_f.pack(fill="x", pady=10)
        tk.Button(batch_f, text="Batch 2x Folder (8-bit)", command=lambda: self.batch_scale(4)).pack(fill="x", pady=2, padx=5)
        tk.Button(batch_f, text="Batch 2x Folder (32-bit)", command=lambda: self.batch_scale(5)).pack(fill="x", pady=2, padx=5)
        
        right = tk.Frame(self)
        right.pack(side="right", fill="both", expand=True)
        
        frame_orig = tk.Frame(right, bg="#222", bd=2, relief="ridge")
        frame_orig.pack(side="left", fill="both", expand=True, padx=2, pady=2)
        tk.Label(frame_orig, text="Original 1x", bg="#222", fg="white").pack(side="top")
        self.canvas_orig = tk.Canvas(frame_orig, bg="#111", highlightthickness=0)
        self.canvas_orig.pack(fill="both", expand=True)
        
        frame_scaled = tk.Frame(right, bg="#222", bd=2, relief="ridge")
        frame_scaled.pack(side="left", fill="both", expand=True, padx=2, pady=2)
        tk.Label(frame_scaled, text="Scaled 2x", bg="#222", fg="white").pack(side="top")
        self.canvas_scaled = tk.Canvas(frame_scaled, bg="#111", highlightthickness=0)
        self.canvas_scaled.pack(fill="both", expand=True)
        
        self.tk_orig = None
        self.tk_scaled = None
        self.canvas_orig.bind("<Configure>", lambda e: self.update_frames())

    def scale_workspace(self, meta, imgs):
        new_meta = copy.deepcopy(meta)
        new_imgs = []
        for i in range(6):
            new_meta["x_shifts"][i] *= 2
            new_meta["y_shifts"][i] *= 2
        for d in new_meta["frame_details"]:
            d["ox"] *= 2
            d["oy"] *= 2
            d["orig_ox"] = d.get("orig_ox", d["ox"]) * 2
            d["orig_oy"] = d.get("orig_oy", d["oy"]) * 2
            d["width"] *= 2
            d["height"] *= 2
        for img in imgs:
            new_imgs.append(img.resize((img.width * 2, img.height * 2), Image.NEAREST))
        return new_meta, new_imgs

    def load_frm(self):
        path = filedialog.askopenfilename(filetypes=[("FRM files", "*.frm *.FRM")])
        if path:
            self.orig_meta, self.orig_imgs = self.app.converter._read_frm(path)
            self.scaled_meta, self.scaled_imgs = self.scale_workspace(self.orig_meta, self.orig_imgs)
            self.f_idx = 0
            self.update_frames()

    def get_accum_offsets(self, meta, d_val, target_idx):
        ax = meta["x_shifts"][d_val]
        ay = meta["y_shifts"][d_val]
        for d in meta["frame_details"]:
            if d["dir"] == d_val:
                ax += d["ox"]
                ay += d["oy"]
                if d["frame_index"] == target_idx: break
        return ax, ay

    def update_frames(self):
        if not self.orig_imgs or not self.scaled_imgs: return
        d = int(self.dir_v.get().split()[0])
        orig_dir_frames = [(i, m) for i, m in zip(self.orig_imgs, self.orig_meta["frame_details"]) if m["dir"] == d]
        scaled_dir_frames = [(i, m) for i, m in zip(self.scaled_imgs, self.scaled_meta["frame_details"]) if m["dir"] == d]
        if not orig_dir_frames:
            self.canvas_orig.delete("all")
            self.canvas_scaled.delete("all")
            return
            
        idx = self.f_idx % len(orig_dir_frames)
        o_img, o_det = orig_dir_frames[idx]
        s_img, s_det = scaled_dir_frames[idx]
        
        self.tk_orig = ImageTk.PhotoImage(o_img)
        cw, ch = self.canvas_orig.winfo_width(), self.canvas_orig.winfo_height()
        if cw > 1:
            cx, cy = cw//2, ch//2
            ax, ay = self.get_accum_offsets(self.orig_meta, d, o_det["frame_index"])
            px = cx + ax - (o_img.width / 2.0)
            py = cy + ay - o_img.height
            self.canvas_orig.delete("all")
            self.canvas_orig.create_image(px, py, anchor="nw", image=self.tk_orig)
            self.canvas_orig.create_line(cx-5, cy, cx+5, cy, fill="red")
            self.canvas_orig.create_line(cx, cy-5, cx, cy+5, fill="red")
        
        self.tk_scaled = ImageTk.PhotoImage(s_img)
        cw, ch = self.canvas_scaled.winfo_width(), self.canvas_scaled.winfo_height()
        if cw > 1:
            cx, cy = cw//2, ch//2
            ax, ay = self.get_accum_offsets(self.scaled_meta, d, s_det["frame_index"])
            px = cx + ax - (s_img.width / 2.0)
            py = cy + ay - s_img.height
            self.canvas_scaled.delete("all")
            self.canvas_scaled.create_image(px, py, anchor="nw", image=self.tk_scaled)
            self.canvas_scaled.create_line(cx-5, cy, cx+5, cy, fill="red")
            self.canvas_scaled.create_line(cx, cy-5, cx, cy+5, fill="red")

    def toggle_play(self):
        self.play = not self.play
        self.btn_play.config(text="Pause" if self.play else "Play Animation")
        if self.play:
            self.anim()
        else:
            if self.anim_id:
                self.after_cancel(self.anim_id)
                self.anim_id = None

    def anim(self):
        if not self.play: return
        self.f_idx += 1
        self.update_frames()
        fps = self.orig_meta.get("fps", 10) if self.orig_meta else 10
        fps = fps if fps > 0 else 10
        self.anim_id = self.after(int(1000/fps), self.anim)

    def save_scaled(self, ver):
        if not self.scaled_meta: return
        out = filedialog.asksaveasfilename(defaultextension=".frm", filetypes=[("FRM files", "*.frm *.FRM")])
        if not out: return
        self.app.converter._write_frm(out, self.scaled_meta, self.scaled_imgs, ver)
        if messagebox.askyesno("Success", f"Scaled FRM saved!\n\nWould you like to load it into the Editor?"):
            self.app.load_workspace_from_path(out)

    def batch_scale(self, ver):
        folder = filedialog.askdirectory(title="Select Folder to Batch Scale 2x")
        if not folder: return
        out_folder = os.path.join(folder, f"Scaled_2x_{ver}bit")
        os.makedirs(out_folder, exist_ok=True)
        count = 0
        for f in os.listdir(folder):
            if f.lower().endswith(('.frm', '.FRM')):
                path = os.path.join(folder, f)
                try:
                    meta, imgs = self.app.converter._read_frm(path)
                    s_meta, s_imgs = self.scale_workspace(meta, imgs)
                    out_path = os.path.join(out_folder, f)
                    self.app.converter._write_frm(out_path, s_meta, s_imgs, ver)
                    count += 1
                except Exception as e:
                    print(f"Error scaling {f}: {e}")
        messagebox.showinfo("Batch Complete", f"Successfully scaled {count} FRMs.\n\nSaved to:\n{out_folder}")


class MirrorTab(ttk.Frame):
    """v3.0 Feature: The Direction Symmetrizer / Mirroring Tab"""
    def __init__(self, parent, app):
        super().__init__(parent)
        self.app = app
        
        left = create_scroll_panel(self, 280)
        
        tk.Label(left, text="Direction Symmetrizer", font=("Arial", 12, "bold")).pack(pady=10)
        tk.Label(left, text="Instantly duplicate and horizontally \nflip all frames/offsets from one \ndirection into another.", justify="center").pack(pady=(0,15))
        
        tk.Button(left, text="Load FRM to Workspace", command=self.app.load_workspace).pack(fill="x", pady=2)
        
        f_src = tk.LabelFrame(left, text="Source Direction (To Copy)")
        f_src.pack(fill="x", pady=10)
        self.src_v = tk.StringVar(value=DIR_NAMES[1])
        ttk.Combobox(f_src, textvariable=self.src_v, values=DIR_NAMES, state="readonly").pack(fill="x", padx=5, pady=5)
        
        f_tgt = tk.LabelFrame(left, text="Target Direction (To Overwrite)")
        f_tgt.pack(fill="x", pady=10)
        self.tgt_v = tk.StringVar(value=DIR_NAMES[4])
        ttk.Combobox(f_tgt, textvariable=self.tgt_v, values=DIR_NAMES, state="readonly").pack(fill="x", padx=5, pady=5)
        
        tk.Label(left, text="⚠️ Warning: Target direction\nwill be completely overwritten.", fg="#ff4444").pack(pady=5)
        tk.Button(left, text="Mirror Direction", command=self.mirror_dir, font=("", 10, "bold"), fg="#1AFF1A").pack(fill="x", pady=10)

        right = tk.Frame(self, bg="#222")
        right.pack(side="right", fill="both", expand=True)
        tk.Label(right, text="Mirroring automatically inverts X-Shifts and OX Offsets.\n\nTypical Fallout 2 Isometric Pairs:\nNE (0)  <-->  NW (5)\nE (1)  <-->  W (4)\nSE (2)  <-->  SW (3)", fg="white", bg="#222", font=("Arial", 12), justify="center").pack(expand=True)

    def mirror_dir(self):
        if not self.app.wk_meta:
            messagebox.showwarning("Warning", "Workspace is empty.")
            return
            
        src_d = int(self.src_v.get().split()[0])
        tgt_d = int(self.tgt_v.get().split()[0])
        
        if src_d == tgt_d:
            messagebox.showwarning("Warning", "Source and Target cannot be the same.")
            return
            
        if not messagebox.askyesno("Confirm", f"This will delete all current frames in Direction {tgt_d} and overwrite them with a mirrored copy of Direction {src_d}.\n\nProceed?"):
            return
            
        src_frames = []
        src_imgs = []
        for i, det in enumerate(self.app.wk_meta["frame_details"]):
            if det["dir"] == src_d:
                src_frames.append(det)
                src_imgs.append(self.app.wk_imgs[i])
                
        if not src_frames:
            messagebox.showinfo("Error", f"No frames found in Source Direction {src_d}.")
            return
            
        new_meta_details = []
        new_wk_imgs = []
        for i, det in enumerate(self.app.wk_meta["frame_details"]):
            if det["dir"] != tgt_d:
                new_meta_details.append(det)
                new_wk_imgs.append(self.app.wk_imgs[i])
                
        self.app.wk_meta["frame_details"] = new_meta_details
        self.app.wk_imgs = new_wk_imgs
        
        for s_det, s_img in zip(src_frames, src_imgs):
            new_det = s_det.copy()
            new_det["dir"] = tgt_d
            new_det["ox"] = -new_det["ox"]
            new_det["orig_ox"] = -new_det.get("orig_ox", new_det["ox"])
            new_det["orig_id"] = f"Mirrored {s_det.get('orig_id', 'Frame')}"
            
            m_img = ImageOps.mirror(s_img)
            
            self.app.wk_meta["frame_details"].append(new_det)
            self.app.wk_imgs.append(m_img)
            
        self.app.wk_meta["x_shifts"][tgt_d] = -self.app.wk_meta["x_shifts"][src_d]
        self.app.wk_meta["y_shifts"][tgt_d] = self.app.wk_meta["y_shifts"][src_d]
        
        self.app.t_ed._reindex_frames()
        self.app.notify_workspace_update()
        messagebox.showinfo("Success", f"Mirrored Direction {src_d} into Direction {tgt_d}.")


class FalloutApp:
    def __init__(self, root):
        self.root = root
        self.root.title("Fallout 2 SpriteSheerer 3000 v3.2")
        self.root.geometry("1150x850")
        
        bg_color = "#2E2E28"
        fg_color = "#1AFF1A"
        active_bg = "#47473C"
        
        if ThemedTk:
            self.root.tk_setPalette(background=bg_color, foreground=fg_color, activeBackground=active_bg, activeForeground=fg_color)
            style = ttk.Style(self.root)
            style.configure(".", background=bg_color, foreground=fg_color, fieldbackground=bg_color)
            style.map("TNotebook.Tab", background=[("selected", active_bg)], foreground=[("selected", fg_color)])
        
        self.converter = FalloutConverter()
        self.out_ver = tk.IntVar(value=4)
        self.add_pad = tk.BooleanVar(value=False)
        
        self.wk_meta = None
        self.wk_imgs = []
        self.backup_meta = None
        self.backup_imgs = []
        
        self.tabs = ttk.Notebook(root)
        self.t_io = ttk.Frame(self.tabs); self.tabs.add(self.t_io, text="Pipeline")
        self.t_cv = ttk.Frame(self.tabs); self.tabs.add(self.t_cv, text="Conversion")
        self.t_vw = ViewerTab(self.tabs, self); self.tabs.add(self.t_vw, text="Viewer")
        self.t_ed = EditorTab(self.tabs, self); self.tabs.add(self.t_ed, text="Editor")
        self.t_pt = PaintTab(self.tabs, self); self.tabs.add(self.t_pt, text="Paint")
        self.t_gf = GifExporterTab(self.tabs, self); self.tabs.add(self.t_gf, text="GIF Export")
        self.t_sc = FrmScalingTab(self.tabs, self); self.tabs.add(self.t_sc, text="FRM Scaling")
        self.t_mr = MirrorTab(self.tabs, self); self.tabs.add(self.t_mr, text="Symmetrizer")
        self.t_ab = ttk.Frame(self.tabs); self.tabs.add(self.t_ab, text="About")
        self.tabs.pack(fill="both", expand=True)

        tk.Label(self.t_io, text="1. Palette Setup", font=("", 10, "bold")).pack(pady=5)
        tk.Button(self.t_io, text="Load color.pal", command=self.load_pal).pack()
        self.pal_lbl = tk.Label(self.t_io, text="No Palette", fg="red"); self.pal_lbl.pack()
        
        tk.Label(self.t_io, text="2. Map Extraction", font=("", 10, "bold")).pack(pady=10)
        tk.Checkbutton(self.t_io, text="Add 30px Notes Stripe to Extraction", variable=self.add_pad).pack()
        btn_ext = tk.Frame(self.t_io); btn_ext.pack()
        tk.Button(btn_ext, text="Extract Single FRM -> PNG", command=self.extract).pack(side="left", padx=5)
        tk.Button(btn_ext, text="Batch Extract Folder", command=self.batch_extract).pack(side="left", padx=5)
        
        tk.Label(self.t_io, text="3. Map Rebuild", font=("", 10, "bold")).pack(pady=10)
        f_rad = tk.Frame(self.t_io); f_rad.pack(pady=2)
        tk.Radiobutton(f_rad, text="8-bit", variable=self.out_ver, value=4).pack(side="left")
        tk.Radiobutton(f_rad, text="32-bit", variable=self.out_ver, value=5).pack(side="left")
        btn_bld = tk.Frame(self.t_io); btn_bld.pack()
        tk.Button(btn_bld, text="Rebuild Single PNG -> FRM", command=self.build_json).pack(side="left", padx=5)
        tk.Button(btn_bld, text="Batch Rebuild Folder", command=self.batch_build_json).pack(side="left", padx=5)
        
        tk.Label(self.t_io, text="4. Raw Builder", font=("", 10, "bold")).pack(pady=10)
        tk.Button(self.t_io, text="Launch Raw Builder...", command=self.open_raw).pack()

        tk.Label(self.t_cv, text="Format Migration", font=("", 12, "bold")).pack(pady=20)
        tk.Button(self.t_cv, text="v4 -> v5 (32-bit)", command=lambda: self.direct(5)).pack(pady=5)
        tk.Button(self.t_cv, text="v5 -> v4 (8-bit)", command=lambda: self.direct(4)).pack(pady=5)

        txt_f = tk.Frame(self.t_ab)
        txt_f.pack(fill="both", expand=True)
        sb = ttk.Scrollbar(txt_f, orient="vertical")
        txt = tk.Text(txt_f, wrap="word", padx=20, pady=20, font=("Arial", 10), yscrollcommand=sb.set)
        sb.config(command=txt.yview)
        sb.pack(side="right", fill="y")
        txt.pack(side="left", fill="both", expand=True)
        
        txt.insert("1.0", 
                   "FALLOUT 2 SPRITESHEETER 3000 - v3.2\n\n"
                   "Made by Terberus using Abominable Inteligence\n\n"
                   "HOW TO USE FEATURES:\n\n"
                   "1. Pipeline / Extraction:\n"
                   "   - Map Extraction: Renders FRM files to a flat PNG and saves JSON metadata containing exact offsets.\n"
                   "   - Map Rebuild: Reverse the process, compiling PNG/JSON pairs back to 8-bit or 32-bit FRMs.\n"
                   "   - Raw Builder: Slice raw 3rd-party PNGs into frames using the Advanced GUI Grid Slicer. Features 'Auto-Detect' to mathematically find the boundaries of your sprites.\n\n"
                   "2. Viewer & Editor:\n"
                   "   - Preview up to 9 files concurrently.\n"
                   "   - Reorder, duplicate, delete, and nudge individual frames. Changes are tracked permanently by original ID.\n"
                   "   - Internal and External Ghosting allows you to overlay previous frames or completely separate files for pixel-perfect alignment adjustments.\n\n"
                   "3. Paint:\n"
                   "   - Paint directly onto frames. Integrates Fallout's exact 8-bit color palette or unrestricted 32-bit truecolor. Instantly auto-trims dead alpha space to reduce file size.\n\n"
                   "4. GIF Export:\n"
                   "   - Export perfectly aligned, shareable animated GIFs of your sprites.\n"
                   "   - Compile ALL Directions into a single animated loop file.\n\n"
                   "5. FRM Scaling:\n"
                   "   - 200% Nearest-Neighbor mathematical scaler. Scales image pixels without blurring while simultaneously doubling Engine/Metadata shifts to preserve perfect animation stabilization.\n\n"
                   "6. Symmetrizer (Mirroring):\n"
                   "   - Automatically duplicate, horizontally flip, and mathematically invert the metadata (ox, oy, x_shifts) of one direction into another.")
        txt.config(state="disabled")

    def load_pal(self):
        p = filedialog.askopenfilename(filetypes=[("PAL files", "*.pal *.PAL")])
        if p:
            self.converter = FalloutConverter(p)
            if self.converter.palette:
                self.pal_lbl.config(text="Palette Loaded", fg="green")
                self.t_pt.draw_palette()
                
    def load_workspace_from_path(self, path):
        try:
            self.wk_meta, self.wk_imgs = self.converter._read_frm(path)
            self.backup_meta = copy.deepcopy(self.wk_meta)
            self.backup_imgs = [img.copy() for img in self.wk_imgs]
            self.t_ed.cam_x, self.t_ed.cam_y = 0, 0
            self.t_pt.cam_x, self.t_pt.cam_y = 0, 0
            self.notify_workspace_update()
            self.tabs.select(self.t_ed)
        except Exception as e:
            messagebox.showerror("Error", f"Failed to load FRM:\n{e}")

    def load_workspace(self):
        f = filedialog.askopenfilename(filetypes=[("FRM files", "*.frm *.FRM")])
        if f: self.load_workspace_from_path(f)

    def save_workspace(self, version):
        if not self.wk_meta: return
        f = filedialog.asksaveasfilename(defaultextension=".frm", filetypes=[("FRM files", "*.frm *.FRM")])
        if f: 
            self.converter._write_frm(f, self.wk_meta, self.wk_imgs, version)
            messagebox.showinfo("Saved", "Workspace successfully exported.")

    def notify_workspace_update(self):
        self.t_ed.refresh_listbox(sync=False)
        self.t_pt.refresh_listbox(sync=False)
        self.t_ed.on_select(None)

    def extract(self):
        f = filedialog.askopenfilename(filetypes=[("FRM files", "*.frm *.FRM")])
        if f:
            self.converter.frm_to_png(f, os.path.splitext(f)[0], self.add_pad.get())
            messagebox.showinfo("Done", "Extracted.")

    def batch_extract(self):
        folder = filedialog.askdirectory(title="Select Folder to Extract")
        if folder:
            count = 0
            for f in os.listdir(folder):
                if f.lower().endswith(('.frm', '.FRM')):
                    path = os.path.join(folder, f)
                    self.converter.frm_to_png(path, os.path.splitext(path)[0], self.add_pad.get())
                    count += 1
            messagebox.showinfo("Batch Complete", f"Extracted {count} FRMs.")

    def build_json(self):
        p = filedialog.askopenfilename(filetypes=[("PNG files", "*.png *.PNG")])
        if p:
            self._do_build_json(p)
            messagebox.showinfo("Done", "Built.")

    def batch_build_json(self):
        folder = filedialog.askdirectory(title="Select Folder to Rebuild")
        if folder:
            count = 0
            for f in os.listdir(folder):
                if f.lower().endswith(('.png', '.PNG')):
                    p = os.path.join(folder, f)
                    if os.path.exists(p.replace(".png", ".json").replace(".PNG", ".json")):
                        self._do_build_json(p)
                        count += 1
            messagebox.showinfo("Batch Complete", f"Rebuilt {count} FRMs.")

    def _do_build_json(self, p):
        j = p.replace(".png", ".json").replace(".PNG", ".json")
        if os.path.exists(j):
            with open(j, 'r') as f: meta = json.load(f)
            img = Image.open(p).convert("RGBA")
            cw, ch = meta["grid_cell_w"], meta["grid_cell_h"]
            imgs = [img.crop((d["frame_index"]*cw, d["dir"]*ch, d["frame_index"]*cw+d["width"], d["dir"]*ch+d["height"])) for d in meta["frame_details"]]
            out_name = p.replace(".png", "_new.frm").replace(".PNG", "_new.frm")
            self.converter._write_frm(out_name, meta, imgs, self.out_ver.get())

    def open_raw(self):
        f = filedialog.askopenfilename(filetypes=[("PNG files", "*.png *.PNG")])
        if f: RawPNGBuilder(self.root, f, self)

    def direct(self, v):
        f = filedialog.askopenfilename(filetypes=[("FRM files", "*.frm *.FRM")])
        if f:
            m, i = self.converter._read_frm(f)
            self.converter._write_frm(f.replace(".frm", f"_v{v}.frm").replace(".FRM", f"_v{v}.frm"), m, i, v)
            messagebox.showinfo("Done", "Converted.")

if __name__ == "__main__":
    if ThemedTk:
        root = ThemedTk(theme="radiance")
    else:
        root = tk.Tk()
    app = FalloutApp(root)
    root.mainloop()
