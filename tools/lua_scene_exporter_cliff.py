#!/usr/bin/env python3
"""Generate XML event files from Cliff Hanger DirkSimple game.lua.

Reads the Cliff Hanger scenes table (flat moves arrays) and generates
one XML file per chapter, matching the format expected by xmlsceneparser.py.

Cliff Hanger has 8 scenes with a flat array of moves per scene, unlike
Dragon's Lair which has nested sequence tables with timeouts and actions.

Usage:
    python3 tools/lua_scene_exporter_cliff.py
    python3 tools/lua_scene_exporter_cliff.py --input data/game.lua --outfolder data/events
"""

import argparse
import logging
import os
import re
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logging.basicConfig(level=logging.INFO, format='%(message)s')

# ============ Constants ============

# Cliff Hanger laserdisc runs at 29.97 fps (NTSC)
FRAMERATE = 29.97

# Scene name abbreviations (max 4 chars for wla-dx path length limits)
SCENE_ABBREVS = {
    'casino_heist': 'csno',
    'the_getaway': 'gtwy',
    'rooftops': 'roof',
    'highway': 'hway',
    'the_castle_battle': 'cstl',
    'finale': 'fnle',
    'finale_ii': 'fn2',
    'ending': 'endg',
}

# Human-readable scene name -> normalized name mapping
SCENE_NAME_NORMALIZE = {
    'Casino Heist': 'casino_heist',
    'The Getaway': 'the_getaway',
    'Rooftops': 'rooftops',
    'Highway': 'highway',
    'The Castle Battle': 'the_castle_battle',
    'Finale': 'finale',
    'Finale II': 'finale_ii',
    'Ending': 'ending',
}

# wla-dx path length limit: .include "data/chapters/{NAME}/chapter.script" = 41 chars overhead
# Chapter names must be <= 22 chars
MAX_CHAPTER_NAME = 22


# ============ Frame/Time Conversion ============

def frame_to_ms(frame: int) -> float:
    """Convert a laserdisc frame number to milliseconds at 29.97 fps."""
    return (frame / FRAMERATE) * 1000.0


def ms_to_attrs(total_ms: float, frame: Optional[int] = None) -> str:
    """Convert milliseconds to min/second/ms XML attributes, with optional frame."""
    if total_ms < 0:
        total_ms = 0
    total_ms = round(total_ms)
    minutes = int(total_ms // 60000)
    seconds = int((total_ms % 60000) // 1000)
    ms = int(total_ms % 1000)
    attrs = f'min="{minutes}" second="{seconds}" ms="{ms}"'
    if frame is not None:
        attrs += f' frame="{frame}"'
    return attrs


# ============ Name Helpers ============

def normalize_scene_name(display_name: str) -> str:
    """Convert display name like 'Casino Heist' to snake_case 'casino_heist'."""
    if display_name in SCENE_NAME_NORMALIZE:
        return SCENE_NAME_NORMALIZE[display_name]
    # Fallback: lowercase, replace spaces with underscores
    return re.sub(r'\s+', '_', display_name.strip().lower())


def abbreviate_scene(scene_name: str) -> str:
    """Get the 4-char abbreviation for a scene name."""
    abbrev = SCENE_ABBREVS.get(scene_name)
    if abbrev is None:
        raise ValueError(
            f'No abbreviation defined for scene "{scene_name}". '
            f'Add it to SCENE_ABBREVS.'
        )
    return abbrev


def make_move_chapter_name(scene_name: str, move_index: int) -> str:
    """Create chapter name for a gameplay move: {abbrev}_move{NN}."""
    name = f'{abbreviate_scene(scene_name)}_move{move_index:02d}'
    if len(name) > MAX_CHAPTER_NAME:
        raise ValueError(f'Chapter name too long ({len(name)}): {name}')
    return name


def make_death_chapter_name(scene_name: str, death_index: int) -> str:
    """Create chapter name for a death sequence: {abbrev}_death{NN}."""
    name = f'{abbreviate_scene(scene_name)}_death{death_index:02d}'
    if len(name) > MAX_CHAPTER_NAME:
        raise ValueError(f'Chapter name too long ({len(name)}): {name}')
    return name


def make_start_alive_name(scene_name: str) -> str:
    """Create chapter name for scene entry routing node: {abbrev}_start_alive."""
    name = f'{abbreviate_scene(scene_name)}_start_alive'
    if len(name) > MAX_CHAPTER_NAME:
        raise ValueError(f'Chapter name too long ({len(name)}): {name}')
    return name


# ============ Lua Parser ============

def strip_lua_comments(text: str) -> str:
    """Remove Lua single-line and multi-line comments."""
    # Remove multi-line comments --[[ ... ]]
    text = re.sub(r'--\[\[.*?\]\]', '', text, flags=re.DOTALL)
    # Remove single-line comments (but not inside strings)
    lines = []
    for line in text.splitlines():
        # Simple approach: split on first -- outside a string
        # This is sufficient for the regular structure of game.lua
        result = []
        in_string = False
        string_char = None
        i = 0
        while i < len(line):
            ch = line[i]
            if in_string:
                result.append(ch)
                if ch == '\\' and i + 1 < len(line):
                    result.append(line[i + 1])
                    i += 2
                    continue
                if ch == string_char:
                    in_string = False
            else:
                if ch in ('"', "'"):
                    in_string = True
                    string_char = ch
                    result.append(ch)
                elif ch == '-' and i + 1 < len(line) and line[i + 1] == '-':
                    break  # Rest is comment
                else:
                    result.append(ch)
            i += 1
        lines.append(''.join(result))
    return '\n'.join(lines)


def parse_scenes_table(lua_text: str) -> List[Dict[str, Any]]:
    """Parse the scenes table from Cliff Hanger game.lua.

    The structure is a Lua array of scene tables, each containing:
    - scene_name (string)
    - start_frame, end_frame (int)
    - moves (array of move tables)

    Returns a list of scene dicts.
    """
    # Find the scenes = { ... } block
    match = re.search(r'^scenes\s*=\s*\{', lua_text, re.MULTILINE)
    if not match:
        raise ValueError('Could not locate "scenes = {" in Lua source')

    # Find the matching closing brace by counting braces
    start = match.end() - 1  # position of opening {
    brace_count = 0
    end = start
    for i in range(start, len(lua_text)):
        if lua_text[i] == '{':
            brace_count += 1
        elif lua_text[i] == '}':
            brace_count -= 1
            if brace_count == 0:
                end = i + 1
                break

    scenes_text = lua_text[start:end]

    # Use regex-based parsing for the regular structure
    scenes = []
    # Find each scene block (top-level { ... } within scenes)
    scene_pattern = re.compile(r'\{[^{}]*scene_name\s*=\s*"([^"]+)".*?\}(?:\s*,)?', re.DOTALL)

    # More robust: split by scene blocks using brace counting
    scenes = _parse_scene_array(scenes_text)

    return scenes


def _parse_scene_array(text: str) -> List[Dict[str, Any]]:
    """Parse the outer array of scenes using brace-counting to find scene boundaries."""
    scenes = []
    # Skip the outer opening brace
    i = text.index('{') + 1

    while i < len(text):
        # Skip whitespace and commas
        while i < len(text) and text[i] in ' \t\n\r,':
            i += 1

        if i >= len(text) or text[i] == '}':
            break

        if text[i] == '{':
            # Find the matching close brace for this scene
            brace_count = 0
            start = i
            while i < len(text):
                if text[i] == '{':
                    brace_count += 1
                elif text[i] == '}':
                    brace_count -= 1
                    if brace_count == 0:
                        scene_text = text[start:i + 1]
                        scene = _parse_scene_table(scene_text)
                        if scene:
                            scenes.append(scene)
                        i += 1
                        break
                i += 1
        else:
            i += 1

    return scenes


def _parse_scene_table(text: str) -> Optional[Dict[str, Any]]:
    """Parse a single scene table."""
    scene: Dict[str, Any] = {}

    # Extract scene_name
    m = re.search(r'scene_name\s*=\s*"([^"]+)"', text)
    if not m:
        return None
    scene['scene_name'] = m.group(1)

    # Extract numeric fields
    for field in ('start_frame', 'end_frame', 'dunno1_frame', 'dunno2_frame'):
        m = re.search(rf'{field}\s*=\s*(\d+)', text)
        if m:
            scene[field] = int(m.group(1))
        else:
            scene[field] = 0

    # Extract moves array
    moves_match = re.search(r'moves\s*=\s*\{', text)
    if not moves_match:
        scene['moves'] = []
        return scene

    # Find the moves array content
    moves_start = moves_match.end() - 1
    brace_count = 0
    moves_end = moves_start
    for i in range(moves_start, len(text)):
        if text[i] == '{':
            brace_count += 1
        elif text[i] == '}':
            brace_count -= 1
            if brace_count == 0:
                moves_end = i + 1
                break

    moves_text = text[moves_start:moves_end]
    scene['moves'] = _parse_moves_array(moves_text)

    return scene


def _parse_moves_array(text: str) -> List[Dict[str, Any]]:
    """Parse the moves array from a scene."""
    moves = []
    # Skip the outer opening brace
    i = text.index('{') + 1

    while i < len(text):
        # Skip whitespace and commas
        while i < len(text) and text[i] in ' \t\n\r,':
            i += 1

        if i >= len(text) or text[i] == '}':
            break

        if text[i] == '{':
            # Find the matching close brace for this move
            brace_count = 0
            start = i
            while i < len(text):
                if text[i] == '{':
                    brace_count += 1
                elif text[i] == '}':
                    brace_count -= 1
                    if brace_count == 0:
                        move_text = text[start:i + 1]
                        move = _parse_move_table(move_text)
                        moves.append(move)
                        i += 1
                        break
                i += 1
        else:
            i += 1

    return moves


def _parse_move_table(text: str) -> Dict[str, Any]:
    """Parse a single move table."""
    move: Dict[str, Any] = {}

    # Extract optional name
    m = re.search(r'name\s*=\s*"([^"]*)"', text)
    move['name'] = m.group(1) if m else None

    # Extract numeric fields
    for field in ('start_frame', 'end_frame', 'death_start_frame',
                  'death_end_frame', 'restart_move'):
        m = re.search(rf'{field}\s*=\s*(\d+)', text)
        move[field] = int(m.group(1)) if m else 0

    # Extract correct_moves array
    move['correct_moves'] = _parse_string_array(text, 'correct_moves')

    # Extract incorrect_moves array
    move['incorrect_moves'] = _parse_string_array(text, 'incorrect_moves')

    return move


def _parse_string_array(text: str, field_name: str) -> List[str]:
    """Parse a Lua string array like { "hands", "feet" }."""
    m = re.search(rf'{field_name}\s*=\s*\{{([^}}]*)\}}', text)
    if not m:
        return []
    content = m.group(1).strip()
    if not content:
        return []
    # Extract quoted strings
    return re.findall(r'"([^"]+)"', content)


# ============ Death Chapter Deduplication ============

def collect_death_sequences(scenes: List[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    """Collect unique death sequences across all scenes.

    Returns a dict mapping (scene_name, death_key) -> death chapter info,
    where death_key is "{death_start_frame}_{death_end_frame}".

    Each scene gets its own death index counter (death01, death02, ...).
    """
    all_deaths: Dict[str, Dict[str, Any]] = {}

    for scene in scenes:
        scene_name = normalize_scene_name(scene['scene_name'])
        death_index = 0
        seen_in_scene: Dict[str, str] = {}  # death_key -> chapter_name

        for move in scene['moves']:
            dsf = move.get('death_start_frame', 0)
            def_ = move.get('death_end_frame', 0)
            if dsf == 0 and def_ == 0:
                continue

            death_key = f'{dsf}_{def_}'
            full_key = f'{scene_name}_{death_key}'

            if full_key not in all_deaths:
                death_index += 1
                chapter_name = make_death_chapter_name(scene_name, death_index)
                all_deaths[full_key] = {
                    'chapter_name': chapter_name,
                    'scene_name': scene_name,
                    'death_start_frame': dsf,
                    'death_end_frame': def_,
                    'death_key': death_key,
                }
                seen_in_scene[death_key] = chapter_name

            # Also track in the per-scene map for fast lookup
            if death_key not in seen_in_scene:
                seen_in_scene[death_key] = all_deaths[full_key]['chapter_name']

    return all_deaths


def get_death_chapter_name(all_deaths: Dict[str, Dict[str, Any]],
                           scene_name: str, dsf: int, def_: int) -> Optional[str]:
    """Look up the death chapter name for a given scene and death frame pair."""
    death_key = f'{dsf}_{def_}'
    full_key = f'{scene_name}_{death_key}'
    info = all_deaths.get(full_key)
    return info['chapter_name'] if info else None


# ============ Checkpoint Detection ============

def find_checkpoint_moves(moves: List[Dict[str, Any]]) -> set:
    """Find which move indices (1-indexed) are referenced by restart_move fields.

    These are the moves where checkpoints should be placed.
    """
    checkpoint_indices = set()
    for move in moves:
        rm = move.get('restart_move', 0)
        if rm > 0:
            checkpoint_indices.add(rm)
    return checkpoint_indices


# ============ XML Generation ============

def generate_move_xml(scene_name: str, scene: Dict[str, Any],
                      move_index: int, move: Dict[str, Any],
                      moves: List[Dict[str, Any]],
                      all_deaths: Dict[str, Dict[str, Any]],
                      checkpoint_moves: set,
                      scene_index: int, total_scenes: int) -> str:
    """Generate XML for a single gameplay/cutscene move chapter.

    Args:
        scene_name: Normalized scene name (e.g. 'casino_heist')
        scene: Full scene dict
        move_index: 1-indexed move number
        move: The move dict
        moves: All moves in this scene
        all_deaths: Death chapter lookup table
        checkpoint_moves: Set of move indices that are checkpoint targets
        scene_index: 1-indexed scene number (for scene routing)
        total_scenes: Total number of scenes
    """
    chapter_name = make_move_chapter_name(scene_name, move_index)
    is_cutscene = len(move['correct_moves']) == 0
    has_input = not is_cutscene

    # Chapter timeline: from this move's start_frame to the next move's start_frame
    # (or scene end_frame for the last move)
    chapter_start_frame = move['start_frame']

    if move_index < len(moves):
        # Next move exists
        next_move = moves[move_index]  # move_index is 1-indexed, so moves[move_index] is the next
        chapter_end_frame = next_move['start_frame']
    else:
        # Last move in scene
        chapter_end_frame = scene['end_frame']

    # Ensure end > start
    if chapter_end_frame <= chapter_start_frame:
        chapter_end_frame = chapter_start_frame + 1

    chapter_start_ms = frame_to_ms(chapter_start_frame)
    chapter_end_ms = frame_to_ms(chapter_end_frame)

    # Build XML
    lines: List[str] = []
    lines.append(f'<chapter name="{chapter_name}">')
    lines.append(f'\t<timeline>')
    lines.append(f'\t\t<timestart {ms_to_attrs(chapter_start_ms, chapter_start_frame)} />')
    lines.append(f'\t\t<timeend {ms_to_attrs(chapter_end_ms, chapter_end_frame)} />')
    lines.append(f'\t</timeline>')
    lines.append(f'\t<params />')
    lines.append(f'\t<macros />')
    lines.append(f'\t<events>')

    # Checkpoint event if this move is a restart target
    if move_index in checkpoint_moves:
        lines.append(f'\t\t<event type="checkpoint">')
        lines.append(f'\t\t\t<timeline>')
        lines.append(f'\t\t\t\t<timestart {ms_to_attrs(chapter_start_ms)} />')
        lines.append(f'\t\t\t</timeline>')
        lines.append(f'\t\t</event>')

    # Direction event for input moves
    if has_input:
        # Use the first correct move as the primary input type
        primary_input = move['correct_moves'][0]

        # Map input types for the XML
        # "hands" -> "action" (maps to JOY_BUTTON_A in xmlsceneparser)
        # "feet" -> "feet" (maps to JOY_BUTTON_B in xmlsceneparser -- needs adding)
        # "left", "right", "up", "down" -> as-is
        input_type = primary_input
        if primary_input == 'hands':
            input_type = 'action'
        # "feet" stays as "feet", "left"/"right"/"up"/"down" stay as-is

        # Event timing: from move's start_frame to end_frame (the input window)
        event_start_frame = move['start_frame']
        event_end_frame = move['end_frame']

        # Convert to ms
        event_start_ms = frame_to_ms(event_start_frame)
        event_end_ms = frame_to_ms(event_end_frame)

        # Determine automacro
        if input_type in ('left', 'right', 'up', 'down'):
            automacro = 'direction'
        elif input_type == 'action':
            automacro = 'action'
        elif input_type == 'feet':
            automacro = 'feet'
        else:
            automacro = 'direction'

        # Success result: advance to next move chapter
        if move_index < len(moves):
            next_chapter = make_move_chapter_name(scene_name, move_index + 1)
            success_result = f'<playchapter name="{next_chapter}" />'
        else:
            # Last move in scene -- success advances to scene_router
            if scene_index < total_scenes:
                success_result = '<playchapter name="scene_router" />'
            else:
                # Last scene (ending) -- go to title screen or score entry
                success_result = '<playchapter name="title_screen" />'

        lines.append(f'\t\t<event type="direction" automacro="{automacro}">')
        lines.append(f'\t\t\t<timeline>')
        lines.append(f'\t\t\t\t<timestart {ms_to_attrs(event_start_ms, event_start_frame)} />')
        lines.append(f'\t\t\t\t<timeend {ms_to_attrs(event_end_ms, event_end_frame)} />')
        lines.append(f'\t\t\t</timeline>')
        lines.append(f'\t\t\t<params>')
        lines.append(f'\t\t\t\t<str key="type" value="{input_type}" />')
        lines.append(f'\t\t\t</params>')
        lines.append(f'\t\t\t<result>{success_result}</result>')
        lines.append(f'\t\t</event>')

    lines.append(f'\t</events>')

    # Chapter-level default result (what happens on timeout or wrong input)
    if is_cutscene:
        # Cutscene: auto-advance to next move
        if move_index < len(moves):
            next_chapter = make_move_chapter_name(scene_name, move_index + 1)
            lines.append(f'\t<result><playchapter name="{next_chapter}" /></result>')
        else:
            # Last move in scene
            if scene_index < total_scenes:
                lines.append(f'\t<result><playchapter name="scene_router" /></result>')
            else:
                lines.append(f'\t<result><playchapter name="title_screen" /></result>')
    else:
        # Input move: timeout/wrong input -> death or lastcheckpoint
        dsf = move.get('death_start_frame', 0)
        def_ = move.get('death_end_frame', 0)
        if dsf > 0 and def_ > 0:
            death_chapter = get_death_chapter_name(all_deaths, scene_name, dsf, def_)
            if death_chapter:
                lines.append(f'\t<result><playchapter name="{death_chapter}" /></result>')
            else:
                lines.append(f'\t<result><lastcheckpoint /></result>')
        else:
            lines.append(f'\t<result><lastcheckpoint /></result>')

    lines.append(f'</chapter>')
    return '\n'.join(lines) + '\n'


def generate_death_xml(death_info: Dict[str, Any]) -> str:
    """Generate XML for a death chapter.

    Death chapters play a death animation and then trigger lastcheckpoint
    (which makes the player lose a life and restart from the last checkpoint).
    """
    chapter_name = death_info['chapter_name']
    dsf = death_info['death_start_frame']
    def_ = death_info['death_end_frame']

    start_ms = frame_to_ms(dsf)
    end_ms = frame_to_ms(def_)

    lines: List[str] = []
    lines.append(f'<chapter name="{chapter_name}">')
    lines.append(f'\t<timeline>')
    lines.append(f'\t\t<timestart {ms_to_attrs(start_ms, dsf)} />')
    lines.append(f'\t\t<timeend {ms_to_attrs(end_ms, def_)} />')
    lines.append(f'\t</timeline>')
    lines.append(f'\t<params>')
    lines.append(f'\t\t<int key="kills_player" value="1" />')
    lines.append(f'\t</params>')
    lines.append(f'\t<macros />')
    lines.append(f'\t<events />')
    lines.append(f'\t<result><lastcheckpoint /></result>')
    lines.append(f'</chapter>')
    return '\n'.join(lines) + '\n'


def generate_start_alive_xml(scene_name: str, first_move_chapter: str) -> str:
    """Generate XML for a start_alive routing node.

    This is a zero-duration chapter that immediately creates the first move chapter.
    """
    chapter_name = make_start_alive_name(scene_name)

    lines: List[str] = []
    lines.append(f'<chapter name="{chapter_name}">')
    lines.append(f'\t<timeline>')
    lines.append(f'\t\t<timestart {ms_to_attrs(0)} />')
    lines.append(f'\t\t<timeend {ms_to_attrs(0)} />')
    lines.append(f'\t</timeline>')
    lines.append(f'\t<params />')
    lines.append(f'\t<macros />')
    lines.append(f'\t<events>')
    lines.append(f'\t\t<event type="start_alive">')
    lines.append(f'\t\t\t<timeline>')
    lines.append(f'\t\t\t\t<timestart {ms_to_attrs(0)} />')
    lines.append(f'\t\t\t</timeline>')
    lines.append(f'\t\t</event>')
    lines.append(f'\t</events>')
    lines.append(f'\t<result><playchapter name="{first_move_chapter}" /></result>')
    lines.append(f'</chapter>')
    return '\n'.join(lines) + '\n'


# ============ Main ============

def main() -> None:
    parser = argparse.ArgumentParser(
        description='Generate XML event files from Cliff Hanger DirkSimple game.lua')
    parser.add_argument('--input', default='data/game.lua',
                        help='Path to game.lua (default: data/game.lua)')
    parser.add_argument('--outfolder', default='data/events',
                        help='Output folder for XML files (default: data/events)')
    args = parser.parse_args()

    # Read and preprocess Lua source
    input_path = Path(args.input)
    if not input_path.exists():
        logging.error(f'Input file not found: {input_path}')
        sys.exit(1)

    lua_text = input_path.read_text(encoding='utf-8')
    lua_text = strip_lua_comments(lua_text)

    # Parse scenes table
    scenes = parse_scenes_table(lua_text)
    if not scenes:
        logging.error('No scenes found in game.lua')
        sys.exit(1)
    logging.info(f'Parsed {len(scenes)} scenes from game.lua')

    for i, scene in enumerate(scenes):
        move_count = len(scene.get('moves', []))
        logging.info(f'  Scene {i + 1}: {scene["scene_name"]} ({move_count} moves)')

    # Collect all unique death sequences
    all_deaths = collect_death_sequences(scenes)
    logging.info(f'Found {len(all_deaths)} unique death sequences')

    # Create output folder
    outfolder = Path(args.outfolder)
    outfolder.mkdir(parents=True, exist_ok=True)

    # Generate XML files
    total_scenes = len(scenes)
    total_chapters = 0
    total_moves = 0
    total_cutscenes = 0
    total_deaths = 0
    total_start_alive = 0
    generated_files: List[str] = []

    for scene_index, scene in enumerate(scenes, start=1):
        scene_display = scene['scene_name']
        scene_name = normalize_scene_name(scene_display)
        moves = scene.get('moves', [])

        if not moves:
            logging.warning(f'Scene {scene_display} has no moves, skipping')
            continue

        # Find checkpoint moves for this scene
        checkpoint_moves = find_checkpoint_moves(moves)

        # Generate start_alive chapter
        first_move_chapter = make_move_chapter_name(scene_name, 1)
        start_alive_xml = generate_start_alive_xml(scene_name, first_move_chapter)
        start_alive_name = make_start_alive_name(scene_name)
        xml_path = outfolder / f'{start_alive_name}.xml'
        xml_path.write_text(start_alive_xml, encoding='utf-8')
        generated_files.append(start_alive_name)
        total_start_alive += 1
        total_chapters += 1

        # Generate move chapters
        for move_idx, move in enumerate(moves, start=1):
            is_cutscene = len(move['correct_moves']) == 0

            move_xml = generate_move_xml(
                scene_name, scene, move_idx, move, moves,
                all_deaths, checkpoint_moves,
                scene_index, total_scenes
            )

            chapter_name = make_move_chapter_name(scene_name, move_idx)
            xml_path = outfolder / f'{chapter_name}.xml'
            xml_path.write_text(move_xml, encoding='utf-8')
            generated_files.append(chapter_name)
            total_chapters += 1
            total_moves += 1

            if is_cutscene:
                total_cutscenes += 1

    # Generate death chapters
    for full_key, death_info in sorted(all_deaths.items()):
        death_xml = generate_death_xml(death_info)
        xml_path = outfolder / f'{death_info["chapter_name"]}.xml'
        xml_path.write_text(death_xml, encoding='utf-8')
        generated_files.append(death_info['chapter_name'])
        total_chapters += 1
        total_deaths += 1

    # Check for orphan XML files
    existing_xmls = {f.stem for f in outfolder.glob('*.xml')}
    generated_set = set(generated_files)
    orphans = existing_xmls - generated_set
    if orphans:
        logging.warning(f'{len(orphans)} orphan XML files not generated by this run:')
        for name in sorted(orphans):
            logging.warning(f'  {name}.xml')

    # Verify all generated files have <result> elements
    missing_result = 0
    for name in generated_files:
        xml_path = outfolder / f'{name}.xml'
        content = xml_path.read_text()
        # Count chapter-level results (exclude event-level results)
        # The last <result> before </chapter> should be the chapter result
        if '<result>' not in content:
            logging.warning(f'Missing <result> in {name}.xml')
            missing_result += 1

    if missing_result:
        logging.warning(f'{missing_result} files missing <result> elements!')
    else:
        logging.info('All generated XML files have <result> elements.')

    # Count input types
    input_counts: Dict[str, int] = {}
    for scene in scenes:
        for move in scene.get('moves', []):
            for cm in move.get('correct_moves', []):
                input_counts[cm] = input_counts.get(cm, 0) + 1

    # Summary
    logging.info(f'\nSummary:')
    logging.info(f'  Scenes: {total_scenes}')
    logging.info(f'  Total chapters generated: {total_chapters}')
    logging.info(f'    Start alive: {total_start_alive}')
    logging.info(f'    Move chapters: {total_moves}')
    logging.info(f'      Cutscenes (no input): {total_cutscenes}')
    logging.info(f'      Input moves: {total_moves - total_cutscenes}')
    logging.info(f'    Death chapters: {total_deaths}')
    logging.info(f'  Orphan XMLs: {len(orphans)}')
    logging.info(f'  Missing results: {missing_result}')
    logging.info(f'  Input type distribution:')
    for input_type, count in sorted(input_counts.items()):
        logging.info(f'    {input_type}: {count}')


if __name__ == '__main__':
    main()
