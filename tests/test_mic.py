"""Microphone selection: pactl parsing, config editing, device plumbing."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from vani import audio, config

PACTL_SOURCES = """\
Source #1
	State: SUSPENDED
	Name: alsa_output.pci-0000_00_1f.3.analog-stereo.monitor
	Description: Monitor of Built-in Audio Analog Stereo
Source #11
	State: SUSPENDED
	Name: alsa_input.usb-046d_HD_Pro_Webcam_C920-02.analog-stereo
	Description: HD Pro Webcam C920 Analog Stereo
Source #31
	State: RUNNING
	Name: bluez_source.60_55_56_3C_33_AC.headset_head_unit
	Description: WH-1000XM4
"""

PACTL_CARDS = """\
Card #40
	Name: bluez_card.60_55_56_3C_33_AC
	Driver: module-bluez5-device.c
	Properties:
		device.description = "WH-1000XM4"
	Profiles:
		a2dp-sink: High Fidelity Playback (A2DP Sink) (sinks: 1, sources: 0, priority: 40, available: yes)
		headset-head-unit: Headset Head Unit (HSP/HFP) (sinks: 1, sources: 1, priority: 30, available: yes)
		off: Off (sinks: 0, sources: 0, priority: 0, available: yes)
	Active Profile: a2dp-sink
Card #41
	Name: alsa_card.usb-046d_HD_Pro_Webcam_C920-02
	Properties:
		device.description = "HD Pro Webcam C920"
	Profiles:
		input:analog-stereo: Analog Stereo Input (sinks: 0, sources: 1, priority: 65, available: yes)
	Active Profile: input:analog-stereo
"""


def fake_pactl(*args):
    return {"sources": PACTL_SOURCES, "cards": PACTL_CARDS}[args[1]]


class SourceParsingTest(unittest.TestCase):
    def test_list_sources_excludes_monitors(self):
        with mock.patch.object(audio, "_pactl", fake_pactl):
            sources = audio.list_sources()
        self.assertEqual(sources, [
            ("alsa_input.usb-046d_HD_Pro_Webcam_C920-02.analog-stereo",
             "HD Pro Webcam C920 Analog Stereo"),
            ("bluez_source.60_55_56_3C_33_AC.headset_head_unit", "WH-1000XM4"),
        ])

    def test_bluetooth_candidates_are_inactive_mic_profiles_only(self):
        with mock.patch.object(audio, "_pactl", fake_pactl):
            cands = audio.bluetooth_mic_candidates()
        # The BT card is in a2dp-sink, so its headset profile is a candidate;
        # the webcam card is not a bluez card and never appears.
        self.assertEqual(cands, [("bluez_card.60_55_56_3C_33_AC",
                                  "headset-head-unit", "WH-1000XM4")])

    def test_no_candidate_once_the_headset_profile_is_active(self):
        active = PACTL_CARDS.replace("Active Profile: a2dp-sink",
                                     "Active Profile: headset-head-unit")
        with mock.patch.object(audio, "_pactl",
                               lambda *a: {"sources": PACTL_SOURCES,
                                           "cards": active}[a[1]]):
            self.assertEqual(audio.bluetooth_mic_candidates(), [])

    def test_bluez_source_presence_matches_on_device_prefix(self):
        with mock.patch.object(audio, "_pactl", fake_pactl):
            # The persisted name may carry a stale profile suffix.
            self.assertTrue(audio._source_present(
                "bluez_source.60_55_56_3C_33_AC.some_old_suffix"))
            self.assertFalse(audio._source_present(
                "bluez_source.11_22_33_44_55_66"))


class MicCommandTest(unittest.TestCase):
    def test_alsa_names_are_recognised(self):
        for name in ("hw:1,0", "plughw:C920", "default", "pulse"):
            self.assertTrue(audio.is_alsa_name(name))
        for name in ("alsa_input.usb-x.analog-stereo", "bluez_source.AA.x"):
            self.assertFalse(audio.is_alsa_name(name))

    def test_pulse_source_goes_through_the_pulse_plugin(self):
        mic = audio.Microphone(16000, 4000, "alsa_input.usb-x.analog-stereo")
        cmd = mic._command()
        self.assertIn("pulse", cmd)
        self.assertEqual(mic._env()["PULSE_SOURCE"],
                         "alsa_input.usb-x.analog-stereo")

    def test_raw_alsa_device_is_passed_straight_to_arecord(self):
        mic = audio.Microphone(16000, 4000, "hw:1,0")
        self.assertIn("hw:1,0", mic._command())
        self.assertIsNone(mic._env())


class SetKeyTest(unittest.TestCase):
    def path(self, text: str) -> Path:
        p = Path(tempfile.mkdtemp()) / "config.toml"
        p.write_text(text)
        return p

    def test_replaces_an_existing_key_in_place(self):
        p = self.path("[recording]\n# a comment\ndevice = \"old\"\nmax_sec = 60\n")
        config.set_key("recording", "device", "new", p)
        text = p.read_text()
        self.assertIn('device = "new"', text)
        self.assertNotIn("old", text)
        self.assertIn("# a comment", text)       # untouched
        self.assertIn("max_sec = 60", text)      # untouched

    def test_inserts_into_an_existing_section(self):
        p = self.path("[recording]\nmax_sec = 60\n\n[output]\nnotify = true\n")
        config.set_key("recording", "device", "mic1", p)
        cfg = config.load(p)
        self.assertEqual(cfg.recording.device, "mic1")
        self.assertEqual(cfg.recording.max_sec, 60.0)
        self.assertIs(cfg.output.notify, True)

    def test_appends_a_missing_section(self):
        p = self.path("[output]\nnotify = false\n")
        config.set_key("recording", "device", "mic1", p)
        cfg = config.load(p)
        self.assertEqual(cfg.recording.device, "mic1")
        self.assertIs(cfg.output.notify, False)

    def test_result_stays_loadable_and_private(self):
        p = self.path("[recording]\ndevice = \"x\"\n")
        config.set_key("recording", "device", 'we"ird', p)
        self.assertEqual(config.load(p).recording.device, 'we"ird')
        self.assertEqual(p.stat().st_mode & 0o777, 0o600)


if __name__ == "__main__":
    unittest.main()
