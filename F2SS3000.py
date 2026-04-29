import struct
import json
import os
import copy
import sys
import math
from PIL import Image, ImageDraw, ImageOps, ImageFilter
from PIL.ImageQt import ImageQt

try:
    import cv2
    import numpy as np
    OPENCV_AVAILABLE = True
except ImportError:
    OPENCV_AVAILABLE = False

from PySide6.QtWidgets import (QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, 
                               QPushButton, QLabel, QFileDialog, QMessageBox, QTabWidget, 
                               QScrollArea, QDialog, QLineEdit, QCheckBox, QComboBox, 
                               QListWidget, QSpinBox, QDoubleSpinBox, QRadioButton, QSlider, 
                               QGroupBox, QGraphicsView, QGraphicsScene, QColorDialog, 
                               QListWidgetItem, QAbstractItemView, QGridLayout, QButtonGroup,
                               QSplitter, QFrame)
from PySide6.QtCore import Qt, QTimer, QPoint, QPointF, QRectF, Signal, QEvent
from PySide6.QtGui import QPixmap, QImage, QPainter, QColor, QPen, QBrush, QMouseEvent, QWheelEvent, QCursor

DIR_NAMES = [
    "0 (Northeast)", "1 (East)", "2 (Southeast)", 
    "3 (Southwest)", "4 (West)", "5 (Northwest)"
]

def pil_to_qpixmap(pil_img):
    qim = ImageQt(pil_img)
    return QPixmap.fromImage(qim)

# =====================================================================
# CORE CONVERTER
# =====================================================================
class FalloutConverter:
    def __init__(self, palette_path=None):
        self.palette = self._load_palette(palette_path) if palette_path else None

    def _load_palette(self, path):
        try:
            with open(path, 'rb') as f:
                return [c * 4 for c in f.read(768)]
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
                        "orig_ox": ox, "orig_oy": oy, "width": w, "height": h, "orig_id": f"Frame {i}"
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
                    if alpha_data[i] < 255: final_pixels.append(0)
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
# UI TOOLKIT
# =====================================================================
def create_scroll_panel(min_width=350):
    scroll = QScrollArea()
    scroll.setWidgetResizable(True)
    scroll.setMinimumWidth(min_width)
    inner = QWidget()
    scroll.setWidget(inner)
    layout = QVBoxLayout(inner)
    layout.setAlignment(Qt.AlignTop)
    return scroll, inner, layout

class CustomGraphicsView(QGraphicsView):
    def __init__(self, scene, parent=None):
        super().__init__(scene, parent)
        self.setDragMode(QGraphicsView.NoDrag)
        self.setTransformationAnchor(QGraphicsView.AnchorUnderMouse)
        self._is_panning = False
        self._pan_start = QPoint()

    def wheelEvent(self, event):
        zoomInFactor = 1.1
        zoomOutFactor = 0.9
        if event.angleDelta().y() > 0:
            zoomFactor = zoomInFactor
        else:
            zoomFactor = zoomOutFactor
        self.scale(zoomFactor, zoomFactor)

    def mousePressEvent(self, event):
        if event.button() == Qt.RightButton:
            self._is_panning = True
            self._pan_start = event.pos()
            self.viewport().setCursor(Qt.ClosedHandCursor)
            event.accept()
        else:
            super().mousePressEvent(event)

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.RightButton:
            self._is_panning = False
            self.viewport().unsetCursor()
            event.accept()
        else:
            super().mouseReleaseEvent(event)

    def mouseMoveEvent(self, event):
        if self._is_panning:
            delta = event.pos() - self._pan_start
            self.horizontalScrollBar().setValue(self.horizontalScrollBar().value() - delta.x())
            self.verticalScrollBar().setValue(self.verticalScrollBar().value() - delta.y())
            self._pan_start = event.pos()
            event.accept()
        else:
            super().mouseMoveEvent(event)

# =====================================================================
# SUB-WINDOWS (Shifts, Slicer, Builder)
# =====================================================================
class ShiftsWindow(QDialog):
    def __init__(self, parent, editor):
        super().__init__(parent)
        self.editor = editor
        self.setWindowTitle("Edit Global Shifts")
        self.setFixedSize(300, 250)
        self.cur_d = self.editor.current_dir
        
        layout = QVBoxLayout(self)
        layout.addWidget(QLabel(f"Shifts for Direction: {DIR_NAMES[self.cur_d]}"))
        
        fx = QHBoxLayout()
        fx.addWidget(QLabel("X Shift:"))
        self.x_var = QSpinBox(); self.x_var.setRange(-9999, 9999); self.x_var.setValue(self.editor.app.wk_meta["x_shifts"][self.cur_d])
        fx.addWidget(self.x_var)
        layout.addLayout(fx)

        fy = QHBoxLayout()
        fy.addWidget(QLabel("Y Shift:"))
        self.y_var = QSpinBox(); self.y_var.setRange(-9999, 9999); self.y_var.setValue(self.editor.app.wk_meta["y_shifts"][self.cur_d])
        fy.addWidget(self.y_var)
        layout.addLayout(fy)

        self.all_dirs_var = QCheckBox("Apply to ALL Directions")
        layout.addWidget(self.all_dirs_var)

        btn_layout = QHBoxLayout()
        btn_save = QPushButton("Save Shifts"); btn_save.clicked.connect(self.save_shifts)
        btn_reset = QPushButton("Reset to Original"); btn_reset.clicked.connect(self.reset_shifts)
        btn_layout.addWidget(btn_save); btn_layout.addWidget(btn_reset)
        layout.addLayout(btn_layout)

    def save_shifts(self):
        new_x, new_y = self.x_var.value(), self.y_var.value()
        if self.all_dirs_var.isChecked():
            for i in range(6):
                self.editor.app.wk_meta["x_shifts"][i] = new_x
                self.editor.app.wk_meta["y_shifts"][i] = new_y
        else:
            self.editor.app.wk_meta["x_shifts"][self.cur_d] = new_x
            self.editor.app.wk_meta["y_shifts"][self.cur_d] = new_y
        self.editor.redraw()
        self.accept()

    def reset_shifts(self):
        if hasattr(self.editor.app, 'backup_meta') and self.editor.app.backup_meta:
            if self.all_dirs_var.isChecked():
                for i in range(6):
                    self.editor.app.wk_meta["x_shifts"][i] = self.editor.app.backup_meta["x_shifts"][i]
                    self.editor.app.wk_meta["y_shifts"][i] = self.editor.app.backup_meta["y_shifts"][i]
            else:
                self.editor.app.wk_meta["x_shifts"][self.cur_d] = self.editor.app.backup_meta["x_shifts"][self.cur_d]
                self.editor.app.wk_meta["y_shifts"][self.cur_d] = self.editor.app.backup_meta["y_shifts"][self.cur_d]
            self.x_var.setValue(self.editor.app.wk_meta["x_shifts"][self.cur_d])
            self.y_var.setValue(self.editor.app.wk_meta["y_shifts"][self.cur_d])
            self.editor.redraw()
            QMessageBox.information(self, "Reset", "Shifts restored to original values.")
        else:
            QMessageBox.information(self, "Info", "No original state backed up to restore.")

class GridSlicerWindow(QDialog):
    def __init__(self, parent, builder):
        super().__init__(parent)
        self.setWindowTitle("Advanced Grid Slicer & Builder")
        self.resize(1100, 700)
        self.builder = builder
        self.app = builder.app
        self.img = builder.img.convert("RGBA")
        self.prev_img = None
        self.f_count = max(1, builder.f_var.value())
        self.d_count = max(1, builder.rows_var.value())
        self.pad = builder.chk_pad.isChecked()
        self.work_w = self.img.width - 30 if self.pad else self.img.width

        self.y_lines = [int(i * self.img.height / self.d_count) for i in range(1, self.d_count)]
        self.x_lines = [[int(i * self.work_w / self.f_count) for i in range(1, self.f_count)] for _ in range(self.d_count)]

        layout = QVBoxLayout(self)
        top = QHBoxLayout()
        btn_auto = QPushButton("Auto-Detect Gaps"); btn_auto.clicked.connect(self.auto_detect)
        btn_reset = QPushButton("Reset Grid"); btn_reset.clicked.connect(self.reset_grid)
        btn_b32 = QPushButton("Build 32-bit FRM"); btn_b32.clicked.connect(lambda: self.build(5))
        btn_b8 = QPushButton("Build 8-bit FRM"); btn_b8.clicked.connect(lambda: self.build(4))
        
        top.addWidget(btn_auto); top.addWidget(btn_reset); top.addStretch()
        top.addWidget(btn_b32); top.addWidget(btn_b8)
        layout.addLayout(top)

        if OPENCV_AVAILABLE:
            self.bg_grp = QGroupBox("OpenCV Background Removal")
            self.bg_lay = QHBoxLayout(self.bg_grp)
            
            self.tool_grp = QButtonGroup(self)
            self.rb_grid = QRadioButton("Edit Grid"); self.rb_grid.setChecked(True)
            self.rb_magic = QRadioButton("Magic Wand (Click BG)")
            self.tool_grp.addButton(self.rb_grid); self.tool_grp.addButton(self.rb_magic)
            
            self.bg_lay.addWidget(self.rb_grid); self.bg_lay.addWidget(self.rb_magic)
            
            btn_auto_bg = QPushButton("Auto Remove BG"); btn_auto_bg.clicked.connect(self.auto_remove_bg)
            self.bg_lay.addWidget(btn_auto_bg)
            
            self.bg_lay.addWidget(QLabel("Thresh:"))
            self.bg_thresh = QSlider(Qt.Horizontal)
            self.bg_thresh.setRange(0, 100); self.bg_thresh.setValue(5)
            self.bg_lay.addWidget(self.bg_thresh)
            
            btn_color_bg = QPushButton("Color Remove..."); btn_color_bg.clicked.connect(self.color_replace_bg)
            self.bg_lay.addWidget(btn_color_bg)
            
            btn_undo_bg = QPushButton("Undo"); btn_undo_bg.clicked.connect(self.undo_bg)
            self.bg_lay.addWidget(btn_undo_bg)
            
            layout.addWidget(self.bg_grp)
        
        layout.addWidget(QLabel("Right-Click to Pan | Mousewheel to Zoom"))
        
        main_h = QHBoxLayout()
        self.scene = QGraphicsScene()
        self.view = CustomGraphicsView(self.scene)
        self.view.viewport().installEventFilter(self)
        main_h.addWidget(self.view, stretch=3)
        
        map_scroll, _, map_layout = create_scroll_panel(250)
        map_layout.addWidget(QLabel("Row to Direction Mapping"))
        self.row_mappings = []
        options = ["Ignore"] + DIR_NAMES
        for r in range(self.d_count):
            h = QHBoxLayout()
            h.addWidget(QLabel(f"Row {r+1}:"))
            cb = QComboBox(); cb.addItems(options)
            cb.setCurrentText(options[r+1] if r < 6 else "Ignore")
            self.row_mappings.append(cb)
            h.addWidget(cb)
            map_layout.addLayout(h)
            
        main_h.addWidget(map_scroll, stretch=1)
        layout.addLayout(main_h)
        self.redraw()

    def eventFilter(self, source, event):
        if source == self.view.viewport() and event.type() == QEvent.MouseButtonPress:
            if getattr(self, 'rb_magic', None) and self.rb_magic.isChecked():
                if event.buttons() & Qt.LeftButton:
                    pos = self.view.mapToScene(event.pos())
                    self.magic_wand_remove(int(pos.x()), int(pos.y()))
                    return True
        return super().eventFilter(source, event)

    def undo_bg(self):
        if hasattr(self, 'prev_img') and self.prev_img:
            self.img = self.prev_img.copy()
            self.redraw()

    def magic_wand_remove(self, x, y):
        if not OPENCV_AVAILABLE: return
        if not (0 <= x < self.img.width and 0 <= y < self.img.height): return
        
        self.prev_img = self.img.copy()
        
        np_img = np.array(self.img).copy()
        h, w = np_img.shape[:2]
        mask = np.zeros((h+2, w+2), np.uint8)
        
        t = self.bg_thresh.value()
        
        rgb = np_img[:,:,:3].copy()
        flags = 4 | (255 << 8) | cv2.FLOODFILL_FIXED_RANGE | cv2.FLOODFILL_MASK_ONLY
        cv2.floodFill(rgb, mask, (x, y), 0, (t,t,t), (t,t,t), flags)
        
        np_img[mask[1:-1, 1:-1] == 255] = [0, 0, 0, 0]
        
        self.img = Image.fromarray(np_img)
        self.redraw()

    def auto_remove_bg(self):
        self.magic_wand_remove(0, 0)

    def color_replace_bg(self):
        c = QColorDialog.getColor()
        if not c.isValid(): return
        
        self.prev_img = self.img.copy()
        
        np_img = np.array(self.img).copy()
        target = np.array([c.red(), c.green(), c.blue(), c.alpha()])
        
        diff = np.abs(np_img.astype(int) - target.astype(int))
        dist = np.sum(diff[:,:,:3], axis=2) / 3.0
        thresh = self.bg_thresh.value() * 2.55 
        np_img[dist <= thresh] = [0,0,0,0]
        
        self.img = Image.fromarray(np_img)
        self.redraw()

    def redraw(self):
        self.scene.clear()
        pixmap = pil_to_qpixmap(self.img)
        self.scene.addPixmap(pixmap)
        
        pen_v = QPen(QColor("#00aaff")); pen_v.setWidth(2); pen_v.setStyle(Qt.DashLine)
        pen_h = QPen(QColor("#ff4444")); pen_h.setWidth(3)
        
        for r in range(self.d_count):
            y_top = self.y_lines[r-1] if r > 0 else 0
            y_bot = self.y_lines[r] if r < self.d_count - 1 else self.img.height
            for c, x in enumerate(self.x_lines[r]):
                self.scene.addLine(x, y_top, x, y_bot, pen_v)
                
        for i, y in enumerate(self.y_lines):
            self.scene.addLine(0, y, self.work_w, y, pen_h)

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
        out, _ = QFileDialog.getSaveFileName(self, "Save FRM", "", "FRM files (*.frm *.FRM)")
        if not out: return

        meta = {"fps": self.builder.fps_var.value(), "action_frame": 0, "frames_per_dir": self.f_count, "x_shifts": [0]*6, "y_shifts": [0]*6, "frame_details": []}
        imgs = []

        for r in range(self.d_count):
            mapping = self.row_mappings[r].currentText()
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
        
        reply = QMessageBox.question(self, "Success", f"FRM generated successfully!\n\nWould you like to instantly load it into the Editor workspace?", QMessageBox.Yes | QMessageBox.No)
        if reply == QMessageBox.Yes:
            self.app.load_workspace_from_path(out)
            
        self.accept()
        self.builder.accept()

class RawPNGBuilder(QDialog):
    def __init__(self, parent, png_path, app):
        super().__init__(parent)
        self.setWindowTitle("Raw Builder Configuration")
        self.setFixedSize(350, 400)
        self.app = app
        self.png_path = png_path
        self.img = Image.open(png_path).convert("RGBA")
        
        layout = QVBoxLayout(self)
        layout.addWidget(QLabel(f"File: {os.path.basename(png_path)}"))
        
        layout.addWidget(QLabel("Frames Per Row:"))
        self.f_var = QSpinBox(); self.f_var.setRange(1, 100); self.f_var.setValue(1)
        layout.addWidget(self.f_var)
        
        layout.addWidget(QLabel("Number of Rows on Spritesheet:"))
        self.rows_var = QSpinBox(); self.rows_var.setRange(1, 100); self.rows_var.setValue(6)
        layout.addWidget(self.rows_var)

        layout.addWidget(QLabel("Animation FPS:"))
        self.fps_var = QSpinBox(); self.fps_var.setRange(1, 120); self.fps_var.setValue(10)
        layout.addWidget(self.fps_var)

        self.chk_pad = QCheckBox("Ignore rightmost 30px (Notes Stripe)")
        layout.addWidget(self.chk_pad)
        
        btn = QPushButton("Open Advanced Slicer >>>"); btn.clicked.connect(self.open_slicer)
        layout.addWidget(btn)

    def open_slicer(self):
        slicer = GridSlicerWindow(self, self)
        slicer.exec()

# =====================================================================
# MAIN TABS
# =====================================================================
class ViewerCell(QFrame):
    def __init__(self, parent, app, tab):
        super().__init__(parent)
        self.app = app
        self.tab = tab
        self.imgs = {}
        self.f_idx = 0
        self.fps = 10
        self.play = False
        self.anim_timer = QTimer(self)
        self.anim_timer.timeout.connect(self.anim)
        
        self.setFrameShape(QFrame.Box)
        self.setLineWidth(2)
        self.setStyleSheet("ViewerCell { border: 2px solid #444; }")
        
        layout = QVBoxLayout(self)
        self.lbl = QLabel("Empty")
        layout.addWidget(self.lbl)
        
        self.scene = QGraphicsScene()
        self.view = QGraphicsView(self.scene)
        self.view.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.view.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.view.setAttribute(Qt.WA_TransparentForMouseEvents)
        layout.addWidget(self.view)
        
        btn_h = QHBoxLayout()
        self.btn_play = QPushButton("Play"); self.btn_play.clicked.connect(self.toggle)
        btn_clear = QPushButton("Clear"); btn_clear.clicked.connect(self.clear)
        btn_h.addWidget(self.btn_play); btn_h.addWidget(btn_clear)
        layout.addLayout(btn_h)

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            for c in self.tab.cells:
                c.setStyleSheet("ViewerCell { border: 2px solid #444; }")
            self.setStyleSheet("ViewerCell { border: 2px solid #1AFF1A; }")
            self.tab.active_cell = self
        super().mousePressEvent(event)

    def load(self, path):
        self.lbl.setText(os.path.basename(path))
        meta, flat = self.app.converter._read_frm(path)
        self.imgs = {d: [] for d in range(6)}
        for i, det in enumerate(meta["frame_details"]): 
            self.imgs[det["dir"]].append(flat[i])
        self.fps = meta["fps"] if meta["fps"] > 0 else 10
        self.f_idx = 0
        self.update_frame()

    def clear(self):
        self.imgs = {}
        self.lbl.setText("Empty")
        self.scene.clear()
        if self.play: self.toggle()

    def update_frame(self):
        if not self.imgs: return
        d = int(self.tab.dir_v.currentText().split()[0])
        frames = self.imgs.get(d, [])
        if not frames: 
            self.scene.clear()
            return
        img = frames[self.f_idx % len(frames)]
        self.scene.clear()
        pix = pil_to_qpixmap(img)
        self.scene.addPixmap(pix)

    def toggle(self):
        self.play = not self.play
        self.btn_play.setText("Pause" if self.play else "Play")
        if self.play:
            fps = self.fps if self.fps > 0 else 10
            self.anim_timer.start(int(1000/fps))
        else:
            self.anim_timer.stop()

    def anim(self):
        self.f_idx += 1
        self.update_frame()

class ViewerTab(QWidget):
    def __init__(self, parent, app):
        super().__init__(parent)
        self.app = app
        self.files = []
        self.cur_folder = ""
        
        main_layout = QHBoxLayout(self)
        splitter = QSplitter(Qt.Horizontal)
        
        left_scroll, _, left = create_scroll_panel(300)
        btn_folder = QPushButton("Open Folder"); btn_folder.clicked.connect(self.load_folder)
        left.addWidget(btn_folder)
        
        self.listbox = QListWidget()
        self.listbox.setMinimumHeight(250)
        self.listbox.itemDoubleClicked.connect(self.load_selected)
        left.addWidget(self.listbox)
        
        h_dir = QHBoxLayout()
        h_dir.addWidget(QLabel("Dir:"))
        self.dir_v = QComboBox(); self.dir_v.addItems(DIR_NAMES)
        self.dir_v.currentIndexChanged.connect(self.update_all)
        h_dir.addWidget(self.dir_v)
        left.addLayout(h_dir)
        
        btn_load = QPushButton("Load to Active Slot"); btn_load.clicked.connect(self.load_selected)
        left.addWidget(btn_load)
        
        nav = QHBoxLayout()
        btn_prev = QPushButton("<< Prev"); btn_prev.clicked.connect(lambda: self.cycle(-1))
        btn_next = QPushButton("Next >>"); btn_next.clicked.connect(lambda: self.cycle(1))
        nav.addWidget(btn_prev); nav.addWidget(btn_next)
        left.addLayout(nav)
        
        btn_ed = QPushButton("Load to Editor Workspace"); btn_ed.clicked.connect(self.load_to_editor)
        btn_ghost = QPushButton("Load as Ghost in Editor"); btn_ghost.clicked.connect(self.load_ghost_to_editor)
        left.addWidget(btn_ed); left.addWidget(btn_ghost)
        
        btn_play_all = QPushButton("Play All"); btn_play_all.clicked.connect(lambda: self.set_play_all(True))
        btn_pause_all = QPushButton("Pause All"); btn_pause_all.clicked.connect(lambda: self.set_play_all(False))
        left.addWidget(btn_play_all); left.addWidget(btn_pause_all)

        self.grid_w = QWidget()
        self.grid_layout = QGridLayout(self.grid_w)
        self.cells = []
        for r in range(3):
            for c in range(3):
                cell = ViewerCell(self.grid_w, app, self)
                self.grid_layout.addWidget(cell, r, c)
                self.cells.append(cell)
        
        self.active_cell = self.cells[0]
        self.active_cell.setStyleSheet("ViewerCell { border: 2px solid #1AFF1A; }")
        
        splitter.addWidget(left_scroll)
        splitter.addWidget(self.grid_w)
        splitter.setSizes([300, 800])
        main_layout.addWidget(splitter)

    def load_folder(self):
        folder = QFileDialog.getExistingDirectory(self, "Select Folder")
        if not folder: return
        self.cur_folder = folder
        self.files = sorted([f for f in os.listdir(folder) if f.lower().endswith(('.frm', '.FRM'))])
        self.listbox.clear()
        self.listbox.addItems(self.files)

    def load_selected(self, item=None):
        sel = self.listbox.currentRow()
        if sel < 0: return
        path = os.path.join(self.cur_folder, self.files[sel])
        self.active_cell.load(path)
        
    def load_to_editor(self):
        sel = self.listbox.currentRow()
        if sel < 0: return
        path = os.path.join(self.cur_folder, self.files[sel])
        self.app.load_workspace_from_path(path)

    def load_ghost_to_editor(self):
        sel = self.listbox.currentRow()
        if sel < 0: return
        path = os.path.join(self.cur_folder, self.files[sel])
        try:
            meta, imgs = self.app.converter._read_frm(path)
            self.app.t_ed.ext_ghost_meta = meta
            self.app.t_ed.ext_ghost_imgs = imgs
            self.app.t_ed.btn_g_ext.setChecked(True)
            self.app.tabs.setCurrentWidget(self.app.t_ed)
            self.app.t_ed.redraw()
            QMessageBox.information(self, "Ghost Loaded", f"Successfully loaded '{os.path.basename(path)}' as the External Ghost layer.")
        except Exception as e:
            QMessageBox.critical(self, "Error", f"Failed to load ghost:\n{e}")

    def cycle(self, d):
        if not self.files: return
        sel = self.listbox.currentRow()
        idx = (sel + d) % len(self.files) if sel >= 0 else 0
        self.listbox.setCurrentRow(idx)
        self.load_selected()

    def update_all(self):
        for c in self.cells: c.update_frame()

    def set_play_all(self, state):
        for c in self.cells:
            if c.imgs and c.play != state:
                c.toggle()

class EditorTab(QWidget):
    def __init__(self, parent, app):
        super().__init__(parent)
        self.app = app
        self._syncing = False
        self.current_dir = 0
        self.show_hex = True
        self.show_bg = True
        self.show_ghost = True
        self.bg_image = None
        self.ext_ghost_meta = None
        self.ext_ghost_imgs = []
        self.anim_timer = QTimer(self)
        self.anim_timer.timeout.connect(self.animate)
        self.is_playing = False
        
        main_layout = QHBoxLayout(self)
        splitter = QSplitter(Qt.Horizontal)
        left_scroll, _, lp = create_scroll_panel(320)
        
        btn_load = QPushButton("Load FRM to Workspace"); btn_load.clicked.connect(self.app.load_workspace)
        lp.addWidget(btn_load)
        
        h_bg = QHBoxLayout()
        btn_lbg = QPushButton("Load Background"); btn_lbg.clicked.connect(self.load_bg)
        self.chk_bg = QCheckBox("Show BG"); self.chk_bg.setChecked(True); self.chk_bg.stateChanged.connect(self.redraw)
        h_bg.addWidget(btn_lbg); h_bg.addWidget(self.chk_bg)
        lp.addLayout(h_bg)
        
        lp.addWidget(QLabel("Direction:"))
        self.dir_v = QComboBox(); self.dir_v.addItems(DIR_NAMES)
        self.dir_v.currentIndexChanged.connect(self.change_dir)
        lp.addWidget(self.dir_v)
        
        self.listbox = QListWidget()
        self.listbox.setMinimumHeight(250)
        self.listbox.setDragDropMode(QAbstractItemView.InternalMove)
        self.listbox.itemSelectionChanged.connect(self.on_select)
        self.listbox.model().rowsMoved.connect(self.on_listbox_dropped)
        lp.addWidget(self.listbox)
        
        f_btns = QHBoxLayout()
        btn_up = QPushButton("Up"); btn_up.clicked.connect(lambda: self.move_frame(-1))
        btn_dn = QPushButton("Dn"); btn_dn.clicked.connect(lambda: self.move_frame(1))
        btn_dup = QPushButton("Dup"); btn_dup.clicked.connect(self.duplicate_frame)
        btn_del = QPushButton("Del"); btn_del.clicked.connect(self.delete_frame)
        f_btns.addWidget(btn_up); f_btns.addWidget(btn_dn); f_btns.addWidget(btn_dup); f_btns.addWidget(btn_del)
        lp.addLayout(f_btns)

        f_io = QHBoxLayout()
        btn_imp = QPushButton("Import Frame"); btn_imp.clicked.connect(self.import_frame)
        btn_exp = QPushButton("Export Frame"); btn_exp.clicked.connect(self.export_frame)
        f_io.addWidget(btn_imp); f_io.addWidget(btn_exp)
        lp.addLayout(f_io)

        fps_f = QHBoxLayout()
        fps_f.addWidget(QLabel("Anim FPS:"))
        self.fps_var = QSpinBox(); self.fps_var.setRange(1, 120); self.fps_var.setValue(10); self.fps_var.valueChanged.connect(self.update_fps)
        fps_f.addWidget(self.fps_var)
        fps_f.addWidget(QLabel("Action:"))
        self.action_var = QSpinBox(); self.action_var.setRange(0, 255); self.action_var.setValue(0); self.action_var.valueChanged.connect(self.update_action)
        fps_f.addWidget(self.action_var)
        lp.addLayout(fps_f)
        
        self.btn_play = QPushButton("Play Animation"); self.btn_play.clicked.connect(self.toggle_play)
        lp.addWidget(self.btn_play)
        
        off_f = QGroupBox("Offsets (ox, oy)")
        off_l = QVBoxLayout(off_f)
        off_top = QHBoxLayout()
        self.ox_v = QSpinBox(); self.ox_v.setRange(-999, 999)
        self.oy_v = QSpinBox(); self.oy_v.setRange(-999, 999)
        btn_set = QPushButton("Set"); btn_set.clicked.connect(self.set_offs)
        btn_set_all = QPushButton("Set All"); btn_set_all.clicked.connect(self.set_offs_all)
        off_top.addWidget(self.ox_v); off_top.addWidget(self.oy_v); off_top.addWidget(btn_set); off_top.addWidget(btn_set_all)
        off_l.addLayout(off_top)
        
        off_pad = QGridLayout()
        btn_u = QPushButton("U"); btn_u.clicked.connect(lambda: self.nudge_offset(0, -1))
        btn_l = QPushButton("L"); btn_l.clicked.connect(lambda: self.nudge_offset(-1, 0))
        btn_d = QPushButton("D"); btn_d.clicked.connect(lambda: self.nudge_offset(0, 1))
        btn_r = QPushButton("R"); btn_r.clicked.connect(lambda: self.nudge_offset(1, 0))
        off_pad.addWidget(btn_u, 0, 1); off_pad.addWidget(btn_l, 1, 0); off_pad.addWidget(btn_d, 1, 1); off_pad.addWidget(btn_r, 1, 2)
        off_l.addLayout(off_pad)
        
        btn_shifts = QPushButton("Edit Global Shifts..."); btn_shifts.clicked.connect(self.open_shifts_window)
        off_l.addWidget(btn_shifts)
        lp.addWidget(off_f)

        align_f = QHBoxLayout()
        btn_ah = QPushButton("Align to Hex"); btn_ah.clicked.connect(self.auto_align)
        btn_ag = QPushButton("Align to Ghost"); btn_ag.clicked.connect(self.align_to_ghost)
        align_f.addWidget(btn_ah); align_f.addWidget(btn_ag)
        lp.addLayout(align_f)
        
        res_f = QHBoxLayout()
        btn_ro = QPushButton("Reset Offs"); btn_ro.clicked.connect(self.reset_offs)
        btn_rw = QPushButton("Reset Workspc"); btn_rw.clicked.connect(self.reset_workspace)
        res_f.addWidget(btn_ro); res_f.addWidget(btn_rw)
        lp.addLayout(res_f)
        
        ghost_f = QGroupBox("Ghosting Overlay")
        gl = QVBoxLayout(ghost_f)
        self.chk_ghost = QCheckBox("Enabled"); self.chk_ghost.setChecked(True); self.chk_ghost.stateChanged.connect(self.redraw)
        gl.addWidget(self.chk_ghost)
        
        self.ghost_grp = QButtonGroup(self)
        self.btn_g_prev = QRadioButton("Previous Frame"); self.btn_g_prev.setChecked(True); self.btn_g_prev.toggled.connect(self.redraw)
        self.btn_g_f0 = QRadioButton("Frame 0 (Idle)"); self.btn_g_f0.toggled.connect(self.redraw)
        self.btn_g_ext = QRadioButton("External FRM"); self.btn_g_ext.toggled.connect(self.redraw)
        self.ghost_grp.addButton(self.btn_g_prev); self.ghost_grp.addButton(self.btn_g_f0); self.ghost_grp.addButton(self.btn_g_ext)
        gl.addWidget(self.btn_g_prev); gl.addWidget(self.btn_g_f0); gl.addWidget(self.btn_g_ext)
        
        btn_lext = QPushButton("Load External Ghost..."); btn_lext.clicked.connect(self.load_ext_ghost)
        gl.addWidget(btn_lext)
        self.ghost_alpha = QSlider(Qt.Horizontal); self.ghost_alpha.setRange(1, 9); self.ghost_alpha.setValue(5); self.ghost_alpha.valueChanged.connect(self.redraw)
        gl.addWidget(self.ghost_alpha)
        lp.addWidget(ghost_f)
        
        shad_f = QGroupBox("Shadow Generator")
        sl = QVBoxLayout(shad_f)
        if not OPENCV_AVAILABLE:
            sl.addWidget(QLabel("cv2 / numpy required."))
        else:
            self.chk_shad = QCheckBox("Preview Shadow"); self.chk_shad.stateChanged.connect(self.redraw)
            sl.addWidget(self.chk_shad)
            self.shad_skew = QSlider(Qt.Horizontal); self.shad_skew.setRange(-30, 30); self.shad_skew.setValue(5); self.shad_skew.valueChanged.connect(self.redraw)
            sl.addWidget(QLabel("Lean (Skew)")); sl.addWidget(self.shad_skew)
            self.shad_len = QSlider(Qt.Horizontal); self.shad_len.setRange(1, 40); self.shad_len.setValue(10); self.shad_len.valueChanged.connect(self.redraw)
            sl.addWidget(QLabel("Length")); sl.addWidget(self.shad_len)
            self.shad_opc = QSlider(Qt.Horizontal); self.shad_opc.setRange(0, 100); self.shad_opc.setValue(50); self.shad_opc.valueChanged.connect(self.redraw)
            sl.addWidget(QLabel("Opacity")); sl.addWidget(self.shad_opc)
            self.shad_blur = QSlider(Qt.Horizontal); self.shad_blur.setRange(0, 20); self.shad_blur.setValue(2); self.shad_blur.valueChanged.connect(self.redraw)
            sl.addWidget(QLabel("Blur")); sl.addWidget(self.shad_blur)
            
            btn_scol = QPushButton("Pick Shadow Color"); btn_scol.clicked.connect(self.pick_shadow_color)
            sl.addWidget(btn_scol)
            self.shadow_color = (0, 0, 0)
            
            h_sn = QHBoxLayout()
            self.shad_ox = QSlider(Qt.Horizontal); self.shad_ox.setRange(-50, 50); self.shad_ox.setValue(0); self.shad_ox.valueChanged.connect(self.redraw)
            self.shad_oy = QSlider(Qt.Horizontal); self.shad_oy.setRange(-50, 50); self.shad_oy.setValue(0); self.shad_oy.valueChanged.connect(self.redraw)
            h_sn.addWidget(QLabel("X")); h_sn.addWidget(self.shad_ox); h_sn.addWidget(QLabel("Y")); h_sn.addWidget(self.shad_oy)
            sl.addLayout(h_sn)
            
            h_sb = QHBoxLayout()
            btn_bkf = QPushButton("Bake(Frame)"); btn_bkf.clicked.connect(self.bake_shadow_frame)
            btn_bkd = QPushButton("Bake(Dir)"); btn_bkd.clicked.connect(self.bake_shadow_dir)
            btn_bka = QPushButton("Bake(All)"); btn_bka.clicked.connect(self.bake_shadow_all)
            h_sb.addWidget(btn_bkf); h_sb.addWidget(btn_bkd); h_sb.addWidget(btn_bka)
            sl.addLayout(h_sb)
        lp.addWidget(shad_f)

        self.chk_hex = QCheckBox("Show Hex Grid"); self.chk_hex.setChecked(True); self.chk_hex.stateChanged.connect(self.redraw)
        lp.addWidget(self.chk_hex)
        
        btn_s8 = QPushButton("Save Workspace (8-bit)"); btn_s8.clicked.connect(lambda: self.app.save_workspace(4))
        btn_s32 = QPushButton("Save Workspace (32-bit)"); btn_s32.clicked.connect(lambda: self.app.save_workspace(5))
        lp.addWidget(btn_s8); lp.addWidget(btn_s32)

        self.scene = QGraphicsScene()
        self.view = CustomGraphicsView(self.scene)
        self.view.setBackgroundBrush(QColor("#222"))
        
        splitter.addWidget(left_scroll)
        splitter.addWidget(self.view)
        splitter.setSizes([320, 800])
        main_layout.addWidget(splitter)

    def on_listbox_dropped(self, parent, start, end, destination, row):
        self._sync_listbox_to_data()

    def _sync_listbox_to_data(self):
        dir_items = [d for d in self.app.wk_meta["frame_details"] if d["dir"] == self.current_dir]
        img_items = [self.app.wk_imgs[i] for i, d in enumerate(self.app.wk_meta["frame_details"]) if d["dir"] == self.current_dir]
        
        ordered_det = []
        ordered_img = []
        for i in range(self.listbox.count()):
            txt = self.listbox.item(i).text()
            for j, d in enumerate(dir_items):
                orig = d.get('orig_id', 'New')
                if f"Frame {d['frame_index']} [Orig: {orig}]" == txt:
                    ordered_det.append(d)
                    ordered_img.append(img_items[j])
                    break
        
        new_meta_det = []
        new_imgs = []
        for i, d in enumerate(self.app.wk_meta["frame_details"]):
            if d["dir"] != self.current_dir:
                new_meta_det.append(d)
                new_imgs.append(self.app.wk_imgs[i])
        
        new_meta_det.extend(ordered_det)
        new_imgs.extend(ordered_img)
        self.app.wk_meta["frame_details"] = new_meta_det
        self.app.wk_imgs = new_imgs
        self._reindex_frames()
        self.app.notify_workspace_update()

    def pick_shadow_color(self):
        c = QColorDialog.getColor()
        if c.isValid():
            self.shadow_color = (c.red(), c.green(), c.blue())
            self.redraw()

    def generate_cv2_shadow(self, pil_img):
        if not OPENCV_AVAILABLE: return None, 0
        sw, sh = pil_img.size
        pad = max(sw, sh) * 2
        padded = Image.new("RGBA", (sw + pad * 2, sh + pad * 2), (0, 0, 0, 0))
        padded.paste(pil_img, (pad, pad))

        img_np = np.array(padded)
        alpha = img_np[:, :, 3]

        _, thresh = cv2.threshold(alpha, 10, 255, cv2.THRESH_BINARY)
        contours, _ = cv2.findContours(thresh, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)

        shadow_canvas = np.zeros((padded.size[1], padded.size[0]), dtype=np.uint8)
        skew = self.shad_skew.value() / 10.0
        v_scale = self.shad_len.value() / 10.0

        for cnt in contours:
            x, y, w, h = cv2.boundingRect(cnt)
            sprite_mask = thresh[y:y+h, x:x+w]
            
            x_tl = h * skew
            y_tl = h - h * v_scale
            x_tr = w + h * skew
            y_tr = y_tl
            x_bl = 0
            y_bl = h
            x_br = w
            y_br = h
            
            min_x = min(x_tl, x_tr, x_bl, x_br)
            min_y = min(y_tl, y_tr, y_bl, y_br)
            max_x = max(x_tl, x_tr, x_bl, x_br)
            max_y = max(y_tl, y_tr, y_bl, y_br)
            
            out_w = int(np.ceil(max_x - min_x))
            out_h = int(np.ceil(max_y - min_y))
            
            offset_x = -min_x
            offset_y = -min_y
            
            src_pts = np.float32([[0, 0], [w, 0], [0, h]])
            dst_pts = np.float32([
                [x_tl + offset_x, y_tl + offset_y],
                [x_tr + offset_x, y_tr + offset_y],
                [x_bl + offset_x, y_bl + offset_y]
            ])
            
            M = cv2.getAffineTransform(src_pts, dst_pts)
            warped_mask = cv2.warpAffine(sprite_mask, M, (out_w, out_h))
            
            g_x = int(x - offset_x)
            g_y = int(y - offset_y)
            
            y1, y2 = max(0, g_y), min(shadow_canvas.shape[0], g_y + out_h)
            x1, x2 = max(0, g_x), min(shadow_canvas.shape[1], g_x + out_w)
            
            wy1, wy2 = y1 - g_y, y2 - g_y
            wx1, wx2 = x1 - g_x, x2 - g_x
            
            if y1 < y2 and x1 < x2:
                shadow_canvas[y1:y2, x1:x2] = np.maximum(shadow_canvas[y1:y2, x1:x2], warped_mask[wy1:wy2, wx1:wx2])

        c = self.shadow_color
        shadow_img = Image.new("RGBA", padded.size, c)
        shadow_alpha = Image.fromarray(shadow_canvas)
        shadow_img.putalpha(shadow_alpha)

        if self.shad_blur.value() > 0:
            shadow_img = shadow_img.filter(ImageFilter.GaussianBlur(self.shad_blur.value()))
        
        opacity = int((self.shad_opc.value() / 100.0) * 255)
        r, g, b, a = shadow_img.split()
        a = a.point(lambda p: int(p * (opacity / 255.0)))
        shadow_img.putalpha(a)
        
        return shadow_img, pad

    def bake_shadow_frame(self):
        if not self.app.wk_meta: return
        idx = self.get_idx()
        if idx is not None:
            self._do_bake_shadow([idx])

    def bake_shadow_dir(self):
        if not self.app.wk_meta: return
        indices = [i for i, d in enumerate(self.app.wk_meta["frame_details"]) if d["dir"] == self.current_dir]
        if not indices: return
        self._do_bake_shadow(indices)

    def bake_shadow_all(self):
        if not self.app.wk_meta: return
        indices = list(range(len(self.app.wk_meta["frame_details"])))
        if not indices: return
        self._do_bake_shadow(indices)

    def _do_bake_shadow(self, indices):
        for idx in indices:
            det = self.app.wk_meta["frame_details"][idx]
            img = self.app.wk_imgs[idx]
            ax, ay = self.get_accum_offsets(self.app.wk_meta, det["dir"], det["frame_index"])
            
            pil_shad, pad = self.generate_cv2_shadow(img)
            if not pil_shad: continue
            
            canvas = Image.new("RGBA", (2000, 2000), (0,0,0,0))
            
            AX = int(1000 + ax)
            AY = int(1000 + ay)
            
            px = int(AX - img.width / 2.0)
            py = int(AY - img.height)
            
            shad_px = int(px - pad + self.shad_ox.value())
            shad_py = int(py - pad + self.shad_oy.value())
            
            canvas.paste(pil_shad, (shad_px, shad_py), pil_shad)
            canvas.paste(img, (px, py), img)
            
            bbox = canvas.getbbox()
            if not bbox: continue
            
            dist_left = AX - bbox[0]
            dist_right = bbox[2] - AX
            half_w = int(max(dist_left, dist_right))
            if half_w < 1: half_w = 1
            new_w = half_w * 2
            
            new_h = int(AY - bbox[1])
            if new_h < 1: new_h = 1
            
            crop_box = (AX - half_w, AY - new_h, AX + half_w, AY)
            cropped = canvas.crop(crop_box)
            
            self.app.wk_imgs[idx] = cropped
            det["width"] = new_w
            det["height"] = new_h
            
        self.on_select()
        self.app.notify_workspace_update()
        QMessageBox.information(self, "Done", "Shadows baked into selected frames successfully.")

    def open_shifts_window(self):
        if not self.app.wk_meta: return
        w = ShiftsWindow(self, self)
        w.exec()

    def load_bg(self):
        f, _ = QFileDialog.getOpenFileName(self, "Load Background", "", "PNG files (*.png *.PNG)")
        if f:
            self.bg_image = Image.open(f).convert("RGBA")
            self.redraw()

    def load_ext_ghost(self):
        path, _ = QFileDialog.getOpenFileName(self, "Load External Ghost", "", "FRM files (*.frm *.FRM)")
        if path:
            try:
                self.ext_ghost_meta, self.ext_ghost_imgs = self.app.converter._read_frm(path)
                self.btn_g_ext.setChecked(True)
                self.redraw()
            except Exception as e:
                QMessageBox.critical(self, "Error", f"Failed to load external ghost FRM:\n{e}")

    def update_fps(self):
        if self.app.wk_meta:
            self.app.wk_meta["fps"] = self.fps_var.value()

    def update_action(self):
        if self.app.wk_meta:
            self.app.wk_meta["action_frame"] = self.action_var.value()

    def change_dir(self):
        self.current_dir = self.dir_v.currentIndex()
        self.refresh_listbox(sync=True)

    def refresh_listbox(self, sync=False):
        old_sel = self.listbox.currentRow()
        self.listbox.clear()
        if not self.app.wk_meta: return
        self.fps_var.setValue(self.app.wk_meta.get("fps", 10))
        self.action_var.setValue(self.app.wk_meta.get("action_frame", 0))
        
        for i, d in enumerate(self.app.wk_meta["frame_details"]):
            if d["dir"] == self.current_dir:
                orig = d.get('orig_id', 'New')
                self.listbox.addItem(f"Frame {d['frame_index']} [Orig: {orig}]")
                
        if old_sel >= 0 and old_sel < self.listbox.count():
            self.listbox.setCurrentRow(old_sel)
        elif self.listbox.count() > 0:
            self.listbox.setCurrentRow(0)
        if sync:
            self.on_select()
        else:
            self.redraw()

    def get_idx(self):
        sel = self.listbox.currentRow()
        if sel < 0: return None
        count = 0
        for i, d in enumerate(self.app.wk_meta["frame_details"]):
            if d["dir"] == self.current_dir:
                if count == sel: return i
                count += 1
        return None

    def on_select(self, e=None):
        if self._syncing: return
        self._syncing = True
        idx = self.get_idx()
        if idx is not None:
            self.ox_v.setValue(self.app.wk_meta["frame_details"][idx]["ox"])
            self.oy_v.setValue(self.app.wk_meta["frame_details"][idx]["oy"])
            self.redraw()
            sel = self.listbox.currentRow()
            if sel >= 0:
                self.app.t_pt.sync_selection(self.current_dir, sel)
        self._syncing = False

    def sync_selection(self, d_val, sel_idx):
        self._syncing = True
        self.dir_v.setCurrentIndex(d_val)
        self.current_dir = d_val
        self.listbox.clear()
        if not self.app.wk_meta:
            self._syncing = False
            return
        for i, d in enumerate(self.app.wk_meta["frame_details"]):
            if d["dir"] == self.current_dir:
                orig = d.get('orig_id', 'New')
                self.listbox.addItem(f"Frame {d['frame_index']} [Orig: {orig}]")
        if sel_idx < self.listbox.count():
            self.listbox.setCurrentRow(sel_idx)
            idx = self.get_idx()
            if idx is not None:
                self.ox_v.setValue(self.app.wk_meta["frame_details"][idx]["ox"])
                self.oy_v.setValue(self.app.wk_meta["frame_details"][idx]["oy"])
            self.redraw()
        self._syncing = False

    def move_frame(self, direction):
        idx = self.get_idx()
        if idx is None: return
        list_idx = self.listbox.currentRow()
        new_list_idx = list_idx + direction
        if new_list_idx < 0 or new_list_idx >= self.listbox.count(): return
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
            self.listbox.setCurrentRow(new_list_idx)
            self.on_select()

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
        if self.listbox.count() <= 1:
            QMessageBox.warning(self, "Warning", "Cannot delete last frame in direction.")
            return
        del self.app.wk_meta["frame_details"][idx]
        del self.app.wk_imgs[idx]
        self._reindex_frames()
        self.app.notify_workspace_update()
        self.listbox.setCurrentRow(0)
        self.on_select()

    def import_frame(self):
        if not self.app.wk_meta: return
        f, _ = QFileDialog.getOpenFileName(self, "Import Image", "", "Images (*.png *.bmp *.jpg *.jpeg)")
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
        f, _ = QFileDialog.getSaveFileName(self, "Export Frame", "", "PNG files (*.png);;BMP files (*.bmp);;JPEG files (*.jpg *.jpeg)")
        if f:
            img_to_save = self.app.wk_imgs[idx]
            if f.lower().endswith(('.jpg', '.jpeg')):
                img_to_save = img_to_save.convert("RGB")
            img_to_save.save(f)
            QMessageBox.information(self, "Done", "Frame exported successfully.")

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
            self.app.wk_meta["frame_details"][idx]["ox"] = self.ox_v.value()
            self.app.wk_meta["frame_details"][idx]["oy"] = self.oy_v.value()
            self.redraw()

    def set_offs_all(self):
        if not self.app.wk_meta: return
        idx = self.get_idx()
        if idx is not None:
            new_ox = self.ox_v.value()
            new_oy = self.oy_v.value()
            for det in self.app.wk_meta["frame_details"]:
                if det["dir"] == self.current_dir:
                    det["ox"] = new_ox
                    det["oy"] = new_oy
            self.redraw()

    def nudge_offset(self, dx, dy):
        idx = self.get_idx()
        if idx is not None:
            self.app.wk_meta["frame_details"][idx]["ox"] += dx
            self.app.wk_meta["frame_details"][idx]["oy"] += dy
            self.ox_v.setValue(self.app.wk_meta["frame_details"][idx]["ox"])
            self.oy_v.setValue(self.app.wk_meta["frame_details"][idx]["oy"])
            self.redraw()

    def reset_offs(self):
        idx = self.get_idx()
        if idx is not None:
            orig_ox = self.app.wk_meta["frame_details"][idx].get("orig_ox", 0)
            orig_oy = self.app.wk_meta["frame_details"][idx].get("orig_oy", 0)
            self.app.wk_meta["frame_details"][idx]["ox"] = orig_ox
            self.app.wk_meta["frame_details"][idx]["oy"] = orig_oy
            self.on_select()
            
    def reset_workspace(self):
        if hasattr(self.app, 'backup_meta') and self.app.backup_meta:
            self.app.wk_meta = copy.deepcopy(self.app.backup_meta)
            self.app.wk_imgs = [img.copy() for img in self.app.backup_imgs]
            self.app.notify_workspace_update()
            QMessageBox.information(self, "Workspace Reset", "Successfully restored workspace to original loaded state.")
        else:
            QMessageBox.information(self, "Info", "No original state backed up to restore.")

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
            self.on_select()

    def align_to_ghost(self):
        if not self.app.wk_meta: return
        idx = self.get_idx()
        if idx is None: return
        
        gx, gy = None, None
        if self.btn_g_prev.isChecked() and self.listbox.currentRow() > 0:
            target_f = self.listbox.currentRow() - 1
            g_idx = self.get_idx_for_frame(target_f)
            if g_idx is not None:
                gx, gy = self.get_accum_offsets(self.app.wk_meta, self.current_dir, target_f)
        elif self.btn_g_f0.isChecked():
            g_idx = self.get_idx_for_frame(0)
            if g_idx is not None:
                gx, gy = self.get_accum_offsets(self.app.wk_meta, self.current_dir, 0)
        elif self.btn_g_ext.isChecked() and self.ext_ghost_meta:
            curr_idx = self.listbox.currentRow() if self.listbox.currentRow() >= 0 else 0
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
            self.on_select()
        else:
            QMessageBox.information(self, "Align to Ghost", "No active or valid Ghost frame to align to.")

    def get_idx_for_frame(self, frame_index):
        for i, d in enumerate(self.app.wk_meta["frame_details"]):
            if d["dir"] == self.current_dir and d["frame_index"] == frame_index:
                return i
        return None

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
        self.scene.clear()
        
        if self.bg_image and self.chk_bg.isChecked():
            bg_pix = pil_to_qpixmap(self.bg_image)
            item = self.scene.addPixmap(bg_pix)
            item.setPos(-bg_pix.width()/2, -bg_pix.height()/2)
        
        if self.chk_hex.isChecked():
            w, h = 32, 16
            pen = QPen(QColor("#444"))
            for r in range(-3, 4):
                for c in range(-3, 4):
                    px, py = (c * w) + (r%2)*(w/2), (r * h * 0.75)
                    poly = [QPointF(px, py-h/2), QPointF(px+w/2, py-h/4), QPointF(px+w/2, py+h/4),
                            QPointF(px, py+h/2), QPointF(px-w/2, py+h/4), QPointF(px-w/2, py-h/4)]
                    self.scene.addPolygon(poly, pen)

        idx = self.get_idx()
        if idx is None: return
        
        det = self.app.wk_meta["frame_details"][idx]
        accum_x, accum_y = self.get_accum_offsets(self.app.wk_meta, self.current_dir, det["frame_index"])

        if self.chk_ghost.isChecked():
            g_img = None
            gx, gy = 0, 0
            
            if self.btn_g_prev.isChecked() and self.listbox.currentRow() > 0:
                count = 0
                target_f = self.listbox.currentRow() - 1
                for i, d in enumerate(self.app.wk_meta["frame_details"]):
                    if d["dir"] == self.current_dir:
                        if count == target_f:
                            gx, gy = self.get_accum_offsets(self.app.wk_meta, self.current_dir, d["frame_index"])
                            g_img = self.app.wk_imgs[i].convert("RGBA")
                            break
                        count += 1
            elif self.btn_g_f0.isChecked():
                for i, d in enumerate(self.app.wk_meta["frame_details"]):
                    if d["dir"] == self.current_dir:
                        gx, gy = self.get_accum_offsets(self.app.wk_meta, self.current_dir, d["frame_index"])
                        g_img = self.app.wk_imgs[i].convert("RGBA")
                        break
            elif self.btn_g_ext.isChecked() and self.ext_ghost_meta:
                curr_idx = self.listbox.currentRow() if self.listbox.currentRow() >= 0 else 0
                ext_frames = [(i, m) for i, m in enumerate(self.ext_ghost_meta["frame_details"]) if m["dir"] == self.current_dir]
                if ext_frames:
                    target_f = min(curr_idx, len(ext_frames) - 1)
                    g_idx, g_det = ext_frames[target_f]
                    gx, gy = self.get_accum_offsets(self.ext_ghost_meta, self.current_dir, g_det["frame_index"])
                    g_img = self.ext_ghost_imgs[g_idx].convert("RGBA")
            
            if g_img:
                is_same_internal = self.btn_g_prev.isChecked() or self.btn_g_f0.isChecked() and g_img is self.app.wk_imgs[idx]
                if not is_same_internal:
                    r, g, b, a = g_img.split()
                    r = r.point(lambda p: int(p * 0.4))
                    g = g.point(lambda p: int(p * 0.8))
                    b = b.point(lambda p: int(min(255, p * 1.5)))
                    alpha_val = self.ghost_alpha.value() / 10.0
                    a = a.point(lambda p: int(p * alpha_val))
                    g_img = Image.merge("RGBA", (r, g, b, a))
                    g_pix = pil_to_qpixmap(g_img)
                    item = self.scene.addPixmap(g_pix)
                    item.setPos(gx - g_pix.width()/2.0, gy - g_pix.height())

        if OPENCV_AVAILABLE and self.chk_shad.isChecked():
            img = self.app.wk_imgs[idx]
            pil_shad, pad = self.generate_cv2_shadow(img)
            if pil_shad:
                shad_pix = pil_to_qpixmap(pil_shad)
                item = self.scene.addPixmap(shad_pix)
                shad_px = accum_x - (img.width / 2.0) - pad + self.shad_ox.value()
                shad_py = accum_y - img.height - pad + self.shad_oy.value()
                item.setPos(shad_px, shad_py)

        img = self.app.wk_imgs[idx]
        cur_pix = pil_to_qpixmap(img)
        item = self.scene.addPixmap(cur_pix)
        item.setPos(accum_x - cur_pix.width()/2.0, accum_y - cur_pix.height())
        
        pen_red = QPen(Qt.red)
        self.scene.addLine(-5, 0, 5, 0, pen_red)
        self.scene.addLine(0, -5, 0, 5, pen_red)

    def toggle_play(self):
        self.is_playing = not self.is_playing
        self.btn_play.setText("Pause" if self.is_playing else "Play Animation")
        if self.is_playing:
            fps = self.fps_var.value() if self.fps_var.value() > 0 else 10
            self.anim_timer.start(int(1000/fps))
        else:
            self.anim_timer.stop()

    def animate(self):
        if self.listbox.count() > 0:
            cur = self.listbox.currentRow()
            nxt = (cur + 1) % self.listbox.count()
            self.listbox.setCurrentRow(nxt)
            self.on_select()

class PaintTab(QWidget):
    def __init__(self, parent, app):
        super().__init__(parent)
        self.app = app
        self._syncing = False
        self.current_dir = 0
        self.brush_color = (255, 255, 255, 255)
        self.captured_color = None
        self.selection_rect = None
        
        main_layout = QHBoxLayout(self)
        splitter = QSplitter(Qt.Horizontal)
        left_scroll, _, lp = create_scroll_panel(320)
        
        btn_load = QPushButton("Load FRM to Workspace"); btn_load.clicked.connect(self.app.load_workspace)
        lp.addWidget(btn_load)
        
        lp.addWidget(QLabel("Direction:"))
        self.dir_v = QComboBox(); self.dir_v.addItems(DIR_NAMES)
        self.dir_v.currentIndexChanged.connect(self.change_dir)
        lp.addWidget(self.dir_v)
        
        self.listbox = QListWidget()
        self.listbox.setMinimumHeight(250)
        self.listbox.itemSelectionChanged.connect(self.on_select)
        lp.addWidget(self.listbox)
        
        tools_f = QGroupBox("Paint Tools")
        tl = QVBoxLayout(tools_f)
        self.tool_grp = QButtonGroup(self)
        self.rb_brush = QRadioButton("Brush"); self.rb_brush.setChecked(True)
        self.rb_eraser = QRadioButton("Eraser")
        self.rb_select = QRadioButton("Marquee Select")
        self.tool_grp.addButton(self.rb_brush); self.tool_grp.addButton(self.rb_eraser)
        self.tool_grp.addButton(self.rb_select)
        tl.addWidget(self.rb_brush); tl.addWidget(self.rb_eraser); tl.addWidget(self.rb_select)

        btn_clear_sel = QPushButton("Clear Selection")
        btn_clear_sel.clicked.connect(self.clear_selection)
        tl.addWidget(btn_clear_sel)

        self.brush_size = QSlider(Qt.Horizontal); self.brush_size.setRange(1, 10); self.brush_size.setValue(1)
        self.brush_size.valueChanged.connect(self.update_hover_cursor)
        tl.addWidget(QLabel("Brush Size")); tl.addWidget(self.brush_size)
        lp.addWidget(tools_f)

        if OPENCV_AVAILABLE:
            cv_f = QGroupBox("OpenCV Advanced Tools")
            cv_l = QVBoxLayout(cv_f)
            
            btn_outline = QPushButton("Add 1px Outline (Current Color)")
            btn_outline.clicked.connect(self.cv_add_outline)
            cv_l.addWidget(btn_outline)

            btn_smooth = QPushButton("Smooth Alpha Edges")
            btn_smooth.clicked.connect(self.cv_smooth_alpha)
            cv_l.addWidget(btn_smooth)
            lp.addWidget(cv_f)

        pal_f = QGroupBox("Color Selection")
        pl = QVBoxLayout(pal_f)
        self.cm_grp = QButtonGroup(self)
        self.rb_pal = QRadioButton("Fallout 8-bit Palette"); self.rb_pal.setChecked(True)
        self.rb_rgb = QRadioButton("32-bit Truecolor")
        self.cm_grp.addButton(self.rb_pal); self.cm_grp.addButton(self.rb_rgb)
        pl.addWidget(self.rb_pal); pl.addWidget(self.rb_rgb)
        btn_prgb = QPushButton("Pick 32-bit Color"); btn_prgb.clicked.connect(self.pick_rgb_color)
        pl.addWidget(btn_prgb)
        
        self.pal_scene = QGraphicsScene()
        self.pal_view = QGraphicsView(self.pal_scene)
        self.pal_view.setFixedSize(180, 180)
        self.pal_view.mousePressEvent = self.pick_pal_color
        pl.addWidget(self.pal_view)
        self.lbl_curr_col = QLabel("Current Color")
        self.lbl_curr_col.setStyleSheet("background-color: white; color: black;")
        pl.addWidget(self.lbl_curr_col)
        lp.addWidget(pal_f)

        ops_f = QGroupBox("Frame Operations")
        ol = QVBoxLayout(ops_f)
        btn_trim = QPushButton("Trim Alpha Space"); btn_trim.clicked.connect(self.trim)
        ol.addWidget(btn_trim)
        ol.addWidget(QLabel("Middle-click canvas to capture color"))
        self.lbl_cap = QLabel("Captured: None"); self.lbl_cap.setStyleSheet("background-color: gray; color: black;")
        ol.addWidget(self.lbl_cap)
        btn_swap = QPushButton("Swap Color Globally"); btn_swap.clicked.connect(self.apply_swap)
        ol.addWidget(btn_swap)
        lp.addWidget(ops_f)

        self.scene = QGraphicsScene()
        self.view = CustomGraphicsView(self.scene)
        self.view.setBackgroundBrush(QColor("#333"))
        
        self.view.viewport().setMouseTracking(True)
        self.view.viewport().installEventFilter(self)
        self.hover_rect = None

        splitter.addWidget(left_scroll)
        splitter.addWidget(self.view)
        splitter.setSizes([320, 800])
        main_layout.addWidget(splitter)

    def draw_palette(self):
        if not self.app.converter.palette:
            self.pal_scene.clear()
            self.pal_scene.addText("No color.pal loaded").setDefaultTextColor(Qt.white)
            return
        self.pal_scene.clear()
        sw, sh = 10, 10
        for i in range(256):
            r = self.app.converter.palette[i*3]
            g = self.app.converter.palette[i*3+1]
            b = self.app.converter.palette[i*3+2]
            c = QColor(r, g, b)
            x, y = (i % 16) * sw, (i // 16) * sh
            self.pal_scene.addRect(x, y, sw, sh, QPen(Qt.NoPen), QBrush(c))

    def pick_pal_color(self, event):
        if not self.app.converter.palette: return
        self.rb_pal.setChecked(True)
        pos = self.pal_view.mapToScene(event.pos())
        idx = (int(pos.y()) // 10) * 16 + (int(pos.x()) // 10)
        if 0 <= idx < 256:
            r = self.app.converter.palette[idx*3]
            g = self.app.converter.palette[idx*3+1]
            b = self.app.converter.palette[idx*3+2]
            self.brush_color = (r, g, b, 255)
            h = '#%02x%02x%02x' % (r,g,b)
            self.lbl_curr_col.setStyleSheet(f"background-color: {h}; color: {'black' if (r*0.299 + g*0.587 + b*0.114) > 186 else 'white'};")

    def pick_rgb_color(self):
        c = QColorDialog.getColor()
        if c.isValid():
            self.rb_rgb.setChecked(True)
            r, g, b = c.red(), c.green(), c.blue()
            self.brush_color = (r, g, b, 255)
            h = c.name()
            self.lbl_curr_col.setStyleSheet(f"background-color: {h}; color: {'black' if (r*0.299 + g*0.587 + b*0.114) > 186 else 'white'};")

    def change_dir(self):
        self.current_dir = self.dir_v.currentIndex()
        self.refresh_listbox(sync=True)

    def refresh_listbox(self, sync=False):
        old_sel = self.listbox.currentRow()
        self.listbox.clear()
        if not self.app.wk_meta: return
        for i, d in enumerate(self.app.wk_meta["frame_details"]):
            if d["dir"] == self.current_dir:
                orig = d.get('orig_id', 'New')
                self.listbox.addItem(f"Frame {d['frame_index']} [Orig: {orig}]")
        if old_sel >= 0 and old_sel < self.listbox.count():
            self.listbox.setCurrentRow(old_sel)
        elif self.listbox.count() > 0:
            self.listbox.setCurrentRow(0)
        if sync:
            self.on_select()
        else:
            self.redraw()

    def get_idx(self):
        sel = self.listbox.currentRow()
        if sel < 0: return None
        count = 0
        for i, d in enumerate(self.app.wk_meta["frame_details"]):
            if d["dir"] == self.current_dir:
                if count == sel: return i
                count += 1
        return None

    def on_select(self, e=None):
        if self._syncing: return
        self._syncing = True
        self.redraw()
        sel = self.listbox.currentRow()
        if sel >= 0:
            self.app.t_ed.sync_selection(self.current_dir, sel)
        self._syncing = False

    def sync_selection(self, d_val, sel_idx):
        self._syncing = True
        self.dir_v.setCurrentIndex(d_val)
        self.current_dir = d_val
        self.listbox.clear()
        if not self.app.wk_meta:
            self._syncing = False
            return
        for i, d in enumerate(self.app.wk_meta["frame_details"]):
            if d["dir"] == self.current_dir:
                orig = d.get('orig_id', 'New')
                self.listbox.addItem(f"Frame {d['frame_index']} [Orig: {orig}]")
        if sel_idx < self.listbox.count():
            self.listbox.setCurrentRow(sel_idx)
            self.redraw()
        self._syncing = False

    def eventFilter(self, source, event):
        if source == self.view.viewport():
            if event.type() == QEvent.MouseMove:
                self.update_hover_cursor(event.pos())
                if event.buttons() & Qt.LeftButton:
                    if getattr(self, 'rb_select', None) and self.rb_select.isChecked():
                        scene_pos = self.view.mapToScene(event.pos())
                        if hasattr(self, 'sel_start'):
                            self.selection_rect = QRectF(self.sel_start, scene_pos).normalized()
                            self.redraw()
                    else:
                        self.paint(event.pos())
            elif event.type() == QEvent.MouseButtonPress:
                if event.buttons() & Qt.LeftButton:
                    if getattr(self, 'rb_select', None) and self.rb_select.isChecked():
                        self.sel_start = self.view.mapToScene(event.pos())
                        self.selection_rect = QRectF(self.sel_start, self.sel_start)
                        self.redraw()
                    else:
                        self.paint(event.pos())
                elif event.buttons() & Qt.MiddleButton:
                    self.capture_color(event.pos())
        return super().eventFilter(source, event)

    def update_hover_cursor(self, pos=None):
        if not self.hover_rect or not self.app.wk_meta: return
        
        if isinstance(pos, int) or pos is None:
            global_pos = self.view.mapFromGlobal(self.view.cursor().pos())
            scene_pos = self.view.mapToScene(global_pos)
        else:
            scene_pos = self.view.mapToScene(pos)
            
        idx = self.get_idx()
        if idx is None: return
        img = self.app.wk_imgs[idx]

        ix = int(scene_pos.x() + img.width / 2.0)
        iy = int(scene_pos.y() + img.height / 2.0)
        
        r = self.brush_size.value() - 1
        hx = ix - r - img.width / 2.0
        hy = iy - r - img.height / 2.0
        hw = r * 2 + 1
        
        self.hover_rect.setRect(hx, hy, hw, hw)
        self.hover_rect.setVisible(not self.rb_select.isChecked())

    def clear_selection(self):
        self.selection_rect = None
        self.redraw()

    def paint(self, pos):
        idx = self.get_idx()
        if idx is None: return
        img = self.app.wk_imgs[idx]
        
        scene_pos = self.view.mapToScene(pos)
        ix = int(scene_pos.x() + img.width / 2.0)
        iy = int(scene_pos.y() + img.height / 2.0)
        
        if 0 <= ix < img.width and 0 <= iy < img.height:
            if self.selection_rect and self.selection_rect.isValid():
                scene_x = ix - img.width / 2.0
                scene_y = iy - img.height / 2.0
                if not self.selection_rect.contains(scene_x, scene_y): return

            draw = ImageDraw.Draw(img)
            c = self.brush_color if self.rb_brush.isChecked() else (0,0,0,0)
            r = self.brush_size.value() - 1
            
            box_x0, box_y0, box_x1, box_y1 = ix-r, iy-r, ix+r, iy+r
            if self.selection_rect and self.selection_rect.isValid():
                local_sel_x0 = self.selection_rect.left() + img.width / 2.0
                local_sel_y0 = self.selection_rect.top() + img.height / 2.0
                local_sel_x1 = self.selection_rect.right() + img.width / 2.0
                local_sel_y1 = self.selection_rect.bottom() + img.height / 2.0
                
                box_x0 = max(box_x0, int(local_sel_x0))
                box_y0 = max(box_y0, int(local_sel_y0))
                box_x1 = min(box_x1, int(local_sel_x1))
                box_y1 = min(box_y1, int(local_sel_y1))
                
                if box_x0 > box_x1 or box_y0 > box_y1: return
            
            draw.rectangle([box_x0, box_y0, box_x1, box_y1], fill=c)
            self.redraw()
            self.app.t_ed.redraw()

    def capture_color(self, pos):
        idx = self.get_idx()
        if idx is None: return
        img = self.app.wk_imgs[idx]
        scene_pos = self.view.mapToScene(pos)
        ix = int(scene_pos.x() + img.width / 2.0)
        iy = int(scene_pos.y() + img.height / 2.0)
        
        if 0 <= ix < img.width and 0 <= iy < img.height:
            c = img.getpixel((ix, iy))
            if c[3] > 0:
                self.captured_color = c
                h = '#%02x%02x%02x' % c[:3]
                self.lbl_cap.setText(f"Captured: {h}")
                self.lbl_cap.setStyleSheet(f"background-color: {h}; color: {'black' if sum(c[:3])>382 else 'white'};")

    def apply_swap(self):
        if not self.captured_color: return
        if self.rb_brush.isChecked():
            target = self.brush_color
        else:
            c = QColorDialog.getColor()
            if not c.isValid(): return
            target = (c.red(), c.green(), c.blue(), 255)
        
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

    def cv_add_outline(self):
        idx = self.get_idx()
        if idx is None or not OPENCV_AVAILABLE: return
        img = self.app.wk_imgs[idx]
        np_img = np.array(img).copy()
        alpha = np_img[:, :, 3]
        
        kernel = np.ones((3,3), np.uint8)
        dilated = cv2.dilate(alpha, kernel, iterations=1)
        outline_mask = cv2.subtract(dilated, alpha)
        
        c = (int(self.brush_color[0]), int(self.brush_color[1]), int(self.brush_color[2]), 255)
        np_img[outline_mask > 0] = c
        
        self.app.wk_imgs[idx] = Image.fromarray(np_img)
        self.redraw()
        self.app.t_ed.redraw()

    def cv_smooth_alpha(self):
        idx = self.get_idx()
        if idx is None or not OPENCV_AVAILABLE: return
        img = self.app.wk_imgs[idx]
        np_img = np.array(img).copy()
        alpha = np_img[:, :, 3]
        
        kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (3,3))
        smoothed_alpha = cv2.morphologyEx(alpha, cv2.MORPH_OPEN, kernel)
        smoothed_alpha = cv2.morphologyEx(smoothed_alpha, cv2.MORPH_CLOSE, kernel)
        
        np_img[:, :, 3] = smoothed_alpha
        self.app.wk_imgs[idx] = Image.fromarray(np_img)
        self.redraw()
        self.app.t_ed.redraw()

    def redraw(self):
        if not self.app.wk_meta: return
        self.scene.clear()
        
        idx = self.get_idx()
        if idx is None: return
        img = self.app.wk_imgs[idx]
        
        pix = pil_to_qpixmap(img)
        item = self.scene.addPixmap(pix)
        item.setPos(-pix.width()/2.0, -pix.height()/2.0)
        
        pen = QPen(Qt.blue); pen.setStyle(Qt.DashLine)
        self.scene.addRect(-pix.width()/2.0 - 1, -pix.height()/2.0 - 1, pix.width() + 2, pix.height() + 2, pen)

        if hasattr(self, 'selection_rect') and self.selection_rect and self.selection_rect.isValid():
            sel_pen = QPen(Qt.yellow)
            sel_pen.setStyle(Qt.DashLine)
            sel_pen.setWidth(1)
            self.scene.addRect(self.selection_rect, sel_pen)

        hover_pen = QPen(QColor(255, 255, 255, 180))
        hover_pen.setWidth(1)
        hover_pen.setStyle(Qt.DashLine)
        self.hover_rect = self.scene.addRect(0, 0, 0, 0, hover_pen, QBrush(Qt.NoBrush))
        self.hover_rect.setZValue(10)
        self.hover_rect.setVisible(False)

class GifExporterTab(QWidget):
    def __init__(self, parent, app):
        super().__init__(parent)
        self.app = app
        
        main_layout = QHBoxLayout(self)
        splitter = QSplitter(Qt.Horizontal)
        left_scroll, _, left = create_scroll_panel(300)
        
        lbl_title = QLabel("GIF Engine Exporter"); lbl_title.setStyleSheet("font-weight: bold; font-size: 14px;")
        left.addWidget(lbl_title)
        
        btn_load = QPushButton("Load FRM to Workspace"); btn_load.clicked.connect(self.app.load_workspace)
        left.addWidget(btn_load)
        
        left.addWidget(QLabel("Direction to Export:"))
        self.dir_v = QComboBox(); self.dir_v.addItems(DIR_NAMES)
        left.addWidget(self.dir_v)
        
        self.chk_all = QCheckBox("Export ALL Directions")
        left.addWidget(self.chk_all)
        
        left.addWidget(QLabel("Scale Factor:"))
        self.scale_v = QDoubleSpinBox(); self.scale_v.setRange(1.0, 10.0); self.scale_v.setValue(2.0); self.scale_v.setSingleStep(1.0)
        left.addWidget(self.scale_v)
        
        left.addWidget(QLabel("Background Color (Hex):"))
        self.bg_color = QLineEdit("#333333")
        left.addWidget(self.bg_color)
        
        btn_color = QPushButton("Pick Background Color"); btn_color.clicked.connect(self.pick_color)
        left.addWidget(btn_color)
        btn_exp = QPushButton("Export Workspace to GIF"); btn_exp.clicked.connect(self.export_gif)
        left.addWidget(btn_exp)

        right = QWidget(); right.setStyleSheet("background-color: #222; color: white;")
        r_lay = QVBoxLayout(right)
        r_lay.addWidget(QLabel("Export Settings configured on the left panel.\n\nAll frame alignment processing occurs natively using\nWorkspace Meta shifts and origin points."), alignment=Qt.AlignCenter)
        
        splitter.addWidget(left_scroll)
        splitter.addWidget(right)
        splitter.setSizes([300, 800])
        main_layout.addWidget(splitter)

    def pick_color(self):
        c = QColorDialog.getColor()
        if c.isValid(): self.bg_color.setText(c.name())

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
            QMessageBox.warning(self, "Warning", "Workspace is empty. Load an FRM first.")
            return
            
        out_path, _ = QFileDialog.getSaveFileName(self, "Save GIF", "", "GIF files (*.gif *.GIF)")
        if not out_path: return
        
        export_all = self.chk_all.isChecked()
        dirs_to_export = range(6) if export_all else [self.dir_v.currentIndex()]
        
        frames_to_process = []
        min_x, min_y, max_x, max_y = 0, 0, 0, 0
        
        for d_val in dirs_to_export:
            for i, enumerate_det in enumerate(self.app.wk_meta["frame_details"]):
                if enumerate_det["dir"] == d_val:
                    img = self.app.wk_imgs[i]
                    ax, ay = self.get_accum_offsets(self.app.wk_meta, d_val, enumerate_det["frame_index"])
                    px = ax - (img.width / 2.0)
                    py = ay - img.height
                    frames_to_process.append((img, px, py))
                    if px < min_x: min_x = px
                    if py < min_y: min_y = py
                    if px + img.width > max_x: max_x = px + img.width
                    if py + img.height > max_y: max_y = py + img.height
                
        if not frames_to_process:
            QMessageBox.information(self, "Error", "No frames found to export.")
            return
            
        canvas_w = int(max_x - min_x)
        canvas_h = int(max_y - min_y)
        
        gif_frames = []
        scale = self.scale_v.value()
        
        for img, px, py in frames_to_process:
            canvas = Image.new("RGBA", (canvas_w, canvas_h), self.bg_color.text())
            paste_x = int(px - min_x)
            paste_y = int(py - min_y)
            canvas.paste(img, (paste_x, paste_y), mask=img)
            if scale != 1.0:
                canvas = canvas.resize((int(canvas_w * scale), int(canvas_h * scale)), Image.NEAREST)
            gif_frames.append(canvas)
            
        fps = self.app.wk_meta.get("fps", 10)
        duration = int(1000 / fps) if fps > 0 else 100
        
        gif_frames[0].save(out_path, save_all=True, append_images=gif_frames[1:], optimize=False, duration=duration, loop=0)
        QMessageBox.information(self, "Success", f"GIF animation saved successfully:\n{out_path}")

class FrmScalingTab(QWidget):
    def __init__(self, parent, app):
        super().__init__(parent)
        self.app = app
        self.orig_meta = None
        self.orig_imgs = []
        self.scaled_meta = None
        self.scaled_imgs = []
        self.f_idx = 0
        self.play = False
        self.anim_timer = QTimer(self)
        self.anim_timer.timeout.connect(self.anim)
        
        main_layout = QHBoxLayout(self)
        splitter = QSplitter(Qt.Horizontal)
        left_scroll, _, left = create_scroll_panel(300)
        
        lbl_title = QLabel("FRM 2x Scaler"); lbl_title.setStyleSheet("font-weight: bold; font-size: 14px;")
        left.addWidget(lbl_title)
        
        btn_load = QPushButton("Load FRM to Scale"); btn_load.clicked.connect(self.load_frm)
        left.addWidget(btn_load)
        
        left.addWidget(QLabel("Preview Direction:"))
        self.dir_v = QComboBox(); self.dir_v.addItems(DIR_NAMES)
        left.addWidget(self.dir_v)
        
        self.btn_play = QPushButton("Play Animation"); self.btn_play.clicked.connect(self.toggle_play)
        left.addWidget(self.btn_play)
        
        ops_f = QGroupBox("Single Save")
        ol = QVBoxLayout(ops_f)
        btn_s8 = QPushButton("Save 2x Scaled (8-bit)"); btn_s8.clicked.connect(lambda: self.save_scaled(4))
        btn_s32 = QPushButton("Save 2x Scaled (32-bit)"); btn_s32.clicked.connect(lambda: self.save_scaled(5))
        ol.addWidget(btn_s8); ol.addWidget(btn_s32)
        left.addWidget(ops_f)
        
        batch_f = QGroupBox("Batch Processing")
        bl = QVBoxLayout(batch_f)
        btn_b8 = QPushButton("Batch 2x Folder (8-bit)"); btn_b8.clicked.connect(lambda: self.batch_scale(4))
        btn_b32 = QPushButton("Batch 2x Folder (32-bit)"); btn_b32.clicked.connect(lambda: self.batch_scale(5))
        bl.addWidget(btn_b8); bl.addWidget(btn_b32)
        left.addWidget(batch_f)
        
        right = QWidget()
        r_lay = QHBoxLayout(right)
        
        f_orig = QGroupBox("Original 1x")
        f_orig_lay = QVBoxLayout(f_orig)
        self.scene_orig = QGraphicsScene()
        self.view_orig = CustomGraphicsView(self.scene_orig)
        f_orig_lay.addWidget(self.view_orig)
        r_lay.addWidget(f_orig)
        
        f_scaled = QGroupBox("Scaled 2x")
        f_scaled_lay = QVBoxLayout(f_scaled)
        self.scene_scaled = QGraphicsScene()
        self.view_scaled = CustomGraphicsView(self.scene_scaled)
        f_scaled_lay.addWidget(self.view_scaled)
        r_lay.addWidget(f_scaled)
        
        splitter.addWidget(left_scroll)
        splitter.addWidget(right)
        splitter.setSizes([300, 800])
        main_layout.addWidget(splitter)

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
        path, _ = QFileDialog.getOpenFileName(self, "Load FRM", "", "FRM files (*.frm *.FRM)")
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
        d = self.dir_v.currentIndex()
        orig_dir_frames = [(i, m) for i, m in zip(self.orig_imgs, self.orig_meta["frame_details"]) if m["dir"] == d]
        scaled_dir_frames = [(i, m) for i, m in zip(self.scaled_imgs, self.scaled_meta["frame_details"]) if m["dir"] == d]
        if not orig_dir_frames:
            self.scene_orig.clear(); self.scene_scaled.clear()
            return
            
        idx = self.f_idx % len(orig_dir_frames)
        o_img, o_det = orig_dir_frames[idx]
        s_img, s_det = scaled_dir_frames[idx]
        
        self.scene_orig.clear()
        o_pix = pil_to_qpixmap(o_img)
        item_o = self.scene_orig.addPixmap(o_pix)
        ax, ay = self.get_accum_offsets(self.orig_meta, d, o_det["frame_index"])
        item_o.setPos(ax - o_img.width/2.0, ay - o_img.height)
        self.scene_orig.addLine(-5, 0, 5, 0, QPen(Qt.red))
        self.scene_orig.addLine(0, -5, 0, 5, QPen(Qt.red))
        
        self.scene_scaled.clear()
        s_pix = pil_to_qpixmap(s_img)
        item_s = self.scene_scaled.addPixmap(s_pix)
        ax_s, ay_s = self.get_accum_offsets(self.scaled_meta, d, s_det["frame_index"])
        item_s.setPos(ax_s - s_img.width/2.0, ay_s - s_img.height)
        self.scene_scaled.addLine(-5, 0, 5, 0, QPen(Qt.red))
        self.scene_scaled.addLine(0, -5, 0, 5, QPen(Qt.red))

    def toggle_play(self):
        self.play = not self.play
        self.btn_play.setText("Pause" if self.play else "Play Animation")
        if self.play:
            fps = self.orig_meta.get("fps", 10) if self.orig_meta else 10
            self.anim_timer.start(int(1000/fps) if fps > 0 else 100)
        else:
            self.anim_timer.stop()

    def anim(self):
        self.f_idx += 1
        self.update_frames()

    def save_scaled(self, ver):
        if not self.scaled_meta: return
        out, _ = QFileDialog.getSaveFileName(self, "Save FRM", "", "FRM files (*.frm *.FRM)")
        if not out: return
        self.app.converter._write_frm(out, self.scaled_meta, self.scaled_imgs, ver)
        rep = QMessageBox.question(self, "Success", f"Scaled FRM saved!\n\nWould you like to load it into the Editor?", QMessageBox.Yes | QMessageBox.No)
        if rep == QMessageBox.Yes:
            self.app.load_workspace_from_path(out)

    def batch_scale(self, ver):
        folder = QFileDialog.getExistingDirectory(self, "Select Folder to Batch Scale 2x")
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
        QMessageBox.information(self, "Batch Complete", f"Successfully scaled {count} FRMs.\n\nSaved to:\n{out_folder}")

class MirrorTab(QWidget):
    def __init__(self, parent, app):
        super().__init__(parent)
        self.app = app
        main_layout = QHBoxLayout(self)
        splitter = QSplitter(Qt.Horizontal)
        left_scroll, _, left = create_scroll_panel(300)
        
        lbl_title = QLabel("Direction Symmetrizer"); lbl_title.setStyleSheet("font-weight: bold; font-size: 14px;")
        left.addWidget(lbl_title)
        
        btn_load = QPushButton("Load FRM to Workspace"); btn_load.clicked.connect(self.app.load_workspace)
        left.addWidget(btn_load)
        
        f_src = QGroupBox("Source Direction")
        fl_src = QVBoxLayout(f_src)
        self.src_v = QComboBox(); self.src_v.addItems(DIR_NAMES); self.src_v.setCurrentIndex(1)
        fl_src.addWidget(self.src_v)
        left.addWidget(f_src)
        
        f_tgt = QGroupBox("Target Direction (To Overwrite)")
        fl_tgt = QVBoxLayout(f_tgt)
        self.tgt_v = QComboBox(); self.tgt_v.addItems(DIR_NAMES); self.tgt_v.setCurrentIndex(4)
        fl_tgt.addWidget(self.tgt_v)
        left.addWidget(f_tgt)
        
        btn_mirror = QPushButton("Mirror Direction"); btn_mirror.clicked.connect(self.mirror_dir)
        left.addWidget(btn_mirror)

        right = QWidget(); right.setStyleSheet("background-color: #222; color: white;")
        r_lay = QVBoxLayout(right)
        r_lay.addWidget(QLabel("Mirroring automatically inverts X-Shifts and OX Offsets.\n\nTypical Fallout 2 Isometric Pairs:\nNE (0)  <-->  NW (5)\nE (1)  <-->  W (4)\nSE (2)  <-->  SW (3)"), alignment=Qt.AlignCenter)
        
        splitter.addWidget(left_scroll)
        splitter.addWidget(right)
        splitter.setSizes([300, 800])
        main_layout.addWidget(splitter)

    def mirror_dir(self):
        if not self.app.wk_meta:
            QMessageBox.warning(self, "Warning", "Workspace is empty.")
            return
        src_d = self.src_v.currentIndex()
        tgt_d = self.tgt_v.currentIndex()
        if src_d == tgt_d:
            QMessageBox.warning(self, "Warning", "Source and Target cannot be the same.")
            return
        rep = QMessageBox.question(self, "Confirm", f"This will delete all current frames in Direction {tgt_d} and overwrite them with a mirrored copy of Direction {src_d}.\n\nProceed?", QMessageBox.Yes | QMessageBox.No)
        if rep != QMessageBox.Yes: return
            
        src_frames = []
        src_imgs = []
        for i, det in enumerate(self.app.wk_meta["frame_details"]):
            if det["dir"] == src_d:
                src_frames.append(det)
                src_imgs.append(self.app.wk_imgs[i])
        if not src_frames:
            QMessageBox.information(self, "Error", f"No frames found in Source Direction {src_d}.")
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
        QMessageBox.information(self, "Success", f"Mirrored Direction {src_d} into Direction {tgt_d}.")

class FalloutApp(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Fallout 2 SpriteSheeter 3000 v3.8 (PySide6 Port)")
        self.resize(1150, 850)
        self.setStyleSheet("QMainWindow { background-color: #2E2E28; color: #1AFF1A; } QLabel { color: #1AFF1A; } QPushButton { background-color: #47473C; color: #1AFF1A; border: 1px solid #1AFF1A; padding: 5px; } QCheckBox { color: #1AFF1A; } QGroupBox { border: 1px solid #1AFF1A; color: #1AFF1A; margin-top: 2ex; } QGroupBox::title { subcontrol-origin: margin; left: 10px; padding: 0 3px 0 3px; } QRadioButton { color: #1AFF1A; } QSplitter::handle { background-color: #1AFF1A; width: 2px; }")
        
        self.converter = FalloutConverter()
        self.wk_meta = None
        self.wk_imgs = []
        self.backup_meta = None
        self.backup_imgs = []
        
        self.tabs = QTabWidget()
        self.setCentralWidget(self.tabs)
        
        self.t_io = QWidget(); self.setup_io_tab()
        self.t_cv = QWidget(); self.setup_cv_tab()
        self.t_vw = ViewerTab(self, self)
        self.t_ed = EditorTab(self, self)
        self.t_pt = PaintTab(self, self)
        self.t_gf = GifExporterTab(self, self)
        self.t_sc = FrmScalingTab(self, self)
        self.t_mr = MirrorTab(self, self)
        self.t_ab = QWidget(); self.setup_about_tab()
        
        self.tabs.addTab(self.t_io, "Pipeline")
        self.tabs.addTab(self.t_cv, "Conversion")
        self.tabs.addTab(self.t_vw, "Viewer")
        self.tabs.addTab(self.t_ed, "Editor")
        self.tabs.addTab(self.t_pt, "Paint")
        self.tabs.addTab(self.t_gf, "GIF Export")
        self.tabs.addTab(self.t_sc, "FRM Scaling")
        self.tabs.addTab(self.t_mr, "Symmetrizer")
        self.tabs.addTab(self.t_ab, "About")

        if os.path.exists("color.pal"):
            self.converter = FalloutConverter("color.pal")
            if self.converter.palette:
                self.t_io_pal_lbl.setText("color.pal Auto-Loaded")
                self.t_io_pal_lbl.setStyleSheet("color: green;")
                self.t_pt.draw_palette()

    def setup_io_tab(self):
        layout = QVBoxLayout(self.t_io)
        layout.setAlignment(Qt.AlignTop)
        
        layout.addWidget(QLabel("1. Palette Setup"))
        btn_pal = QPushButton("Load color.pal"); btn_pal.clicked.connect(self.load_pal)
        layout.addWidget(btn_pal)
        self.t_io_pal_lbl = QLabel("No Palette"); self.t_io_pal_lbl.setStyleSheet("color: red;")
        layout.addWidget(self.t_io_pal_lbl)
        
        layout.addWidget(QLabel("2. Map Extraction"))
        self.chk_pad = QCheckBox("Add 30px Notes Stripe to Extraction")
        layout.addWidget(self.chk_pad)
        h_ext = QHBoxLayout()
        btn_ext_s = QPushButton("Extract Single FRM -> PNG"); btn_ext_s.clicked.connect(self.extract)
        btn_ext_b = QPushButton("Batch Extract Folder"); btn_ext_b.clicked.connect(self.batch_extract)
        h_ext.addWidget(btn_ext_s); h_ext.addWidget(btn_ext_b)
        layout.addLayout(h_ext)
        
        layout.addWidget(QLabel("3. Map Rebuild"))
        h_rad = QHBoxLayout()
        self.rb_8 = QRadioButton("8-bit"); self.rb_8.setChecked(True)
        self.rb_32 = QRadioButton("32-bit")
        h_rad.addWidget(self.rb_8); h_rad.addWidget(self.rb_32)
        layout.addLayout(h_rad)
        h_bld = QHBoxLayout()
        btn_bld_s = QPushButton("Rebuild Single PNG -> FRM"); btn_bld_s.clicked.connect(self.build_json)
        btn_bld_b = QPushButton("Batch Rebuild Folder"); btn_bld_b.clicked.connect(self.batch_build_json)
        h_bld.addWidget(btn_bld_s); h_bld.addWidget(btn_bld_b)
        layout.addLayout(h_bld)
        
        layout.addWidget(QLabel("4. Raw Builder"))
        btn_raw = QPushButton("Launch Raw Builder..."); btn_raw.clicked.connect(self.open_raw)
        layout.addWidget(btn_raw)

    def setup_cv_tab(self):
        layout = QVBoxLayout(self.t_cv)
        layout.setAlignment(Qt.AlignTop)
        layout.addWidget(QLabel("Format Migration"))
        btn_v5 = QPushButton("v4 -> v5 (32-bit)"); btn_v5.clicked.connect(lambda: self.direct(5))
        btn_v4 = QPushButton("v5 -> v4 (8-bit)"); btn_v4.clicked.connect(lambda: self.direct(4))
        layout.addWidget(btn_v5); layout.addWidget(btn_v4)

    def setup_about_tab(self):
        layout = QVBoxLayout(self.t_ab)
        txt = QLabel("FALLOUT 2 SPRITESHEETER 3000 - v3.8 (PySide6 Port)\n\n"
                     "Made by Terberus using Abominable Inteligence\n\n"
                   "1. Pipeline / Extraction:\n"
                   "   - Map Extraction: Renders FRM files to a flat PNG and saves JSON metadata containing exact offsets.\n"
                   "   - Map Rebuild: Reverse the process, compiling PNG/JSON pairs back to 8-bit or 32-bit FRMs.\n"
                   "   - Raw Builder: Slice raw 3rd-party PNGs into frames using the Advanced GUI Grid Slicer. Features 'Auto-Detect' to mathematically find the boundaries of your sprites.\n\n"
                   "2. Viewer & Editor:\n"
                   "   - Preview up to 9 files concurrently.\n"
                   "   - Reorder, duplicate, delete, and nudge individual frames. Changes are tracked permanently by original ID.\n"
                   "   - Adjust offsets, frames shift, choose the action frame and fps\n"
                   "   - Shadow Generator: Auto-create OpenCV contour shadows. Bake them perfectly into your sprites per-frame or globally.\n"
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
        txt.setAlignment(Qt.AlignTop)
        layout.addWidget(txt)

    def load_pal(self):
        p, _ = QFileDialog.getOpenFileName(self, "Load Palette", "", "PAL files (*.pal *.PAL)")
        if p:
            self.converter = FalloutConverter(p)
            if self.converter.palette:
                self.t_io_pal_lbl.setText("Palette Loaded")
                self.t_io_pal_lbl.setStyleSheet("color: green;")
                self.t_pt.draw_palette()

    def load_workspace_from_path(self, path):
        try:
            self.wk_meta, self.wk_imgs = self.converter._read_frm(path)
            self.backup_meta = copy.deepcopy(self.wk_meta)
            self.backup_imgs = [img.copy() for img in self.wk_imgs]
            self.notify_workspace_update()
            self.tabs.setCurrentWidget(self.t_ed)
        except Exception as e:
            QMessageBox.critical(self, "Error", f"Failed to load FRM:\n{e}")

    def load_workspace(self):
        f, _ = QFileDialog.getOpenFileName(self, "Load FRM", "", "FRM files (*.frm *.FRM)")
        if f: self.load_workspace_from_path(f)

    def save_workspace(self, version):
        if not self.wk_meta: return
        f, _ = QFileDialog.getSaveFileName(self, "Save Workspace", "", "FRM files (*.frm *.FRM)")
        if f: 
            self.converter._write_frm(f, self.wk_meta, self.wk_imgs, version)
            QMessageBox.information(self, "Saved", "Workspace successfully exported.")

    def notify_workspace_update(self):
        self.t_ed.refresh_listbox(sync=False)
        self.t_pt.refresh_listbox(sync=False)
        self.t_ed.on_select(None)

    def extract(self):
        f, _ = QFileDialog.getOpenFileName(self, "Extract FRM", "", "FRM files (*.frm *.FRM)")
        if f:
            self.converter.frm_to_png(f, os.path.splitext(f)[0], self.chk_pad.isChecked())
            QMessageBox.information(self, "Done", "Extracted.")

    def batch_extract(self):
        folder = QFileDialog.getExistingDirectory(self, "Select Folder to Extract")
        if folder:
            count = 0
            for f in os.listdir(folder):
                if f.lower().endswith(('.frm', '.FRM')):
                    path = os.path.join(folder, f)
                    self.converter.frm_to_png(path, os.path.splitext(path)[0], self.chk_pad.isChecked())
                    count += 1
            QMessageBox.information(self, "Batch Complete", f"Extracted {count} FRMs.")

    def build_json(self):
        p, _ = QFileDialog.getOpenFileName(self, "Select PNG", "", "PNG files (*.png *.PNG)")
        if p:
            self._do_build_json(p)
            QMessageBox.information(self, "Done", "Built.")

    def batch_build_json(self):
        folder = QFileDialog.getExistingDirectory(self, "Select Folder to Rebuild")
        if folder:
            count = 0
            for f in os.listdir(folder):
                if f.lower().endswith(('.png', '.PNG')):
                    p = os.path.join(folder, f)
                    if os.path.exists(p.replace(".png", ".json").replace(".PNG", ".json")):
                        self._do_build_json(p)
                        count += 1
            QMessageBox.information(self, "Batch Complete", f"Rebuilt {count} FRMs.")

    def _do_build_json(self, p):
        j = p.replace(".png", ".json").replace(".PNG", ".json")
        if os.path.exists(j):
            with open(j, 'r') as f: meta = json.load(f)
            img = Image.open(p).convert("RGBA")
            cw, ch = meta["grid_cell_w"], meta["grid_cell_h"]
            imgs = [img.crop((d["frame_index"]*cw, d["dir"]*ch, d["frame_index"]*cw+d["width"], d["dir"]*ch+d["height"])) for d in meta["frame_details"]]
            out_name = p.replace(".png", "_new.frm").replace(".PNG", "_new.frm")
            v = 5 if self.rb_32.isChecked() else 4
            self.converter._write_frm(out_name, meta, imgs, v)

    def open_raw(self):
        f, _ = QFileDialog.getOpenFileName(self, "Select Raw PNG", "", "PNG files (*.png *.PNG)")
        if f: 
            w = RawPNGBuilder(self, f, self)
            w.exec()

    def direct(self, v):
        f, _ = QFileDialog.getOpenFileName(self, "Select FRM", "", "FRM files (*.frm *.FRM)")
        if f:
            m, i = self.converter._read_frm(f)
            self.converter._write_frm(f.replace(".frm", f"_v{v}.frm").replace(".FRM", f"_v{v}.frm"), m, i, v)
            QMessageBox.information(self, "Done", "Converted.")

if __name__ == "__main__":
    app = QApplication(sys.argv)
    window = FalloutApp()
    window.show()
    sys.exit(app.exec())
