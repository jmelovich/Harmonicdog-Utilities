Harmonicdog Utilities
---------------------


### Code and scripts associated with the MultiTrack DAW iOS app

***
**mtdawRead.py**
This script reads a MultiTrack DAW song project and prints some info about the song to the terminal. Uses PyObjC to parse the `Tracks2.plist` and `project.plist` files. Also decodes the wav filenames into metadata for each wav file in the  projects `Bin` directory. Call it with the song project directory as its only argument, e.g
	./mtdawRead.py path/to/song.mtdaw

*Dependencies:*
python3

```python
pip install pyobjc
pip install crcmod
```

### Reaper Tools
***
**convert_mtdaw_to_rpp.py**
Convert a MultiTrackDAW project folder (`.mtdaw`) to a REAPER project (`.rpp`). Cross-platform.

Default behavior:
- Creates a bundle folder named `<project>_Reaper` next to the input (or zip). Inside it, writes `<project>.rpp` and copies all referenced WAVs into `Media/`, updating item paths accordingly.

Usage:
```bash
pip install nska_deserialize
# point to a project path (contains the .plist files)
python convert_mtdaw_to_rpp.py /path/to/song
# or a zip containing the project (like exported from wifi server)
python convert_mtdaw_to_rpp.py /path/to/song.zip
```

Options:
- `-o, --output`: explicit path to the `.rpp` file (overrides bundle naming/location)
- `--output-dir`: directory to create the output bundle folder in (default: next to input)
- `--bundle-dir-suffix`: customize bundle suffix (default `_Reaper`)
- `--dry-run`: show actions without writing files

Notes:
- Requires `project.plist`, `Tracks2.plist` (or `Tracks.plist`), and the `Bins/` folder inside the `.mtdaw` directory (or within the zip with the same structure).
- On Windows/macOS/Linux, `nska_deserialize` enables reading the archived `Tracks2.plist` without PyObjC.

Limitations:
- Effects: Track compressor/EQ settings from MultiTrackDAW are not currently mapped to REAPER FX. The converter does not insert ReaComp/ReaEQ or set their parameters.
- Automation: Time-varying automation (volume, pan, sends, FX params) is not exported. Only static per-track volume/pan, and per-item gain/fades are applied.
- Sends/Buses: Sends A/B from MultiTrackDAW are not recreated in REAPER. No buses/aux tracks are generated.
- Markers/Tempo Map: Only a single global tempo and time signature are written. Tempo changes or multiple time signatures are not exported.
- Media Types: Only WAV audio bins are supported. MIDI items and non-WAV sources are not handled.
- Sample Rate Mismatches: Item `SOFFS` uses the bin's samplerate to compute source offsets; extreme mismatches may require manual verification.
- Multiple Projects in Zip: If a zip contains multiple valid projects, only the first one found is converted.

