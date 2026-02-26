# Cliff Hanger Arcade — Next Steps

This document outlines what remains to complete the SNES Cliff Hanger Arcade port.
The engine, build system, chapter data, and tooling are all in place. What's left is
primarily asset creation, MSU-1 video pipeline execution, and testing.

## What's Already Done

- Engine ported from Super Dragon's Lair Arcade (65816 assembly, OOP framework)
- ROM builds successfully (1MB, header "CLIFF HANGER ARCADE")
- 265 chapters generated from 8 Cliff Hanger scenes (DirkSimple game.lua timing)
- Title screen simplified to ARCADE MODE + OPTIONS (8-scene select)
- Scene router rewritten for linear 8-scene progression
- Event.direction_generic supports hands (A) and feet (B) buttons
- `lua_scene_exporter_cliff.py` — converts DirkSimple Lua to XML events
- `generate_msu_data_cliff.py` — MSU-1 video/audio pipeline for single .m2v
- `xmlsceneparser.py` updated with JOY_BUTTON_B ("feet") mapping

## 8 Scenes (Linear Order)

| # | Scene | Prefix | Moves | Deaths |
|---|-------|--------|-------|--------|
| 1 | Casino Heist | csno | 12 | 2 |
| 2 | The Getaway | gtwy | 29 | 5 |
| 3 | Rooftops | roof | 36 | 4 |
| 4 | Highway | hway | 51 | 1 |
| 5 | The Castle Battle | cstl | 11 | 2 |
| 6 | Finale | fnle | 12 | 4 |
| 7 | Finale II | fn2 | 31 | 2 |
| 8 | Ending | endg | 50 | 5 |

---

## Priority 1: Laserdisc Setup

The source video files must be placed correctly for the MSU-1 pipeline.

### Move Laserdisc Files

The `cliff/` directory (currently in the project root, excluded from git) contains:
- `cliff.m2v` — MPEG-2 video (1.23 GB, single file)
- `cliff.ogg` — Ogg Vorbis audio
- `cliff.txt` — frame index

Copy or symlink these to `data/laserdisc/segments/`:

```bash
mkdir -p data/laserdisc/segments
cp cliff/cliff.m2v data/laserdisc/segments/
cp cliff/cliff.ogg data/laserdisc/segments/
cp cliff/cliff.txt data/laserdisc/segments/
```

### Create Daphne Framefile

Create `data/laserdisc/dlcdrom.TXT` with a single-segment entry pointing to cliff.m2v.
The `generate_msu_data_cliff.py` pipeline reads this to locate the video source.

---

## Priority 2: Background Art (Replace Dragon's Lair Placeholders)

All background assets currently use Dragon's Lair artwork. Each needs a Cliff Hanger
replacement. Backgrounds are 256x224 PNG images in 4BPP SNES format.

| Directory | Purpose | Notes |
|-----------|---------|-------|
| `data/backgrounds/titlescreen.gfx_bg/` | Title screen background | Main menu backdrop |
| `data/backgrounds/logo.gfx_bg/` | Logo/intro animation | Shown during boot |
| `data/backgrounds/msu1.gfx_bg/` | MSU-1 splash screen | Shown while MSU-1 initializes |
| `data/backgrounds/losers.gfx_bg/` | Credits/losers screen | Shown after MSU-1 init |
| `data/backgrounds/hiscore.gfx_bg/` | High score display | Hall of fame background |
| `data/backgrounds/scoreentry.gfx_bg/` | Score entry screen | Name entry after game over |
| `data/backgrounds/levelcomplete.*.gfx_bg/` | Level complete screens (3) | Between-scene celebration |
| `data/backgrounds/hud.gfx_directcolor/` | HUD overlay (8BPP) | In-game score/lives display |

**Format requirements:**
- BG mode: 256x224 (or 256x192 for video area), 4BPP, max 3 palettes (8 colors each)
- HUD: 8BPP direct color mode
- Place the PNG as `<dirname>/<dirname>.png` inside each folder
- The makefile gfx conversion rules handle the rest

---

## Priority 3: Sprite Assets

### Action Icon Sprites (Required)

Cliff Hanger uses two action prompts instead of Dragon's Lair's single sword:

| Sprite | Directory | Purpose |
|--------|-----------|---------|
| **Hands icon** | `data/sprites/hands.gfx_sprite/` | Shown when player must press A (hands) |
| **Feet icon** | `data/sprites/feet.gfx_sprite/` | Shown when player must press B (feet) |

Currently, `Event.direction_generic.65816` uses `Sword_icon` as a placeholder for both.
After creating these sprites:
1. Create the sprite directories with animation frames (same format as `sword.gfx_sprite/`)
2. Register the new sprite classes in `src/core/oop.h` (add OBJID entries)
3. Create sprite class files (`.65816` + `.h`) following the `Sword_icon` pattern
4. Update `Event.direction_generic.65816` to use `Hands_icon` for JOY_BUTTON_A
   and `Feet_icon` for JOY_BUTTON_B

**Reference**: Look at `data/sprites/sword.gfx_sprite/` and
`src/object/sprite/Sword_icon.65816` for the exact pattern.

### Arrow Sprites (Can Reuse)

The directional arrow sprites (`up_arrow`, `down_arrow`, `left_arrow`, `right_arrow`)
can be reused as-is from Dragon's Lair, or replaced with Cliff Hanger-themed versions.

### Other Sprites to Review

| Sprite | Status | Notes |
|--------|--------|-------|
| `life_dirk.gfx_sprite` | **Replace** | Dirk life counter icon → Cliff Hanger protagonist |
| `life_counter.gfx_sprite` | Keep or replace | Numeric life counter |
| `points.*.gfx_sprite` | Keep or replace | Score popup sprites |
| `shield.gfx_sprite` | **Remove or replace** | DL-specific (Dirk's shield) |
| `steering_wheel.*.gfx_sprite` | **Remove** | RoadBlaster leftover, not used in CH |
| `dashboard.gfx_sprite` | **Remove** | RoadBlaster leftover |
| `bang.gfx_sprite` | Keep or replace | Explosion/impact effect |
| `super.gfx_sprite` | Keep or replace | "SUPER" bonus text |
| `sword.gfx_sprite` | **Replace** → hands/feet | See action icons above |

---

## Priority 4: Sound Effects

All SPC700 BRR samples are Dragon's Lair-specific. Replace with Cliff Hanger sounds.

| File | Purpose | Notes |
|------|---------|-------|
| `data/sounds/dl_accept.sfx_normal.wav` | Menu accept | Replace or keep |
| `data/sounds/dl_buzz.sfx_normal.wav` | Menu buzz/error | Replace or keep |
| `data/sounds/dl_credit.sfx_normal.wav` | Credit insert | Replace or keep |
| `data/sounds/ok.sfx_normal.wav` | Confirmation | Replace or keep |
| `data/sounds/saveme.sfx_normal.wav` | "Save me!" voice clip | **Replace** (DL-specific) |
| `data/sounds/shuriken.sfx_normal.wav` | Action sound | Replace or keep |
| `data/sounds/technique.sfx_normal.wav` | Technique sound | Replace or keep |
| `data/sounds/brake.sfx_loop.wav` | Braking loop | **Remove** (RoadBlaster) |
| `data/sounds/turbo.sfx_loop.wav` | Turbo loop | **Remove** (RoadBlaster) |
| `data/sounds/turn.sfx_loop.wav` | Turn loop | **Remove** (RoadBlaster) |
| `data/sounds/dragon_roar*.wav` | MSU-1 splash sound | **Replace** with CH-appropriate sound |

**SPC700 constraints:**
- 64KB total RAM, ~57.5KB available for samples
- Current 7 samples use ~53KB BRR
- All samples must be 16-bit mono WAV
- `.sfx_normal.wav` = one-shot, `.sfx_loop.wav` = looping

### Music (Optional)

No music tracks currently exist. If you want SPC700 title screen music:
1. Create a ProTracker `.mod` file in `data/songs/`
2. `tools/mod2snes.py` converts it to custom SPC format
3. Total BRR sample budget is shared between SFX and music

---

## Priority 5: Font

The current font (`data/font/fixed8x8.gfx_font.png` and `16x16.gfx_font4bpp.png`) is
from Dragon's Lair. You can:
- **Keep it** — the font is generic enough to work
- **Replace it** — use `cliffglyphs.png` (in project root) as reference
  for a Cliff Hanger-themed font

---

## Priority 6: MSU-1 Video Pipeline

Once laserdisc files are in place, run the video pipeline:

```bash
# Full pipeline: extract frames + audio, convert tiles, package .msu
wsl -e bash -c "cd <wsl-project-root> && python3 tools/generate_msu_data_cliff.py --workers 8"

# Output: build/CliffHangerArcade.msu + per-chapter .pcm files
# Copied to: distribution/
```

The pipeline:
1. Extracts frames from `cliff.m2v` using frame ranges from chapter data
2. Extracts audio from `cliff.ogg` per chapter
3. Converts frames to SNES tiles via superfamiconv
4. Reduces tiles from 768 to 512 (VRAM limit)
5. Packages into `.msu` file + `.pcm` audio tracks

**Key differences from DL pipeline:**
- Single .m2v source (not 204 segments)
- 29.97fps native (no fps conversion needed, just deinterlace)
- Uses `generate_msu_data_cliff.py` (not `generate_msu_data.py`)

---

## Priority 7: Testing

### Build Verification

```bash
# Build ROM
wsl -e bash -c "cd <wsl-project-root> && make clean && make"

# ROM: build/CliffHangerArcade.sfc (copy to distribution/)
```

### Emulator Testing

```bat
:: Copy ROM to distribution/ where .msu/.pcm files live
:: Load from distribution/ — loading from build/ crashes (no MSU data)
cmd.exe /c "cd /d <project>\distribution && <project>\mesen\Mesen.exe CliffHangerArcade.sfc"
```

### Automated Playthrough Tests

After the MSU pipeline runs:

```bash
# Generate test scripts for all 8 scenes
wsl -e bash -c "cd <wsl-project-root> && python3 tools/generate_playthrough_tests.py"

# Run a test
cmd.exe /c "cd /d <project>\distribution && <project>\mesen\Mesen.exe --testrunner CliffHangerArcade.sfc test_scene_1_casino_heist.lua > out_1.txt 2>&1"
```

### Manual Testing Checklist

- [ ] Title screen displays correctly with ARCADE MODE / OPTIONS
- [ ] Scene select shows all 8 Cliff Hanger scenes
- [ ] Each scene loads and plays video
- [ ] Hands (A) and Feet (B) inputs register correctly
- [ ] Direction arrows display at correct times
- [ ] Death sequences play and restart from checkpoint
- [ ] Scene transitions work (scene 1 → 2 → ... → 8 → victory)
- [ ] Score entry screen works after completing all 8 scenes
- [ ] High scores display and persist

---

## Priority 8: Polish (Future)

### Attract Mode
Currently, idle title screen routes to `hall_of_fame` (high scores). A proper attract
mode would play demo gameplay footage. This requires:
- Creating attract mode chapter data (auto-playing inputs)
- Routing title screen idle to attract mode chapters

### Game Tuning
- Verify move timing windows feel correct (adjust event start/end frames if needed)
- Tune score values per move type
- Adjust life count and continue system

### Hanging Scene (Cliff Hanger-Specific)
The original Cliff Hanger arcade had a unique death animation where the protagonist
hangs from a cliff. This could be implemented as a custom death sequence overlay.

### Hardware Testing
- Test on real SD2SNES/FXPAK Pro
- Verify MSU-1 video playback timing
- Check audio sync

---

## Quick Reference: Build Commands

```bash
# Standard build
wsl -e bash -c "cd <wsl-project-root> && make clean && make"

# MSU-1 video pipeline
wsl -e bash -c "cd <wsl-project-root> && python3 tools/generate_msu_data_cliff.py --workers 8"

# Generate playthrough tests
wsl -e bash -c "cd <wsl-project-root> && python3 tools/generate_playthrough_tests.py"

# Run emulator test
cmd.exe /c "cd /d <project>\distribution && <project>\mesen\Mesen.exe --testrunner CliffHangerArcade.sfc test.lua > out.txt 2>&1"
```

## File Structure Summary

```
SNES-CliffHangerArcade/
  cliff/                          # Laserdisc source (gitignored)
    cliff.m2v                     # MPEG-2 video (1.23 GB)
    cliff.ogg                     # Audio
    cliff.txt                     # Frame index
  data/
    backgrounds/                  # BG art (replace DL placeholders)
    chapters/                     # 265 generated chapter dirs
    events/                       # 265 XML event files
    font/                         # 8x8 and 16x16 fonts
    game.lua                      # DirkSimple Cliff Hanger timing data
    laserdisc/segments/           # .m2v/.ogg go here for pipeline
    sounds/                       # SPC700 BRR samples (replace DL sounds)
    sprites/                      # Sprite animations (create hands/feet icons)
  src/
    core/                         # Engine core (boot, oop, error, nmi)
    object/event/                 # Event classes (direction_generic, chapter, etc.)
    object/script/                # Script system + chapter data
    level1.script                 # Entry point → csno_start_alive
    scene_router.script           # Linear 8-scene routing
    title_screen.script           # 2-item menu + 8-scene select
  tools/
    lua_scene_exporter_cliff.py   # game.lua → XML events
    generate_msu_data_cliff.py    # MSU-1 video pipeline (single .m2v)
    xmlsceneparser.py             # XML → assembly chapters
    generate_playthrough_tests.py # Auto-generate Mesen test scripts
```
