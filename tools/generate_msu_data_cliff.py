#!/usr/bin/env python3
"""
generate_msu_data_cliff.py - Generate MSU-1 .msu video data for Cliff Hanger Arcade

Adapted from generate_msu_data.py (Dragon's Lair) for Cliff Hanger's single-file
video format. Instead of 204 Daphne .m2v segments, Cliff Hanger uses a single
cliff.m2v video file and cliff.ogg audio file.

Pipeline:
1. Parse chapter XMLs for frame ranges (laserdisc frame numbers)
2. Extract video frames from cliff.m2v per chapter (ffmpeg, CPU decode)
   - 29.97fps source -> fps=24000/1001 rate conversion
   - yadif deinterlace -> trim by time -> scale 256x192 -> 16-color palette
3. Convert frames to SNES tiles/tilemap/palette (superfamiconv)
   - Each 256x192 frame produces up to 768 unique 8x8 tiles
   - reduce_tiles() merges down to 384 (VRAM limit at 4BPP)
4. Package into .msu file (msu1blockwriter.py)
5. Extract audio per chapter from cliff.ogg -> MSU-1 PCM format

Usage (from project root, in WSL or Windows):
  python3 tools/generate_msu_data_cliff.py [--workers N] [--chapter NAME] [--skip-extract] [--skip-convert] [--skip-package]

Source files (user must place these):
  data/laserdisc/segments/cliff.m2v   - Single MPEG-2 video file
  data/laserdisc/segments/cliff.ogg   - Single Ogg Vorbis audio file
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

# ---------- Configuration ----------
from paths import PROJECT_ROOT, BUILD_DIR, TOOLS_DIR, DISTRIBUTION, FFMPEG

PROJECT_DIR = str(PROJECT_ROOT)
EVENTS_DIR = os.path.join(PROJECT_DIR, 'data', 'events')
CHAPTERS_DIR = os.path.join(PROJECT_DIR, 'data', 'chapters')
FRAMES_DIR = os.path.join(PROJECT_DIR, 'data', 'videos', 'frames')
SUPERFAMICONV = os.path.join(str(TOOLS_DIR), 'superfamiconv', 'superfamiconv.exe')
MSU_WRITER = os.path.join(str(TOOLS_DIR), 'msu1blockwriter.py')

# Cliff Hanger source files (single video + audio)
LASERDISC_SEGMENTS = os.path.join(PROJECT_DIR, 'data', 'laserdisc', 'segments')
VIDEO_FILE = os.path.join(LASERDISC_SEGMENTS, 'cliff.m2v')
AUDIO_FILE = os.path.join(LASERDISC_SEGMENTS, 'cliff.ogg')

# Cliff Hanger video parameters
SOURCE_FPS = 29.97       # Source video fps (NTSC laserdisc)
FPS = 24                 # MSU-1 playback fps (integer, passed to msu1blockwriter)
BPP = 4
PALETTES = 1             # Single 16-color sub-palette per frame
MAX_COLORS = PALETTES * (2 ** BPP)  # 1 * 16 = 16 colors
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
    # 6. palettegen/paletteuse: quantize to 16 colors with bayer dithering
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
        '-i', vid_path,
        '-filter_complex', filter_str,
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


def decode_tiles_4bpp_rgb(tiles_raw, palette_rgb):
    """Decode SNES 4BPP tiles to RGB values using the frame's actual palette.

    tiles_raw: (N, 32) uint8 array of raw SNES 4BPP tile data
    palette_rgb: (C, 3) float32 array of RGB values (16 for single palette)
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
    # Clamp to palette size
    flat_indices = np.minimum(flat_indices, len(palette_rgb) - 1)
    rgb = palette_rgb[flat_indices]
    return rgb.reshape(N, 192)


def reduce_tiles(tile_file, tilemap_file, palette_file, max_tiles=MAX_TILES):
    """Reduce tile count to max_tiles using global greedy merge in RGB color space.

    SNES VRAM budget is $3000 bytes = 384 tiles at 4BPP. Video frames at
    256x192 can have up to 768 unique tiles (32x24 grid). This function finds
    the most similar tile pairs across the ENTIRE image and merges them,
    distributing quality loss evenly rather than concentrating it in specific rows.

    Uses L2 distance on actual RGB color values (decoded through the frame's
    palette) for accurate visual similarity matching.
    """
    bytes_per_tile = 8 * BPP  # 32 for 4BPP

    with open(tile_file, 'rb') as f:
        tile_data = f.read()
    num_tiles = len(tile_data) // bytes_per_tile
    if num_tiles <= max_tiles:
        return  # nothing to do

    tiles = np.frombuffer(tile_data, dtype=np.uint8).reshape(num_tiles, bytes_per_tile)

    # Decode tiles to RGB color space using the frame's actual palette
    palette_rgb = read_snes_palette(palette_file)
    pixels = decode_tiles_4bpp_rgb(tiles, palette_rgb)  # (N, 192)

    # Compute pairwise L2 squared distance matrix using dot product trick:
    # ||A-B||^2 = ||A||^2 + ||B||^2 - 2*A.B
    sq_norms = np.sum(pixels * pixels, axis=1)  # (N,)
    dot_products = pixels @ pixels.T             # (N, N) via BLAS
    dist = sq_norms[:, None] + sq_norms[None, :] - 2 * dot_products

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
    """Convert one PNG frame to SNES tiles/tilemap/palette using superfamiconv.

    Cliff Hanger uses a single 16-color palette (no dual sub-palette split).
    """
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

    # 1. Generate palette (single 16-color sub-palette)
    r = subprocess.run([sfc, 'palette', '-i', rel_png, '-d', rel_pal,
                        '-C', str(MAX_COLORS)], **run_kw)
    if r.returncode != 0:
        return False, f"palette: {r.stderr.strip()}"

    # 2. Tile conversion (no tile limit -- post-process to reduce)
    r = subprocess.run([sfc, 'tiles', '-i', rel_png, '-p', rel_pal, '-d', rel_tile,
                        '-B', str(BPP)], **run_kw)
    if r.returncode != 0:
        return False, f"tiles: {r.stderr.strip()}"

    # 3. Tilemap generation
    r = subprocess.run([sfc, 'map', '-i', rel_png, '-p', rel_pal, '-t', rel_tile,
                        '-d', rel_map, '-B', str(BPP)], **run_kw)
    if r.returncode != 0:
        return False, f"map: {r.stderr.strip()}"

    # 4. Reduce tiles to fit in VRAM buffer (384 max at 4BPP)
    reduce_tiles(tile_file, map_file, pal_file)

    # 5. Pad tilemap to 32x24
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
    parser.add_argument('--video', type=str, default=VIDEO_FILE,
                        help=f'Path to cliff.m2v video file (default: {VIDEO_FILE})')
    parser.add_argument('--audio', type=str, default=AUDIO_FILE,
                        help=f'Path to cliff.ogg audio file (default: {AUDIO_FILE})')
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
    print(f"Colors:       {MAX_COLORS} ({PALETTES} sub-palette)")

    ffmpeg_path, needs_win_paths = get_ffmpeg()
    print(f"ffmpeg:       {ffmpeg_path}")
    print(f"superfamiconv: {SUPERFAMICONV}")

    # Validate source files
    video_path = args.video
    audio_path = args.audio

    if not args.skip_extract:
        if not os.path.exists(video_path):
            print(f"\nERROR: Video file not found: {video_path}")
            print(f"  Place the Cliff Hanger .m2v file at: {VIDEO_FILE}")
            sys.exit(1)
        else:
            video_size_mb = os.path.getsize(video_path) / 1024 / 1024
            print(f"Video file:   {video_path} ({video_size_mb:.1f} MB)")

    if not args.skip_audio:
        if not os.path.exists(audio_path):
            print(f"\nWARNING: Audio file not found: {audio_path}")
            print(f"  Place the Cliff Hanger .ogg file at: {AUDIO_FILE}")
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
              f"(superfamiconv, {args.workers} workers) ---")
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
