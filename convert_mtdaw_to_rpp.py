#!/usr/bin/env python3

"""
convert_mtdaw_to_rpp.py

Cross-platform converter from MultiTrackDAW (.mtdaw project folder) to REAPER (.rpp).

Requirements (install via pip):
  - nska_deserialize (pure-Python NSKeyedArchive deserializer)

Default behavior:
  - Writes an .rpp file next to the input .mtdaw folder (same basename)
  - Moves all referenced WAV files from the project's Bins/ folder into a 'Media' folder
    placed next to the output .rpp, and rewrites item sources to those new relative paths

Use --copy-media to copy instead of move, or --keep-media to leave files in place.
"""

import argparse
import os
import sys
import shutil
import math
import plistlib
import zipfile
import tempfile
from typing import Dict, List, Tuple

# Use local utilities for consistent volume mappings
try:
    from Utilities import ScalarToAmplitude, MMtoDB, DBtoMM, IntVolToFloat
except Exception:
    # Fallbacks if Utilities is missing; keep behavior consistent with repo Utilities
    def ScalarToAmplitude(scalar: float) -> float:
        if scalar == 0.0:
            return 0.0
        the_db = (-45.0) + (12.0 - -45.0) * scalar
        return math.pow(10.0, 0.05 * the_db)

    def MMtoDB(mm: float) -> float:
        return (-45.0) + (12.0 - -45.0) * mm

    def DBtoMM(db: float) -> float:
        return (db - -45.0) / (12.0 - -45.0)

    def IntVolToFloat(ivol: int) -> float:
        return ivol / float(0x1000)


def signature_for_index(sig_index: int) -> Tuple[int, int]:
    mapping = {
        0: (2, 2), 1: (2, 4), 2: (3, 4), 3: (4, 4), 4: (5, 4),
        5: (7, 4), 6: (6, 8), 7: (7, 8), 8: (9, 8), 9: (11, 8), 10: (12, 8)
    }
    return mapping.get(sig_index, (4, 4))


class BinHolder:
    """Parse MultiTrackDAW wav filename metadata to map binID -> file and properties."""

    def __init__(self, fullpath: str):
        self.fullpath = fullpath
        self.binID = None
        self.channels = None
        self.samples = None
        self.bitsPerSample = None
        self.samplerate = None
        self.hash = None
        self.offset = None
        self.name_bytes = None
        self._parse()

    @staticmethod
    def _bitrate_to_samplerate(bitrate_format: int) -> int:
        bitrates = {0: 0, 1: 11025, 2: 12000, 3: 22050, 4: 24000, 5: 44100, 6: 48000, 7: 88200, 8: 96000}
        return bitrates.get(bitrate_format, 44100)

    def _parse(self) -> None:
        base = os.path.basename(self.fullpath)
        if not base.lower().endswith('.wav'):
            raise ValueError('Not a wav file')
        hexstring = base[:-4]
        try:
            binstring = bytes.fromhex(hexstring)
        except Exception as e:
            raise ValueError(f'Invalid hex in filename: {base}') from e

        if len(binstring) < 1:
            raise ValueError('Empty header')
        version = binstring[0]
        if version == 1:
            # >HBiQI from [1:20]
            if len(binstring) < 20:
                raise ValueError('Short v1 header')
            import struct
            binID, channels, samples, _hash, offset = struct.unpack('>HBiQI', binstring[1:20])
            name = binstring[20:]
            bytes_per_channel = 2
            bitrate_format = 5
        elif version == 2:
            # >HBiBBQI from [1:22]
            if len(binstring) < 22:
                raise ValueError('Short v2 header')
            import struct
            binID, channels, samples, bytes_per_channel, bitrate_format, _hash, offset = struct.unpack('>HBiBBQI', binstring[1:22])
            name = binstring[22:]
        else:
            raise ValueError(f'Unsupported bin version: {version}')

        self.binID = int(binID)
        self.channels = int(channels)
        self.samples = int(samples)
        self.bitsPerSample = int(bytes_per_channel) * 8
        self.samplerate = self._bitrate_to_samplerate(int(bitrate_format))
        self.hash = int(_hash)
        self.offset = int(offset)
        self.name_bytes = name

    def display_name(self) -> str:
        # Best-effort decoding of embedded name
        if not self.name_bytes:
            return ''
        for enc in ('utf-8', 'utf-16-le', 'latin-1'):
            try:
                return self.name_bytes.decode(enc, errors='ignore')
            except Exception:
                pass
        return ''


def index_bins(bin_dir: str) -> Dict[int, BinHolder]:
    mapping: Dict[int, BinHolder] = {}
    if not os.path.isdir(bin_dir):
        return mapping
    for name in os.listdir(bin_dir):
        full = os.path.join(bin_dir, name)
        if not os.path.isfile(full):
            continue
        if not name.lower().endswith('.wav'):
            continue
        try:
            holder = BinHolder(full)
            mapping[holder.binID] = holder
        except Exception:
            # ignore non-conforming files
            continue
    return mapping


def load_project_plist(project_plist_path: str) -> Dict:
    with open(project_plist_path, 'rb') as f:
        proj = plistlib.load(f)
    # Normalize expected fields with defaults
    tempo = float(proj.get('tempo', 120.0))
    sample_rate = int(proj.get('sampleRate', 44100))
    bit_depth = int(proj.get('bitDepth', 16))
    input_db = float(proj.get('inputVolumeDB', 0.0))
    output_db = float(proj.get('outputVolumeDB', 0.0))
    sig_index = proj.get('timeSignature2', proj.get('timeSignature', 3))
    num, den = signature_for_index(int(sig_index))
    return {
        'tempo': tempo,
        'sampleRate': sample_rate,
        'bitDepth': bit_depth,
        'inputVolumeDB': input_db,
        'outputVolumeDB': output_db,
        'timeSigNum': num,
        'timeSigDen': den,
        'raw': proj,
    }


def find_project_dirs(root_dir: str) -> List[str]:
    """Find directories under root_dir that look like a MultiTrackDAW project.
    Criteria: contains project.plist and (Tracks2.plist or Tracks.plist) and Bins/ directory.
    """
    matches: List[str] = []
    for dirpath, dirnames, filenames in os.walk(root_dir):
        files = set(filenames)
        if 'project.plist' in files and ('Tracks2.plist' in files or 'Tracks.plist' in files):
            if os.path.isdir(os.path.join(dirpath, 'Bins')):
                matches.append(dirpath)
    return matches


def try_import_nska():
    try:
        import nska_deserialize as nd  # type: ignore
        return nd
    except Exception as e:
        sys.exit('Error: nska_deserialize is required to read Tracks2.plist on Windows.\n'
                 'Install with: pip install nska_deserialize\n'
                 f'Details: {e}')


def deserialize_tracks(tracks_plist_path: str) -> object:
    nd = try_import_nska()
    with open(tracks_plist_path, 'rb') as f:
        # full_recurse_convert_nska=True converts NSKeyedArchiver structures into nested dict/list
        return nd.deserialize_plist(f, full_recurse_convert_nska=True, format='dict')


def find_tracks_in_deserialized(obj: object) -> List[dict]:
    """Attempt to locate the array of track objects within the deserialized NSKeyedArchive.

    We search recursively for lists of dicts having keys expected for CacheTrack
    (e.g., 'friendlyName', 'orderNum', 'virtualTrack', 'controlValues').
    """
    tracks: List[dict] = []

    def is_track_dict(d: dict) -> bool:
        keys = set(d.keys())
        expected = {'friendlyName', 'orderNum', 'virtualTrack', 'controlValues'}
        return expected.issubset(keys)

    def rec(node):
        nonlocal tracks
        if isinstance(node, list):
            # If list of track dicts, accept it
            if len(node) > 0 and all(isinstance(x, dict) for x in node):
                if any(is_track_dict(x) for x in node):
                    tracks.extend([x for x in node if is_track_dict(x)])
                    return
            for x in node:
                rec(x)
        elif isinstance(node, dict):
            if is_track_dict(node):
                tracks.append(node)
                return
            for v in node.values():
                rec(v)

    rec(obj)
    return tracks


def scalar_from_int_field(d: dict, key: str, default_scalar: float = 0.0) -> float:
    if key not in d:
        return default_scalar
    try:
        return float(IntVolToFloat(int(d[key])))
    except Exception:
        return default_scalar


def normalize_tracks(deser_root: object) -> List[dict]:
    raw_tracks = find_tracks_in_deserialized(deser_root)
    norm_tracks: List[dict] = []
    for t in raw_tracks:
        name = t.get('friendlyName', 'Track')
        order_num = int(t.get('orderNum', 0))
        muted = bool(t.get('muted', False))
        soloed = bool(t.get('soloed', False))
        cv = t.get('controlValues', {}) if isinstance(t.get('controlValues', {}), dict) else {}
        volume_scalar = scalar_from_int_field(cv, 'vol2', 1.0)
        pan_scalar = scalar_from_int_field(cv, 'pan2', 0.0) * 2.0 - 1.0  # stored as [0,1] -> [-1,1]
        send_a = scalar_from_int_field(cv, 'send2a', 0.0)
        send_b = scalar_from_int_field(cv, 'send2b', 0.0)

        # Regions
        regions: List[dict] = []
        vtrack = t.get('virtualTrack', {})
        if isinstance(vtrack, dict):
            num_regions = int(vtrack.get('numRegions', 0))
            for i in range(num_regions):
                reg = vtrack.get(f'region {i}', {})
                if not isinstance(reg, dict):
                    continue
                region = {
                    'binID': int(reg.get('binID', 0)),
                    'name': reg.get('name', ''),
                    'realStart': int(reg.get('realStart', 0)),
                    'realLength': int(reg.get('realLength', 0)),
                    'binStart': int(reg.get('binStart', 0)),
                    'volumeScalar': float(reg.get('volume', DBtoMM(0.0))),
                    'fadeA': int(reg.get('fadeA', 0)),
                    'fadeB': int(reg.get('fadeB', 0)),
                    'muted': bool(reg.get('muted', False)),
                }
                regions.append(region)

        norm_tracks.append({
            'name': name,
            'orderNum': order_num,
            'muted': muted,
            'soloed': soloed,
            'volumeScalar': volume_scalar,
            'pan': pan_scalar,
            'sendA': send_a,
            'sendB': send_b,
            'regions': regions,
        })

    # sort by orderNum
    norm_tracks.sort(key=lambda x: x.get('orderNum', 0))
    return norm_tracks


def ensure_media_dir(media_dir: str) -> None:
    os.makedirs(media_dir, exist_ok=True)


def plan_media_paths(
    referenced_bins: Dict[int, BinHolder],
    media_dir: str,
    mode: str
) -> Dict[int, str]:
    """Return mapping binID -> dest fullpath in Media dir. Mode is 'move'|'copy'|'keep'."""
    mapping: Dict[int, str] = {}
    if mode == 'keep':
        # Keep original locations
        for bid, holder in referenced_bins.items():
            mapping[bid] = holder.fullpath
        return mapping

    ensure_media_dir(media_dir)
    seen_names = set()
    for bid, holder in referenced_bins.items():
        base_name = os.path.basename(holder.fullpath)
        dest_name = base_name
        # Avoid collisions
        stem, ext = os.path.splitext(base_name)
        idx = 1
        while dest_name.lower() in seen_names:
            dest_name = f"{stem}_{idx}{ext}"
            idx += 1
        seen_names.add(dest_name.lower())
        mapping[bid] = os.path.join(media_dir, dest_name)
    return mapping


def perform_media_transfer(referenced_bins: Dict[int, BinHolder], dest_paths: Dict[int, str], mode: str) -> None:
    if mode == 'keep':
        return
    for bid, holder in referenced_bins.items():
        src = holder.fullpath
        dst = dest_paths[bid]
        if os.path.abspath(src) == os.path.abspath(dst):
            continue
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        # Always copy per new requirement; mode retained for compatibility
        shutil.copy2(src, dst)


def rpp_escape_path(path: str) -> str:
    # RPP wants backslashes on Windows and supports forward slashes; we keep OS native
    # Wrap in quotes, escape backslashes as needed is not required; quotes suffice
    return path.replace('"', '\\"')


def write_rpp(
    out_path: str,
    project_info: Dict,
    tracks: List[dict],
    bin_map: Dict[int, BinHolder],
    media_dest: Dict[int, str]
) -> None:
    sr = project_info['sampleRate']
    tempo = project_info['tempo']
    tsn = project_info['timeSigNum']
    tsd = project_info['timeSigDen']
    master_amp = math.pow(10.0, 0.05 * float(project_info.get('outputVolumeDB', 0.0)))

    lines: List[str] = []
    lines.append(f"<REAPER_PROJECT 0.1 \"7.16/win64\" 0")
    lines.append(f"  RIPPLE 0")
    lines.append(f"  GROUPOVERRIDE 0 0 0")
    lines.append(f"  AUTOXFADE 1")
    lines.append(f"  SAMPLERATE {sr} 0 0")
    # Optional record path; also helps REAPER default media location next to RPP
    lines.append(f"  RECORD_PATH \"Media\" \"\"")
    # Global tempo/time signature marker at start
    lines.append(f"  TEMPO {tempo:.6f} {tsn} {tsd}")

    for t in tracks:
        t_amp = ScalarToAmplitude(float(t['volumeScalar']))
        t_pan = float(t['pan'])
        t_mute = 1 if t.get('muted', False) else 0
        t_solo = 1 if t.get('soloed', False) else 0
        lines.append(f"  <TRACK")
        # NAME
        name = str(t.get('name', 'Track'))
        lines.append(f"    NAME \"{name}\"")
        # MUTE/SOLO
        lines.append(f"    MUTESOLO {t_mute} {t_solo} 0")
        # TRACK VOL/PAN
        lines.append(f"    VOLPAN {t_amp:.12g} {t_pan:.12g} -1 -1 1")

        for r in t.get('regions', []):
            bin_id = int(r['binID'])
            holder = bin_map.get(bin_id)
            if not holder:
                # Skip regions without a matching bin file
                continue
            # Positions in seconds based on project sample rate
            pos_sec = r['realStart'] / float(sr)
            len_sec = r['realLength'] / float(sr)
            fadein_sec = max(0.0, r.get('fadeA', 0) / float(sr))
            fadeout_sec = max(0.0, r.get('fadeB', 0) / float(sr))
            item_amp = ScalarToAmplitude(float(r.get('volumeScalar', DBtoMM(0.0))))
            muted_flag = 1 if r.get('muted', False) else 0

            # Source offset is in samples at the BIN sample rate
            # If bin samplerate differs from project, keep SOFFS in bin seconds
            soff_sec = r['binStart'] / float(holder.samplerate if holder.samplerate else sr)

            lines.append(f"    <ITEM")
            lines.append(f"      POSITION {pos_sec:.12g}")
            lines.append(f"      LENGTH {len_sec:.12g}")
            lines.append(f"      MUTE {muted_flag}")
            # REAPER expects FADEIN/FADEOUT with additional parameters; use defaults
            lines.append(f"      FADEIN {fadein_sec:.12g} 0 0 1 0 0 0")
            lines.append(f"      FADEOUT {fadeout_sec:.12g} 0 0 1 0 0 0")
            lines.append(f"      VOLPAN {item_amp:.12g} 0 -1 -1 1")
            lines.append(f"      SOFFS {soff_sec:.12g}")

            # Media path: make relative to RPP location if inside Media/
            dest_full = media_dest.get(bin_id, holder.fullpath)
            try:
                rel = os.path.relpath(dest_full, start=os.path.dirname(out_path))
            except Exception:
                rel = dest_full
            lines.append(f"      <SOURCE WAVE")
            lines.append(f"        FILE \"{rpp_escape_path(rel)}\"")
            lines.append(f"      >")
            lines.append(f"    >")

        lines.append(f"  >")

    lines.append(f">")

    with open(out_path, 'w', newline='\n', encoding='utf-8') as f:
        f.write("\n".join(lines))


def main():
    parser = argparse.ArgumentParser(description='Convert MultiTrackDAW project (.mtdaw folder or .zip) to REAPER .rpp')
    parser.add_argument('project_path', help='Path to the .mtdaw project folder OR a .zip containing one')
    parser.add_argument('-o', '--output', help='Path to output .rpp file (overrides bundle folder/name logic)')
    parser.add_argument('--output-dir', help='Directory to create the output bundle folder in (default: next to input)')
    parser.add_argument('--bundle-dir-suffix', default='_Reaper', help="Suffix for created output folder (default: '_Reaper')")
    parser.add_argument('--dry-run', action='store_true', help='Do not write files; just print planned actions')
    args = parser.parse_args()

    input_path = os.path.abspath(args.project_path)
    temp_dir: str = ''
    project_roots: List[str] = []
    input_is_zip = False
    if os.path.isdir(input_path):
        project_roots = [input_path]
    elif input_path.lower().endswith('.zip') and os.path.isfile(input_path):
        # Extract to a temporary directory
        temp_dir = tempfile.mkdtemp(prefix='mtdaw_zip_')
        with zipfile.ZipFile(input_path, 'r') as zf:
            zf.extractall(temp_dir)
        # Find all project-like directories in the extracted tree
        project_roots = find_project_dirs(temp_dir)
        if not project_roots:
            sys.exit('No valid MultiTrackDAW project found inside zip')
        input_is_zip = True
    else:
        sys.exit(f'Input is neither a directory nor a .zip: {input_path}')

    # Use first project if multiple found in zip
    song_path = os.path.abspath(project_roots[0])
    base_project_name = os.path.splitext(os.path.basename(song_path))[0]
    base_input_name = os.path.splitext(os.path.basename(input_path))[0] if input_is_zip else base_project_name

    # Prepare output bundle directory: <base> + suffix, sibling to input (or next to zip)
    if args.output:
        out_rpp = os.path.abspath(args.output)
        out_bundle_dir = os.path.dirname(out_rpp)
    else:
        parent_for_bundle = args.output_dir if args.output_dir else os.path.dirname(input_path)
        out_bundle_dir = os.path.join(parent_for_bundle, f'{base_input_name}{args.bundle_dir_suffix}')
        os.makedirs(out_bundle_dir, exist_ok=True)
        out_rpp = os.path.join(out_bundle_dir, f'{base_input_name}.rpp')
    out_rpp = os.path.abspath(out_rpp)

    project_plist = os.path.join(song_path, 'project.plist')
    tracks_plist = os.path.join(song_path, 'Tracks2.plist')
    if not os.path.isfile(tracks_plist):
        tracks_plist = os.path.join(song_path, 'Tracks.plist')
    if not os.path.isfile(project_plist) or not os.path.isfile(tracks_plist):
        sys.exit('Missing project.plist or Tracks2.plist/Tracks.plist in the project folder')

    proj_info = load_project_plist(project_plist)

    deser = deserialize_tracks(tracks_plist)
    tracks = normalize_tracks(deser)

    bins_dir = os.path.join(song_path, 'Bins')
    all_bins = index_bins(bins_dir)
    # Collect only referenced bins
    referenced_ids = {int(r['binID']) for t in tracks for r in t.get('regions', [])}
    referenced_bins = {bid: holder for bid, holder in all_bins.items() if bid in referenced_ids}

    # Plan media locations (always copy by default)
    mode = 'copy'
    media_dir = os.path.join(os.path.dirname(out_rpp), 'Media')
    dest_map = plan_media_paths(referenced_bins, media_dir, mode)

    if args.dry_run:
        print('Would write RPP to:', out_rpp)
        for bid, holder in referenced_bins.items():
            print(f'  Bin {bid}: {holder.fullpath} -> {dest_map[bid]}')
        return

    # Transfer media (copies by default)
    perform_media_transfer(referenced_bins, dest_map, mode)

    # Write RPP
    write_rpp(out_rpp, proj_info, tracks, all_bins, dest_map)
    print('Wrote:', out_rpp)
    if temp_dir:
        try:
            shutil.rmtree(temp_dir, ignore_errors=True)
        except Exception:
            pass


if __name__ == '__main__':
    main()


