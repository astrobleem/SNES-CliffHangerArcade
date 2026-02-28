#!/usr/bin/env python3
"""
generate_msu_data_cliff.py - Generate MSU-1 .msu video data for Cliff Hanger Arcade

Cliff Hanger uses a single .m2v video file and .ogg audio file (unlike Dragon's
Lair which has 204 segments). The video/audio filenames are read from cliff/cliff.txt.

Pipeline:
1. Parse cliff/cliff.txt to discover video/audio filenames
2. Parse chapter XMLs for frame ranges (laserdisc frame numbers)
3. Extract video frames per chapter (ffmpeg, CPU decode)
   - 29.97fps source -> fps=24000/1001 rate conversion
   - yadif deinterlace -> trim by time -> scale 256x192 -> full-color 24-bit PNG
4. Convert frames to SNES tiles/tilemap/palette (per-tile Floyd-Steinberg dithering)
   - K-means clusters 8x8 tiles into 8 groups by mean color
   - Builds 15-color sub-palette per cluster (120 unique colors vs old 15)
   - Floyd-Steinberg error diffusion across tile boundaries for smooth gradients
   - reduce_tiles() merges down to 384 (VRAM limit at 4BPP)
5. Package into .msu file (msu1blockwriter.py)
6. Extract audio per chapter from cliff.ogg -> MSU-1 PCM format

Usage (from project root, in WSL or Windows):
  python3 tools/generate_msu_data_cliff.py [--workers N] [--chapter NAME] [--skip-extract] [--skip-convert] [--skip-package]

Source files (place in cliff/ directory):
  cliff/cliff.m2v   - Single MPEG-2 video file
  cliff/cliff.ogg   - Single Ogg Vorbis audio file
  cliff/cliff.txt   - Frame index (maps laserdisc frame number to .m2v filename)
"""

import os
import sys

# CRITICAL: Set BLAS to single-threaded BEFORE importing numpy.
# reduce_tiles() uses numpy matrix multiplication (pixels @ pixels.T) which calls
# multi-threaded BLAS internally. When multiple Python threads in ThreadPoolExecutor
# call BLAS concurrently, the shared BLAS thread pool corrupts results.
os.environ['OPENBLAS_NUM_THREADS'] = '1'
os.environ['MKL_NUM_THREADS'] = '1'
os.environ['OMP_NUM_THREADS'] = '1'

import xml.dom.minidom
import subprocess
import concurrent.futures
import time
import glob
import argparse
import shutil
import struct
import numpy as np
from PIL import Image

# ---------- Configuration ----------
from paths import PROJECT_ROOT, BUILD_DIR, TOOLS_DIR, DISTRIBUTION, CLIFF_DIR, CLIFF_FRAMEFILE, FFMPEG

PROJECT_DIR = str(PROJECT_ROOT)
EVENTS_DIR = os.path.join(PROJECT_DIR, 'data', 'events')
CHAPTERS_DIR = os.path.join(PROJECT_DIR, 'data', 'chapters')
FRAMES_DIR = os.path.join(PROJECT_DIR, 'data', 'videos', 'frames')
SUPERFAMICONV = os.path.join(str(TOOLS_DIR), 'superfamiconv', 'superfamiconv.exe')
MSU_WRITER = os.path.join(str(TOOLS_DIR), 'msu1blockwriter.py')


def parse_cliff_framefile(framefile_path=None):
    """Parse cliff.txt to discover the video and audio filenames.

    cliff.txt format:
      Line 1: relative directory (usually '.')
      Subsequent non-blank lines: <frame_number> <filename.m2v>

    Returns (video_path, audio_path) as absolute paths in the cliff/ directory.
    The audio filename is derived by replacing .m2v with .ogg.
    """
    if framefile_path is None:
        framefile_path = str(CLIFF_FRAMEFILE)

    cliff_dir = os.path.dirname(os.path.abspath(framefile_path))

    with open(framefile_path, 'r') as f:
        lines = f.readlines()

    # First line is relative directory (usually '.')
    relative_dir = lines[0].strip() if lines else '.'
    content_dir = os.path.normpath(os.path.join(cliff_dir, relative_dir))

    # Find the first entry line: <frame_number> <filename>
    m2v_filename = None
    for line in lines[1:]:
        line = line.strip()
        if not line:
            continue
        parts = line.split(None, 1)
        if len(parts) == 2:
            m2v_filename = parts[1].strip()
            break

    if not m2v_filename:
        raise FileNotFoundError(
            f"No video filename found in {framefile_path}. "
            f"Expected format: '<frame_number> <filename.m2v>'"
        )

    video_path = os.path.join(content_dir, m2v_filename)
    # Derive audio filename by replacing .m2v extension with .ogg
    base = os.path.splitext(m2v_filename)[0]
    audio_path = os.path.join(content_dir, base + '.ogg')

    return video_path, audio_path


# Discover video/audio paths from cliff.txt (deferred to main() for error handling)
DEFAULT_CLIFF_DIR = str(CLIFF_DIR)

# Cliff Hanger video parameters
SOURCE_FPS = 29.97       # Source video fps (NTSC laserdisc)
FPS = 24                 # MSU-1 playback fps (integer, passed to msu1blockwriter)
BPP = 4
PALETTES = 8             # 8 sub-palettes per frame (120 unique colors)
MAX_COLORS = PALETTES * (2 ** BPP)  # 8 * 16 = 128 colors
MAX_TILES = 384          # VRAM tile buffer: $3000 bytes = 384 tiles at 4BPP
FRAME_WIDTH = 256
FRAME_HEIGHT = 192
TILEMAP_WIDTH = 32       # tiles per row (256 / 8)
TILEMAP_HEIGHT = 24      # tiles per column (192 / 8)
TILEMAP_TARGET_SIZE = TILEMAP_WIDTH * TILEMAP_HEIGHT * 2  # 1536 bytes
AUDIO_SAMPLE_RATE = 44100
AUDIO_CHANNELS = 2
MSU1_AUDIO_HEADER = b"MSU1" + struct.pack('<I', 0)  # "MSU1" + loop point (0 = no loop)

# Output paths
ROM_NAME = 'CliffHangerArcade'
MSU_TITLE = "CLIFF HANGER ARCADE"
OUTPUT_MSU = os.path.join(str(BUILD_DIR), f'{ROM_NAME}.msu')
FINAL_MSU_PATH = os.path.join(str(DISTRIBUTION), f'{ROM_NAME}.msu')


# ---------- Path conversion ----------
_is_wsl = None
def is_wsl():
    global _is_wsl
    if _is_wsl is None:
        try:
            with open('/proc/version', 'r') as f:
                _is_wsl = 'microsoft' in f.read().lower()
        except FileNotFoundError:
            _is_wsl = False
    return _is_wsl


def to_win_path(path):
    """Convert WSL path to Windows path for Windows executables."""
    if not is_wsl():
        return path
    path = os.path.abspath(path)
    if path.startswith('/mnt/'):
        parts = path[5:]
        drive = parts[0].upper()
        rest = parts[1:]
        return drive + ':' + rest.replace('/', '\\')
    return path


# ---------- XML Parsing ----------
def parse_time(element):
    """Parse a timestart/timeend element into milliseconds.

    Cliff Hanger XMLs use 'second' as the attribute name (not 'sec').
    """
    return (int(element.getAttribute('min')) * 60 * 1000 +
            int(element.getAttribute('second')) * 1000 +
            int(element.getAttribute('ms')))


def parse_chapter_xml(xml_path):
    """Parse a chapter XML and return timing info with laserdisc frame numbers.

    Returns dict with: name, timestart_ms, timeend_ms, duration_ms,
                        start_frame, end_frame
    """
    with open(xml_path, 'rb') as f:
        dom = xml.dom.minidom.parseString(f.read())
    chapter = dom.getElementsByTagName('chapter')[0]
    # Get the chapter's own timeline (not nested event timelines)
    timeline = [t for t in chapter.getElementsByTagName('timeline')
                if t.parentNode == chapter][0]
    timestart_el = timeline.getElementsByTagName('timestart')[0]
    timeend_el = timeline.getElementsByTagName('timeend')[0]
    timestart = parse_time(timestart_el)
    timeend = parse_time(timeend_el)

    # Read laserdisc frame attributes (Cliff Hanger stores absolute frame numbers)
    start_frame = None
    end_frame = None
    if timestart_el.hasAttribute('frame'):
        start_frame = int(timestart_el.getAttribute('frame'))
    if timeend_el.hasAttribute('frame'):
        end_frame = int(timeend_el.getAttribute('frame'))

    return {
        'name': chapter.getAttribute('name'),
        'timestart_ms': timestart,
        'timeend_ms': timeend,
        'duration_ms': max(0, timeend - timestart),
        'start_frame': start_frame,
        'end_frame': end_frame,
    }


# ---------- ffmpeg helpers ----------
def get_ffmpeg():
    """Return (exe_path, needs_win_paths).

    Uses the FFMPEG path from paths.py (configurable via project.conf or
    FFMPEG env var). If the resolved path is a Windows executable accessed
    from WSL, needs_win_paths is True.
    """
    ffmpeg_path = FFMPEG
    needs_win = is_wsl() and (ffmpeg_path.endswith('.exe') or '\\' in ffmpeg_path)
    return ffmpeg_path, needs_win


def format_time(ms):
    """Format milliseconds as HH:MM:SS.mmm for ffmpeg -t flag."""
    return "%02d:%02d:%02d.%03d" % (0, ms // 60000, (ms % 60000) // 1000, ms % 1000)


# ---------- Frame Extraction ----------
def extract_chapter_frames(chapter_info, chapter_dir, video_path):
    """Extract video frames from cliff.m2v for a single chapter.

    Seeks to the chapter's start frame position (converted to time offset)
    and extracts frames for the chapter's duration. Uses CPU-only decode
    with yadif deinterlace and fps conversion.

    Returns frame count on success, 0 if skipped, or -1 on error.
    """
    if chapter_info['duration_ms'] <= 0:
        return 0

    start_frame = chapter_info.get('start_frame')
    end_frame = chapter_info.get('end_frame')
    if start_frame is None or end_frame is None:
        return 0  # No frame info, skip

    if end_frame <= start_frame:
        return 0

    # Convert laserdisc frame numbers to time offsets
    # Source is 29.97fps (NTSC)
    offset_seconds = start_frame / SOURCE_FPS
    duration_s = (end_frame - start_frame) / SOURCE_FPS

    ffmpeg_path, needs_win_paths = get_ffmpeg()

    if needs_win_paths:
        out_pattern = to_win_path(os.path.join(chapter_dir, "video_%06d.gfx_video.png"))
        vid_path = to_win_path(video_path)
    else:
        out_pattern = os.path.join(chapter_dir, "video_%06d.gfx_video.png")
        vid_path = video_path

    # Filter chain:
    # 1. yadif: deinterlace 29.97i -> progressive
    # 2. fps=24000/1001: rate conversion to ~23.976fps MSU-1 playback rate
    # 3. trim: select the frame range by time offset
    # 4. setpts: reset timestamps after trim
    # 5. scale: resize to SNES resolution (256x192)
    # Output: full-color 24-bit RGB PNGs (dithering done in Python pipeline)
    filter_str = (
        f'yadif,fps=24000/1001,'
        f'trim=start={offset_seconds:.6f}:duration={duration_s:.6f},'
        f'setpts=PTS-STARTPTS,'
        f'scale={FRAME_WIDTH}:{FRAME_HEIGHT}'
    )

    cmd = [
        ffmpeg_path, '-y',
        '-i', vid_path,
        '-vf', filter_str,
        '-f', 'image2',
        out_pattern
    ]

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
        if result.returncode != 0:
            print(f"    ffmpeg error: {result.stderr[-300:] if result.stderr else 'unknown'}")
            return -1
    except subprocess.TimeoutExpired:
        return -1

    frame_count = len(glob.glob(os.path.join(chapter_dir, "video_*.gfx_video.png")))

    # Copy frames to the per-chapter visual debugging directory
    frames_debug_dir = os.path.join(FRAMES_DIR, chapter_info['name'])
    if frame_count > 0:
        os.makedirs(frames_debug_dir, exist_ok=True)
        for png in glob.glob(os.path.join(chapter_dir, "video_*.gfx_video.png")):
            shutil.copy2(png, os.path.join(frames_debug_dir, os.path.basename(png)))

    return frame_count


# ---------- Audio Extraction ----------
def extract_chapter_audio(chapter_info, chapter_dir, audio_path):
    """Extract audio from cliff.ogg for a single chapter.

    Seeks to the chapter's start time and extracts the chapter's duration,
    converting to MSU-1 PCM format (44100 Hz stereo 16-bit LE with MSU1 header).

    Returns True on success, False on error/skip.
    """
    if chapter_info['duration_ms'] <= 0:
        return False

    start_frame = chapter_info.get('start_frame')
    end_frame = chapter_info.get('end_frame')
    if start_frame is None or end_frame is None:
        return False

    if end_frame <= start_frame:
        return False

    # Convert frame numbers to time
    offset_seconds = start_frame / SOURCE_FPS
    duration_ms = chapter_info['duration_ms']
    dur = format_time(duration_ms)

    ffmpeg_path, needs_win_paths = get_ffmpeg()

    if needs_win_paths:
        aud_path = to_win_path(audio_path)
    else:
        aud_path = audio_path

    raw_audio_path = os.path.join(chapter_dir, "sfx_video.raw")
    pcm_output_path = os.path.join(chapter_dir, "sfx_video.pcm")

    if needs_win_paths:
        raw_out = to_win_path(raw_audio_path)
    else:
        raw_out = raw_audio_path

    # Use double-seeking for .ogg files (coarse seek + precise seek)
    pre_seek_s = max(0, offset_seconds - 5)
    precise_offset_s = offset_seconds - pre_seek_s

    cmd = [
        ffmpeg_path, '-y',
        '-ss', f'{pre_seek_s:.3f}',
        '-i', aud_path,
        '-ss', f'{precise_offset_s:.3f}',
        '-t', dur,
        '-vn',
        '-ar', str(AUDIO_SAMPLE_RATE),
        '-ac', str(AUDIO_CHANNELS),
        '-f', 's16le',
        '-acodec', 'pcm_s16le',
        raw_out
    ]

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        if result.returncode != 0:
            return False
    except subprocess.TimeoutExpired:
        return False

    try:
        with open(raw_audio_path, 'rb') as f:
            raw_pcm = f.read()
        with open(pcm_output_path, 'wb') as f:
            f.write(MSU1_AUDIO_HEADER)
            f.write(raw_pcm)
        os.remove(raw_audio_path)
        return True
    except IOError:
        return False


# ---------- Tile Conversion ----------
def pad_tilemap(tilemap_file):
    """Pad tilemap to target size for SNES compatibility.

    256x192 = 32x24 tiles = 1536 bytes. Pad with zeros if shorter.
    """
    with open(tilemap_file, 'rb') as f:
        data = f.read()
    if len(data) < TILEMAP_TARGET_SIZE:
        with open(tilemap_file, 'wb') as f:
            f.write(data)
            f.write(b'\x00' * (TILEMAP_TARGET_SIZE - len(data)))


def read_snes_palette(palette_file):
    """Read SNES BGR555 palette file and return (N, 3) float32 RGB array.

    SNES color format: 0BBBBBGG GGGRRRRR (little-endian 16-bit)
    Returns RGB values scaled to 0.0-255.0 for distance computation.
    """
    with open(palette_file, 'rb') as f:
        data = f.read()
    num_colors = len(data) // 2
    palette = np.zeros((max(16, num_colors), 3), dtype=np.float32)
    for i in range(num_colors):
        bgr555 = struct.unpack_from('<H', data, i * 2)[0]
        palette[i, 0] = (bgr555 & 0x1F) * (255.0 / 31.0)         # R
        palette[i, 1] = ((bgr555 >> 5) & 0x1F) * (255.0 / 31.0)  # G
        palette[i, 2] = ((bgr555 >> 10) & 0x1F) * (255.0 / 31.0) # B
    return palette


def rgb_to_bgr555(r, g, b):
    """Convert 8-bit RGB to SNES BGR555 (16-bit value)."""
    r5 = int(round(r * 31.0 / 255.0)) & 0x1F
    g5 = int(round(g * 31.0 / 255.0)) & 0x1F
    b5 = int(round(b * 31.0 / 255.0)) & 0x1F
    return r5 | (g5 << 5) | (b5 << 10)


def bgr555_to_rgb_float(bgr555):
    """Convert BGR555 to (R, G, B) as floats in 0-255 range."""
    r = (bgr555 & 0x1F) * (255.0 / 31.0)
    g = ((bgr555 >> 5) & 0x1F) * (255.0 / 31.0)
    b = ((bgr555 >> 10) & 0x1F) * (255.0 / 31.0)
    return (r, g, b)


def simple_kmeans(data, k, max_iter=20):
    """K-means++ clustering on (N, D) float32 array. Returns (labels, centers)."""
    N = data.shape[0]
    if N <= k:
        labels = np.arange(N, dtype=np.int32)
        centers = data.copy()
        # Pad with duplicates if fewer points than clusters
        if N < k:
            centers = np.vstack([centers, np.tile(centers[0], (k - N, 1))])
            labels = np.arange(N, dtype=np.int32)
        return labels, centers

    rng = np.random.RandomState(42)

    # K-means++ initialization
    centers = np.empty((k, data.shape[1]), dtype=data.dtype)
    idx = rng.randint(N)
    centers[0] = data[idx]

    for c in range(1, k):
        dists = np.min(np.sum((data[:, None, :] - centers[None, :c, :]) ** 2, axis=2), axis=1)
        probs = dists / (dists.sum() + 1e-10)
        idx = rng.choice(N, p=probs)
        centers[c] = data[idx]

    for _ in range(max_iter):
        dists = np.sum((data[:, None, :] - centers[None, :, :]) ** 2, axis=2)
        labels = np.argmin(dists, axis=1).astype(np.int32)

        new_centers = np.empty_like(centers)
        for c in range(k):
            mask = labels == c
            if mask.any():
                new_centers[c] = data[mask].mean(axis=0)
            else:
                new_centers[c] = centers[c]

        if np.allclose(new_centers, centers):
            break
        centers = new_centers

    return labels, centers


def encode_tiles_4bpp(pixel_indices, tile_palettes, width, height):
    """Encode pixel index grid to SNES 4BPP tile data + tilemap.

    pixel_indices: (H, W) uint8, values 0-15 (local to each tile's sub-palette)
    tile_palettes: (tiles_h, tiles_w) uint8, sub-palette number per tile
    Returns: (tile_data_bytes, tilemap_bytes)
    """
    tiles_h = height // 8
    tiles_w = width // 8

    # Reshape into tiles: (tiles_h, tiles_w, 8, 8)
    tiles = pixel_indices.reshape(tiles_h, 8, tiles_w, 8)
    tiles = tiles.transpose(0, 2, 1, 3)

    tile_dict = {}
    tile_data_list = []
    tilemap = np.zeros(tiles_h * tiles_w, dtype=np.uint16)

    for tr in range(tiles_h):
        for tc in range(tiles_w):
            tile = tiles[tr, tc]  # (8, 8) uint8

            # Encode SNES 4BPP bitplane format
            encoded = bytearray(32)
            for row in range(8):
                bp0 = bp1 = bp2 = bp3 = 0
                for px in range(8):
                    idx = int(tile[row, px])
                    bit = 7 - px
                    bp0 |= ((idx >> 0) & 1) << bit
                    bp1 |= ((idx >> 1) & 1) << bit
                    bp2 |= ((idx >> 2) & 1) << bit
                    bp3 |= ((idx >> 3) & 1) << bit
                encoded[2 * row] = bp0
                encoded[2 * row + 1] = bp1
                encoded[16 + 2 * row] = bp2
                encoded[16 + 2 * row + 1] = bp3

            encoded_bytes = bytes(encoded)
            if encoded_bytes not in tile_dict:
                tile_dict[encoded_bytes] = len(tile_data_list)
                tile_data_list.append(encoded_bytes)

            tile_idx = tile_dict[encoded_bytes]
            pal_num = int(tile_palettes[tr, tc])
            tilemap[tr * tiles_w + tc] = tile_idx | (pal_num << 10)

    tile_data = b''.join(tile_data_list)
    tilemap_bytes = tilemap.astype('<u2').tobytes()
    return tile_data, tilemap_bytes


def per_tile_palette_optimize(png_path, pal_file, tile_file, map_file):
    """Convert full-color PNG to SNES tiles with 8 sub-palettes + Floyd-Steinberg dithering.

    Algorithm:
    1. Load PNG, quantize pixels to BGR555 color space
    2. Cluster 8x8 tiles into 8 groups by mean color (K-means)
    3. Build 15-color sub-palettes per cluster (color 0 reserved = $0000)
    4. Build BGR555 lookup tables for O(1) nearest-color per sub-palette
    5. Floyd-Steinberg dithering across entire image (error flows across tile boundaries)
    6. Encode to SNES 4BPP tiles + tilemap + palette
    """
    img = Image.open(png_path).convert('RGB')
    rgb = np.array(img, dtype=np.float32)  # (H, W, 3)
    H, W = rgb.shape[:2]
    tiles_h, tiles_w = H // 8, W // 8

    # Quantize to BGR555 (what SNES can actually display)
    rgb5 = np.round(rgb * 31.0 / 255.0).clip(0, 31).astype(np.uint8)  # (H, W, 3) 5-bit
    rgb_q = rgb5.astype(np.float32) * (255.0 / 31.0)  # back to float for processing

    # Per-tile mean colors for clustering
    tile_blocks = rgb_q.reshape(tiles_h, 8, tiles_w, 8, 3).transpose(0, 2, 1, 3, 4)
    tile_means = tile_blocks.mean(axis=(2, 3))  # (tiles_h, tiles_w, 3)
    flat_means = tile_means.reshape(-1, 3)

    labels, _centers = simple_kmeans(flat_means, PALETTES)
    tile_labels = labels.reshape(tiles_h, tiles_w)

    # Build sub-palettes: collect unique BGR555 values per cluster, reduce to 15
    sub_palettes = []  # list of 8 lists of 16 BGR555 values
    sub_palette_rgb = np.zeros((PALETTES, 16, 3), dtype=np.float32)

    for p in range(PALETTES):
        mask = tile_labels == p
        positions = np.argwhere(mask)

        bgr555_set = set()
        for tr, tc in positions:
            block = rgb5[tr * 8:(tr + 1) * 8, tc * 8:(tc + 1) * 8]  # (8,8,3) uint8
            for y in range(8):
                for x in range(8):
                    r5, g5, b5 = int(block[y, x, 0]), int(block[y, x, 1]), int(block[y, x, 2])
                    bgr = r5 | (g5 << 5) | (b5 << 10)
                    bgr555_set.add(bgr)

        bgr555_set.discard(0)  # color 0 is reserved (transparent/black)

        if len(bgr555_set) <= 15:
            colors = sorted(bgr555_set)
        else:
            # K-means reduction to 15 representative colors
            unique_list = sorted(bgr555_set)
            unique_rgb = np.array([bgr555_to_rgb_float(c) for c in unique_list], dtype=np.float32)
            clabels, ccenters = simple_kmeans(unique_rgb, 15)
            colors = []
            for c in ccenters:
                colors.append(rgb_to_bgr555(int(round(c[0])), int(round(c[1])), int(round(c[2]))))
            # Deduplicate (rounding might produce duplicates)
            colors = sorted(set(colors))
            if 0 in colors:
                colors.remove(0)
            colors = colors[:15]

        full_palette = [0] + colors
        while len(full_palette) < 16:
            full_palette.append(0)
        sub_palettes.append(full_palette)

        for ci, bgr in enumerate(full_palette):
            sub_palette_rgb[p, ci] = bgr555_to_rgb_float(bgr)

    # Build BGR555 LUTs for O(1) nearest-color lookup per sub-palette
    all_bgr = np.arange(32768, dtype=np.uint16)
    all_r = (all_bgr & 0x1F).astype(np.float32) * (255.0 / 31.0)
    all_g = ((all_bgr >> 5) & 0x1F).astype(np.float32) * (255.0 / 31.0)
    all_b = ((all_bgr >> 10) & 0x1F).astype(np.float32) * (255.0 / 31.0)
    all_rgb_lut = np.stack([all_r, all_g, all_b], axis=1)  # (32768, 3)

    lut_index = np.zeros((PALETTES, 32768), dtype=np.uint8)
    lut_rgb = np.zeros((PALETTES, 32768, 3), dtype=np.float32)

    for p in range(PALETTES):
        pal_rgb = sub_palette_rgb[p]  # (16, 3)
        diffs = all_rgb_lut[:, None, :] - pal_rgb[None, :, :]  # (32768, 16, 3)
        dists = np.sum(diffs * diffs, axis=2)  # (32768, 16)
        nearest = np.argmin(dists, axis=1)
        lut_index[p] = nearest.astype(np.uint8)
        lut_rgb[p] = pal_rgb[nearest]

    # Floyd-Steinberg dithering (sequential scan, error flows across tile boundaries)
    # Convert numpy arrays to Python lists for fast element access in the tight loop
    lut_idx_lists = [lut_index[p].tolist() for p in range(PALETTES)]
    lut_r_lists = [lut_rgb[p, :, 0].tolist() for p in range(PALETTES)]
    lut_g_lists = [lut_rgb[p, :, 1].tolist() for p in range(PALETTES)]
    lut_b_lists = [lut_rgb[p, :, 2].tolist() for p in range(PALETTES)]
    tile_labels_list = tile_labels.tolist()

    # Error buffer: padded +1 col on each side, +1 row on bottom
    err_r = [[0.0] * (W + 2) for _ in range(H + 1)]
    err_g = [[0.0] * (W + 2) for _ in range(H + 1)]
    err_b = [[0.0] * (W + 2) for _ in range(H + 1)]

    output = np.zeros((H, W), dtype=np.uint8)

    # Pre-extract source image channels as Python lists
    src_r = rgb_q[:, :, 0].tolist()
    src_g = rgb_q[:, :, 1].tolist()
    src_b = rgb_q[:, :, 2].tolist()

    for y in range(H):
        xp1 = 1  # error buffer x offset (pixel x=0 maps to err index 1)
        tr = y >> 3  # y // 8
        er_row = err_r[y]
        eg_row = err_g[y]
        eb_row = err_b[y]
        er_next = err_r[y + 1]
        eg_next = err_g[y + 1]
        eb_next = err_b[y + 1]
        sr_row = src_r[y]
        sg_row = src_g[y]
        sb_row = src_b[y]

        for x in range(W):
            ex = x + xp1  # error buffer index for this pixel

            # Accumulated color = original + diffused error
            ar = sr_row[x] + er_row[ex]
            ag = sg_row[x] + eg_row[ex]
            ab = sb_row[x] + eb_row[ex]

            # Clamp to [0, 255]
            if ar < 0.0: ar = 0.0
            elif ar > 255.0: ar = 255.0
            if ag < 0.0: ag = 0.0
            elif ag > 255.0: ag = 255.0
            if ab < 0.0: ab = 0.0
            elif ab > 255.0: ab = 255.0

            # Quantize accumulated color to BGR555 for LUT lookup
            r5 = int(ar * 31.0 / 255.0 + 0.5)
            g5 = int(ag * 31.0 / 255.0 + 0.5)
            b5 = int(ab * 31.0 / 255.0 + 0.5)
            if r5 > 31: r5 = 31
            if g5 > 31: g5 = 31
            if b5 > 31: b5 = 31
            bgr = r5 | (g5 << 5) | (b5 << 10)

            # Look up tile's sub-palette
            p = tile_labels_list[tr][x >> 3]

            # Nearest color via LUT
            idx = lut_idx_lists[p][bgr]
            nr = lut_r_lists[p][bgr]
            ng = lut_g_lists[p][bgr]
            nb = lut_b_lists[p][bgr]

            output[y, x] = idx

            # Quantization error
            qr = ar - nr
            qg = ag - ng
            qb = ab - nb

            # Distribute error (Floyd-Steinberg: 7/16, 3/16, 5/16, 1/16)
            # Right (x+1)
            er_row[ex + 1] += qr * 0.4375
            eg_row[ex + 1] += qg * 0.4375
            eb_row[ex + 1] += qb * 0.4375
            # Bottom-left (x-1, y+1)
            er_next[ex - 1] += qr * 0.1875
            eg_next[ex - 1] += qg * 0.1875
            eb_next[ex - 1] += qb * 0.1875
            # Bottom (x, y+1)
            er_next[ex] += qr * 0.3125
            eg_next[ex] += qg * 0.3125
            eb_next[ex] += qb * 0.3125
            # Bottom-right (x+1, y+1)
            er_next[ex + 1] += qr * 0.0625
            eg_next[ex + 1] += qg * 0.0625
            eb_next[ex + 1] += qb * 0.0625

    # Encode to SNES format
    tile_data, tilemap_data = encode_tiles_4bpp(output, tile_labels.astype(np.uint8), W, H)

    # Write palette: 8 sub-palettes x 16 colors x 2 bytes = 256 bytes
    pal_bytes = bytearray(PALETTES * 16 * 2)
    for p in range(PALETTES):
        for ci in range(16):
            bgr = sub_palettes[p][ci]
            offset = (p * 16 + ci) * 2
            pal_bytes[offset] = bgr & 0xFF
            pal_bytes[offset + 1] = (bgr >> 8) & 0xFF

    with open(pal_file, 'wb') as f:
        f.write(pal_bytes)
    with open(tile_file, 'wb') as f:
        f.write(tile_data)
    with open(map_file, 'wb') as f:
        f.write(tilemap_data)


def decode_tiles_4bpp_rgb(tiles_raw, palette_rgb, tile_pal_offsets=None):
    """Decode SNES 4BPP tiles to RGB values using the frame's actual palette.

    tiles_raw: (N, 32) uint8 array of raw SNES 4BPP tile data
    palette_rgb: (C, 3) float32 array of RGB values (C=16 single, C=128 multi)
    tile_pal_offsets: optional (N,) uint16, per-tile palette base offset
                      (palette_num * 16). If None, all tiles use palette 0.
    Returns: (N, 192) float32 array (64 pixels x 3 RGB channels)

    SNES 4BPP tile format (32 bytes per 8x8 tile):
      Bytes  0-15: bitplanes 0,1 interleaved by row (2 bytes/row x 8 rows)
      Bytes 16-31: bitplanes 2,3 interleaved by row (2 bytes/row x 8 rows)
    Each pixel's 4-bit color index = bp0 | (bp1<<1) | (bp2<<2) | (bp3<<3)
    """
    N = tiles_raw.shape[0]
    pixel_indices = np.zeros((N, 8, 8), dtype=np.uint8)
    for row in range(8):
        bp0 = tiles_raw[:, 2 * row].astype(np.uint16)
        bp1 = tiles_raw[:, 2 * row + 1].astype(np.uint16)
        bp2 = tiles_raw[:, 16 + 2 * row].astype(np.uint16)
        bp3 = tiles_raw[:, 16 + 2 * row + 1].astype(np.uint16)
        for px in range(8):
            bit = 7 - px
            pixel_indices[:, row, px] = (
                ((bp0 >> bit) & 1) |
                (((bp1 >> bit) & 1) << 1) |
                (((bp2 >> bit) & 1) << 2) |
                (((bp3 >> bit) & 1) << 3)
            ).astype(np.uint8)
    flat_indices = pixel_indices.reshape(N, 64)

    if tile_pal_offsets is not None:
        # Offset indices by per-tile sub-palette base for multi-palette lookup
        flat_indices = flat_indices.astype(np.uint16) + tile_pal_offsets[:, None]

    rgb = palette_rgb[flat_indices]  # (N, 64, 3)
    return rgb.reshape(N, 192)


def reduce_tiles(tile_file, tilemap_file, palette_file, max_tiles=MAX_TILES):
    """Reduce tile count to max_tiles using global greedy merge in RGB color space.

    SNES VRAM budget is $3000 bytes = 384 tiles at 4BPP. Video frames at
    256x192 can have up to 768 unique tiles (32x24 grid). This function finds
    the most similar tile pairs across the ENTIRE image and merges them,
    distributing quality loss evenly rather than concentrating it in specific rows.

    Uses L2 distance on actual RGB color values (decoded through the frame's
    palette) for accurate visual similarity matching. Only merges tiles that
    share the same sub-palette to preserve color accuracy.
    """
    bytes_per_tile = 8 * BPP  # 32 for 4BPP

    with open(tile_file, 'rb') as f:
        tile_data = f.read()
    num_tiles = len(tile_data) // bytes_per_tile
    if num_tiles <= max_tiles:
        return  # nothing to do

    tiles = np.frombuffer(tile_data, dtype=np.uint8).reshape(num_tiles, bytes_per_tile)

    palette_rgb = read_snes_palette(palette_file)

    # Read tilemap to get per-tile palette assignment
    with open(tilemap_file, 'rb') as f:
        tilemap_raw = f.read()
    tilemap_arr = np.frombuffer(tilemap_raw, dtype=np.uint16).copy()
    tm_tile_indices = tilemap_arr & 0x3ff
    tm_pal_bits = (tilemap_arr >> 10) & 0x7

    # Determine palette for each unique tile (from first tilemap reference)
    tile_palettes = np.zeros(num_tiles, dtype=np.uint8)
    seen = np.zeros(num_tiles, dtype=bool)
    for i in range(len(tilemap_arr)):
        ti = int(tm_tile_indices[i])
        if ti < num_tiles and not seen[ti]:
            tile_palettes[ti] = tm_pal_bits[i]
            seen[ti] = True

    # Decode tiles to RGB using per-tile sub-palettes
    tile_pal_offsets = tile_palettes.astype(np.uint16) * 16
    pixels = decode_tiles_4bpp_rgb(tiles, palette_rgb, tile_pal_offsets)  # (N, 192)

    # Compute pairwise L2 squared distance matrix using dot product trick:
    # ||A-B||^2 = ||A||^2 + ||B||^2 - 2*A.B
    sq_norms = np.sum(pixels * pixels, axis=1)  # (N,)
    dot_products = pixels @ pixels.T             # (N, N) via BLAS
    dist = sq_norms[:, None] + sq_norms[None, :] - 2 * dot_products

    # Block cross-palette merges (set distance to infinity)
    same_pal = tile_palettes[:, None] == tile_palettes[None, :]
    dist = np.where(same_pal, dist, np.inf)

    # Get all unique pairs (i < j) sorted by distance
    rows_idx, cols_idx = np.triu_indices(num_tiles, k=1)
    pair_dists = dist[rows_idx, cols_idx]
    sort_order = np.argsort(pair_dists)

    # Greedy merge: iterate through closest pairs, merge when both alive
    to_remove = num_tiles - max_tiles
    alive = set(range(num_tiles))
    merge_target = list(range(num_tiles))  # merge_target[i] = tile that i maps to
    removed = 0

    for idx in sort_order:
        if removed >= to_remove:
            break
        i = int(rows_idx[idx])
        j = int(cols_idx[idx])
        if i not in alive or j not in alive:
            continue
        if not np.isfinite(dist[i, j]):
            break  # only inf-distance pairs remain
        # Remove the higher-indexed tile, keep the lower
        alive.discard(j)
        merge_target[j] = i
        removed += 1

    # Resolve transitive merges (j->i, but i may also have been merged later)
    for idx in range(num_tiles):
        target = merge_target[idx]
        while merge_target[target] != target:
            target = merge_target[target]
        merge_target[idx] = target

    # Re-index surviving tiles to contiguous 0..(max_tiles-1)
    alive_sorted = sorted(alive)
    reindex = {}
    for new_i, old_i in enumerate(alive_sorted):
        reindex[old_i] = new_i

    # Build final remap: old tile index -> new contiguous index
    final_remap = np.array([reindex[merge_target[i]] for i in range(num_tiles)],
                           dtype=np.uint16)

    # Update tilemap (preserve palette/flip flags)
    tile_indices = tilemap_arr & 0x3ff
    flags = tilemap_arr & 0xfc00
    new_indices = final_remap[tile_indices]
    tilemap_arr = flags | new_indices

    # Write reduced tiles (only surviving tiles, in original order)
    new_tile_data = tiles[alive_sorted].tobytes()
    with open(tile_file, 'wb') as f:
        f.write(new_tile_data)

    # Write updated tilemap
    with open(tilemap_file, 'wb') as f:
        f.write(tilemap_arr.tobytes())


def convert_frame_superfamiconv(png_path):
    """Convert one PNG frame to SNES tiles/tilemap/palette.

    Uses per-tile 8-sub-palette optimization with Floyd-Steinberg dithering
    for smooth gradients and high color fidelity (120 unique colors).
    """
    base = png_path[:-4]  # Remove .png
    pal_file = base + '.palette'
    tile_file = base + '.tiles'
    map_file = base + '.tilemap'

    try:
        per_tile_palette_optimize(png_path, pal_file, tile_file, map_file)
    except Exception as e:
        return False, str(e)

    # Reduce tiles to fit in VRAM buffer (384 max at 4BPP)
    reduce_tiles(tile_file, map_file, pal_file)

    # Pad tilemap to 32x24
    pad_tilemap(map_file)

    return True, ""


def convert_chapter_frames(chapter_dir, max_workers=4):
    """Convert all PNG frames in a chapter directory to SNES tiles."""
    pngs = sorted(glob.glob(os.path.join(chapter_dir, "*.gfx_video.png")))
    if not pngs:
        return 0

    converted = 0
    failed = 0
    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {executor.submit(convert_frame_superfamiconv, p): p for p in pngs}
        for future in concurrent.futures.as_completed(futures):
            success, err = future.result()
            if success:
                converted += 1
            else:
                failed += 1
                if failed <= 3:  # Only print first few errors
                    print(f"    WARN: Failed {os.path.basename(futures[future])}: {err}")

    return converted


# ---------- Main Pipeline ----------
def main():
    parser = argparse.ArgumentParser(
        description='Generate MSU-1 .msu video data for Cliff Hanger Arcade')
    parser.add_argument('--workers', type=int, default=8,
                        help='Parallel workers for tile conversion (default: 8)')
    parser.add_argument('--chapter', type=str,
                        help='Process a single chapter by name')
    parser.add_argument('--skip-extract', action='store_true',
                        help='Skip frame extraction (use existing PNGs)')
    parser.add_argument('--skip-convert', action='store_true',
                        help='Skip tile conversion (use existing tiles)')
    parser.add_argument('--skip-package', action='store_true',
                        help='Skip .msu packaging step')
    parser.add_argument('--skip-audio', action='store_true',
                        help='Skip audio extraction')
    parser.add_argument('--clean', action='store_true',
                        help='Remove existing video frames before extraction')
    parser.add_argument('--framefile', type=str, default=str(CLIFF_FRAMEFILE),
                        help=f'Path to cliff.txt frame index (default: {CLIFF_FRAMEFILE})')
    parser.add_argument('--video', type=str, default=None,
                        help='Path to .m2v video file (default: auto-detected from cliff.txt)')
    parser.add_argument('--audio', type=str, default=None,
                        help='Path to .ogg audio file (default: auto-detected from cliff.txt)')
    args = parser.parse_args()

    print("=" * 60)
    print("MSU-1 Video Data Generator - Cliff Hanger Arcade")
    print("=" * 60)
    print(f"Chapters dir: {CHAPTERS_DIR}")
    print(f"Output MSU:   {OUTPUT_MSU}")
    print(f"MSU title:    {MSU_TITLE}")
    print(f"Workers:      {args.workers}")
    print(f"Resolution:   {FRAME_WIDTH}x{FRAME_HEIGHT} ({TILEMAP_WIDTH}x{TILEMAP_HEIGHT} tiles)")
    print(f"Max tiles:    {MAX_TILES} (VRAM $3000 bytes at {BPP}BPP)")
    print(f"Colors:       {MAX_COLORS} ({PALETTES} sub-palettes)")

    ffmpeg_path, needs_win_paths = get_ffmpeg()
    print(f"ffmpeg:       {ffmpeg_path}")
    print(f"superfamiconv: {SUPERFAMICONV}")

    # Discover video/audio paths from cliff.txt (or use explicit overrides)
    if args.video and args.audio:
        video_path = args.video
        audio_path = args.audio
    else:
        if not os.path.exists(args.framefile):
            print(f"\nERROR: Frame index not found: {args.framefile}")
            print(f"  Place cliff.txt in the cliff/ directory.")
            sys.exit(1)
        print(f"Frame index:  {args.framefile}")
        detected_video, detected_audio = parse_cliff_framefile(args.framefile)
        video_path = args.video or detected_video
        audio_path = args.audio or detected_audio

    if not args.skip_extract:
        if not os.path.exists(video_path):
            print(f"\nERROR: Video file not found: {video_path}")
            print(f"  Place the Cliff Hanger .m2v file in the cliff/ directory.")
            sys.exit(1)
        else:
            video_size_mb = os.path.getsize(video_path) / 1024 / 1024
            print(f"Video file:   {video_path} ({video_size_mb:.1f} MB)")

    if not args.skip_audio:
        if not os.path.exists(audio_path):
            print(f"\nWARNING: Audio file not found: {audio_path}")
            print(f"  Place the Cliff Hanger .ogg file in the cliff/ directory.")
            print(f"  Audio extraction will be skipped.")
            args.skip_audio = True
        else:
            audio_size_mb = os.path.getsize(audio_path) / 1024 / 1024
            print(f"Audio file:   {audio_path} ({audio_size_mb:.1f} MB)")

    print(f"\nSource format: Single .m2v file at {SOURCE_FPS}fps")
    print()

    # Build chapter list
    if not os.path.isdir(CHAPTERS_DIR):
        print(f"ERROR: Chapters directory not found: {CHAPTERS_DIR}")
        print(f"  Run 'make' first to generate chapter data from XML events.")
        sys.exit(1)

    chapters = []
    for chapter_name in sorted(os.listdir(CHAPTERS_DIR)):
        chapter_dir = os.path.join(CHAPTERS_DIR, chapter_name)
        if not os.path.isdir(chapter_dir):
            continue
        xml_path = os.path.join(EVENTS_DIR, chapter_name + '.xml')
        if not os.path.exists(xml_path):
            continue  # Skip non-XML chapters (include files, etc.)
        if args.chapter and chapter_name != args.chapter:
            continue
        chapters.append((chapter_name, chapter_dir, xml_path))

    print(f"Found {len(chapters)} chapters to process\n")

    if not chapters:
        print("ERROR: No chapters found. Ensure data/events/ and data/chapters/ are populated.")
        sys.exit(1)

    # ========== Phase 1: Extract video frames ==========
    total_frames = 0
    extract_errors = 0
    skipped_no_frame = 0
    if not args.skip_extract:
        print("--- Phase 1: Extracting video frames (ffmpeg from cliff.m2v) ---")
        extract_start = time.time()

        for i, (name, cdir, xml) in enumerate(chapters):
            info = parse_chapter_xml(xml)
            if info['duration_ms'] <= 0:
                if (i + 1) % 50 == 0 or i == 0:
                    print(f"[{i+1:3d}/{len(chapters)}] {name}: skip (0 duration)")
                continue

            if args.clean:
                for f in glob.glob(os.path.join(cdir, "*.gfx_video.png")):
                    os.remove(f)

            existing = glob.glob(os.path.join(cdir, "*.gfx_video.png"))
            if existing and not args.clean:
                n = len(existing)
                total_frames += n
                if (i + 1) % 50 == 0 or i == len(chapters) - 1:
                    print(f"[{i+1:3d}/{len(chapters)}] {name}: {n} frames (cached)")
                continue

            if info.get('start_frame') is None or info.get('end_frame') is None:
                skipped_no_frame += 1
                if skipped_no_frame <= 10:
                    print(f"[{i+1:3d}/{len(chapters)}] {name}: SKIP (no frame attributes)")
                continue

            n = extract_chapter_frames(info, cdir, video_path)
            if n > 0:
                total_frames += n
                print(f"[{i+1:3d}/{len(chapters)}] {name}: {n} frames "
                      f"(ld frames {info['start_frame']}-{info['end_frame']})")
            elif n == 0:
                print(f"[{i+1:3d}/{len(chapters)}] {name}: 0 frames (skipped)")
            else:
                print(f"[{i+1:3d}/{len(chapters)}] {name}: EXTRACTION ERROR")
                extract_errors += 1

        extract_elapsed = time.time() - extract_start
        print(f"\nExtraction done: {total_frames} frames in {extract_elapsed:.1f}s "
              f"({extract_errors} errors, {skipped_no_frame} skipped no frame attrs)\n")
    else:
        for name, cdir, xml in chapters:
            total_frames += len(glob.glob(os.path.join(cdir, "*.gfx_video.png")))
        print(f"Skipping extraction. {total_frames} existing PNG frames found.\n")

    # ========== Phase 2: Extract audio per chapter ==========
    total_audio = 0
    audio_errors = 0
    if not args.skip_audio:
        print("--- Phase 2: Extracting audio per chapter (ffmpeg from cliff.ogg) ---")
        audio_start = time.time()

        for i, (name, cdir, xml) in enumerate(chapters):
            info = parse_chapter_xml(xml)
            if info['duration_ms'] <= 0:
                continue

            pcm_path = os.path.join(cdir, "sfx_video.pcm")
            if os.path.exists(pcm_path) and not args.clean:
                total_audio += 1
                if (i + 1) % 50 == 0 or i == len(chapters) - 1:
                    print(f"[{i+1:3d}/{len(chapters)}] {name}: audio (cached)")
                continue

            if extract_chapter_audio(info, cdir, audio_path):
                total_audio += 1
                if (i + 1) % 50 == 0 or i == len(chapters) - 1:
                    print(f"[{i+1:3d}/{len(chapters)}] {name}: audio extracted")
            else:
                audio_errors += 1
                if audio_errors <= 5:
                    print(f"[{i+1:3d}/{len(chapters)}] {name}: AUDIO ERROR")

        audio_elapsed = time.time() - audio_start
        print(f"\nAudio extraction done: {total_audio} chapters in {audio_elapsed:.1f}s "
              f"({audio_errors} errors)\n")
    else:
        for name, cdir, xml in chapters:
            if os.path.exists(os.path.join(cdir, "sfx_video.pcm")):
                total_audio += 1
        print(f"Skipping audio extraction. {total_audio} existing PCM files found.\n")

    # ========== Phase 2b: Copy PCM files to numbered output ==========
    pcm_copied = 0
    if total_audio > 0:
        print("--- Phase 2b: Copying PCM files to numbered output ---")
        build_dir = os.path.dirname(OUTPUT_MSU)
        os.makedirs(build_dir, exist_ok=True)
        final_dir = str(DISTRIBUTION)
        os.makedirs(final_dir, exist_ok=True)

        for name, cdir, xml in chapters:
            pcm_path = os.path.join(cdir, "sfx_video.pcm")
            if not os.path.exists(pcm_path):
                continue

            # Read chapter ID from chapter.id.NNN file
            id_files = [f for f in os.listdir(cdir) if f.startswith('chapter.id')]
            if not id_files:
                continue
            try:
                chapter_id = int(id_files[0].split('chapter.id')[-1].lstrip('.'))
            except ValueError:
                continue

            out_name = f"{ROM_NAME}-{chapter_id}.pcm"
            build_pcm = os.path.join(build_dir, out_name)
            shutil.copy2(pcm_path, build_pcm)
            pcm_copied += 1

            if os.path.isdir(final_dir):
                shutil.copy2(pcm_path, os.path.join(final_dir, out_name))

        print(f"Copied {pcm_copied} PCM files to {build_dir}")
        if os.path.isdir(final_dir):
            print(f"Also copied to {final_dir}")
        print()

    # ========== Phase 3: Convert frames to SNES tiles ==========
    total_converted = 0
    if not args.skip_convert:
        print(f"--- Phase 3: Converting frames to SNES tiles "
              f"(Floyd-Steinberg dithering, {args.workers} workers) ---")
        convert_start = time.time()

        for i, (name, cdir, xml) in enumerate(chapters):
            pngs = glob.glob(os.path.join(cdir, "*.gfx_video.png"))
            existing_tiles = glob.glob(os.path.join(cdir, "*.gfx_video.tiles"))

            if not pngs:
                continue

            if len(existing_tiles) == len(pngs) and not args.clean:
                total_converted += len(existing_tiles)
                if (i + 1) % 50 == 0 or i == len(chapters) - 1:
                    print(f"[{i+1:3d}/{len(chapters)}] {name}: "
                          f"{len(existing_tiles)} tiles (cached)")
                continue

            n = convert_chapter_frames(cdir, max_workers=args.workers)
            total_converted += n
            print(f"[{i+1:3d}/{len(chapters)}] {name}: {n}/{len(pngs)} converted")

        convert_elapsed = time.time() - convert_start
        print(f"\nConversion done: {total_converted} tiles in {convert_elapsed:.1f}s\n")
    else:
        for name, cdir, xml in chapters:
            total_converted += len(glob.glob(os.path.join(cdir, "*.gfx_video.tiles")))
        print(f"Skipping conversion. {total_converted} existing tile files found.\n")

    # ========== Phase 4: Package .msu file ==========
    if not args.skip_package:
        print("--- Phase 4: Packaging .msu file ---")

        # Ensure build directory exists
        os.makedirs(os.path.dirname(OUTPUT_MSU), exist_ok=True)

        cmd = [
            sys.executable, MSU_WRITER,
            '-title', MSU_TITLE,
            '-infilebase', CHAPTERS_DIR,
            '-outfile', OUTPUT_MSU,
            '-bpp', str(BPP),
            '-fps', str(FPS),
        ]
        print(f"Running: {' '.join(cmd)}")
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=3600)
        if result.returncode == 0:
            msu_size = os.path.getsize(OUTPUT_MSU) if os.path.exists(OUTPUT_MSU) else 0
            print(f"Success! MSU file: {OUTPUT_MSU} ({msu_size / 1024 / 1024:.1f} MB)")
            if result.stdout.strip():
                print(result.stdout.strip())

            # Copy to distribution folder
            final_path = os.path.normpath(FINAL_MSU_PATH)
            os.makedirs(os.path.dirname(final_path), exist_ok=True)
            shutil.copy2(OUTPUT_MSU, final_path)
            print(f"Copied to: {final_path}")
        else:
            print(f"ERROR packaging .msu:")
            print(result.stderr)
            sys.exit(1)
    else:
        print("Skipping .msu packaging.\n")

    # ========== Summary ==========
    print("\n" + "=" * 60)
    print("Done!")
    print(f"  Frames extracted: {total_frames}")
    print(f"  Audio extracted:  {total_audio}")
    print(f"  PCM files copied: {pcm_copied}")
    print(f"  Tiles converted:  {total_converted}")
    if os.path.exists(OUTPUT_MSU):
        print(f"  MSU file size:    {os.path.getsize(OUTPUT_MSU) / 1024 / 1024:.1f} MB")
    print("=" * 60)


if __name__ == '__main__':
    main()
