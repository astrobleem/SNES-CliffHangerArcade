#!/usr/bin/env python3
"""
generate_msu_data.py - Generate MSU-1 .msu video data file from dl_arcade.mp4

Pipeline:
1. Parse chapter XMLs for timing info
2. Extract video frames from MP4 per chapter (ffmpeg with CUDA GPU accel)
3. Convert frames to SNES tiles/tilemap/palette (superfamiconv)
   - Each 256x160 frame produces up to 640 unique 8x8 tiles, but SNES VRAM
     budget is 384 at 4BPP. reduce_tiles() merges the 256 most visually
     similar pairs using RGB-space L2 distance with a global greedy algorithm.
4. Package into .msu file (msu1blockwriter.py)

Usage (from project root, in WSL or Windows):
  python3 tools/generate_msu_data.py [--workers N] [--chapter NAME] [--skip-extract] [--skip-convert] [--skip-package]
"""

import os
import sys

# CRITICAL: Set BLAS to single-threaded BEFORE importing numpy.
# reduce_tiles() uses numpy matrix multiplication (pixels @ pixels.T) which calls
# multi-threaded BLAS internally. When multiple Python threads in ThreadPoolExecutor
# call BLAS concurrently, the shared BLAS thread pool corrupts results. This caused
# 63% of video frames to have scrambled tilemaps.
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

# ---------- Configuration ----------
from paths import PROJECT_ROOT, BUILD_DIR, TOOLS_DIR, DISTRIBUTION, DAPHNE_FRAMEFILE, DAPHNE_CONTENT, FFMPEG

PROJECT_DIR = str(PROJECT_ROOT)
EVENTS_DIR = os.path.join(PROJECT_DIR, 'data', 'events')
CHAPTERS_DIR = os.path.join(PROJECT_DIR, 'data', 'chapters')
SUPERFAMICONV = os.path.join(str(TOOLS_DIR), 'superfamiconv', 'superfamiconv.exe')
MSU_WRITER = os.path.join(str(TOOLS_DIR), 'msu1blockwriter.py')
DRAGON_ROAR_PCM = os.path.join(PROJECT_DIR, 'data', 'sounds', 'SuperDragonsLairArcade-900.pcm')

FPS = 24  # MSU-1 playback fps (msu1blockwriter uses integer)
SOURCE_FPS = 23.9777  # Source video fps for frame number calculation
BPP = 4
PALETTES = 2
MAX_COLORS = PALETTES * (2 ** BPP)  # 2 * 16 = 32 (two sub-palettes per frame)
MAX_TILES = 384  # VRAM tile buffer: 384 tiles at 4BPP (32 bytes/tile) = $3000 bytes
FRAME_WIDTH = 256
FRAME_HEIGHT = 160
TILEMAP_TARGET_SIZE = 1280  # 32x20 tiles * 2 bytes per entry
AUDIO_SAMPLE_RATE = 44100
AUDIO_CHANNELS = 2
MSU1_AUDIO_HEADER = b"MSU1" + struct.pack('<I', 0)  # "MSU1" + loop point (0 = no loop)

OUTPUT_MSU = os.path.join(str(BUILD_DIR), 'SuperDragonsLairArcade.msu')
FINAL_MSU_PATH = os.path.join(str(DISTRIBUTION), 'SuperDragonsLairArcade.msu')

DEFAULT_FRAMEFILE = str(DAPHNE_FRAMEFILE)
DEFAULT_CONTENT_ROOT = str(DAPHNE_CONTENT)

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

# ---------- Daphne Framefile ----------
def parse_framefile(framefile_path, content_root=None):
    """Parse Daphne framefile into sorted list of segment entries.

    Each entry: {frame, m2v_path, ogg_path, filename}
    The framefile format is: first line = relative content dir, then frame<tab>filename per line.
    """
    segments = []
    with open(framefile_path) as f:
        lines = f.readlines()

    if not content_root:
        # First line is relative path to content directory (may use backslashes)
        framefile_dir = os.path.dirname(os.path.abspath(framefile_path))
        relative_dir = lines[0].strip().replace('\\', '/')
        content_root = os.path.normpath(os.path.join(framefile_dir, relative_dir))

    for line in lines[1:]:
        line = line.strip()
        if not line:
            continue
        parts = line.split(None, 1)
        if len(parts) != 2:
            continue
        frame = int(parts[0])
        filename = parts[1].strip()

        m2v_path = os.path.join(content_root, filename)
        ogg_path = m2v_path.replace('.m2v', '.ogg')

        segments.append({
            'frame': frame,
            'm2v_path': m2v_path,
            'ogg_path': ogg_path,
            'filename': filename,
        })

    return sorted(segments, key=lambda s: s['frame'])


def find_segment(segments, target_frame):
    """Find the segment containing target_frame via binary search.

    Returns (segment_dict, offset_seconds) or (None, 0).
    offset_seconds is the time offset from the segment's start frame.
    """
    lo, hi = 0, len(segments) - 1
    result = None
    while lo <= hi:
        mid = (lo + hi) // 2
        if segments[mid]['frame'] <= target_frame:
            result = mid
            lo = mid + 1
        else:
            hi = mid - 1
    if result is not None:
        seg = segments[result]
        offset_seconds = (target_frame - seg['frame']) / 23.976
        return seg, offset_seconds
    return None, 0


# ---------- XML Parsing ----------
def parse_time(element):
    return (int(element.getAttribute('min')) * 60 * 1000 +
            int(element.getAttribute('second')) * 1000 +
            int(element.getAttribute('ms')))

def parse_chapter_xml(xml_path):
    with open(xml_path, 'rb') as f:
        dom = xml.dom.minidom.parseString(f.read())
    chapter = dom.getElementsByTagName('chapter')[0]
    # Get the chapter's own timeline (not nested event timelines)
    timeline = [t for t in chapter.getElementsByTagName('timeline')
                if t.parentNode == chapter][0]
    timestart_el = timeline.getElementsByTagName('timestart')[0]
    timestart = parse_time(timestart_el)
    timeend = parse_time(timeline.getElementsByTagName('timeend')[0])

    # Read optional laserdisc frame attribute
    start_frame = None
    if timestart_el.hasAttribute('frame'):
        start_frame = int(timestart_el.getAttribute('frame'))

    return {
        'name': chapter.getAttribute('name'),
        'timestart_ms': timestart,
        'timeend_ms': timeend,
        'duration_ms': max(0, timeend - timestart),
        'start_frame': start_frame,
    }

# ---------- Frame Extraction ----------
def format_time(ms):
    return "%02d:%02d:%02d.%03d" % (0, ms // 60000, (ms % 60000) // 1000, ms % 1000)

def get_ffmpeg():
    """Return (exe_path, needs_win_paths, has_cuda).

    Uses the FFMPEG path from paths.py (configurable via project.conf or
    FFMPEG env var). If the resolved path is a Windows executable accessed
    from WSL, needs_win_paths is True.
    """
    ffmpeg_path = FFMPEG
    needs_win = is_wsl() and (ffmpeg_path.endswith('.exe') or '\\' in ffmpeg_path)
    has_cuda = ffmpeg_path != "ffmpeg" and os.path.exists(ffmpeg_path)
    return ffmpeg_path, needs_win, has_cuda

def extract_chapter_frames_from_segment(chapter_info, chapter_dir, segments):
    """Extract video frames directly from a Daphne .m2v segment.

    Uses CPU-only decode with yadif deinterlace, fps conversion via filter,
    and trim filter for frame-accurate extraction (no -ss seeking).

    Returns frame count on success, 0 if skipped, or -1 on error.
    """
    if chapter_info['duration_ms'] <= 0:
        return 0

    start_frame = chapter_info.get('start_frame')
    if start_frame is None:
        return 0  # No frame info, skip

    seg, offset_seconds = find_segment(segments, start_frame)
    if seg is None:
        return 0

    m2v_path = seg['m2v_path']
    if not os.path.exists(m2v_path):
        return 0

    duration_s = chapter_info['duration_ms'] / 1000.0

    ffmpeg_path, needs_win_paths, _ = get_ffmpeg()

    if needs_win_paths:
        out_pattern = to_win_path(os.path.join(chapter_dir, "video_%06d.gfx_video.png"))
        video_path = to_win_path(m2v_path)
    else:
        out_pattern = os.path.join(chapter_dir, "video_%06d.gfx_video.png")
        video_path = m2v_path

    # Filter chain: yadif deinterlace (29.97i -> progressive) -> fps conversion
    # to 23.976 -> trim to target range -> reset PTS -> scale -> palette
    # No CUDA, no -ss seeking — CPU decode from start, trim handles offset
    filter_str = (
        f'yadif,fps=24000/1001,'
        f'trim=start={offset_seconds:.6f}:duration={duration_s:.6f},'
        f'setpts=PTS-STARTPTS,'
        f'scale={FRAME_WIDTH}:{FRAME_HEIGHT}[s];'
        f'[s]split[s1][s2];'
        f'[s1]palettegen=max_colors={MAX_COLORS}:stats_mode=single[p];'
        f'[s2][p]paletteuse=new=1:dither=bayer'
    )

    cmd = [
        ffmpeg_path, '-y',
        '-i', video_path,
        '-filter_complex', filter_str,
        '-f', 'image2',
        out_pattern
    ]

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
        if result.returncode != 0:
            print(f"    ffmpeg error: {result.stderr[-200:] if result.stderr else 'unknown'}")
            return -1
    except subprocess.TimeoutExpired:
        return -1

    return len(glob.glob(os.path.join(chapter_dir, "video_*.gfx_video.png")))


# ---------- Audio Extraction ----------
def extract_chapter_audio_from_segment(chapter_info, chapter_dir, segments):
    """Extract audio from a Daphne .ogg segment.

    Returns True on success, or False on error/skip.
    """
    if chapter_info['duration_ms'] <= 0:
        return False

    start_frame = chapter_info.get('start_frame')
    if start_frame is None:
        return False

    seg, offset_seconds = find_segment(segments, start_frame)
    if seg is None:
        return False

    ogg_path = seg['ogg_path']
    if not os.path.exists(ogg_path):
        return False

    dur = format_time(chapter_info['duration_ms'])

    ffmpeg_path, needs_win_paths, _ = get_ffmpeg()

    if needs_win_paths:
        audio_path = to_win_path(ogg_path)
    else:
        audio_path = ogg_path

    raw_audio_path = os.path.join(chapter_dir, "sfx_video.raw")
    pcm_output_path = os.path.join(chapter_dir, "sfx_video.pcm")

    if needs_win_paths:
        raw_out = to_win_path(raw_audio_path)
    else:
        raw_out = raw_audio_path

    # Double-seeking for .ogg
    pre_seek_s = max(0, offset_seconds - 5)
    precise_offset_s = offset_seconds - pre_seek_s

    cmd = [
        ffmpeg_path, '-y',
        '-ss', f'{pre_seek_s:.3f}',
        '-i', audio_path,
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
    """Pad tilemap to 32x32 (2048 bytes) for SNES compatibility."""
    with open(tilemap_file, 'rb') as f:
        data = f.read()
    if len(data) < TILEMAP_TARGET_SIZE:
        with open(tilemap_file, 'wb') as f:
            f.write(data)
            f.write(b'\x00' * (TILEMAP_TARGET_SIZE - len(data)))

def tile_aware_palette_split(png_path, pal_file):
    """Split 32-color bayer-dithered PNG into 2x16 SNES sub-palettes.

    Uses tile-color co-occurrence to keep colors that appear together in 8x8
    tiles in the same sub-palette. Remaps each tile's pixels to exact palette
    colors so superfamiconv assigns tiles correctly.

    Steps:
    1. Read palettized PNG (32 colors from ffmpeg bayer dithering)
    2. Build co-occurrence matrix: how many tiles use color i AND color j
    3. Spectral bipartition (Fiedler vector) to split into 2 groups of ~16
    4. Assign each tile to the sub-palette containing most of its colors
    5. Remap cross-palette pixels to nearest color in assigned sub-palette
    6. Write .palette file (2x16 BGR555) and save remapped PNG
    """
    from PIL import Image

    img = Image.open(png_path)
    if img.mode != 'P':
        # Not palettized (e.g., blank frame) — generate trivial palette
        img = img.convert('RGB')
        px = np.array(img)
        if px.max() == 0:
            with open(pal_file, 'wb') as f:
                f.write(b'\x00\x00' * (2 * 16))
            return True
        # Shouldn't happen — ffmpeg should always produce palettized PNGs
        return False

    pal_flat = img.getpalette()  # flat [R,G,B,R,G,B,...] up to 256*3
    pixels = np.array(img)  # (H, W) uint8 palette indices
    H, W = pixels.shape

    # Count actual palette entries used
    used_indices = np.unique(pixels)
    num_pal_entries = max(int(used_indices.max()) + 1, 1)
    num_pal_entries = min(num_pal_entries, len(pal_flat) // 3)

    # Convert palette to BGR555 and RGB arrays
    rgb_pal = np.zeros((num_pal_entries, 3), dtype=np.float32)
    bgr555_pal = np.zeros(num_pal_entries, dtype=np.uint16)
    for i in range(num_pal_entries):
        r, g, b = pal_flat[i*3], pal_flat[i*3+1], pal_flat[i*3+2]
        rgb_pal[i] = [r, g, b]
        bgr555_pal[i] = (r >> 3) | ((g >> 3) << 5) | ((b >> 3) << 10)

    # If 16 or fewer colors, no split needed — single sub-palette
    if num_pal_entries <= 16:
        with open(pal_file, 'wb') as f:
            # Sub-palette 0: transparent + colors
            f.write(struct.pack('<H', 0x0000))
            for i in range(15):
                c = int(bgr555_pal[i]) if i < num_pal_entries else 0
                # Skip if this color IS transparent
                if c == 0 and i < num_pal_entries:
                    c = 0  # keep as transparent
                f.write(struct.pack('<H', c))
            # Sub-palette 1: empty (all transparent)
            for i in range(16):
                f.write(struct.pack('<H', 0x0000))
        return True

    # Reshape pixels into 8x8 tiles
    tiles_h, tiles_w = H // 8, W // 8
    tiled = pixels[:tiles_h*8, :tiles_w*8].reshape(tiles_h, 8, tiles_w, 8)
    tiled = tiled.transpose(0, 2, 1, 3).reshape(-1, 64)
    T = tiled.shape[0]

    # Build co-occurrence matrix: tiles sharing colors
    cooccur = np.zeros((num_pal_entries, num_pal_entries), dtype=np.float64)
    for t in range(T):
        colors_in_tile = np.unique(tiled[t])
        # Filter to valid range
        colors_in_tile = colors_in_tile[colors_in_tile < num_pal_entries]
        n = len(colors_in_tile)
        for i in range(n):
            for j in range(i, n):
                ci, cj = colors_in_tile[i], colors_in_tile[j]
                cooccur[ci, cj] += 1
                if ci != cj:
                    cooccur[cj, ci] += 1

    # Spectral bipartition via Fiedler vector
    degree = cooccur.sum(axis=1)
    # Handle isolated colors (zero degree) — add small epsilon
    degree = np.maximum(degree, 1e-10)
    laplacian = np.diag(degree) - cooccur
    eigenvalues, eigenvectors = np.linalg.eigh(laplacian)

    # Fiedler vector = eigenvector for 2nd smallest eigenvalue
    # (1st eigenvalue is ~0 with constant eigenvector)
    fiedler = eigenvectors[:, 1]

    # Split into two balanced groups of ~16 each
    order = np.argsort(fiedler)
    half = num_pal_entries // 2
    group = np.ones(num_pal_entries, dtype=int)
    group[order[:half]] = 0  # first half → group 0

    # Collect color indices per group (up to 15 visible per sub-palette)
    group0_indices = np.where(group == 0)[0]
    group1_indices = np.where(group == 1)[0]

    # Build sub-palette BGR555 arrays and their RGB8 roundtrip equivalents.
    # CRITICAL: PNG pixels must use BGR555-roundtripped RGB values so
    # superfamiconv finds exact matches when assigning tiles to sub-palettes.
    def bgr555_to_rgb8(bgr):
        """Convert BGR555 to RGB8 matching superfamiconv's roundtrip."""
        r5 = bgr & 0x1F
        g5 = (bgr >> 5) & 0x1F
        b5 = (bgr >> 10) & 0x1F
        return (round(r5 * 255.0 / 31.0),
                round(g5 * 255.0 / 31.0),
                round(b5 * 255.0 / 31.0))

    # Build sub-palette entries: [transparent, color1, ..., color15]
    sp_bgr555 = [[], []]  # BGR555 values for each sub-palette
    sp_rgb8 = [[], []]    # RGB8 roundtripped values for pixel matching
    for sp_idx, grp_idx in enumerate([group0_indices, group1_indices]):
        for ci in grp_idx:
            if len(sp_bgr555[sp_idx]) >= 15:
                break
            bgr = int(bgr555_pal[ci])
            if bgr == 0:
                bgr = 0x0001  # avoid transparent collision
            sp_bgr555[sp_idx].append(bgr)
            sp_rgb8[sp_idx].append(bgr555_to_rgb8(bgr))

    # Convert to numpy for vectorized nearest-color lookup
    g0_rgb_rt = np.array(sp_rgb8[0], dtype=np.float32) if sp_rgb8[0] else np.zeros((0, 3), dtype=np.float32)
    g1_rgb_rt = np.array(sp_rgb8[1], dtype=np.float32) if sp_rgb8[1] else np.zeros((0, 3), dtype=np.float32)

    # Assign each tile to the sub-palette containing most of its colors
    tile_subpal = np.zeros(T, dtype=int)
    for t in range(T):
        colors_in_tile = np.unique(tiled[t])
        colors_in_tile = colors_in_tile[colors_in_tile < num_pal_entries]
        g0_count = np.sum(group[colors_in_tile] == 0)
        g1_count = np.sum(group[colors_in_tile] == 1)
        tile_subpal[t] = 0 if g0_count >= g1_count else 1

    # Remap pixels: each tile's pixels → nearest color in assigned sub-palette
    # Using BGR555-roundtripped RGB8 values so superfamiconv sees exact matches
    out_rgb = np.zeros((H, W, 3), dtype=np.uint8)
    for t in range(T):
        ty, tx = divmod(t, tiles_w)
        tile_px = tiled[t]  # (64,) palette indices
        tile_px_clamped = np.minimum(tile_px, num_pal_entries - 1)
        tile_rgb = rgb_pal[tile_px_clamped]  # (64, 3) original RGB

        sp = tile_subpal[t]
        sp_rgb = g0_rgb_rt if sp == 0 else g1_rgb_rt

        if len(sp_rgb) == 0:
            out_rgb[ty*8:(ty+1)*8, tx*8:(tx+1)*8] = 0
            continue

        # Find nearest color in assigned sub-palette (using roundtripped values)
        diffs = tile_rgb[:, None, :] - sp_rgb[None, :, :]  # (64, C, 3)
        dists = np.sum(diffs * diffs, axis=2)  # (64, C)
        nearest_idx = np.argmin(dists, axis=1)  # (64,)
        remapped = sp_rgb[nearest_idx].astype(np.uint8)  # (64, 3) exact BGR555 roundtrip

        out_rgb[ty*8:(ty+1)*8, tx*8:(tx+1)*8] = remapped.reshape(8, 8, 3)

    # Save remapped RGB image (overwrite source PNG)
    Image.fromarray(out_rgb, 'RGB').save(png_path)

    # Write .palette file: 2 sub-palettes x 16 entries (BGR555)
    # Index 0 of each = transparent (0x0000), then up to 15 visible colors
    with open(pal_file, 'wb') as f:
        for sp_idx in range(2):
            f.write(struct.pack('<H', 0x0000))  # transparent at index 0
            for i in range(15):
                bgr = sp_bgr555[sp_idx][i] if i < len(sp_bgr555[sp_idx]) else 0
                f.write(struct.pack('<H', bgr))

    return True


def read_snes_palette(palette_file):
    """Read SNES BGR555 palette file and return (16, 3) float32 RGB array.

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


def decode_tiles_4bpp_rgb(tiles_raw, palette_rgb, tile_pal_offsets=None):
    """Decode SNES 4BPP tiles to RGB values using the frame's actual palette.

    tiles_raw: (N, 32) uint8 array of raw SNES 4BPP tile data
    palette_rgb: (C, 3) float32 array of RGB values (16 for 1 sub-palette, 32 for 2)
    tile_pal_offsets: optional (N,) uint16 array — palette index offset per tile
                      (e.g., 0 for sub-palette 0, 16 for sub-palette 1)
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
    flat_indices = pixel_indices.reshape(N, 64).astype(np.uint16)
    # Apply per-tile sub-palette offset (0 or 16) to get absolute palette index
    if tile_pal_offsets is not None:
        flat_indices = flat_indices + tile_pal_offsets[:, None]
    # Clamp to palette size
    flat_indices = np.minimum(flat_indices, len(palette_rgb) - 1)
    rgb = palette_rgb[flat_indices]
    return rgb.reshape(N, 192)


def reduce_tiles(tile_file, tilemap_file, palette_file, max_tiles=MAX_TILES):
    """Reduce tile count to max_tiles using global greedy merge in RGB color space.

    SNES VRAM budget is $3000 bytes = 384 tiles at 4BPP. Video frames at
    256x160 can have up to 640 unique tiles. This function finds the most
    similar tile pairs across the ENTIRE image and merges them, distributing
    quality loss evenly rather than concentrating it in the bottom rows.

    Uses L2 distance on actual RGB color values (decoded through the frame's
    palette) for accurate visual similarity matching. This is critical because
    palette indices have no inherent ordering — two indices that are numerically
    far apart may map to nearly identical colors.
    """
    bytes_per_tile = 8 * BPP  # 32 for 4BPP

    with open(tile_file, 'rb') as f:
        tile_data = f.read()
    num_tiles = len(tile_data) // bytes_per_tile
    if num_tiles <= max_tiles:
        return  # nothing to do

    tiles = np.frombuffer(tile_data, dtype=np.uint8).reshape(num_tiles, bytes_per_tile)

    # Decode tiles to RGB color space using the frame's actual palette.
    # With 2 sub-palettes, read tilemap palette bits to offset each tile's indices.
    palette_rgb = read_snes_palette(palette_file)

    with open(tilemap_file, 'rb') as f:
        map_for_decode = np.frombuffer(f.read(), dtype=np.uint16).copy()
    # Extract sub-palette number (bits 13-14) for each tilemap entry
    # Build per-unique-tile offset: palette_number * 16
    tile_pal_offsets = None
    if len(palette_rgb) > 16:
        # Map tilemap entries to their tile index and palette
        map_tile_idx = map_for_decode & 0x3ff
        map_pal_num = (map_for_decode >> 10) & 0x07  # 3 bits for palette
        # Build per-tile offset array — use first tilemap reference for each tile
        tile_pal_offsets = np.zeros(num_tiles, dtype=np.uint16)
        seen = set()
        for entry_idx in range(len(map_for_decode)):
            ti = int(map_tile_idx[entry_idx])
            if ti < num_tiles and ti not in seen:
                tile_pal_offsets[ti] = int(map_pal_num[entry_idx]) * 16
                seen.add(ti)

    pixels = decode_tiles_4bpp_rgb(tiles, palette_rgb, tile_pal_offsets)  # (N, 192)

    # Compute pairwise L2 squared distance matrix using dot product trick:
    # ||A-B||^2 = ||A||^2 + ||B||^2 - 2*A·B
    sq_norms = np.sum(pixels * pixels, axis=1)  # (N,)
    dot_products = pixels @ pixels.T             # (N, N) via BLAS
    dist = sq_norms[:, None] + sq_norms[None, :] - 2 * dot_products

    # Block cross-palette merges: tiles using different sub-palettes cannot
    # be merged (their pixel indices map to different colors)
    if tile_pal_offsets is not None:
        cross_pal = tile_pal_offsets[:, None] != tile_pal_offsets[None, :]
        dist[cross_pal] = np.finfo(np.float32).max

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
        # Remove the higher-indexed tile, keep the lower
        alive.discard(j)
        merge_target[j] = i
        removed += 1

    # Resolve transitive merges (j→i, but i may also have been merged later)
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

    # Build final remap: old tile index → new contiguous index
    final_remap = np.array([reindex[merge_target[i]] for i in range(num_tiles)],
                           dtype=np.uint16)

    # Update tilemap
    with open(tilemap_file, 'rb') as f:
        tilemap_raw = f.read()
    tilemap = np.frombuffer(tilemap_raw, dtype=np.uint16).copy()
    tile_indices = tilemap & 0x3ff
    flags = tilemap & 0xfc00

    # Vectorized remap of all tilemap indices
    new_indices = final_remap[tile_indices]
    tilemap = flags | new_indices

    # Write reduced tiles (only surviving tiles, in original order)
    new_tile_data = tiles[alive_sorted].tobytes()
    with open(tile_file, 'wb') as f:
        f.write(new_tile_data)

    # Write updated tilemap
    with open(tilemap_file, 'wb') as f:
        f.write(tilemap.tobytes())

def convert_frame_superfamiconv(png_path):
    """Convert one PNG frame to SNES tiles/tilemap/palette using superfamiconv."""
    base = png_path[:-4]  # Remove .png
    pal_file = base + '.palette'
    tile_file = base + '.tiles'
    map_file = base + '.tilemap'

    # superfamiconv.exe (Windows binary) only works with relative paths in WSL.
    # Use cwd=PROJECT_DIR and make all paths relative.
    sfc = os.path.relpath(SUPERFAMICONV, PROJECT_DIR)
    rel_png = os.path.relpath(png_path, PROJECT_DIR)
    rel_pal = os.path.relpath(pal_file, PROJECT_DIR)
    rel_tile = os.path.relpath(tile_file, PROJECT_DIR)
    rel_map = os.path.relpath(map_file, PROJECT_DIR)

    run_kw = dict(capture_output=True, text=True, timeout=30, cwd=PROJECT_DIR)

    # 1. Tile-aware palette split: reads 32-color bayer-dithered PNG, splits
    # colors into 2x16 sub-palettes via co-occurrence, remaps pixel colors,
    # writes .palette file and saves remapped RGB PNG
    ok = tile_aware_palette_split(png_path, pal_file)
    if not ok:
        return False, "palette split failed"

    # 2. Tile conversion (no tile limit — post-process to reduce)
    r = subprocess.run([sfc, 'tiles', '-i', rel_png, '-p', rel_pal, '-d', rel_tile, '-B', str(BPP)], **run_kw)
    if r.returncode != 0:
        return False, f"tiles: {r.stderr.strip()}"

    # 3. Tilemap generation
    r = subprocess.run([sfc, 'map', '-i', rel_png, '-p', rel_pal, '-t', rel_tile, '-d', rel_map, '-B', str(BPP)], **run_kw)
    if r.returncode != 0:
        return False, f"map: {r.stderr.strip()}"

    # 4. Reduce tiles to fit in VRAM buffer (512 max at 4BPP)
    reduce_tiles(tile_file, map_file, pal_file)

    # 5. Pad tilemap to 32x32
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
    parser = argparse.ArgumentParser(description='Generate MSU-1 .msu video data')
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
    parser.add_argument('--framefile', type=str, default=DEFAULT_FRAMEFILE,
                        help='Path to Daphne framefile (default: %(default)s)')
    parser.add_argument('--content-root', type=str, default=DEFAULT_CONTENT_ROOT,
                        help='Path to Daphne content directory (default: %(default)s)')
    args = parser.parse_args()

    print("=" * 60)
    print("MSU-1 Video Data Generator")
    print("=" * 60)
    print(f"Chapters dir: {CHAPTERS_DIR}")
    print(f"Output MSU:   {OUTPUT_MSU}")
    print(f"Workers:      {args.workers}")

    ffmpeg_path, needs_win_paths, has_cuda = get_ffmpeg()
    print(f"ffmpeg:       {ffmpeg_path}")
    print(f"CUDA GPU:     {'Yes' if has_cuda else 'No (CPU fallback)'}")
    print(f"superfamiconv: {SUPERFAMICONV}")

    # Load Daphne framefile for direct .m2v/.ogg extraction (mandatory)
    if not os.path.exists(args.framefile):
        print(f"\nERROR: Daphne framefile not found: {args.framefile}")
        sys.exit(1)

    daphne_segments = parse_framefile(args.framefile, args.content_root)
    print(f"Framefile:    {args.framefile} ({len(daphne_segments)} segments)")

    if daphne_segments:
        sample_m2v = daphne_segments[0]['m2v_path']
        content_dir = os.path.dirname(sample_m2v)
        if os.path.isdir(content_dir):
            print(f"Content root: {content_dir}")
        else:
            print(f"\nERROR: Content root not found: {content_dir}")
            sys.exit(1)
    else:
        print(f"\nERROR: Framefile parsed 0 segments")
        sys.exit(1)

    print(f"\nUsing direct .m2v/.ogg extraction from Daphne segments")
    print()

    # Build chapter list
    chapters = []
    for chapter_name in sorted(os.listdir(CHAPTERS_DIR)):
        chapter_dir = os.path.join(CHAPTERS_DIR, chapter_name)
        if not os.path.isdir(chapter_dir):
            continue
        xml_path = os.path.join(EVENTS_DIR, chapter_name + '.xml')
        if not os.path.exists(xml_path):
            print(f"WARN: No XML for chapter {chapter_name}, skipping")
            continue
        if args.chapter and chapter_name != args.chapter:
            continue
        chapters.append((chapter_name, chapter_dir, xml_path))

    print(f"Found {len(chapters)} chapters to process\n")

    # Phase 1: Extract video frames from .m2v segments
    total_frames = 0
    extract_errors = 0
    skipped_no_frame = 0
    if not args.skip_extract:
        print("--- Phase 1: Extracting video frames (ffmpeg from .m2v) ---")
        extract_start = time.time()

        for i, (name, cdir, xml) in enumerate(chapters):
            info = parse_chapter_xml(xml)
            if info['duration_ms'] <= 0:
                print(f"[{i+1:3d}/{len(chapters)}] {name}: skip (0 duration)")
                continue

            if args.clean:
                for f in glob.glob(os.path.join(cdir, "*.gfx_video.png")):
                    os.remove(f)

            existing = glob.glob(os.path.join(cdir, "*.gfx_video.png"))
            if existing and not args.clean:
                n = len(existing)
                print(f"[{i+1:3d}/{len(chapters)}] {name}: {n} frames (cached)")
                total_frames += n
                continue

            if info.get('start_frame') is None:
                print(f"[{i+1:3d}/{len(chapters)}] {name}: SKIP (no start_frame)")
                skipped_no_frame += 1
                continue

            n = extract_chapter_frames_from_segment(info, cdir, daphne_segments)
            if n > 0:
                total_frames += n
                print(f"[{i+1:3d}/{len(chapters)}] {name}: {n} frames (from .m2v)")
            elif n == 0:
                print(f"[{i+1:3d}/{len(chapters)}] {name}: 0 frames (no segment)")
            else:
                print(f"[{i+1:3d}/{len(chapters)}] {name}: EXTRACTION ERROR")
                extract_errors += 1

        extract_elapsed = time.time() - extract_start
        print(f"\nExtraction done: {total_frames} frames in {extract_elapsed:.1f}s "
              f"({extract_errors} errors, {skipped_no_frame} skipped no start_frame)\n")
    else:
        for name, cdir, xml in chapters:
            total_frames += len(glob.glob(os.path.join(cdir, "*.gfx_video.png")))
        print(f"Skipping extraction. {total_frames} existing PNG frames found.\n")

    # Phase 1b: Extract audio per chapter from .ogg segments
    total_audio = 0
    audio_errors = 0
    if not args.skip_audio:
        print("--- Phase 1b: Extracting audio per chapter (ffmpeg from .ogg) ---")
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

            if extract_chapter_audio_from_segment(info, cdir, daphne_segments):
                total_audio += 1
                if (i + 1) % 50 == 0 or i == len(chapters) - 1:
                    print(f"[{i+1:3d}/{len(chapters)}] {name}: audio (from .ogg)")
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

    # Phase 1c: Copy PCM files to numbered output files (SuperDragonsLairArcade-{chapterID}.pcm)
    pcm_copied = 0
    if total_audio > 0:
        print("--- Phase 1c: Copying PCM files to numbered output ---")
        build_dir = os.path.dirname(OUTPUT_MSU)
        os.makedirs(build_dir, exist_ok=True)
        final_dir = str(DISTRIBUTION)
        os.makedirs(final_dir, exist_ok=True)
        base_name = os.path.splitext(os.path.basename(OUTPUT_MSU))[0]

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

            out_name = f"{base_name}-{chapter_id}.pcm"
            build_pcm = os.path.join(build_dir, out_name)
            shutil.copy2(pcm_path, build_pcm)
            pcm_copied += 1

            if os.path.isdir(final_dir):
                shutil.copy2(pcm_path, os.path.join(final_dir, out_name))

        print(f"Copied {pcm_copied} PCM files to {build_dir}")
        if os.path.isdir(final_dir):
            print(f"Also copied to {final_dir}")
        print()

    # Phase 1d: Copy dragon roar PCM (track 900) to build and distribution directories
    if os.path.exists(DRAGON_ROAR_PCM):
        build_dir = os.path.dirname(OUTPUT_MSU)
        final_dir = str(DISTRIBUTION)
        os.makedirs(final_dir, exist_ok=True)
        roar_name = os.path.basename(DRAGON_ROAR_PCM)
        shutil.copy2(DRAGON_ROAR_PCM, os.path.join(build_dir, roar_name))
        shutil.copy2(DRAGON_ROAR_PCM, os.path.join(final_dir, roar_name))
        print(f"Copied dragon roar PCM (track 900) to build + distribution directories\n")
    else:
        print(f"WARNING: Dragon roar PCM not found at {DRAGON_ROAR_PCM}\n"
              f"  Run: python3 tools/convert_roar_pcm.py\n")


    # Phase 2: Convert frames to SNES tiles
    total_converted = 0
    if not args.skip_convert:
        print(f"--- Phase 2: Converting frames to SNES tiles (superfamiconv, {args.workers} workers) ---")
        convert_start = time.time()

        for i, (name, cdir, xml) in enumerate(chapters):
            pngs = glob.glob(os.path.join(cdir, "*.gfx_video.png"))
            existing_tiles = glob.glob(os.path.join(cdir, "*.gfx_video.tiles"))

            if not pngs:
                continue

            if len(existing_tiles) == len(pngs) and not args.clean:
                print(f"[{i+1:3d}/{len(chapters)}] {name}: {len(existing_tiles)} tiles (cached)")
                total_converted += len(existing_tiles)
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

    # Phase 3: Package .msu file
    if not args.skip_package:
        print("--- Phase 3: Packaging .msu file ---")

        # Ensure build directory exists
        os.makedirs(os.path.dirname(OUTPUT_MSU), exist_ok=True)

        cmd = [
            sys.executable, MSU_WRITER,
            '-title', "SUPER DRAGON'S LAIR",
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

    print("\n" + "=" * 60)
    print("Done!")
    print(f"  Frames extracted: {total_frames}")
    print(f"  Audio extracted:  {total_audio}")
    print(f"  Tiles converted:  {total_converted}")
    if os.path.exists(OUTPUT_MSU):
        print(f"  MSU file size:    {os.path.getsize(OUTPUT_MSU) / 1024 / 1024:.1f} MB")
    print("=" * 60)


if __name__ == '__main__':
    main()
