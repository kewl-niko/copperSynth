#!/usr/bin/env python3
"""copperSynth — SoundFont workstation and live MIDI visualizer.

Dependencies:
    pip install pyfluidsynth mido python-rtmidi dearpygui

FluidSynth's shared library must also be installed by the operating system.
"""

from __future__ import annotations

import ctypes
import ctypes.util
import json
import queue
import threading
import time
import traceback
from pathlib import Path
from typing import Any

import fluidsynth
import mido
import dearpygui.dearpygui as dpg


# -----------------------------------------------------------------------------
# Application configuration
# -----------------------------------------------------------------------------

APP_NAME = "copperSynth"
CONFIG_DIR = Path.home() / ".config" / "coppersynth"
CONFIG_FILE = CONFIG_DIR / "settings.json"

WHITE_KEYS = {0, 2, 4, 5, 7, 9, 11}
VIS_WIDTH = 1100
VIS_HEIGHT = 560
KEYBOARD_TOP = 430
KEYBOARD_HEIGHT = 110

ACCENT_PRESETS: dict[str, tuple[int, int, int]] = {
    "Copper": (205, 132, 88),
    "Burnt Orange": (204, 105, 65),
    "Amber": (218, 164, 76),
    "Rust": (190, 100, 70),
    "Sand": (190, 160, 119),
    "Teal": (83, 166, 155),
    "Sage": (133, 169, 132),
    "Ice": (116, 178, 194),
    "Blue": (111, 151, 202),
    "Violet": (157, 129, 195),
    "Rose": (196, 119, 136),
    "Crimson": (190, 82, 91),
    "White": (220, 220, 220),
}

# Interface themes and accent colors are intentionally independent.
THEMES: dict[str, dict[str, Any]] = {
    "OLED": {
        "description": "Near-black surfaces for OLED displays and low-light setups.",
        "bg": (5, 5, 6), "panel": (12, 12, 13), "panel_alt": (18, 18, 19),
        "popup": (15, 15, 16), "frame": (23, 23, 24), "border": (39, 39, 40),
        "text": (228, 225, 220), "muted": (133, 130, 126),
        "white_key": (205, 202, 195), "black_key": (18, 18, 19),
        "keyboard": (10, 10, 11), "grid": (34, 34, 35), "light": False,
    },
    "Graphite": {
        "description": "Balanced charcoal surfaces with restrained contrast.",
        "bg": (19, 20, 21), "panel": (26, 27, 28), "panel_alt": (32, 33, 34),
        "popup": (30, 31, 32), "frame": (42, 43, 44), "border": (57, 58, 59),
        "text": (232, 228, 221), "muted": (145, 141, 135),
        "white_key": (216, 212, 204), "black_key": (27, 27, 28),
        "keyboard": (30, 30, 30), "grid": (46, 46, 46), "light": False,
    },
    "Carbon": {
        "description": "Deep slate, a little cooler than the default graphite look.",
        "bg": (15, 18, 21), "panel": (22, 26, 30), "panel_alt": (28, 33, 38),
        "popup": (25, 29, 34), "frame": (35, 42, 48), "border": (52, 61, 68),
        "text": (226, 232, 235), "muted": (137, 149, 155),
        "white_key": (211, 219, 220), "black_key": (20, 25, 28),
        "keyboard": (22, 26, 29), "grid": (42, 50, 55), "light": False,
    },
    "Midnight": {
        "description": "Dark blue-tinted surfaces without bright neon colors.",
        "bg": (13, 16, 24), "panel": (20, 24, 34), "panel_alt": (27, 32, 44),
        "popup": (22, 27, 38), "frame": (34, 41, 54), "border": (49, 59, 76),
        "text": (225, 229, 238), "muted": (139, 148, 166),
        "white_key": (214, 218, 228), "black_key": (20, 23, 32),
        "keyboard": (20, 23, 31), "grid": (39, 46, 60), "light": False,
    },
    "Ember": {
        "description": "Warm brown-black surfaces that pair naturally with copper.",
        "bg": (23, 18, 16), "panel": (32, 25, 22), "panel_alt": (41, 32, 27),
        "popup": (35, 28, 24), "frame": (52, 40, 33), "border": (70, 53, 43),
        "text": (239, 226, 213), "muted": (160, 145, 131),
        "white_key": (220, 210, 194), "black_key": (29, 23, 20),
        "keyboard": (29, 24, 21), "grid": (54, 43, 36), "light": False,
    },
    "Moss": {
        "description": "Muted green-black surfaces, kept deliberately understated.",
        "bg": (16, 21, 19), "panel": (23, 30, 27), "panel_alt": (30, 38, 34),
        "popup": (25, 33, 29), "frame": (38, 48, 42), "border": (54, 66, 58),
        "text": (225, 232, 223), "muted": (143, 155, 145),
        "white_key": (211, 220, 207), "black_key": (19, 25, 22),
        "keyboard": (20, 26, 23), "grid": (42, 53, 46), "light": False,
    },
}

CATEGORIES: dict[str, tuple[str, ...]] = {
    "Pianos": ("piano", "grand", "upright", "pianet"),
    "Electric Keys": ("electric piano", "e.piano", "epiano", "clav", "harpsichord", "celesta"),
    "Organs": ("organ", "hammond", "church"),
    "Guitars": ("guitar", "nylon", "steel guitar"),
    "Bass": ("bass", "sub"),
    "Strings": ("violin", "viola", "cello", "string", "orchestra", "harp"),
    "Brass": ("trumpet", "trombone", "tuba", "brass", "horn"),
    "Winds": ("flute", "clarinet", "oboe", "bassoon", "sax", "recorder", "whistle"),
    "Synths": ("synth", "lead", "pad", "analog", "saw", "square", "wave", "electro"),
    "Percussion": ("drum", "percussion", "timpani", "tom", "cymbal", "snare"),
}


# -----------------------------------------------------------------------------
# FluidSynth SoundFont preset scanner
# -----------------------------------------------------------------------------

class PresetScanner:
    """Read preset names/bank/program values from a loaded FluidSynth SoundFont."""

    def __init__(self) -> None:
        library = ctypes.util.find_library("fluidsynth")
        if not library:
            raise RuntimeError("Could not find libfluidsynth. Install FluidSynth first.")

        self.lib = ctypes.CDLL(library)
        self._bind("fluid_synth_get_sfont_by_id", ctypes.c_void_p,
                   [ctypes.c_void_p, ctypes.c_int])
        self._bind("fluid_sfont_iteration_start", None, [ctypes.c_void_p])
        self._bind("fluid_sfont_iteration_next", ctypes.c_void_p, [ctypes.c_void_p])
        self._bind("fluid_preset_get_name", ctypes.c_char_p, [ctypes.c_void_p])
        self._bind("fluid_preset_get_banknum", ctypes.c_int, [ctypes.c_void_p])
        self._bind("fluid_preset_get_num", ctypes.c_int, [ctypes.c_void_p])

    def _bind(self, name: str, result: Any, arguments: list[Any]) -> None:
        function = getattr(self.lib, name)
        function.restype = result
        function.argtypes = arguments
        setattr(self, name, function)

    def scan(self, synth: Any, soundfont_id: int) -> list[dict[str, Any]]:
        pointer = synth.synth
        synth_pointer = pointer if isinstance(pointer, ctypes.c_void_p) else ctypes.c_void_p(pointer)
        soundfont = self.fluid_synth_get_sfont_by_id(synth_pointer, int(soundfont_id))
        if not soundfont:
            raise RuntimeError("Could not retrieve the loaded SoundFont from FluidSynth.")

        soundfont_pointer = ctypes.c_void_p(soundfont)
        self.fluid_sfont_iteration_start(soundfont_pointer)
        presets: list[dict[str, Any]] = []

        # FluidSynth owns these preset objects; this loop only reads their metadata.
        for _ in range(100_000):
            preset = self.fluid_sfont_iteration_next(soundfont_pointer)
            if not preset:
                break
            preset_pointer = ctypes.c_void_p(preset)
            raw_name = self.fluid_preset_get_name(preset_pointer)
            name = raw_name.decode("utf-8", errors="replace") if raw_name else "Unnamed preset"
            presets.append({
                "name": name,
                "bank": int(self.fluid_preset_get_banknum(preset_pointer)),
                "program": int(self.fluid_preset_get_num(preset_pointer)),
            })

        presets.sort(key=lambda item: (item["name"].casefold(), item["bank"], item["program"]))
        return presets


# -----------------------------------------------------------------------------
# Small helpers
# -----------------------------------------------------------------------------

def get_category(name: str) -> str:
    folded = name.casefold()
    for category, words in CATEGORIES.items():
        if any(word in folded for word in words):
            return category
    return "Other"


def clamp(value: int | float, low: int, high: int) -> int:
    return max(low, min(high, int(value)))


# -----------------------------------------------------------------------------
# Main application
# -----------------------------------------------------------------------------

class CopperSynth:
    def __init__(self) -> None:
        self.config = self.load_config()
        self.running = True
        self.synth_lock = threading.RLock()

        self.volume = self._load_volume(self.config.get("volume", 80))
        self.theme_name = self.config.get("theme", "Graphite")
        if self.theme_name not in THEMES:
            self.theme_name = "Graphite"

        self.accent_name, self.accent_color = self._load_accent()
        self.theme_tag: int | None = None
        self.accent_text_items: list[int] = []
        self.theme_colors: dict[str, Any] = {}
        self.refresh_accent_shades()

        self.note_speed = self._safe_float(self.config.get("note_speed", 155.0), 155.0, 40.0, 360.0)
        self.note_grow_speed = self._safe_float(self.config.get("note_grow_speed", 120.0), 120.0, 30.0, 260.0)
        self.max_note_height = self._safe_float(self.config.get("max_note_height", 300.0), 300.0, 60.0, 420.0)

        self.synth = fluidsynth.Synth(gain=self.volume / 100.0, samplerate=44100)
        self.start_audio()
        self.scanner = PresetScanner()

        self.sfid = -1
        self.soundfont_path = ""
        self.presets: list[dict[str, Any]] = []
        self.selected_preset: dict[str, Any] | None = None

        self.midi_backend = mido.Backend("mido.backends.rtmidi")
        self.midi_input = None
        self.midi_queue: queue.Queue[tuple[str, int, int, int]] = queue.Queue()
        self.active_notes: dict[tuple[int, int], dict[str, Any]] = {}
        self.sustain_channels: set[int] = set()
        self.deferred_notes: set[tuple[int, int]] = set()

        self.build_interface()

        saved_soundfont = self.config.get("soundfont")
        if saved_soundfont and Path(saved_soundfont).is_file():
            self.load_soundfont(saved_soundfont)

        self.refresh_midi_inputs()

    # ----- Settings ---------------------------------------------------------

    @staticmethod
    def _safe_float(value: Any, default: float, low: float, high: float) -> float:
        try:
            number = float(value)
        except (TypeError, ValueError):
            number = default
        return max(low, min(high, number))

    @staticmethod
    def _load_volume(value: Any) -> int:
        # Older releases stored FluidSynth gain (0.0–3.0); newer ones store 0–300%.
        try:
            number = float(value)
        except (TypeError, ValueError):
            number = 80.0
        if 0.0 <= number <= 3.0:
            number *= 100.0
        return clamp(number, 0, 300)

    def _load_accent(self) -> tuple[str, tuple[int, int, int]]:
        saved_name = self.config.get("accent", "Copper")
        saved_rgb = self.config.get("accent_rgb")

        if isinstance(saved_name, str) and saved_name in ACCENT_PRESETS:
            return saved_name, ACCENT_PRESETS[saved_name]

        candidate = saved_rgb if saved_rgb is not None else saved_name
        if isinstance(candidate, (list, tuple)) and len(candidate) >= 3:
            try:
                rgb = tuple(clamp(float(component), 0, 255) for component in candidate[:3])
                return "Custom", rgb  # type: ignore[return-value]
            except (TypeError, ValueError):
                pass

        return "Copper", ACCENT_PRESETS["Copper"]

    def load_config(self) -> dict[str, Any]:
        try:
            with CONFIG_FILE.open("r", encoding="utf-8") as file:
                data = json.load(file)
            return data if isinstance(data, dict) else {}
        except FileNotFoundError:
            return {}
        except (OSError, json.JSONDecodeError) as exc:
            print(f"Settings could not be read: {exc}")
            return {}

    def save_config(self, sender: Any = None, app_data: Any = None) -> None:
        data = {
            "volume": self.volume,
            "soundfont": self.soundfont_path,
            "accent": self.accent_name,
            "accent_rgb": list(self.accent_color),
            "theme": self.theme_name,
            "bank": self.selected_preset["bank"] if self.selected_preset else self.config.get("bank", 0),
            "program": self.selected_preset["program"] if self.selected_preset else self.config.get("program", 0),
            "midi_input": self.config.get("midi_input", ""),
            "note_speed": self.note_speed,
            "note_grow_speed": self.note_grow_speed,
            "max_note_height": self.max_note_height,
        }
        try:
            CONFIG_DIR.mkdir(parents=True, exist_ok=True)
            temporary = CONFIG_FILE.with_suffix(".json.tmp")
            with temporary.open("w", encoding="utf-8") as file:
                json.dump(data, file, indent=4)
            temporary.replace(CONFIG_FILE)
            self.config = data
        except OSError as exc:
            print(f"Settings could not be saved: {exc}")
            self.set_status("Could not save settings")

    # ----- Audio ------------------------------------------------------------

    def start_audio(self) -> None:
        errors: list[str] = []
        for driver in ("pulseaudio", "alsa"):
            try:
                self.synth.start(driver=driver)
                self.audio_driver = driver
                return
            except Exception as exc:
                errors.append(f"{driver}: {exc}")
        raise RuntimeError("Could not initialize audio:\n" + "\n".join(errors))

    # ----- Interface construction ------------------------------------------

    def add_accent_text(self, value: str, **kwargs: Any) -> int:
        item = dpg.add_text(value, color=self.accent_light, **kwargs)
        self.accent_text_items.append(item)
        return item

    def build_interface(self) -> None:
        dpg.create_context()
        self.create_theme()
        dpg.create_viewport(
            title=f"{APP_NAME}  /  SoundFont workstation",
            width=1380,
            height=900,
            min_width=1050,
            min_height=700,
        )
        dpg.set_viewport_clear_color(THEMES[self.theme_name]["bg"])

        with dpg.window(label=APP_NAME, tag="main_window", no_close=True):
            # Compact app header.
            with dpg.group(horizontal=True):
                with dpg.group():
                    self.add_accent_text("copperSynth")
                    dpg.add_text("SOUNDFONT WORKSTATION  /  MIDI MONITOR", color=(135, 132, 127))
                dpg.add_spacer(width=30)
                with dpg.group():
                    dpg.add_text("APPEARANCE", color=(130, 128, 124))
                    dpg.add_text(f"{self.theme_name}  ·  {self.accent_name}", tag="palette_status", color=(190, 183, 173))

            dpg.add_spacer(height=10)
            dpg.add_separator()
            dpg.add_spacer(height=8)

            with dpg.tab_bar(tag="main_tabs"):
                with dpg.tab(label="Studio"):
                    self.build_studio_tab()
                with dpg.tab(label="Appearance & settings"):
                    self.build_settings_tab()

        # SoundFont file picker.
        with dpg.file_dialog(
            directory_selector=False,
            show=False,
            callback=self.soundfont_dialog_callback,
            tag="sf_dialog",
            width=720,
            height=470,
        ):
            dpg.add_file_extension(".sf2", color=(220, 160, 110))
            dpg.add_file_extension(".sf3", color=(220, 160, 110))
            dpg.add_file_extension(".*")

        # Dedicated live visualizer window.
        with dpg.window(
            label="copperSynth  /  Live visualizer",
            tag="visualizer_window",
            width=VIS_WIDTH + 50,
            height=VIS_HEIGHT + 130,
            min_size=(920, 590),
            show=False,
        ):
            with dpg.group(horizontal=True):
                self.add_accent_text("LIVE MIDI VISUALIZER")
                dpg.add_spacer(width=10)
                dpg.add_text("88-key range  ·  notes rise after release", color=(137, 133, 127))
            dpg.add_spacer(height=7)
            dpg.add_drawlist(width=VIS_WIDTH, height=VIS_HEIGHT, tag="viz_canvas")

        dpg.setup_dearpygui()
        dpg.set_primary_window("main_window", True)
        dpg.show_viewport()

    def build_studio_tab(self) -> None:
        with dpg.group(horizontal=True):
            # Left side: searchable SoundFont preset library.
            with dpg.child_window(tag="library_panel", width=390, height=-1, border=True):
                with dpg.group(horizontal=True):
                    self.add_accent_text("SOUND LIBRARY")
                    dpg.add_spacer(width=8)
                    dpg.add_text("/  PRESETS", color=(129, 125, 119))

                dpg.add_button(
                    label="Load SoundFont  +", width=-1, height=42,
                    callback=lambda: dpg.show_item("sf_dialog"),
                )
                dpg.add_text("No SoundFont loaded", tag="sf_name", color=(184, 177, 167), wrap=350)
                dpg.add_spacer(height=10)
                dpg.add_separator()
                dpg.add_spacer(height=9)

                with dpg.group(horizontal=True):
                    self.add_accent_text("INSTRUMENT BROWSER")
                    dpg.add_spacer(width=8)
                    dpg.add_text("0 presets", tag="preset_count", color=(138, 133, 126))

                dpg.add_input_text(
                    hint="Search instruments, banks or programs...",
                    tag="search_input", width=-1, callback=self.refresh_preset_list,
                )
                dpg.add_combo(
                    items=["All categories", *CATEGORIES.keys(), "Other"],
                    tag="category_filter", default_value="All categories",
                    width=-1, callback=self.refresh_preset_list,
                )
                dpg.add_spacer(height=5)
                dpg.add_child_window(tag="preset_list", width=-1, height=-1, border=False)

            # Right side: selected voice, output, MIDI and transport controls.
            with dpg.child_window(tag="workstation_panel", width=-1, height=-1, border=True):
                with dpg.child_window(tag="voice_card", width=-1, height=145, border=True):
                    with dpg.group(horizontal=True):
                        with dpg.group():
                            self.add_accent_text("CURRENT VOICE")
                            dpg.add_text("No instrument selected", tag="voice_name", color=(237, 226, 213), wrap=490)
                            dpg.add_text("bank ---     program ---", tag="voice_details", color=(150, 143, 135))
                        dpg.add_spacer(width=24)
                        with dpg.group():
                            dpg.add_spacer(height=12)
                            dpg.add_button(label="Previous", width=130, callback=lambda: self.navigate_preset(-1))
                            dpg.add_button(label="Next", width=130, callback=lambda: self.navigate_preset(1))

                dpg.add_spacer(height=10)
                with dpg.group(horizontal=True):
                    with dpg.child_window(tag="output_card", width=390, height=235, border=True):
                        self.add_accent_text("MASTER OUTPUT")
                        dpg.add_text("FluidSynth playback level", color=(140, 135, 129))
                        dpg.add_slider_int(
                            tag="volume_slider", min_value=0, max_value=300,
                            default_value=self.volume, width=-1, callback=self.change_volume,
                        )
                        dpg.add_text(f"{self.volume}%", tag="volume_label", color=(193, 183, 171))
                        dpg.add_spacer(height=12)
                        dpg.add_separator()
                        dpg.add_spacer(height=9)
                        self.add_accent_text("AUDIO ENGINE")
                        dpg.add_text(f"Driver  ·  {self.audio_driver}", tag="audio_driver_label", color=(173, 164, 153))
                        dpg.add_text("44.1 kHz  ·  16 MIDI channels", color=(137, 132, 126))

                    with dpg.child_window(tag="midi_card", width=-1, height=235, border=True):
                        self.add_accent_text("MIDI INPUT")
                        dpg.add_text("Choose a keyboard or controller", color=(140, 135, 129))
                        dpg.add_combo(
                            items=["No MIDI input"], tag="midi_combo",
                            default_value="No MIDI input", width=-1, callback=self.select_midi,
                        )
                        dpg.add_button(label="Refresh devices", width=-1, callback=self.refresh_midi_inputs)
                        dpg.add_spacer(height=7)
                        dpg.add_text("CONNECTED DEVICE", color=(137, 132, 126))
                        dpg.add_text("No MIDI device selected", tag="midi_status", color=(193, 183, 171), wrap=310)

                dpg.add_spacer(height=12)
                dpg.add_separator()
                dpg.add_spacer(height=12)
                with dpg.group(horizontal=True):
                    dpg.add_button(
                        label="Open live visualizer", width=-1, height=48,
                        callback=self.open_visualizer,
                    )
                    dpg.add_button(label="All notes off", width=155, height=48, callback=self.panic)

                dpg.add_spacer(height=11)
                with dpg.group(horizontal=True):
                    self.add_accent_text("ENGINE STATUS")
                    dpg.add_spacer(width=9)
                    dpg.add_text("Ready", tag="status", color=(194, 184, 173))

    def build_settings_tab(self) -> None:
        with dpg.group(horizontal=True):
            with dpg.child_window(width=430, height=-1, border=True):
                self.add_accent_text("INTERFACE THEMES")
                dpg.add_text("Change the surfaces without changing your accent color.", color=(139, 134, 128), wrap=390)
                dpg.add_combo(
                    items=list(THEMES.keys()), tag="theme_combo",
                    default_value=self.theme_name, width=-1, callback=self.change_theme,
                )
                dpg.add_text(THEMES[self.theme_name]["description"], tag="theme_description", color=(174, 166, 155), wrap=390)
                dpg.add_spacer(height=12)
                dpg.add_separator()
                dpg.add_spacer(height=10)
                self.add_accent_text("ACCENT COLOR")
                dpg.add_text("Pick a preset or choose any RGB color.", color=(139, 134, 128), wrap=390)
                dpg.add_combo(
                    items=[*ACCENT_PRESETS.keys(), "Custom"],
                    tag="accent_preset", default_value=self.accent_name,
                    width=-1, callback=self.change_accent_preset,
                )
                dpg.add_color_edit(
                    tag="accent_picker", default_value=(*self.accent_color, 255),
                    no_alpha=True, width=-1, display_type=dpg.mvColorEdit_uint8,
                    display_mode=dpg.mvColorEdit_rgb, callback=self.change_accent_custom,
                )
                dpg.add_text(self.rgb_label(), tag="accent_value", color=(174, 166, 155))
                dpg.add_spacer(height=13)
                dpg.add_button(label="Save preferences", width=-1, height=38, callback=self.save_config)

            with dpg.child_window(width=-1, height=-1, border=True):
                self.add_accent_text("VISUALIZER")
                dpg.add_text("Tune the piano-roll motion. These values are saved with your settings.", color=(139, 134, 128), wrap=470)

                dpg.add_text("Note travel speed", color=(190, 183, 174))
                dpg.add_slider_float(
                    tag="note_speed_slider", min_value=40.0, max_value=360.0,
                    default_value=self.note_speed, width=-1, callback=self.change_note_speed,
                )
                dpg.add_text(f"{self.note_speed:.0f} px/s", tag="note_speed_value", color=(139, 134, 128))

                dpg.add_spacer(height=9)
                dpg.add_text("Note growth speed", color=(190, 183, 174))
                dpg.add_slider_float(
                    tag="note_growth_slider", min_value=30.0, max_value=260.0,
                    default_value=self.note_grow_speed, width=-1, callback=self.change_note_growth,
                )
                dpg.add_text(f"{self.note_grow_speed:.0f} px/s", tag="note_growth_value", color=(139, 134, 128))

                dpg.add_spacer(height=9)
                dpg.add_text("Maximum held-note height", color=(190, 183, 174))
                dpg.add_slider_float(
                    tag="note_height_slider", min_value=60.0, max_value=420.0,
                    default_value=self.max_note_height, width=-1, callback=self.change_note_height,
                )
                dpg.add_text(f"{self.max_note_height:.0f} px", tag="note_height_value", color=(139, 134, 128))

                dpg.add_spacer(height=16)
                dpg.add_separator()
                dpg.add_spacer(height=11)
                self.add_accent_text("ABOUT THIS BUILD")
                dpg.add_text("copperSynth", color=(224, 215, 203))
                dpg.add_text("A small FluidSynth workstation with a searchable SoundFont library and live MIDI piano roll.", color=(139, 134, 128), wrap=470)
                dpg.add_text("Settings location", color=(190, 183, 174))
                dpg.add_text(str(CONFIG_FILE), color=(139, 134, 128), wrap=470)

    # ----- Theme and accent handling ---------------------------------------

    @staticmethod
    def blend_color(first: tuple[int, int, int], second: tuple[int, int, int], amount: float) -> tuple[int, int, int]:
        return tuple(clamp(a + (b - a) * amount, 0, 255) for a, b in zip(first, second))  # type: ignore[return-value]

    def refresh_accent_shades(self) -> None:
        self.accent_color = tuple(clamp(component, 0, 255) for component in self.accent_color[:3])
        base_text = THEMES.get(self.theme_name, THEMES["Graphite"])["text"]
        self.accent_light = self.blend_color(self.accent_color, base_text, 0.30)
        self.accent_hover = self.blend_color(self.accent_color, (255, 255, 255), 0.13)
        self.accent_active = self.blend_color(self.accent_color, base_text, 0.24)
        self.accent_dark = self.blend_color(self.accent_color, THEMES[self.theme_name]["bg"], 0.68)

    def create_theme(self) -> None:
        old_theme = self.theme_tag
        palette = THEMES[self.theme_name]
        self.theme_colors = palette
        self.refresh_accent_shades()

        with dpg.theme() as theme:
            with dpg.theme_component(dpg.mvAll):
                colors = {
                    dpg.mvThemeCol_Text: palette["text"],
                    dpg.mvThemeCol_TextDisabled: palette["muted"],
                    dpg.mvThemeCol_WindowBg: palette["bg"],
                    dpg.mvThemeCol_ChildBg: palette["panel"],
                    dpg.mvThemeCol_PopupBg: palette["popup"],
                    dpg.mvThemeCol_Border: palette["border"],
                    dpg.mvThemeCol_FrameBg: palette["frame"],
                    dpg.mvThemeCol_FrameBgHovered: self.blend_color(palette["frame"], self.accent_color, 0.24),
                    dpg.mvThemeCol_FrameBgActive: self.blend_color(palette["frame"], self.accent_color, 0.39),
                    dpg.mvThemeCol_TitleBg: palette["panel"],
                    dpg.mvThemeCol_TitleBgActive: palette["panel_alt"],
                    dpg.mvThemeCol_MenuBarBg: palette["panel"],
                    dpg.mvThemeCol_ScrollbarBg: palette["bg"],
                    dpg.mvThemeCol_ScrollbarGrab: palette["border"],
                    dpg.mvThemeCol_ScrollbarGrabHovered: self.accent_dark,
                    dpg.mvThemeCol_ScrollbarGrabActive: self.accent_color,
                    dpg.mvThemeCol_CheckMark: self.accent_light,
                    dpg.mvThemeCol_SliderGrab: self.accent_color,
                    dpg.mvThemeCol_SliderGrabActive: self.accent_active,
                    dpg.mvThemeCol_Button: self.accent_dark,
                    dpg.mvThemeCol_ButtonHovered: self.accent_color,
                    dpg.mvThemeCol_ButtonActive: self.accent_active,
                    dpg.mvThemeCol_Header: self.blend_color(palette["panel_alt"], self.accent_color, 0.25),
                    dpg.mvThemeCol_HeaderHovered: self.blend_color(palette["panel_alt"], self.accent_color, 0.42),
                    dpg.mvThemeCol_HeaderActive: self.blend_color(palette["panel_alt"], self.accent_color, 0.55),
                    dpg.mvThemeCol_Separator: palette["border"],
                    dpg.mvThemeCol_SeparatorHovered: self.accent_hover,
                    dpg.mvThemeCol_SeparatorActive: self.accent_color,
                    dpg.mvThemeCol_ResizeGrip: self.accent_dark,
                    dpg.mvThemeCol_ResizeGripHovered: self.accent_hover,
                    dpg.mvThemeCol_ResizeGripActive: self.accent_active,
                    dpg.mvThemeCol_Tab: palette["panel_alt"],
                    dpg.mvThemeCol_TabHovered: self.blend_color(palette["panel_alt"], self.accent_color, 0.38),
                    dpg.mvThemeCol_TabActive: self.blend_color(palette["panel"], self.accent_color, 0.22),
                    dpg.mvThemeCol_TabUnfocused: palette["panel"],
                    dpg.mvThemeCol_TabUnfocusedActive: palette["panel_alt"],
                    dpg.mvThemeCol_TextSelectedBg: self.blend_color(palette["panel"], self.accent_color, 0.55),
                }
                for color_id, color in colors.items():
                    dpg.add_theme_color(color_id, color)

                styles = {
                    dpg.mvStyleVar_WindowRounding: 5,
                    dpg.mvStyleVar_ChildRounding: 5,
                    dpg.mvStyleVar_FrameRounding: 4,
                    dpg.mvStyleVar_PopupRounding: 4,
                    dpg.mvStyleVar_ScrollbarRounding: 4,
                    dpg.mvStyleVar_GrabRounding: 4,
                    dpg.mvStyleVar_TabRounding: 4,
                }
                for style_id, value in styles.items():
                    dpg.add_theme_style(style_id, value)

                dpg.add_theme_style(dpg.mvStyleVar_WindowPadding, 14, 13)
                dpg.add_theme_style(dpg.mvStyleVar_FramePadding, 10, 7)
                dpg.add_theme_style(dpg.mvStyleVar_ItemSpacing, 9, 8)
                dpg.add_theme_style(dpg.mvStyleVar_CellPadding, 8, 5)

        dpg.bind_theme(theme)
        self.theme_tag = theme
        if old_theme is not None and dpg.does_item_exist(old_theme):
            dpg.delete_item(old_theme)

    def update_accent_ui(self) -> None:
        self.refresh_accent_shades()
        self.create_theme()
        for item in self.accent_text_items:
            if dpg.does_item_exist(item):
                dpg.configure_item(item, color=self.accent_light)

        if dpg.does_item_exist("accent_picker"):
            dpg.set_value("accent_picker", (*self.accent_color, 255))
        if dpg.does_item_exist("accent_preset"):
            dpg.set_value("accent_preset", self.accent_name)
        if dpg.does_item_exist("accent_value"):
            dpg.set_value("accent_value", self.rgb_label())
        self.update_palette_status()
        self.save_config()

    def rgb_label(self) -> str:
        r, g, b = self.accent_color
        return f"#{r:02X}{g:02X}{b:02X}   /   RGB {r}, {g}, {b}"

    def update_palette_status(self) -> None:
        if dpg.does_item_exist("palette_status"):
            dpg.set_value("palette_status", f"{self.theme_name}  ·  {self.accent_name}")

    def change_theme(self, sender: Any, app_data: str) -> None:
        if app_data not in THEMES:
            return
        self.theme_name = app_data
        self.create_theme()
        dpg.set_viewport_clear_color(THEMES[self.theme_name]["bg"])
        self.refresh_accent_text_colors()
        if dpg.does_item_exist("theme_description"):
            dpg.set_value("theme_description", THEMES[self.theme_name]["description"])
        self.update_palette_status()
        self.save_config()

    def refresh_accent_text_colors(self) -> None:
        for item in self.accent_text_items:
            if dpg.does_item_exist(item):
                dpg.configure_item(item, color=self.accent_light)

    def change_accent_preset(self, sender: Any, app_data: str) -> None:
        if app_data == "Custom":
            self.accent_name = "Custom"
        elif app_data in ACCENT_PRESETS:
            self.accent_name = app_data
            self.accent_color = ACCENT_PRESETS[app_data]
        else:
            return
        self.update_accent_ui()

    def change_accent_custom(self, sender: Any, app_data: Any) -> None:
        # Dear PyGui versions can return either 0–255 channels or normalized floats.
        # Some builds wrap the color in a small dictionary, so accept both shapes.
        if isinstance(app_data, dict):
            if "color" in app_data:
                app_data = app_data["color"]
            elif all(key in app_data for key in ("r", "g", "b")):
                app_data = (app_data["r"], app_data["g"], app_data["b"])
        try:
            values = list(app_data)[:3]
            if len(values) != 3:
                return
            normalized = all(isinstance(value, float) and 0.0 <= value <= 1.0 for value in values)
            rgb = tuple(clamp(float(value) * (255 if normalized else 1), 0, 255) for value in values)
        except (TypeError, ValueError):
            return
        self.accent_name = "Custom"
        self.accent_color = rgb  # type: ignore[assignment]
        self.update_accent_ui()

    # ----- SoundFont loading ------------------------------------------------

    def soundfont_dialog_callback(self, sender: Any, app_data: dict[str, Any]) -> None:
        path = app_data.get("file_path_name")
        if path:
            self.load_soundfont(path)

    def load_soundfont(self, path: str | Path) -> None:
        resolved_path = str(Path(path).expanduser().resolve())
        new_sfid = -1
        self.set_status("Loading and scanning SoundFont...")

        try:
            # Stop old voices before swapping the active SoundFont.
            self.panic()
            with self.synth_lock:
                new_sfid = self.synth.sfload(resolved_path, 0)
                if new_sfid < 0:
                    raise RuntimeError("FluidSynth could not load this SoundFont.")
                new_presets = self.scanner.scan(self.synth, new_sfid)

            if not new_presets:
                raise RuntimeError("No presets were found in this SoundFont.")

            old_sfid = self.sfid
            self.sfid = new_sfid
            self.soundfont_path = resolved_path
            self.presets = new_presets
            self.selected_preset = None

            dpg.set_value("sf_name", Path(resolved_path).name)
            self.refresh_preset_list()

            if old_sfid >= 0:
                with self.synth_lock:
                    self.synth.sfunload(old_sfid, 0)

            old_bank = int(self.config.get("bank", 0))
            old_program = int(self.config.get("program", 0))
            matching = next(
                (preset for preset in new_presets
                 if preset["bank"] == old_bank and preset["program"] == old_program),
                new_presets[0],
            )
            self.apply_preset(matching)
            self.set_status(f"{len(new_presets)} presets loaded")
            self.save_config()

        except Exception as exc:
            if new_sfid >= 0 and new_sfid != self.sfid:
                try:
                    with self.synth_lock:
                        self.synth.sfunload(new_sfid, 0)
                except Exception:
                    pass
            self.set_status("SoundFont load failed")
            print(f"SoundFont error: {exc}")

    # ----- Preset browser ---------------------------------------------------

    def filtered_presets(self) -> list[dict[str, Any]]:
        if not dpg.does_item_exist("search_input"):
            return self.presets[:]
        search = dpg.get_value("search_input").strip().casefold()
        category = dpg.get_value("category_filter")
        visible = []
        for preset in self.presets:
            name = preset["name"]
            preset_category = get_category(name)
            searchable = (
                f"{name} {preset['bank']} {preset['program']} "
                f"{preset['program'] + 1} {preset_category}"
            ).casefold()
            if search and search not in searchable:
                continue
            if category != "All categories" and category != preset_category:
                continue
            visible.append(preset)
        return visible

    def refresh_preset_list(self, sender: Any = None, app_data: Any = None) -> None:
        if not dpg.does_item_exist("preset_list"):
            return
        dpg.delete_item("preset_list", children_only=True)
        visible = self.filtered_presets()

        for preset in visible:
            label = f"{preset['name']}    [B{preset['bank']} / P{preset['program'] + 1}]"
            dpg.add_selectable(
                label=label,
                parent="preset_list",
                callback=self.preset_callback,
                user_data=preset,
                default_value=(preset == self.selected_preset),
            )

        dpg.set_value("preset_count", f"{len(visible)} / {len(self.presets)} presets")

    def preset_callback(self, sender: Any, app_data: Any, user_data: dict[str, Any]) -> None:
        self.apply_preset(user_data)
        self.refresh_preset_list()

    def apply_preset(self, preset: dict[str, Any] | None) -> None:
        if not preset:
            return
        self.selected_preset = preset
        bank = int(preset["bank"])
        program = int(preset["program"])

        if self.sfid >= 0:
            try:
                with self.synth_lock:
                    for channel in range(16):
                        self.synth.program_select(channel, self.sfid, bank, program)
            except Exception as exc:
                print(f"Program selection error: {exc}")
                self.set_status("Could not select this preset")
                return

        dpg.set_value("voice_name", preset["name"])
        dpg.set_value("voice_details", f"bank {bank:03d}     program {program + 1:03d}")
        self.config["bank"] = bank
        self.config["program"] = program
        self.save_config()

    def navigate_preset(self, direction: int) -> None:
        visible = self.filtered_presets()
        if not visible:
            return
        try:
            index = visible.index(self.selected_preset) if self.selected_preset in visible else 0
        except ValueError:
            index = 0
        self.apply_preset(visible[(index + direction) % len(visible)])
        self.refresh_preset_list()

    # ----- Master output ----------------------------------------------------

    def change_volume(self, sender: Any, app_data: int) -> None:
        self.volume = clamp(app_data, 0, 300)
        with self.synth_lock:
            self.synth.set_gain(self.volume / 100.0)
        dpg.set_value("volume_label", f"{self.volume}%")
        self.save_config()

    def change_note_speed(self, sender: Any, app_data: float) -> None:
        self.note_speed = self._safe_float(app_data, 155.0, 40.0, 360.0)
        dpg.set_value("note_speed_value", f"{self.note_speed:.0f} px/s")
        self.save_config()

    def change_note_growth(self, sender: Any, app_data: float) -> None:
        self.note_grow_speed = self._safe_float(app_data, 120.0, 30.0, 260.0)
        dpg.set_value("note_growth_value", f"{self.note_grow_speed:.0f} px/s")
        self.save_config()

    def change_note_height(self, sender: Any, app_data: float) -> None:
        self.max_note_height = self._safe_float(app_data, 300.0, 60.0, 420.0)
        dpg.set_value("note_height_value", f"{self.max_note_height:.0f} px")
        self.save_config()

    # ----- MIDI input -------------------------------------------------------

    def refresh_midi_inputs(self, sender: Any = None, app_data: Any = None) -> None:
        if not dpg.does_item_exist("midi_combo"):
            return
        self.close_midi_input()
        try:
            names = self.midi_backend.get_input_names()
        except Exception as exc:
            names = []
            print(f"MIDI enumeration error: {exc}")

        options = ["No MIDI input", *names]
        dpg.configure_item("midi_combo", items=options)
        saved_name = self.config.get("midi_input", "")
        preferred = saved_name if saved_name in names else next(
            (name for name in names if "lpk25" in name.casefold()),
            "No MIDI input",
        )
        dpg.set_value("midi_combo", preferred)

        if preferred != "No MIDI input":
            self.open_midi_input(preferred)
        else:
            self.config["midi_input"] = ""
            dpg.set_value("midi_status", "No MIDI device selected")
            self.set_status("Ready — no MIDI input")

    def select_midi(self, sender: Any, app_data: str) -> None:
        self.close_midi_input()
        if app_data == "No MIDI input":
            self.config["midi_input"] = ""
            dpg.set_value("midi_status", "No MIDI device selected")
            self.save_config()
            return
        self.open_midi_input(app_data)

    def open_midi_input(self, name: str) -> None:
        try:
            self.midi_input = self.midi_backend.open_input(name, callback=self.handle_midi)
            self.config["midi_input"] = name
            dpg.set_value("midi_status", name)
            self.set_status(f"MIDI connected: {name}")
            self.save_config()
        except Exception as exc:
            self.midi_input = None
            dpg.set_value("midi_status", "Connection failed")
            self.set_status("MIDI connection failed")
            print(f"MIDI error: {exc}")

    def close_midi_input(self) -> None:
        current = self.midi_input
        self.midi_input = None
        if current is not None:
            try:
                current.close()
            except Exception:
                pass

    def handle_midi(self, msg: mido.Message) -> None:
        """RtMidi callback. Audio calls are serialized with preset/volume changes."""
        if not self.running or self.sfid < 0:
            return
        channel = int(getattr(msg, "channel", 0))
        try:
            with self.synth_lock:
                if msg.type == "note_on" and msg.velocity > 0:
                    self.synth.noteon(channel, msg.note, msg.velocity)
                    self.midi_queue.put_nowait(("on", channel, msg.note, msg.velocity))
                elif msg.type == "note_off" or (msg.type == "note_on" and msg.velocity == 0):
                    self.synth.noteoff(channel, msg.note)
                    self.midi_queue.put_nowait(("off", channel, msg.note, 0))
                elif msg.type == "control_change":
                    self.synth.cc(channel, msg.control, msg.value)
                    self.midi_queue.put_nowait(("cc", channel, msg.control, msg.value))
                elif msg.type == "pitchwheel":
                    self.synth.pitch_bend(channel, msg.pitch + 8192)
        except Exception as exc:
            print(f"MIDI processing error: {exc}")

    # ----- Live visualizer --------------------------------------------------

    def open_visualizer(self) -> None:
        dpg.configure_item("visualizer_window", show=True)
        dpg.focus_item("visualizer_window")

    def process_midi_events(self) -> None:
        while True:
            try:
                event_type, channel, value_a, value_b = self.midi_queue.get_nowait()
            except queue.Empty:
                break

            if event_type == "on":
                pitch, velocity = value_a, value_b
                key = (channel, pitch)
                self.active_notes[key] = {
                    "pitch": pitch,
                    "velocity": velocity,
                    "start": time.monotonic(),
                    "released": None,
                    "height": 0.0,
                }
                self.deferred_notes.discard(key)

            elif event_type == "off":
                key = (channel, value_a)
                if key not in self.active_notes:
                    continue
                if channel in self.sustain_channels:
                    self.deferred_notes.add(key)
                else:
                    self.release_visual_note(key)

            elif event_type == "cc" and value_a == 64:
                if value_b >= 64:
                    self.sustain_channels.add(channel)
                else:
                    self.sustain_channels.discard(channel)
                    for key in list(self.deferred_notes):
                        if key[0] == channel:
                            self.release_visual_note(key)
                            self.deferred_notes.discard(key)

    def release_visual_note(self, key: tuple[int, int]) -> None:
        note = self.active_notes.get(key)
        if note and note["released"] is None:
            duration = time.monotonic() - note["start"]
            note["height"] = min(self.max_note_height, max(9.0, duration * self.note_grow_speed))
            note["released"] = time.monotonic()

    def get_key_geometry(self) -> dict[int, tuple[float, float, str]]:
        pitches = range(21, 109)
        white_pitches = [pitch for pitch in pitches if pitch % 12 in WHITE_KEYS]
        white_width = VIS_WIDTH / len(white_pitches)
        geometry: dict[int, tuple[float, float, str]] = {}

        for pitch in pitches:
            if pitch % 12 in WHITE_KEYS:
                white_index = white_pitches.index(pitch)
                geometry[pitch] = (white_index * white_width, white_width, "white")
            else:
                previous_white = pitch - 1
                while previous_white >= 21 and previous_white % 12 not in WHITE_KEYS:
                    previous_white -= 1
                white_index = white_pitches.index(previous_white)
                x = white_index * white_width + white_width * 0.68
                geometry[pitch] = (x, white_width * 0.62, "black")
        return geometry

    def draw_visualizer(self) -> None:
        if not dpg.is_item_visible("visualizer_window"):
            return

        dpg.delete_item("viz_canvas", children_only=True)
        palette = self.theme_colors or THEMES[self.theme_name]
        now = time.monotonic()
        geometry = self.get_key_geometry()
        accent = self.accent_color
        accent_light = self.accent_light

        dpg.draw_rectangle(
            (0, 0), (VIS_WIDTH, VIS_HEIGHT),
            color=palette["border"], fill=palette["bg"], parent="viz_canvas",
        )

        for y in range(35, KEYBOARD_TOP, 50):
            dpg.draw_line((0, y), (VIS_WIDTH, y), color=palette["grid"], thickness=1, parent="viz_canvas")

        for pitch in range(24, 109, 12):
            if pitch in geometry:
                x, _, _ = geometry[pitch]
                dpg.draw_line(
                    (x, 0), (x, KEYBOARD_TOP),
                    color=self.blend_color(palette["grid"], accent, 0.28),
                    thickness=1, parent="viz_canvas",
                )

        expired: list[tuple[int, int]] = []
        active_pitches = {
            pitch for (_, pitch), note in self.active_notes.items()
            if note["released"] is None
        }

        for key, note in list(self.active_notes.items()):
            if note["released"] is None:
                bottom = KEYBOARD_TOP
                height = min(self.max_note_height, max(10.0, (now - note["start"]) * self.note_grow_speed))
            else:
                height = note["height"]
                bottom = KEYBOARD_TOP - (now - note["released"]) * self.note_speed
                # Keep each note until its entire bar has left the top of the roll.
                if bottom < 0:
                    expired.append(key)
                    continue

            pitch = note["pitch"]
            if pitch not in geometry:
                continue
            x, width, _ = geometry[pitch]
            top = bottom - height
            brightness = 0.58 + (note["velocity"] / 127.0) * 0.42
            fill = tuple(clamp(component * brightness, 0, 255) for component in accent)
            dpg.draw_rectangle(
                (x + 1, top), (x + width - 1, bottom),
                color=accent_light, fill=(*fill, 255), rounding=3, parent="viz_canvas",
            )

        for key in expired:
            self.active_notes.pop(key, None)

        # Piano keys: white first, then black keys above them.
        dpg.draw_rectangle(
            (0, KEYBOARD_TOP), (VIS_WIDTH, VIS_HEIGHT),
            color=palette["border"], fill=palette["keyboard"], parent="viz_canvas",
        )

        for pitch, (x, width, kind) in geometry.items():
            if kind != "white":
                continue
            key_fill = accent if pitch in active_pitches else palette["white_key"]
            dpg.draw_rectangle(
                (x + 1, KEYBOARD_TOP), (x + width - 1, VIS_HEIGHT - 2),
                color=palette["border"], fill=key_fill, rounding=2, parent="viz_canvas",
            )

        for pitch, (x, width, kind) in geometry.items():
            if kind != "black":
                continue
            key_fill = self.accent_dark if pitch in active_pitches else palette["black_key"]
            dpg.draw_rectangle(
                (x, KEYBOARD_TOP), (x + width, KEYBOARD_TOP + KEYBOARD_HEIGHT * 0.62),
                color=palette["bg"], fill=key_fill, rounding=2, parent="viz_canvas",
            )

        for pitch in range(24, 109, 12):
            if pitch in geometry:
                x, _, kind = geometry[pitch]
                if kind == "white":
                    octave = pitch // 12 - 1
                    dpg.draw_text(
                        (x + 4, VIS_HEIGHT - 20), f"C{octave}",
                        color=palette["muted"], size=11, parent="viz_canvas",
                    )

        dpg.draw_line(
            (0, KEYBOARD_TOP), (VIS_WIDTH, KEYBOARD_TOP),
            color=accent, thickness=2, parent="viz_canvas",
        )
        dpg.draw_text(
            (12, 10), f"LIVE  /  {len(active_pitches)} HELD NOTES",
            color=accent_light, size=13, parent="viz_canvas",
        )

    # ----- Utility actions --------------------------------------------------

    def panic(self) -> None:
        try:
            with self.synth_lock:
                for channel in range(16):
                    try:
                        self.synth.cc(channel, 120, 0)  # All sounds off
                        self.synth.cc(channel, 123, 0)  # All notes off
                        self.synth.cc(channel, 64, 0)   # Sustain off
                    except Exception:
                        pass
        except Exception as exc:
            print(f"Panic error: {exc}")

        self.active_notes.clear()
        self.deferred_notes.clear()
        self.sustain_channels.clear()
        while True:
            try:
                self.midi_queue.get_nowait()
            except queue.Empty:
                break

    def set_status(self, message: str) -> None:
        if dpg.does_item_exist("status"):
            dpg.set_value("status", message)

    # ----- Main loop and cleanup -------------------------------------------

    def run(self) -> None:
        try:
            while dpg.is_dearpygui_running():
                self.process_midi_events()
                self.draw_visualizer()
                dpg.render_dearpygui_frame()
        finally:
            self.shutdown()

    def shutdown(self) -> None:
        if not self.running:
            return
        self.running = False
        self.close_midi_input()
        self.panic()
        self.save_config()

        try:
            with self.synth_lock:
                if self.sfid >= 0:
                    self.synth.sfunload(self.sfid, 1)
                    self.sfid = -1
                self.synth.delete()
        except Exception as exc:
            print(f"Shutdown error: {exc}")

        try:
            dpg.destroy_context()
        except Exception:
            pass


# -----------------------------------------------------------------------------
# Entry point
# -----------------------------------------------------------------------------

def main() -> int:
    try:
        app = CopperSynth()
        app.run()
    except Exception:
        print("\ncopperSynth failed:")
        traceback.print_exc()
        try:
            dpg.destroy_context()
        except Exception:
            pass
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
