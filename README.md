Made by Terberus using Abominable Inteligence


                   1. Pipeline / Extraction:
                      - Map Extraction: Renders FRM files to a flat PNG and saves JSON metadata containing exact offsets.
                      - Map Rebuild: Reverse the process, compiling PNG/JSON pairs back to 8-bit or 32-bit FRMs.
                      - Raw Builder: Slice raw 3rd-party PNGs into frames using the Advanced GUI Grid Slicer. Features 'Auto-Detect' to mathematically find the boundaries of your sprites.
                   2. Viewer & Editor:
                      - Preview up to 9 files concurrently.
                      - Reorder, duplicate, delete, and nudge individual frames. Changes are tracked permanently by original ID.
                      - Adjust offsets, frames shift, choose the action frame and fps\n"
                      - Shadow Generator: Auto-create OpenCV contour shadows. Bake them perfectly into your sprites per-frame or globally.
                      - Internal and External Ghosting allows you to overlay previous frames or completely separate files for pixel-perfect alignment adjustments.
                   3. Paint:
                      - Paint directly onto frames. Integrates Fallout's exact 8-bit color palette or unrestricted 32-bit truecolor. Instantly auto-trims dead alpha space to reduce file size.
                   4. GIF Export:
                      - Export perfectly aligned, shareable animated GIFs of your sprites.
                      - Compile ALL Directions into a single animated loop file.
                   5. FRM Scaling:
                      - 200% Nearest-Neighbor mathematical scaler. Scales image pixels without blurring while simultaneously doubling Engine/Metadata shifts to preserve perfect animation stabilization.
                   6. Symmetrizer (Mirroring):
                      - Automatically duplicate, horizontally flip, and mathematically invert the metadata (ox, oy, x_shifts) of one direction into another.")
                   7. To build requirements:
                   -pip install pyside6 opencv-python numpy pillow
