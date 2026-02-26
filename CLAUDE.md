# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

SNES Cliff Hanger Arcade — a full-motion-video (FMV) retheme of the Cliff Hanger laserdisc arcade game for the Super Nintendo, targeting real NTSC SNES hardware with MSU-1 audio/video on SD2SNES/FXPAK Pro. Written in 65816 assembly with a custom OOP framework.

Cliff Hanger features two action button types: "hands" (A button, `JOY_BUTTON_A`) and "feet" (B button, `JOY_BUTTON_B`), plus directional inputs (left, right, up, down). The game has 8 linear scenes progressing from Casino Heist through the Ending.

## Build Commands

Build runs under WSL. The project uses WLA-DX v9.3 assembler (v9.4+ breaks the build).

```bash
# Standard build (clean + build, ~2-3 min)
wsl -e bash -c "cd <wsl-project-root> && make clean && make"

# Fast rebuild (skip clean if only .65816/.script files changed)
wsl -e bash -c "cd <wsl-project-root> && make"

# Build output
# ROM: build/CliffHangerArcade.sfc
# Also copied to: distribution/CliffHangerArcade.sfc
```

**Build warnings that are normal:**
- `DIRECTIVE_ERROR` about redefined `__init`/`__play`/`__kill` — from CLASS macro in event files
- `DISCARD` messages — unused event sections stripped by `-d` linker flag

**Emulator testing with Mesen 2:**
```bat
:: Mesen.exe is at mesen\Mesen.exe (inside the project)
:: ROM MUST load from distribution/ where .msu/.pcm files live
:: Use cmd.exe redirect for reliable output capture (PowerShell Out-String can truncate)
cmd.exe /c "cd /d <project>\distribution && <project>\mesen\Mesen.exe --testrunner CliffHangerArcade.sfc script.lua > out.txt 2>&1"
```

**Mesen Lua API quirks:**
- `emu.getState()` returns a flat table with dot-separated string keys: use `state["cpu.a"]` NOT `state.cpu.a`
- `emu.setInput({a = true})` — no port number argument, just a table
- `emu.setInput` does NOT inject into hardware JOY1L — NMI's `_checkInputDevice` overwrites WRAM from hardware. To inject input, use an exec callback at `_checkInputDevice`'s RTS address to write WRAM directly: press=`$6D06`, trigger=`$6D08`, old=`$6D0C` (verify in .sym — shifts when maxNumberOopObjs changes)
- `_checkInputDevice` address shifts between builds — look it up in `build/CliffHangerArcade.sym` after each rebuild
- `io.open` does not work in testrunner mode; use `print()` with output redirect
- MSU-1 requires ROM in same folder as .msu/.pcm files. Debug scripts in `mesen/` directory.

**CRITICAL: ROM addresses shift on EVERY code change.** Any change to `.65816`, `.script`, or `.h` files (even adding a comment) can shift all symbol addresses in `build/CliffHangerArcade.sym`. WRAM addresses (`$7E****`) are stable, but ROM code addresses are NOT. After every build, you MUST re-read the sym file to update Mesen Lua test scripts. Hardcoded addresses in test scripts will silently break (callbacks never fire, tests appear to hang or produce no output). This is the #1 cause of "inconclusive" test results.

## Architecture

### Memory Map
- HiROM+FastROM, 16 banks x 64KB = 1MB ROM
- Slot 0: $0000-$FFFF (ROM), Slot 1: $7E2000 (Work RAM), Slot 2: zero page
- Checksum values hardcoded in header — "Invalid Checksum" in emulators is expected

### OOP System (`src/core/oop.65816`)
Custom object system with 48 concurrent object slots. Each object has init/play/kill methods, a direct page (ZP) allocation, and properties bitmask.

**Key macros** (defined in `src/config/macros.inc`):
- `CLASS name method1 method2...` — defines a class with method table
- `METHOD name` — defines an instance method
- `NEW class.CLS.PTR hashPtr args...` — creates object instance, stores hash pointer
- `CALL class.method.MTD hashPtr args...` — dispatches method call via hash pointer
- `TRIGGER_ERROR E_code` — expands to `pea E_code; jsr core.error.trigger` (fatal, calls stp)

**Object properties** (`src/config/globals.inc`):
- `isScript=$0001`, `isChapter=$0002`, `isEvent=$0004`, `isHdma=$0008`, `isSerializable=$1000`
- `killOthers` uses bitmask AND matching — ALL requested bits must be present

**Singleton objects**: Brightness, Spc have `OBJECT.FLAGS.Singleton`. Creating a singleton that already exists returns the existing instance WITHOUT calling init again.

### Script System (`src/object/script/`)
Scripts are 65816 code that runs synchronously during init (via `bra _play`) until the first `jsr SavePC`, then resumes one iteration per frame. Key macros: `SCRIPT`, `DIE`, `SavePC`, `WAIT`.

**Script ZP layout** (96 bytes total):
- iteratorStruct (28 bytes, offset 0) — self, properties, target, index, count, sort fields
- scriptStruct (4 bytes, offset 28) — timestamp, initAddress
- vars (28 bytes, offset 32) — _tmp[16], currPC, buffFlags, buffBank, buffA/X/Y, buffStack
- hashPtr (36 bytes, offset 60) — 9 hash pointers x 4 bytes each (id, count, pntr)

**Hash pointer access**: `hashPtr.N` is 1-indexed. `hashPtr.1` = offset 60, `hashPtr.N` = offset 60 + (N-1)*4.

### Game Flow
```
boot.65816 -> main.script -> msu1.script -> losers.script -> logo_intro.script -> title_screen.script
  -> Arcade Mode: level1.script -> csno_start_alive -> ... -> scene_router -> ... -> ending -> score_entry -> title_screen
```
Each script creates the next via `NEW Script.CLS.PTR oopCreateNoPtr nextScript` then `DIE`.

**Game over flow**: When all lives are lost, `EventResult.lastcheckpoint` routes to `game_over.script`, which transitions directly to `score_entry.script` (name entry + high score save). From there, back to `title_screen`.

**Continue screen**: `continue_screen.script` provides a 9-second countdown for the player to insert credits (SELECT) and continue (START). On timeout, transitions to `score_entry`. On continue, restarts from `GLOBAL.lastCheckpoint`.

### Scene/Chapter System

The game is divided into 8 scenes, each containing multiple chapters (short video segments with events). Chapters chain together via EventResult handlers based on player input.

**Scene flow**: `title_screen` -> `level1` -> first chapter -> player input triggers -> next chapter -> ... -> scene complete -> `scene_router` -> next scene -> ... -> ending -> `score_entry`.

**Level entry script** (`src/level1.script`) is the single entry point:
```asm
SCRIPT level1
NEW Script.CLS.PTR oopCreateNoPtr csno_start_alive
DIE
```

**8 scenes** (with abbreviations used in chapter names):

| # | Scene | Abbrev | Chapters |
|---|-------|--------|----------|
| 1 | casino_heist | csno | 15 |
| 2 | the_getaway | gtwy | 35 |
| 3 | rooftops | roof | 41 |
| 4 | highway | hway | 53 |
| 5 | the_castle_battle | cstl | 14 |
| 6 | finale | fnle | 17 |
| 7 | finale_ii | fn2 | 34 |
| 8 | ending | endg | 56 |

Total: 265 chapter XML files across all scenes.

**Chapter naming convention**: `{abbrev}_move{NN}` for gameplay chapters, `{abbrev}_death{NN}` for death sequences, `{abbrev}_start_alive` for scene entry routing nodes.

### XML Event Files and Conversion Pipeline

**Source**: XML files in `data/events/*.xml` (265 files, generated from Cliff Hanger DirkSimple game data via `lua_scene_exporter_cliff.py`)

Each XML defines one chapter with a timeline and events:
```xml
<chapter name="csno_move01">
  <timeline>
    <timestart min="1" second="0" ms="60" frame="1800" />
    <timeend min="1" second="4" ms="331" frame="1928" />
  </timeline>
  <events>
    <event type="checkpoint">
      <timeline><timestart min="1" second="0" ms="60" /></timeline>
    </event>
  </events>
  <result><playchapter name="csno_move02" /></result>
</chapter>
```

**Conversion tool**: `tools/xmlsceneparser.py` converts each XML into two assembly files:
```bash
python3 tools/xmlsceneparser.py data/events/csno_move01.xml
```

**Output per chapter** (in `data/chapters/<name>/`):
- **`chapter.script`** — code (~10 bytes): `CHAPTER` macro + 24-bit pointer to event data + `DIE`
- **`chapter.data`** — event data table: 7 words per event (14 bytes), terminated by `.dw 0`

**Event data format** (per entry, 14 bytes):
```
+0: .dw Event.{TYPE}.CLS.PTR    (class pointer for event object)
+2: .dw STARTFRAME              (chapter-relative, 16-bit)
+4: .dw ENDFRAME                (chapter-relative, 16-bit)
+6: .dw EventResult.{RESULT}    (result handler: playchapter, restartchapter, lastcheckpoint, none)
+8: .dw {RESULT_TARGET}         (target chapter label, or 'none')
+10: .dw {ARG0}                 (event-specific: direction mask, sequence number, etc.)
+12: .dw {ARG1}                 (event-specific: for direction events, bit 0 = hide arrow sprite)
```

**Type normalization** by xmlsceneparser.py:
- `direction` + `type="left"` -> `Event.direction_generic` with arg0=`JOY_DIR_LEFT`
- `direction` + `type="right"` -> arg0=`JOY_DIR_RIGHT`, etc.
- `direction` + `type="action"` -> arg0=`JOY_BUTTON_A` (hands)
- `direction` + `type="feet"` -> arg0=`JOY_BUTTON_B` (feet)
- `room_transition` subtypes (enter_room, start_alive, start_dead) -> encoded arg0 (0-7)

**Frame timing**: XML uses min/sec/ms with frame attributes (from Cliff Hanger laserdisc at 29.97 fps). Startframe/endframe are chapter-relative (event frame - chapter start frame), clamped to 16-bit.

### Chapter Initialization and Event Results

**`_CHAPTER.init`** (`src/object/script/script.h`): First kills all events from the previous chapter via `kill.byProperties(isEvent)`, then sets properties to `isChapter` and kills other chapter scripts via `killOthers`. Reads 24-bit inline pointer to event data, loops creating event objects from the data table via `core.object.create`.

**EventResult handlers** (`src/object/event/abstract.Event.65816`):
- **`EventResult.none`** — kill self, no further action
- **`EventResult.playchapter`** — create new chapter Script from resultTarget label (the primary scene transition mechanism)
- **`EventResult.restartchapter`** — restart current chapter from last checkpoint
- **`EventResult.lastcheckpoint`** — player loses a life; if game over -> `game_over` script; else restart from checkpoint

**Scene transitions**: When a player presses the correct input during a direction event's active window, `abstract.Event.triggerResult` calls the EventResult handler. `EventResult.playchapter` creates a new chapter Script, whose `_CHAPTER.init` kills all old events and the old chapter. If no input is given, `Event.chapter` fires its default result (usually death/restart) when the chapter's endframe is reached.

### Event/Chapter File Aggregation

All chapter scripts are aggregated in `data/chapters/chapter.include`, all data in `data/chapters/chapter_data.include`. Chapter data goes in ONE `superfree` section in `chapter_data.65816` (wla-dx has a ~512 section-per-file limit).

**Event classes** for gameplay: `direction_generic`, `chapter`, `checkpoint`, `room_transition`, `seq_generic`, `cutscene`, `accelerate`, `brake`, `shake`, `touch`, `target`, `confirm`, `show_help`, `hide_dash`, `change_dash`, plus scene-specific events. Create new events with `python tools/create_event.py Event.myname`.

### Title Screen Menu

The title screen (`src/title_screen.script`) has a menu system:
- **Main menu**: ARCADE MODE, OPTIONS (2 items, cursor 0-1)
- **Options submenu**: HIGH SCORES, SOUND TEST, SCENE SELECT (3 items, cursor 0-2)
- **Sound test**: L/R selects sample 0-6, A plays it
- **Scene select**: L/R selects scene 1-8, A launches it via `_title_screen.sceneTable`

Main menu cursor 0 selects Arcade Mode (needs credits; sets `GLOBAL.gameMode` to 0, dispatches to `level1`). Cursor 1 enters OPTIONS.

Menu items start at tilemap position `$286` (row 20, col 6). Cursor drawn at `$286 + cursor * 32`.

**Credits system**: SELECT inserts a coin (increments `GLOBAL.credits`, max 99). Starting Arcade Mode costs one credit. Konami code grants 30 credits (`GLOBAL.konamiActivated`).

**Idle timeout**: 1 minute (3600 frames) of no input triggers attract mode (`atmd_start_alive`).

**Transition pattern** (must use dedicated SavePC): After fadeTo black, a `jsr SavePC` creates a new resume point. Each frame polls `Brightness.isDone`; when done, cleanup kills objects, clears VRAM/CGRAM, creates target Script, and DIEs. The inline SavePC replaces the menu's SavePC — menu input stops during transition.

### Scene Router

`src/scene_router.script` routes the player to the next scene after completing a scene. Unlike Dragon's Lair, Cliff Hanger uses **linear 8-scene progression** with no randomization and no alternate game modes.

**`GLOBAL.sceneRow`** (word at WRAM): Tracks current scene index (0-7). Incremented after each scene.

**Scene table** (`_scene_router.sceneTable`): 8 entries in order:
```
row 0: csno_start_alive  (Casino Heist)
row 1: gtwy_start_alive  (The Getaway)
row 2: roof_start_alive  (Rooftops)
row 3: hway_start_alive  (Highway)
row 4: cstl_start_alive  (The Castle Battle)
row 5: fnle_start_alive  (Finale)
row 6: fn2_start_alive   (Finale II)
row 7: endg_start_alive  (Ending)
```

After completing all 8 scenes (row >= 8), the router transitions to `score_entry` (victory path). The scene name index for the pause screen is set from a parallel byte table (`_scene_router.sceneNameTable`).

**Scene exit routing** (in `lua_scene_exporter_cliff.py`): The last move of each scene routes to `scene_router`, except the last scene (ending) which routes to `title_screen`.

### MSU-1 Video Data Pipeline

The `generate_msu_data.py` script handles the full video pipeline. The current script still references Dragon's Lair paths/constants internally but the Cliff Hanger project uses the same Daphne framefile-based extraction system.

**Video source**: Daphne .m2v segment files (interlaced 29.97fps MPEG-2 for Cliff Hanger), deinterlaced and rate-converted by ffmpeg.

```bash
# Full pipeline: extract frames + audio from .m2v/.ogg, convert tiles, package .msu
wsl -e bash -c "cd <wsl-project-root> && python3 tools/generate_msu_data.py --workers 8"

# Clean + full pipeline (re-extracts everything from scratch)
wsl -e bash -c "cd <wsl-project-root> && python3 tools/generate_msu_data.py --clean --workers 8"

# Skip extraction (use existing PNG frames), only convert tiles + package .msu
wsl -e bash -c "cd <wsl-project-root> && python3 tools/generate_msu_data.py --skip-extract --workers 8"

# Output: build/CliffHangerArcade.msu
# Also copied to: distribution/CliffHangerArcade.msu
```

**Frame extraction details**: Each .m2v segment is decoded CPU-only (no CUDA) with this ffmpeg filter chain:
```
yadif,fps=24000/1001,trim=start={offset_s}:duration={dur_s},setpts=PTS-STARTPTS,scale=256:160
```
- `yadif` deinterlaces the 29.97fps interlaced MPEG-2
- `fps=24000/1001` rate-converts to MSU-1 playback rate BEFORE trimming
- `trim` selects the frame range (no `-ss` seeking — decode from start for accuracy)
- `setpts=PTS-STARTPTS` resets timestamps after trim
- Output: 16-color paletted PNGs via split/palettegen/paletteuse

**Cliff Hanger scene exporter**: `tools/lua_scene_exporter_cliff.py` reads `data/game.lua` (DirkSimple Cliff Hanger game data) and generates XML event files. Cliff Hanger has 8 scenes with flat move arrays, unlike Dragon's Lair's nested sequence tables. The laserdisc runs at 29.97 fps (NTSC standard).

**Frame timing resolution**: `lua_scene_exporter_cliff.py` uses frame numbers directly from the DirkSimple game.lua moves table (each move has `start_frame` and `end_frame` attributes at 29.97 fps).

**MSU-1 audio track numbering**: PCM files are named `CliffHangerArcade-{chapterID}.pcm` where chapterID matches the chapter's index in the .msu pointer table (from `chapter.id.NNN` files in each chapter directory). The ROM passes `this.currentChapter` as the audio track number to MSU-1 hardware.

**manifest.xml**: Required by some emulators (bsnes/higan) to map MSU-1 PCM tracks. Located at `distribution/manifest.xml`. Must list only tracks that have corresponding PCM files. Regenerate after any MSU data change by scanning actual PCM files in the distribution directory.

**LD frame table**: `tools/generate_ld_frame_table.py` generates `data/chapters/chapter_ld_frames.inc` — a ROM lookup table mapping chapter ID to laserdisc start frame, used for the MSU-1 skip feature. Built automatically during `make`.

**Pipeline steps** (per chapter):
1. ffmpeg extracts 256x160 PNG frames from .m2v segment (CPU decode, yadif+trim filter chain)
2. ffmpeg extracts audio from paired .ogg segment -> WAV -> PCM
3. superfamiconv converts each PNG to SNES palette/tiles/tilemap
4. `reduce_tiles()` merges unique tiles down to 384 (VRAM limit) using global greedy merge with RGB-space L2 distance
5. msu1blockwriter.py packages all chapters into a single `.msu` file + per-chapter `.pcm` files

**Key constraints:**
- VRAM tile buffer = $3000 bytes = 384 tiles at 4BPP (32 bytes/tile). Each 256x160 frame has up to 640 unique tiles, requiring lossy tile reduction.
- MSU title in `.msu` file must exactly match ROM header title (`CLIFF HANGER ARCADE`) or `_isMsu1FilePresent` rejects it. The makefile sets `-title "CLIFF HANGER ARCADE"`.
- `make clean` DELETES `data/chapters/` — wipes all extracted video frames. Run MSU generation AFTER final build, or use `make` without `clean`.
- BLAS thread safety: `OPENBLAS_NUM_THREADS=1` must be set before `import numpy` in generate_msu_data.py — multi-threaded BLAS corrupts results when called from concurrent Python threads.
- Frame dimensions: 256x160 (20 tile rows), tilemap target size = 1280 bytes (32x20 tiles x 2 bytes).
- Two sub-palettes per frame (2 x 16 = 32 max colors).

## Critical Pitfalls

### wla-dx `.def` Cannot Redefine
**`.def X Y` followed by `.def X Z` -> the second definition is SILENTLY IGNORED.** This has caused hash pointer collision bugs. `script.h` predefines: `objBrightness` at `hashPtr+12` (=hashPtr.4), `objPlayer` at `hashPtr+16` (=hashPtr.5). Scripts must work around these slots, not try to redefine them. Use `.redefine` or `.undefine`+`.define` if redefinition is truly needed.

### Hash Pointer Collisions
Each script has 9 hash pointer slots (hashPtr.1 through hashPtr.9). If two `.def` symbols resolve to the same hashPtr slot, the second `NEW` overwrites the first's hash, causing `CALL` to dispatch to the wrong object (silent failure, no crash).

### `oopCreateNoPtr` = $FFFF
Used as null pointer for the hash system. `CALL` with hash pntr=$FFFF dispatches to `dispatchObjMethodHashVoid` (safe no-op). Never use hash pntr=0 — it matches OopStack slot 0.

### SPC700 Audio Constraints
SPC700 has 64KB RAM total. Engine code ~6.5KB, leaving ~57.5KB for BRR samples. Adding samples requires checking total BRR size stays under this limit.

### MSU-1 Sound Effects
Sound effect plays as MSU-1 PCM track 900 during the MSU-1 splash screen (`msu1.script`). Too large for SPC, so it uses the MSU-1 audio hardware instead. Source WAV converted to MSU-1 PCM format (44100 Hz stereo 16-bit LE with `MSU1` header). The `Msu1.audio` singleton auto-mutes when the track ends via `_checkTrackEnd`.

### CGRAM (Palette) Limits
SNES has 8 BG palettes max for 4BPP mode ($100 bytes). `animationWriter_sfc.py` now forces `-P 1` (single sub-palette) in superfamiconv palette generation to prevent CGRAM overflow. CGRAM allocation failure in `abstract.Background.65816` is non-fatal (falls back to palette position 0). Default BG palettes in makefile reduced from 8 to 3.

### wla-dx `_` Prefix = Local Labels
Labels starting with `_` are LOCAL to the compilation unit (.o file). They cannot be referenced from other .o files — causes FIX_REFERENCES at link time. Use labels without `_` prefix for cross-file references.

### wla-dx Section Limit Per File
wla-dx has a maximum of ~512 sections per compilation unit. Exceeding this gives "Out of section numbers. Please start a new file." — solved by combining data into fewer, larger sections.

### wla-dx Anonymous Label Pitfalls
`+`, `++`, `+++` etc. are DISTINCT label tiers in wla-dx — `+` only matches `+`, `++` only matches `++`. A `bra ++` does NOT mean "second `+` forward" — it means "next `++` label forward". Long macro expansions (NEW, CALL) generate many bytes; branches over them easily exceed the 8-bit 127-byte limit. Use named labels or `jmp` for long forward references. The `bne label / jmp target / label:` pattern replaces a too-far `beq target`.

### Event kill Methods Must Use `jmp`, Not `jsr`
Event kill methods that delegate to `Event.template.kill` MUST use `jmp`, not `jsr`. `Event.template.kill` uses `sta 3,s` to write `OBJR_kill` to the stack. With `jsr`, the extra return address shifts the stack so `OBJR_kill` overwrites the wrong location, then `rts` returns to the call site and falls through into CLASS macro binary data, hitting `$00` = BRK -> E_Brk crash. Correct pattern: `METHOD kill / jmp Event.template.kill`.

### Class File Pattern
Every class has a `.h` (header) and `.65816` (implementation). The header defines the ZP struct layout, `CLASS.FLAGS`, `CLASS.PROPERTIES`, `CLASS.ZP_LENGTH`, and optionally `CLASS.IMPLEMENTS`. The `.65816` file includes the `.h`, opens a `.section`, uses `METHOD init`/`play`/`kill` to define methods, and ends with `CLASS ClassName [extraMethods]` + `.ends`.

### Stack-Relative Addressing in Subroutines
When reading `OBJECT.CALL.ARG.N,s` from a subroutine called via `jsr` from an init/play method, add +2 to compensate for the extra return address on the stack. `OBJECT.CALL.ARG` offsets assume the reader is the DIRECT callee of `OopHandlerExecute`'s `jsr (0,x)`. `Event.template.initCommon` uses `OBJECT.CALL.ARG.N+2,s` for this reason. Event classes that read args directly in their init method (Event.chapter, Event.cutscene) do NOT need the +2.

### `core.string.parse_from_object` Overwrites String Arguments
`core.string.parse_from_object` (called by `Background.textlayer.8x8.print`) reads CALL stack arguments and writes them to `GLOBAL.CORE.STRING.argument.0` through `.3`. **Any values written to those globals before the print CALL will be overwritten.** To pass values for `TC_dToS`/`TC_hToS` display, push them on the stack before the CALL (deepest push -> argument.2, shallowest -> argument.0), and pop them after. Example:
```asm
pha           ; push chapter (-> argument.2, deepest)
pha           ; push lives   (-> argument.1)
pha           ; push score   (-> argument.0, shallowest)
lda #T_pause.PTR
CALL Background.textlayer.8x8.print.MTD this.textlayer
pla           ; pop score
pla           ; pop lives
pla           ; pop chapter
```

### Cross-Unit RAMSECTION Addresses Are 24-bit
RAMSECTION labels referenced from a different compilation unit resolve to full 24-bit addresses (e.g., `$7E699C`). `sta.w` and `pea` with such labels cause `COMPUTE_PENDING_CALCULATIONS: out of 16bit range`. Either use `sta.l` (long addressing) or keep the data in ROM within the same section and reference it with local labels + `:label` for the bank byte.

### 16-bit `lda` on `db` (byte) Fields
In 16-bit accumulator mode (`rep #$20`), `lda zp_offset` reads 2 bytes. If a `db` field is at the end of the ZP allocation (offset = zpLen - 1), the second byte reads into the adjacent object's ZP, producing a garbage high byte. Always mask with `and #$00FF` after reading, or use `sep #$20` to switch to 8-bit mode. Similarly, use `sep #$20` / `lda #value` / `sta field` / `rep #$20` when writing byte fields.

## Key Files

| File | Purpose |
|------|---------|
| `src/config/macros.inc` | All macros: CLASS, METHOD, NEW, CALL, SCRIPT, EVENT, etc. |
| `src/config/globals.inc` | Object properties, flags, global enums |
| `src/config/structs.inc` | Data structures: iteratorStruct, animationStruct, eventStruct |
| `src/core/oop.65816` | Object creation, singleton handling, method dispatch |
| `src/core/oop.h` | OBJID enum, OopClassLut (class registration) |
| `src/core/error.h` | Error code enum, hardcoded grey font palette ($6318 BGR555) |
| `src/core/boot.65816` | Entry point, main loop, interrupt vectors |
| `src/object/script/script.h` | Script class definition, hash pointer defaults |
| `src/object/brightness/brightness.65816` | Screen fade control (singleton) |
| `src/object/iterator/abstract.Iterator.65816` | killOthers, each.byProperties, setProperties |
| `src/object/event/abstract.Event.65816` | Base event class, EventResult handlers (playchapter, restartchapter, etc.) |
| `src/object/script/chapter_data.65816` | All chapter event data tables (one superfree section) |
| `src/level1.script` | Level entry point (creates csno_start_alive) |
| `src/title_screen.script` | Title screen with 2-item main menu and scene select (8 scenes) |
| `src/scene_router.script` | Linear 8-scene routing, no randomization |
| `src/score_entry.script` | Name entry + high score save after game completion or game over |
| `src/continue_screen.script` | 9-second countdown continue screen on game over |
| `src/game_over.script` | Routes to score_entry on game over |
| `src/hall_of_fame.script` | High score display screen |
| `src/losers.script` | Credits/losers screen (shown after MSU-1 init) |
| `src/object/player/pause.65816` | Pause menu overlay — shows scene name, score, lives, credits, chapter, frame |
| `tools/xmlsceneparser.py` | XML chapter events -> assembly `.script` + `.data` files |
| `tools/lua_scene_exporter_cliff.py` | Exports Cliff Hanger game.lua scene data to XML events |
| `tools/create_event.py` | Generate boilerplate for new Event classes |
| `tools/generate_msu_data.py` | Full MSU-1 video pipeline: .m2v -> ffmpeg -> superfamiconv -> tile reduction -> .msu |
| `tools/generate_ld_frame_table.py` | Generates chapter_ld_frames.inc (chapter ID -> LD start frame lookup table) |
| `tools/generate_playthrough_tests.py` | Generates per-scene Mesen Lua test scripts that verify scenes are beatable |
| `tools/msu1blockwriter.py` | Packages tile/tilemap/palette data into .msu file format |
| `data/game.lua` | DirkSimple Cliff Hanger game data (source for scene/move definitions) |
| `data/events/*.xml` | 265 XML scene definitions (generated by lua_scene_exporter_cliff.py) |
| `data/chapters/chapter.include` | Aggregates all generated chapter.script files |
| `data/chapters/chapter_data.include` | Aggregates all generated chapter.data files |
| `data/chapters/chapter_ld_frames.inc` | LD start-frame lookup table for MSU-1 skip feature |
| `build/CliffHangerArcade.sym` | Symbol table — look up addresses here after each build |
| `distribution/manifest.xml` | MSU-1 track manifest for emulators (lists PCM files by chapter ID) |

## Mesen Lua Test Runner — MANDATORY

**Mesen path**: `mesen/Mesen.exe` (inside the project)

**Run command** (ROM MUST load from `distribution/` where .msu/.pcm files live, NOT from build/):
```bat
cmd.exe /c "cd /d <project>\distribution && <project>\mesen\Mesen.exe --testrunner CliffHangerArcade.sfc test_myscript.lua > out.txt 2>&1"
```
Loading from `build/` will crash at frame ~4 with an MSU-1 error (no .msu/.pcm files there). Copy test scripts to `distribution/` before running. Output via `print()` only (`io.open` is broken in testrunner mode).

**WRAM addresses** (shift when `maxNumberOopObjs` or any RAMSECTION changes — verify in .sym):
- `$7E7306` — inputDevice.press (current buttons)
- `$7E7308` — inputDevice.trigger (newly pressed this frame)
- `$7E730C` — inputDevice.old (previous frame)
- `$7E6388` — OopStack base (stable)
- `$7E731E` — GLOBAL.currentFrame

**Addresses that CHANGE every rebuild** — look up in `build/CliffHangerArcade.sym`:
- `core.error.trigger` — hook for fatal error detection
- `_checkInputDevice` — entry point; the RTS is at a fixed offset from entry (currently +$1E bytes)
- `abstract.Event.triggerResult` — hook to watch chapter transitions
- `EventResult.lastcheckpoint` — hook for death path monitoring
- `core.object.create` — hook for object creation tracking
- `_playVideo` — MSU-1 video entry point
- `_initScriptNotInvalid` — hook for Script.init validation
- Chapter labels (`csno_start_alive`, `gtwy_start_alive`, `game_over`, etc.)

**CRITICAL: Address update procedure after EVERY build:**
1. Grep the sym file for each address used in your test script
2. For `_checkInputDevice`, add +$1E to the entry address to get the RTS address
3. Add `$C0` bank prefix to all ROM addresses (e.g. sym `$7412` -> `0xC07412`)
4. Update ALL address constants in the Lua test script before running
5. If a test produces no output or "INCONCLUSIVE", stale addresses are the most likely cause

**CRITICAL mistakes to avoid:**
1. **`emu.write()` is SINGLE BYTE.** Joypad values are 16-bit (e.g. `JOY_START=0x1000`). You MUST write two bytes with a `writeWord()` helper — see template below.
2. **Hook at the RTS, not the entry point.** `_checkInputDevice`'s body reads hardware JOY1L and overwrites WRAM. If you hook the entry, the function body runs AFTER your write and clobbers it. Hook the `rts` at the END of the function.
3. **Exec callbacks need `$C0xxxx` bank.** Sym shows `$7412` -> use `0xC07412`. Without `$C0` prefix the callback never fires.
4. **Use `emu.memType.snesMemory`**, not `cpuDebug` or `workRam`, for WRAM reads/writes.
5. **Use `state["ppu.frameCount"]`** for timing, not a manual counter. Mesen's `emu.getState()` returns flat string-keyed tables: `state["cpu.a"]` not `state.cpu.a`.
6. **Stale addresses = silent failure.** ROM code addresses shift on every build. Test scripts with old addresses produce "INCONCLUSIVE" results (callbacks never fire). Always re-read the sym file after building.
7. **Single-frame injection windows for menus.** The title screen processes one input per frame. Multi-frame injection (e.g. `{600,602,JOY_DOWN}`) causes 3 separate presses — DOWN toggles cursor back and forth, A enters options then immediately selects the first item. Use `{frame, frame, button}` for menus.
8. **No START during boot.** msu1.script's SavePC 59 LOOPS when START/A is pressed (`beq +; rts; +`). Pressing START delays the splash instead of skipping it. Let it auto-advance (~128 frames via `SCRIPT.MAX_AGE.DEFAULT`). Boot sequence takes ~575 PPU frames total to reach the title screen menu.
9. **Put navigation logic in exec callback, not endFrame.** `endFrame` fires AFTER the frame's main loop; `exec` at `_checkInputDevice` RTS fires during the NEXT frame's NMI. Using `endFrame` to set a button variable introduces a 1-frame delay. For menu navigation (where timing is exact), check `ppu.frameCount` directly inside the exec callback.

**Standard test script template:**
```lua
-- ============ ADDRESSES (MUST update after every build from .sym file) ============
local ADDR_ERROR_TRIGGER   = 0xC05905  -- grep 'core.error.trigger' build/*.sym
local ADDR_CHECK_INPUT_RTS = 0xC07412  -- _checkInputDevice entry + $1E
local ADDR_TRIGGER_RESULT  = 0xC06638  -- abstract.Event.triggerResult
local ADDR_INPUT_PRESS     = 0x7E7306  -- grep 'inputDevice.press' build/*.sym
local ADDR_INPUT_TRIGGER   = 0x7E7308  -- grep 'inputDevice.trigger' build/*.sym
local ADDR_INPUT_OLD       = 0x7E730C  -- grep 'inputDevice.old' build/*.sym

local JOY_START = 0x1000; local JOY_A = 0x0080; local JOY_B = 0x8000
local JOY_DOWN = 0x0400;  local JOY_RIGHT = 0x0100
local JOY_LEFT = 0x0200;  local JOY_UP = 0x0800
local MAX_FRAMES = 6000

local function readWord(addr)
    return emu.read(addr, emu.memType.snesMemory)
         + emu.read(addr + 1, emu.memType.snesMemory) * 256
end
local function writeWord(addr, val)
    emu.write(addr, val & 0xFF, emu.memType.snesMemory)
    emu.write(addr + 1, (val >> 8) & 0xFF, emu.memType.snesMemory)
end

local injectButton = 0
local errorHit = false

-- Input injection at _checkInputDevice RTS
emu.addMemoryCallback(function()
    if injectButton ~= 0 then
        writeWord(ADDR_INPUT_PRESS, injectButton)
        writeWord(ADDR_INPUT_TRIGGER, injectButton)
        writeWord(ADDR_INPUT_OLD, 0)
    end
end, emu.callbackType.exec, ADDR_CHECK_INPUT_RTS)

-- Error detection
emu.addMemoryCallback(function()
    if errorHit then return end; errorHit = true
    local state = emu.getState()
    local sp = state["cpu.sp"]
    local errCode = readWord(sp + 3)
    print(string.format("FAIL: error code=%d frame=%d", errCode, state["ppu.frameCount"]))
    emu.stop()
end, emu.callbackType.exec, ADDR_ERROR_TRIGGER)

-- Scene select: no START during boot (auto-advances ~575 frames).
-- Main menu has 2 items: ARCADE MODE, OPTIONS
-- Single-frame windows: DOWN to OPTIONS, A, DOWN x2 to SCENE SELECT, A, RIGHT x N, A
-- Menu active at ~frame 575. Start nav at frame 600.
local navSchedule = {
    {600,600,JOY_DOWN},                         -- DOWN to OPTIONS (2nd item)
    {630,630,JOY_A},                            -- A enter OPTIONS
    {660,660,JOY_DOWN},{690,690,JOY_DOWN},      -- DOWN x2 to SCENE SELECT (3rd item)
    {720,720,JOY_A},                            -- A enter SCENE SELECT
    -- RIGHT x N for scene N+1 (omit for scene 1 = casino_heist)
    {750,750,JOY_RIGHT},                        -- scene 2 = the_getaway
    {780,780,JOY_A},                            -- launch
}

-- For menus: put nav logic in exec callback to avoid 1-frame delay
emu.addMemoryCallback(function()
    local frame = emu.getState()["ppu.frameCount"]
    for _, s in ipairs(navSchedule) do
        if frame >= s[1] and frame <= s[2] then
            writeWord(ADDR_INPUT_PRESS, s[3])
            writeWord(ADDR_INPUT_TRIGGER, s[3])
            writeWord(ADDR_INPUT_OLD, 0)
            return
        end
    end
    -- Golden path injection (set by endFrame, 1-frame delay OK for wide event windows)
    if injectButton ~= 0 then
        writeWord(ADDR_INPUT_PRESS, injectButton)
        writeWord(ADDR_INPUT_TRIGGER, injectButton)
        writeWord(ADDR_INPUT_OLD, 0)
    end
end, emu.callbackType.exec, ADDR_CHECK_INPUT_RTS)

emu.addEventCallback(function()
    local frame = emu.getState()["ppu.frameCount"]
    injectButton = 0
    -- (golden path frame monitoring goes here)
    if frame >= MAX_FRAMES then print("TIMEOUT"); emu.stop() end
end, emu.eventType.endFrame)
```

## Automated Playthrough Tests

`tools/generate_playthrough_tests.py` generates per-scene Mesen Lua test scripts that verify every gameplay scene is beatable with the correct inputs. It parses chapter.data files, builds directed chapter graphs, finds golden paths via BFS, and reads the `.sym` file for current ROM addresses.

**Note**: The scene table in `generate_playthrough_tests.py` may need updating for Cliff Hanger's 8 scenes (currently still references Dragon's Lair scene names).

```bash
# Generate test scripts
wsl -e bash -c "cd <wsl-project-root> && python3 tools/generate_playthrough_tests.py"

# Generate for a specific scene only
wsl -e bash -c "cd <wsl-project-root> && python3 tools/generate_playthrough_tests.py --scene 3"

# Dry-run: show golden paths without generating Lua files
wsl -e bash -c "cd <wsl-project-root> && python3 tools/generate_playthrough_tests.py --dry-run"

# Run one test (from distribution/ where .msu/.pcm files live)
cmd.exe /c "cd /d <project>\distribution && <project>\mesen\Mesen.exe --testrunner CliffHangerArcade.sfc test_scene_3_rooftops.lua > out_3.txt 2>&1"
```

**IMPORTANT**: Regenerate test scripts after every build (`make`) because ROM addresses shift. The generator reads `build/CliffHangerArcade.sym` automatically.

**Golden path algorithm**: BFS from `{prefix}_start_alive`, following non-death direction events and default chapter transitions. Death chapters (EventResult.lastcheckpoint targets) and their transitive predecessors are excluded. For each direction event on the path, injects the button at the midpoint of the event's startFrame-endFrame window.

**Input types**: Unlike Dragon's Lair (directional only), Cliff Hanger golden paths may require `JOY_BUTTON_A` (hands) and `JOY_BUTTON_B` (feet) inputs in addition to directional buttons.

**Test output**: Each test prints `PASS`, `FAIL` (with error code or death info), or `TIMEOUT`.
