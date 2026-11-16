
#!/usr/bin/env python3

import sys
import os
import json
import time
import queue
import ctypes
import ctypes.util
import threading
import traceback
from pathlib import Path

import fluidsynth
import mido
import dearpygui.dearpygui as dpg


# config

appName = "copperSynth"

configDir = Path.home() / ".config" / "coppersynth"
configFile = configDir / "settings.json"

whiteKeys = {0, 2, 4, 5, 7, 9, 11}

visWidth = 1100
visHeight = 560
keyboardTop = 430
keyboardHeight = 110

noteSpeed = 155.0
noteGrowSpeed = 120.0
maxNoteHeight = 300.0

accentPresets = {
    "Copper": (205, 132, 88),
    "Amber": (218, 164, 76),
    "Rust": (190, 100, 70),
    "Teal": (83, 166, 155),
    "Sage": (133, 169, 132),
    "Blue": (111, 151, 202),
    "Violet": (157, 129, 195),
    "Rose": (196, 119, 136),
}


# soundfont preset scanner

class PresetScanner:

    def __init__(self):

        library = ctypes.util.find_library("fluidsynth")

        if not library:
            raise RuntimeError(
                "Could not find libfluidsynth."
            )

        self.lib = ctypes.CDLL(library)

        self.bind(
            "fluid_synth_get_sfont_by_id",
            ctypes.c_void_p,
            [ctypes.c_void_p, ctypes.c_int]
        )

        self.bind(
            "fluid_sfont_iteration_start",
            None,
            [ctypes.c_void_p]
        )

        self.bind(
            "fluid_sfont_iteration_next",
            ctypes.c_void_p,
            [ctypes.c_void_p]
        )

        self.bind(
            "fluid_preset_get_name",
            ctypes.c_char_p,
            [ctypes.c_void_p]
        )

        self.bind(
            "fluid_preset_get_banknum",
            ctypes.c_int,
            [ctypes.c_void_p]
        )

        self.bind(
            "fluid_preset_get_num",
            ctypes.c_int,
            [ctypes.c_void_p]
        )

    def bind(self, name, result, arguments):

        function = getattr(self.lib, name)

        function.restype = result
        function.argtypes = arguments

        setattr(self, name, function)

    def scan(self, synth, sfid):

        pointer = synth.synth

        if isinstance(pointer, ctypes.c_void_p):
            synth_pointer = pointer
        else:
            synth_pointer = ctypes.c_void_p(pointer)

        soundfont = self.fluid_synth_get_sfont_by_id(
            synth_pointer,
            int(sfid)
        )

        if not soundfont:
            raise RuntimeError(
                "Could not retrieve the loaded SoundFont."
            )

        soundfont_pointer = ctypes.c_void_p(soundfont)

        self.fluid_sfont_iteration_start(
            soundfont_pointer
        )

        presets = []

        for _ in range(100000):

            preset = self.fluid_sfont_iteration_next(
                soundfont_pointer
            )

            if not preset:
                break

            preset_pointer = ctypes.c_void_p(preset)

            raw_name = self.fluid_preset_get_name(
                preset_pointer
            )

            name = (
                raw_name.decode("utf-8", errors="replace")
                if raw_name else "Unnamed preset"
            )

            bank = self.fluid_preset_get_banknum(
                preset_pointer
            )

            program = self.fluid_preset_get_num(
                preset_pointer
            )

            presets.append({
                "name": name,
                "bank": int(bank),
                "program": int(program)
            })

        presets.sort(
            key=lambda p: (
                p["name"].casefold(),
                p["bank"],
                p["program"]
            )
        )

        return presets


# instrument categories

def get_category(name):

    name = name.casefold()

    categories = {
        "Pianos": [
            "piano", "grand", "upright", "pianet"
        ],

        "Electric Keys": [
            "electric piano", "e.piano", "epiano",
            "clav", "harpsichord", "celesta"
        ],

        "Organs": [
            "organ", "hammond", "church"
        ],

        "Guitars": [
            "guitar", "nylon", "steel guitar"
        ],

        "Bass": [
            "bass", "sub"
        ],

        "Strings": [
            "violin", "viola", "cello", "string",
            "orchestra", "harp"
        ],

        "Brass": [
            "trumpet", "trombone", "tuba",
            "brass", "horn"
        ],

        "Winds": [
            "flute", "clarinet", "oboe", "bassoon",
            "sax", "recorder", "whistle"
        ],

        "Synths": [
            "synth", "lead", "pad", "analog",
            "saw", "square", "wave", "electro"
        ],

        "Percussion": [
            "drum", "percussion", "timpani",
            "tom", "cymbal", "snare"
        ]
    }

    for category, keywords in categories.items():

        if any(word in name for word in keywords):
            return category

    return "Other"


# main application

class copperSynth:

    def __init__(self):

        self.config = self.load_config()

        # volume is stored as a percentage; accept older 0.0-3.0 gain values too
        try:
            saved_volume = float(self.config.get("volume", 0.8))
            if 0.0 <= saved_volume <= 3.0:
                self.volume = round(saved_volume * 100)
            else:
                self.volume = round(saved_volume)
        except (TypeError, ValueError):
            self.volume = 80

        self.volume = max(0, min(300, self.volume))
        self.config["volume"] = self.volume

        saved_accent = self.config.get("accent", "Copper")
        saved_rgb = self.config.get("accent_rgb")

        # accept named accents and saved rgb values
        if isinstance(saved_accent, str) and saved_accent in accentPresets:
            self.accent_name = saved_accent
            self.accent_color = accentPresets[saved_accent]
        else:
            custom_rgb = saved_rgb
            if not (
                isinstance(custom_rgb, (list, tuple))
                and len(custom_rgb) >= 3
                and all(isinstance(v, (int, float)) for v in custom_rgb[:3])
            ):
                custom_rgb = saved_accent

            if (
                isinstance(custom_rgb, (list, tuple))
                and len(custom_rgb) >= 3
                and all(isinstance(v, (int, float)) for v in custom_rgb[:3])
            ):
                self.accent_name = "Custom"
                self.accent_color = tuple(
                    max(0, min(255, int(v))) for v in custom_rgb[:3]
                )
            else:
                self.accent_name = "Copper"
                self.accent_color = accentPresets["Copper"]

        self.accent_text_items = []
        self.theme_tag = None
        self.refresh_accent_shades()

        self.synth = fluidsynth.Synth(
            gain=self.volume / 100.0,
            samplerate=44100
        )

        self.start_audio()

        self.scanner = PresetScanner()

        self.sfid = -1
        self.soundfont_path = ""
        self.presets = []

        self.midi_backend = mido.Backend(
            "mido.backends.rtmidi"
        )

        self.midi_input = None

        self.midi_queue = queue.Queue()

        self.active_notes = {}
        self.sustain_channels = set()
        self.deferred_notes = set()

        self.selected_preset = None

        self.running = True

        self.build_interface()

        saved_soundfont = self.config.get("soundfont")

        if saved_soundfont and Path(saved_soundfont).is_file():
            self.load_soundfont(saved_soundfont)

        self.refresh_midi_inputs()

    # settings

    def load_config(self):

        try:
            with open(configFile, "r") as file:
                return json.load(file)

        except Exception:
            return {}

    def save_config(self):

        configDir.mkdir(
            parents=True,
            exist_ok=True
        )

        data = {
            "volume": self.volume,
            "soundfont": self.soundfont_path,
            "accent": self.accent_name,
            "accent_rgb": list(self.accent_color),
            "bank": (
                self.selected_preset["bank"]
                if self.selected_preset else 0
            ),
            "program": (
                self.selected_preset["program"]
                if self.selected_preset else 0
            )
        }

        with open(configFile, "w") as file:
            json.dump(data, file, indent=4)

    # audio

    def start_audio(self):

        errors = []

        for driver in ("pulseaudio", "alsa"):

            try:
                self.synth.start(driver=driver)
                self.audio_driver = driver
                return

            except Exception as exc:
                errors.append(f"{driver}: {exc}")

        raise RuntimeError(
            "Could not initialize audio:\n" +
            "\n".join(errors)
        )

    # interface

    def add_accent_text(self, value, **kwargs):

        item = dpg.add_text(
            value,
            color=self.accent_light,
            **kwargs
        )
        self.accent_text_items.append(item)
        return item

    def build_interface(self):

        dpg.create_context()
        self.create_theme()

        dpg.create_viewport(
            title=appName,
            width=1380,
            height=900,
            min_width=1050,
            min_height=720
        )

        with dpg.window(
            label=appName,
            tag="main_window",
            no_close=True
        ):
            # header
            with dpg.group(horizontal=True):
                with dpg.group():
                    self.add_accent_text("copperSynth")
                    dpg.add_text(
                        "soundfont workstation  /  midi instrument monitor",
                        color=(137, 133, 128)
                    )

                dpg.add_spacer(width=35)
                dpg.add_text("accent", color=(145, 138, 130))
                dpg.add_combo(
                    items=list(accentPresets.keys()) + ["Custom"],
                    tag="accent_preset",
                    default_value=self.accent_name,
                    width=115,
                    callback=self.change_accent_preset
                )
                dpg.add_color_edit(
                    tag="accent_picker",
                    default_value=(*self.accent_color, 255),
                    no_alpha=True,
                    width=165,
                    callback=self.change_accent_custom
                )

            dpg.add_spacer(height=10)
            dpg.add_separator()
            dpg.add_spacer(height=12)

            with dpg.group(horizontal=True):
                # library panel
                with dpg.child_window(
                    tag="library_panel",
                    width=410,
                    height=-1,
                    border=True
                ):
                    with dpg.group(horizontal=True):
                        self.add_accent_text("soundfont")
                        dpg.add_spacer(width=8)
                        dpg.add_text(
                            "sound library",
                            color=(126, 122, 117)
                        )

                    dpg.add_button(
                        label="Load SoundFont  +",
                        width=-1,
                        height=38,
                        callback=lambda: dpg.show_item("sf_dialog")
                    )
                    dpg.add_text(
                        "No SoundFont loaded",
                        tag="sf_name",
                        color=(174, 166, 157),
                        wrap=370
                    )
                    dpg.add_spacer(height=12)
                    dpg.add_separator()
                    dpg.add_spacer(height=10)

                    with dpg.group(horizontal=True):
                        self.add_accent_text("voice library")
                        dpg.add_spacer(width=8)
                        dpg.add_text(
                            "presets",
                            tag="preset_count",
                            color=(143, 137, 130)
                        )

                    dpg.add_input_text(
                        hint="Search by instrument, bank or program...",
                        tag="search_input",
                        width=-1,
                        callback=self.refresh_preset_list
                    )
                    dpg.add_combo(
                        items=[
                            "All categories",
                            "Pianos",
                            "Electric Keys",
                            "Organs",
                            "Guitars",
                            "Bass",
                            "Strings",
                            "Brass",
                            "Winds",
                            "Synths",
                            "Percussion",
                            "Other"
                        ],
                        tag="category_filter",
                        default_value="All categories",
                        width=-1,
                        callback=self.refresh_preset_list
                    )
                    dpg.add_spacer(height=7)
                    dpg.add_child_window(
                        tag="preset_list",
                        width=-1,
                        height=-1,
                        border=False
                    )

                # workstation panel
                with dpg.child_window(
                    tag="workstation_panel",
                    width=-1,
                    height=-1,
                    border=True
                ):
                    with dpg.child_window(
                        tag="voice_card",
                        width=-1,
                        height=155,
                        border=True
                    ):
                        with dpg.group(horizontal=True):
                            with dpg.group():
                                self.add_accent_text("current voice")
                                dpg.add_text(
                                    "No instrument selected",
                                    tag="voice_name",
                                    color=(235, 222, 207),
                                    wrap=480
                                )
                                dpg.add_text(
                                    "bank ---     program ---",
                                    tag="voice_details",
                                    color=(150, 143, 135)
                                )
                            dpg.add_spacer(width=20)
                            with dpg.group():
                                dpg.add_spacer(height=22)
                                dpg.add_button(
                                    label="Previous",
                                    width=125,
                                    callback=lambda: self.navigate_preset(-1)
                                )
                                dpg.add_button(
                                    label="Next",
                                    width=125,
                                    callback=lambda: self.navigate_preset(1)
                                )

                    dpg.add_spacer(height=10)

                    with dpg.group(horizontal=True):
                        with dpg.child_window(
                            tag="output_card",
                            width=410,
                            height=225,
                            border=True
                        ):
                            self.add_accent_text("master output")
                            dpg.add_text(
                                "FluidSynth playback level",
                                color=(137, 132, 126)
                            )
                            dpg.add_slider_int(
                                tag="volume_slider",
                                min_value=0,
                                max_value=300,
                                default_value=self.volume,
                                width=-1,
                                callback=self.change_volume
                            )
                            dpg.add_text(
                                f"{self.volume}%",
                                tag="volume_label",
                                color=(190, 181, 170)
                            )
                            dpg.add_spacer(height=12)
                            dpg.add_separator()
                            dpg.add_spacer(height=9)
                            self.add_accent_text("audio engine")
                            dpg.add_text(
                                f"Driver  ·  {self.audio_driver}",
                                color=(166, 158, 149)
                            )
                            dpg.add_text(
                                "44.1 kHz  ·  16 MIDI channels",
                                color=(137, 132, 126)
                            )

                        with dpg.child_window(
                            tag="midi_card",
                            width=-1,
                            height=225,
                            border=True
                        ):
                            self.add_accent_text("midi input")
                            dpg.add_text(
                                "Choose the keyboard or controller",
                                color=(137, 132, 126)
                            )
                            dpg.add_combo(
                                items=["No MIDI input"],
                                tag="midi_combo",
                                default_value="No MIDI input",
                                width=-1,
                                callback=self.select_midi
                            )
                            dpg.add_button(
                                label="Refresh devices",
                                width=-1,
                                callback=self.refresh_midi_inputs
                            )
                            dpg.add_spacer(height=9)
                            dpg.add_text(
                                "connected device",
                                color=(137, 132, 126)
                            )
                            dpg.add_text(
                                "Waiting for MIDI input",
                                tag="midi_status",
                                color=(190, 181, 170),
                                wrap=300
                            )

                    dpg.add_spacer(height=10)
                    dpg.add_separator()
                    dpg.add_spacer(height=10)

                    with dpg.group(horizontal=True):
                        dpg.add_button(
                            label="open live visualizer",
                            width=-1,
                            height=48,
                            callback=self.open_visualizer
                        )
                        dpg.add_button(
                            label="all notes off",
                            width=155,
                            height=48,
                            callback=self.panic
                        )

                    dpg.add_spacer(height=9)
                    with dpg.group(horizontal=True):
                        self.add_accent_text("engine status")
                        dpg.add_spacer(width=8)
                        dpg.add_text(
                            "Ready",
                            tag="status",
                            color=(190, 181, 170)
                        )

        # soundfont file picker
        with dpg.file_dialog(
            directory_selector=False,
            show=False,
            callback=self.soundfont_dialog_callback,
            tag="sf_dialog",
            width=700,
            height=450
        ):
            dpg.add_file_extension(".sf2", color=(220, 160, 110))
            dpg.add_file_extension(".sf3", color=(220, 160, 110))
            dpg.add_file_extension(".*")

        # live visualizer
        with dpg.window(
            label="copperSynth  /  live visualizer",
            tag="visualizer_window",
            width=1160,
            height=690,
            min_size=(900, 560),
            show=False
        ):
            self.add_accent_text("live midi visualizer")
            dpg.add_text(
                "Released notes travel upward and remain visible until the entire bar clears the top edge.",
                color=(137, 132, 126)
            )
            dpg.add_drawlist(
                width=visWidth,
                height=visHeight,
                tag="viz_canvas"
            )

        dpg.setup_dearpygui()
        dpg.set_primary_window("main_window", True)
        dpg.show_viewport()

    # theme

    @staticmethod
    def blend_color(first, second, amount):

        return tuple(
            int(a + (b - a) * amount)
            for a, b in zip(first, second)
        )

    def refresh_accent_shades(self):

        self.accent_color = tuple(
            max(0, min(255, int(component)))
            for component in self.accent_color[:3]
        )
        self.accent_light = self.blend_color(
            self.accent_color,
            (255, 226, 202),
            0.34
        )
        self.accent_hover = self.blend_color(
            self.accent_color,
            (255, 255, 255),
            0.15
        )
        self.accent_active = self.blend_color(
            self.accent_color,
            (255, 255, 255),
            0.28
        )
        self.accent_dark = self.blend_color(
            self.accent_color,
            (24, 25, 26),
            0.58
        )

    def create_theme(self):

        old_theme = self.theme_tag

        with dpg.theme() as theme:
            with dpg.theme_component(dpg.mvAll):
                dpg.add_theme_color(
                    dpg.mvThemeCol_WindowBg,
                    (22, 23, 24)
                )
                dpg.add_theme_color(
                    dpg.mvThemeCol_ChildBg,
                    (29, 30, 31)
                )
                dpg.add_theme_color(
                    dpg.mvThemeCol_PopupBg,
                    (32, 32, 32)
                )
                dpg.add_theme_color(
                    dpg.mvThemeCol_FrameBg,
                    (40, 40, 40)
                )
                dpg.add_theme_color(
                    dpg.mvThemeCol_FrameBgHovered,
                    self.blend_color((40, 40, 40), self.accent_color, 0.22)
                )
                dpg.add_theme_color(
                    dpg.mvThemeCol_FrameBgActive,
                    self.blend_color((40, 40, 40), self.accent_color, 0.38)
                )
                dpg.add_theme_color(
                    dpg.mvThemeCol_Button,
                    self.accent_dark
                )
                dpg.add_theme_color(
                    dpg.mvThemeCol_ButtonHovered,
                    self.accent_color
                )
                dpg.add_theme_color(
                    dpg.mvThemeCol_ButtonActive,
                    self.accent_active
                )
                dpg.add_theme_color(
                    dpg.mvThemeCol_Header,
                    self.blend_color((36, 36, 36), self.accent_color, 0.32)
                )
                dpg.add_theme_color(
                    dpg.mvThemeCol_HeaderHovered,
                    self.blend_color((36, 36, 36), self.accent_color, 0.48)
                )
                dpg.add_theme_color(
                    dpg.mvThemeCol_HeaderActive,
                    self.blend_color((36, 36, 36), self.accent_color, 0.58)
                )
                dpg.add_theme_color(
                    dpg.mvThemeCol_SliderGrab,
                    self.accent_color
                )
                dpg.add_theme_color(
                    dpg.mvThemeCol_SliderGrabActive,
                    self.accent_active
                )
                dpg.add_theme_color(
                    dpg.mvThemeCol_CheckMark,
                    self.accent_light
                )
                dpg.add_theme_color(
                    dpg.mvThemeCol_Border,
                    (57, 55, 52)
                )
                dpg.add_theme_color(
                    dpg.mvThemeCol_Separator,
                    (57, 55, 52)
                )
                dpg.add_theme_style(dpg.mvStyleVar_FrameRounding, 4)
                dpg.add_theme_style(dpg.mvStyleVar_WindowRounding, 5)
                dpg.add_theme_style(dpg.mvStyleVar_FramePadding, 9, 7)
                dpg.add_theme_style(dpg.mvStyleVar_WindowPadding, 14, 13)
                dpg.add_theme_style(dpg.mvStyleVar_ItemSpacing, 9, 8)

        dpg.bind_theme(theme)
        self.theme_tag = theme

        if old_theme is not None and dpg.does_item_exist(old_theme):
            dpg.delete_item(old_theme)

    def update_accent_ui(self):

        self.refresh_accent_shades()
        self.create_theme()

        for item in self.accent_text_items:
            if dpg.does_item_exist(item):
                dpg.configure_item(item, color=self.accent_light)

        # update the theme and color picker
        if dpg.does_item_exist("accent_picker"):
            dpg.set_value(
                "accent_picker",
                (*self.accent_color, 255)
            )
        if dpg.does_item_exist("accent_preset"):
            dpg.set_value("accent_preset", self.accent_name)

        self.config["accent"] = self.accent_name
        self.config["accent_rgb"] = list(self.accent_color)

    def change_accent_preset(self, sender, app_data):

        if app_data in accentPresets:
            self.accent_name = app_data
            self.accent_color = accentPresets[app_data]
            self.update_accent_ui()

    def change_accent_custom(self, sender, app_data):

        try:
            self.accent_color = tuple(
                max(0, min(255, int(component)))
                for component in app_data[:3]
            )
        except (TypeError, ValueError):
            return

        self.accent_name = "Custom"
        self.update_accent_ui()

    # soundfont loading

    def soundfont_dialog_callback(self, sender, app_data):

        path = app_data.get("file_path_name")

        if path:
            self.load_soundfont(path)

    def load_soundfont(self, path):

        path = str(
            Path(path).expanduser().resolve()
        )

        new_sfid = -1

        dpg.set_value(
            "status",
            "Scanning SoundFont..."
        )

        try:

            new_sfid = self.synth.sfload(path, 0)

            if new_sfid < 0:
                raise RuntimeError(
                    "FluidSynth could not load the SoundFont."
                )

            new_presets = self.scanner.scan(
                self.synth,
                new_sfid
            )

            if not new_presets:
                raise RuntimeError(
                    "No presets found in this SoundFont."
                )

            old_sfid = self.sfid

            self.sfid = new_sfid
            self.soundfont_path = path
            self.presets = new_presets

            dpg.set_value(
                "sf_name",
                Path(path).name
            )

            self.refresh_preset_list()

            if old_sfid >= 0:
                self.synth.sfunload(old_sfid, 0)

            dpg.set_value(
                "status",
                f"{len(new_presets)} presets loaded"
            )

            # Restore the previous voice if it exists.
            old_bank = self.config.get("bank", 0)
            old_program = self.config.get("program", 0)

            matching = next(
                (
                    p for p in new_presets
                    if p["bank"] == old_bank
                    and p["program"] == old_program
                ),
                new_presets[0]
            )

            self.apply_preset(matching)

        except Exception as exc:

            if new_sfid >= 0 and new_sfid != self.sfid:

                try:
                    self.synth.sfunload(new_sfid, 0)
                except Exception:
                    pass

            dpg.set_value(
                "status",
                "SoundFont error"
            )

            print("SoundFont error:", exc)

    # preset browser

    def refresh_preset_list(self, sender=None, app_data=None):

        if not dpg.does_item_exist("preset_list"):
            return

        dpg.delete_item(
            "preset_list",
            children_only=True
        )

        search = dpg.get_value(
            "search_input"
        ).strip().casefold()

        category = dpg.get_value(
            "category_filter"
        )

        visible = 0

        for preset in self.presets:

            name = preset["name"]
            preset_category = get_category(name)

            searchable = (
                f'{name} {preset["bank"]} '
                f'{preset["program"]} '
                f'{preset["program"] + 1} '
                f'{preset_category}'
            ).casefold()

            if search and search not in searchable:
                continue

            if (
                category != "All categories"
                and category != preset_category
            ):
                continue

            label = (
                f'{name}  '
                f'[B{preset["bank"]} / '
                f'P{preset["program"] + 1}]'
            )

            dpg.add_selectable(
                label=label,
                parent="preset_list",
                callback=self.preset_callback,
                user_data=preset
            )

            visible += 1

        dpg.set_value(
            "preset_count",
            f"{visible} / {len(self.presets)} presets"
        )

    def preset_callback(self, sender, app_data, user_data):

        self.apply_preset(user_data)

    def apply_preset(self, preset):

        if not preset:
            return

        self.selected_preset = preset

        name = preset["name"]
        bank = preset["bank"]
        program = preset["program"]

        dpg.set_value(
            "voice_name",
            name
        )

        dpg.set_value(
            "voice_details",
            f"bank {bank:03d}     program {program + 1:03d}"
        )

        if self.sfid >= 0:

            for channel in range(16):

                try:
                    self.synth.program_select(
                        channel,
                        self.sfid,
                        bank,
                        program
                    )

                except Exception as exc:
                    print("Program selection error:", exc)

        self.config["bank"] = bank
        self.config["program"] = program

    def navigate_preset(self, direction):

        if not self.presets:
            return

        search = dpg.get_value(
            "search_input"
        ).strip().casefold()

        category = dpg.get_value(
            "category_filter"
        )

        visible = []

        for preset in self.presets:

            name = preset["name"]
            preset_category = get_category(name)

            searchable = (
                f'{name} {preset["bank"]} '
                f'{preset["program"]} '
                f'{preset["program"] + 1} '
                f'{preset_category}'
            ).casefold()

            if search and search not in searchable:
                continue

            if (
                category != "All categories"
                and category != preset_category
            ):
                continue

            visible.append(preset)

        if not visible:
            return

        if self.selected_preset in visible:
            index = visible.index(self.selected_preset)
        else:
            index = 0

        index = (index + direction) % len(visible)

        self.apply_preset(visible[index])

    # volume

    def change_volume(self, sender, app_data):

        self.volume = int(app_data)

        self.synth.set_gain(
            self.volume / 100.0
        )

        dpg.set_value(
            "volume_label",
            f"{self.volume}%"
        )

    # midi

    def refresh_midi_inputs(self):

        self.close_midi_input()

        try:
            names = self.midi_backend.get_input_names()

        except Exception as exc:
            names = []
            print("MIDI enumeration error:", exc)

        items = ["No MIDI input"] + names

        dpg.configure_item(
            "midi_combo",
            items=items
        )

        preferred = next(
            (
                name for name in names
                if "lpk25" in name.casefold()
            ),
            "No MIDI input"
        )

        dpg.set_value(
            "midi_combo",
            preferred
        )

        if preferred != "No MIDI input":
            self.open_midi_input(preferred)
        elif dpg.does_item_exist("midi_status"):
            dpg.set_value("midi_status", "No MIDI device selected")

    def select_midi(self, sender, app_data):

        self.close_midi_input()

        if app_data == "No MIDI input":
            if dpg.does_item_exist("midi_status"):
                dpg.set_value("midi_status", "No MIDI device selected")
            return

        self.open_midi_input(app_data)

    def open_midi_input(self, name):

        try:

            self.midi_input = self.midi_backend.open_input(
                name,
                callback=self.handle_midi
            )

            dpg.set_value(
                "status",
                f"MIDI connected: {name}"
            )
            if dpg.does_item_exist("midi_status"):
                dpg.set_value("midi_status", name)

        except Exception as exc:

            dpg.set_value(
                "status",
                "MIDI connection failed"
            )
            if dpg.does_item_exist("midi_status"):
                dpg.set_value("midi_status", "Connection failed")

            print("MIDI error:", exc)

    def close_midi_input(self):

        if self.midi_input is not None:

            try:
                self.midi_input.close()
            except Exception:
                pass

            self.midi_input = None

    def handle_midi(self, msg):

        if self.sfid < 0:
            return

        channel = getattr(msg, "channel", 0)

        try:

            if msg.type == "note_on" and msg.velocity > 0:

                self.synth.noteon(
                    channel,
                    msg.note,
                    msg.velocity
                )

                self.midi_queue.put((
                    "on",
                    channel,
                    msg.note,
                    msg.velocity
                ))

            elif (
                msg.type == "note_off"
                or (
                    msg.type == "note_on"
                    and msg.velocity == 0
                )
            ):

                self.synth.noteoff(
                    channel,
                    msg.note
                )

                self.midi_queue.put((
                    "off",
                    channel,
                    msg.note,
                    0
                ))

            elif msg.type == "control_change":

                self.synth.cc(
                    channel,
                    msg.control,
                    msg.value
                )

                self.midi_queue.put((
                    "cc",
                    channel,
                    msg.control,
                    msg.value
                ))

            elif msg.type == "pitchwheel":

                self.synth.pitch_bend(
                    channel,
                    msg.pitch + 8192
                )

        except Exception as exc:
            print("MIDI processing error:", exc)

    # visualizer window

    def open_visualizer(self):

        dpg.configure_item(
            "visualizer_window",
            show=True
        )

        dpg.focus_item(
            "visualizer_window"
        )

    # midi event queue

    def process_midi_events(self):

        while True:

            try:
                event = self.midi_queue.get_nowait()

            except queue.Empty:
                break

            event_type = event[0]
            channel = event[1]

            if event_type == "on":

                pitch = event[2]
                velocity = event[3]

                self.active_notes[(channel, pitch)] = {
                    "pitch": pitch,
                    "velocity": velocity,
                    "start": time.monotonic(),
                    "released": None,
                    "height": 0
                }

                self.deferred_notes.discard(
                    (channel, pitch)
                )

            elif event_type == "off":

                pitch = event[2]
                key = (channel, pitch)

                if key not in self.active_notes:
                    continue

                if channel in self.sustain_channels:
                    self.deferred_notes.add(key)

                else:
                    self.release_visual_note(key)

            elif event_type == "cc":

                control = event[2]
                value = event[3]

                if control == 64:

                    if value >= 64:
                        self.sustain_channels.add(channel)

                    else:

                        self.sustain_channels.discard(channel)

                        for key in list(self.deferred_notes):

                            if key[0] == channel:
                                self.release_visual_note(key)
                                self.deferred_notes.discard(key)

    def release_visual_note(self, key):

        note = self.active_notes.get(key)

        if note and note["released"] is None:

            now = time.monotonic()

            duration = now - note["start"]

            note["height"] = min(
                maxNoteHeight,
                max(9, duration * noteGrowSpeed)
            )

            note["released"] = now

    # piano geometry

    def get_key_geometry(self):

        pitches = range(21, 109)

        white_pitches = [
            p for p in pitches
            if p % 12 in whiteKeys
        ]

        white_width = visWidth / len(white_pitches)

        geometry = {}

        for pitch in pitches:

            pitch_class = pitch % 12

            if pitch_class in whiteKeys:

                white_index = white_pitches.index(pitch)

                geometry[pitch] = (
                    white_index * white_width,
                    white_width,
                    "white"
                )

            else:

                previous_white = pitch - 1

                while previous_white >= 21:
                    if previous_white % 12 in whiteKeys:
                        break
                    previous_white -= 1

                white_index = white_pitches.index(
                    previous_white
                )

                x = (
                    white_index * white_width
                    + white_width * 0.68
                )

                geometry[pitch] = (
                    x,
                    white_width * 0.62,
                    "black"
                )

        return geometry

    # drawing

    def draw_visualizer(self):

        if not dpg.is_item_visible("visualizer_window"):
            return

        dpg.delete_item("viz_canvas", children_only=True)

        now = time.monotonic()
        geometry = self.get_key_geometry()
        accent = self.accent_color
        accent_light = self.accent_light

        # background and piano roll
        dpg.draw_rectangle(
            (0, 0),
            (visWidth, visHeight),
            color=(36, 34, 32),
            fill=(24, 25, 26),
            parent="viz_canvas"
        )

        for y in range(35, keyboardTop, 50):
            dpg.draw_line(
                (0, y),
                (visWidth, y),
                color=(43, 43, 42),
                thickness=1,
                parent="viz_canvas"
            )

        for pitch in range(24, 109, 12):
            if pitch not in geometry:
                continue
            x, width, kind = geometry[pitch]
            dpg.draw_line(
                (x, 0),
                (x, keyboardTop),
                color=self.blend_color((48, 46, 43), accent, 0.18),
                thickness=1,
                parent="viz_canvas"
            )

        # remove notes after the full bar leaves the roll
        expired = []
        for key, note in list(self.active_notes.items()):
            if note["released"] is None:
                bottom = keyboardTop
                height = min(
                    maxNoteHeight,
                    max(10, (now - note["start"]) * noteGrowSpeed)
                )
            else:
                height = note["height"]
                travel = (now - note["released"]) * noteSpeed
                bottom = keyboardTop - travel

                if bottom < 0:
                    expired.append(key)
                    continue

            pitch = note["pitch"]
            if pitch not in geometry:
                continue

            x, width, kind = geometry[pitch]
            top = bottom - height
            velocity = note["velocity"]
            brightness = 0.58 + (velocity / 127.0) * 0.42
            fill = tuple(
                max(0, min(255, int(component * brightness)))
                for component in accent
            )

            dpg.draw_rectangle(
                (x + 1, top),
                (x + width - 1, bottom),
                color=accent_light,
                fill=(*fill, 255),
                rounding=3,
                parent="viz_canvas"
            )

        for key in expired:
            self.active_notes.pop(key, None)

        # keyboard
        dpg.draw_rectangle(
            (0, keyboardTop),
            (visWidth, visHeight),
            color=(78, 72, 66),
            fill=(32, 32, 31),
            parent="viz_canvas"
        )

        for pitch, (x, width, kind) in geometry.items():
            if kind != "white":
                continue
            active = any(
                note_key[1] == pitch and note["released"] is None
                for note_key, note in self.active_notes.items()
            )
            key_fill = accent if active else (218, 214, 205)
            dpg.draw_rectangle(
                (x + 1, keyboardTop),
                (x + width - 1, visHeight - 2),
                color=(85, 81, 75),
                fill=key_fill,
                rounding=2,
                parent="viz_canvas"
            )

        for pitch, (x, width, kind) in geometry.items():
            if kind != "black":
                continue
            active = any(
                note_key[1] == pitch and note["released"] is None
                for note_key, note in self.active_notes.items()
            )
            key_fill = self.accent_dark if active else (27, 27, 27)
            dpg.draw_rectangle(
                (x, keyboardTop),
                (x + width, keyboardTop + keyboardHeight * 0.62),
                color=(12, 12, 12),
                fill=key_fill,
                rounding=2,
                parent="viz_canvas"
            )

        for pitch in range(24, 109, 12):
            if pitch not in geometry:
                continue
            x, width, kind = geometry[pitch]
            if kind == "white":
                octave = pitch // 12 - 1
                dpg.draw_text(
                    (x + 4, visHeight - 20),
                    f"C{octave}",
                    color=(93, 87, 79),
                    size=11,
                    parent="viz_canvas"
                )

        dpg.draw_line(
            (0, keyboardTop),
            (visWidth, keyboardTop),
            color=accent,
            thickness=2,
            parent="viz_canvas"
        )

    # panic

    def panic(self):

        for channel in range(16):

            try:
                self.synth.cc(channel, 120, 0)
                self.synth.cc(channel, 123, 0)
                self.synth.cc(channel, 64, 0)

            except Exception:
                pass

        self.active_notes.clear()
        self.deferred_notes.clear()
        self.sustain_channels.clear()

        while not self.midi_queue.empty():

            try:
                self.midi_queue.get_nowait()
            except queue.Empty:
                break

    # main loop

    def run(self):

        try:

            while dpg.is_dearpygui_running():

                self.process_midi_events()
                self.draw_visualizer()

                dpg.render_dearpygui_frame()

        finally:
            self.shutdown()

    # shutdown

    def shutdown(self):

        if not self.running:
            return

        self.running = False

        self.close_midi_input()
        self.panic()

        try:

            if self.sfid >= 0:
                self.synth.sfunload(self.sfid, 1)

            self.save_config()

            self.synth.delete()

        except Exception as exc:
            print("Shutdown error:", exc)

        dpg.destroy_context()


# entry point

def main():

    try:
        app = copperSynth()
        app.run()

    except Exception as exc:

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
