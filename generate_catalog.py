#!/usr/bin/env python3
"""
Infinite Dice Tower Catalog Generator
Scans a root folder (containing Volume subfolders) or a single Volume folder
and generates a self-contained HTML catalog viewer.
Usage:
    python3 generate_catalog.py [path_to_root_or_volume_folder]
    python3 generate_catalog.py --serve [path_to_root_or_volume_folder]
"""

import os
import re
import sys
import json
import base64
import glob
import http.server
import socketserver
import webbrowser
import urllib.parse
import time
import tempfile
from pathlib import Path
from io import BytesIO

try:
    from PIL import Image
    HAS_PIL = True
except ImportError:
    HAS_PIL = False
    print("Warning: Pillow not installed. Thumbnails will use full-size images.")

try:
    import yaml
    HAS_YAML = True
except ImportError:
    HAS_YAML = False
    print("Warning: PyYAML not installed (pip install pyyaml). Favorites/tags won't persist to YAML.")
    print("Install with: pip install Pillow")


THUMB_SIZE = 480


def shorten_volume(name):
    """Convert 'Volume 3' -> 'V3', 'Volume 4' -> 'V4', etc."""
    m = re.match(r"volume\s*(\d+)", name, re.I)
    return f"V{m.group(1)}" if m else name


def make_thumbnail_b64(img_path):
    """Create a base64 thumbnail from an image (JPEG output; RGBA converted to RGB)."""
    if HAS_PIL:
        try:
            img = Image.open(img_path)
            # JPEG doesn't support alpha; convert RGBA/PA to RGB (white background)
            if img.mode in ('RGBA', 'LA', 'P'):
                if img.mode == 'P':
                    img = img.convert('RGBA')
                bg = Image.new('RGB', img.size, (255, 255, 255))
                bg.paste(img, mask=img.split()[-1] if img.mode in ('RGBA', 'LA') else None)
                img = bg
            elif img.mode != 'RGB':
                img = img.convert('RGB')
            img.thumbnail((THUMB_SIZE, THUMB_SIZE), Image.LANCZOS)
            buf = BytesIO()
            img.save(buf, format='JPEG', quality=82)
            return base64.b64encode(buf.getvalue()).decode('ascii')
        except Exception as e:
            print(f"  Warning: Could not thumbnail {img_path}: {e}")
    # Fallback: embed full image
    with open(img_path, 'rb') as f:
        return base64.b64encode(f.read()).decode('ascii')


def is_tower_dir(d):
    """A tower directory contains a Master/ subdirectory or STL/JPG files at its level."""
    if not d.is_dir():
        return False
    if any(sub.is_dir() and sub.name.lower().startswith("master") for sub in d.iterdir()):
        return True
    if list(d.glob("*.stl")) or list(d.glob("*.jpg")) or list(d.glob("*.jpeg")):
        return True
    return False


def scan_tower_dir(tower_dir, category, volume_name=None):
    """Scan a single tower directory and return a tower dict."""
    name = tower_dir.name

    # Root-level images only (non-recursive globs). Assume a single main preview at tower root;
    # if multiple exist, the first sorted name is main and the rest appear in the strip.
    image_extensions = ("*.png", "*.jpg", "*.jpeg", "*.webp", "*.gif")
    root_images = []
    for ext in image_extensions:
        root_images.extend(tower_dir.glob(ext))
    root_images = sorted(set(root_images))
    main_path = root_images[0] if root_images else None
    root_screenshots = root_images[1:] if len(root_images) > 1 else []

    # User uploads (persisted extras) live only under user_uploads/
    uploads_dir = tower_dir / "user_uploads"
    upload_images = []
    if uploads_dir.is_dir():
        for ext in image_extensions:
            upload_images.extend(uploads_dir.glob(ext))
    upload_images = sorted(set(upload_images))

    screenshots = root_screenshots + upload_images

    master_candidates = [d for d in tower_dir.iterdir()
                         if d.is_dir() and d.name.lower().startswith("master")]
    master_dir = master_candidates[0] if master_candidates else None
    master_stls = []
    if master_dir:
        dice_tower_sub = None
        has_terrain_sub = False
        unsupported_sub = None
        for sub in master_dir.iterdir():
            if not sub.is_dir():
                continue
            normalized = sub.name.lower().replace('_', ' ')
            if 'dice tower' in normalized:
                dice_tower_sub = sub
            elif 'terrain' in normalized:
                has_terrain_sub = True
            elif 'unsupported' in normalized:
                unsupported_sub = sub

        if dice_tower_sub and has_terrain_sub:
            master_stls = sorted(dice_tower_sub.rglob("*.stl"))
        elif unsupported_sub:
            master_stls = sorted(unsupported_sub.rglob("*.stl"))
        else:
            master_stls = sorted(master_dir.rglob("*.stl"))

    threemf_files = list(tower_dir.glob("*MultiColor*.3mf")) + list(tower_dir.glob("*.3mf"))
    has_multicolor = len(threemf_files) > 0
    threemf_path = threemf_files[0] if threemf_files else None

    stl_info = []
    for stl in master_stls:
        size_mb = stl.stat().st_size / (1024 * 1024)
        stl_info.append({
            'name': stl.name,
            'path': str(stl),
            'size_mb': round(size_mb, 1)
        })

    return {
        'name': name,
        'category': category,
        'volume': volume_name,
        'folder': str(tower_dir),
        'jpg_path': str(main_path) if main_path else None,
        'has_multicolor': has_multicolor,
        'threemf_path': str(threemf_path) if threemf_path else None,
        'master_stls': stl_info,
        'screenshots': [str(s) for s in screenshots],
    }


def scan_volume(volume_path, category_prefix=""):
    """Scan a single volume folder and return a list of tower dicts."""
    towers = []
    volume_path = Path(volume_path)
    volume_name = shorten_volume(volume_path.name)

    def cat(label):
        return f"{category_prefix}{label}" if category_prefix else label

    # Scan Core Set — handle both nesting patterns:
    #   Volume 4 style: Core Set/Core Set/<towers>
    #   Volume 3 style: Core Set/<towers>
    core_path = volume_path / "Core Set"
    if core_path.exists() and core_path.is_dir():
        nested_core = core_path / "Core Set"
        if nested_core.exists() and nested_core.is_dir():
            for d in sorted(nested_core.iterdir()):
                if is_tower_dir(d):
                    towers.append(scan_tower_dir(d, cat("Core Set"), volume_name))
        else:
            for d in sorted(core_path.iterdir()):
                if is_tower_dir(d):
                    towers.append(scan_tower_dir(d, cat("Core Set"), volume_name))

    # Scan Stretch Goals
    for sg_folder in sorted(volume_path.glob("Stretch Goals*")):
        if sg_folder.is_dir():
            for goal_dir in sorted(sg_folder.iterdir()):
                if goal_dir.is_dir() and goal_dir.name.startswith("Goal_"):
                    goal_num = goal_dir.name.replace("Goal_", "")
                    for tower_dir in sorted(goal_dir.iterdir()):
                        if is_tower_dir(tower_dir):
                            towers.append(scan_tower_dir(
                                tower_dir, cat(f"Stretch Goal {goal_num}"), volume_name
                            ))

    return towers


def scan_all(root_path):
    """Scan a root folder containing Volume subdirectories, or a single volume."""
    root_path = Path(root_path)

    volume_dirs = sorted([
        d for d in root_path.iterdir()
        if d.is_dir() and d.name.lower().startswith("volume")
    ])

    if volume_dirs:
        all_towers = []
        for vol_dir in volume_dirs:
            print(f"\nScanning {vol_dir.name}...")
            prefix = f"{shorten_volume(vol_dir.name)} "
            all_towers.extend(scan_volume(vol_dir, category_prefix=prefix))
        return all_towers, True
    else:
        return scan_volume(root_path), False


def _user_data_path():
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), 'user_data.yaml')


def load_user_data():
    """Load favorites and tags from user_data.yaml."""
    path = _user_data_path()
    if not HAS_YAML or not os.path.isfile(path):
        return {'favorites': [], 'tags': {}}
    try:
        with open(path, 'r') as f:
            data = yaml.safe_load(f) or {}
        return {
            'favorites': data.get('favorites', []),
            'tags': data.get('tags', {}),
        }
    except Exception:
        return {'favorites': [], 'tags': {}}


def save_user_data(favorites, tags):
    """Atomically write favorites and tags to user_data.yaml."""
    if not HAS_YAML:
        return
    path = _user_data_path()
    sorted_tags = {k: sorted(v) for k, v in sorted(tags.items())} if tags else {}
    data = {'favorites': sorted(favorites), 'tags': sorted_tags}
    fd, tmp_path = tempfile.mkstemp(dir=os.path.dirname(path), suffix='.tmp')
    try:
        with os.fdopen(fd, 'w') as f:
            f.write("# Infinite Dice Towers - User Data\n")
            f.write("# Favorites and tags tracked here.\n\n")
            yaml.dump(data, f, default_flow_style=False, allow_unicode=True, sort_keys=False)
        os.replace(tmp_path, path)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def generate_html(towers, root_path, output_path, is_multi_volume=False, verbose=True):
    """Generate the self-contained HTML catalog."""
    catalog_name = Path(root_path).name or "Catalog"
    if verbose:
        print(f"Generating catalog for {len(towers)} towers...")

    # Build thumbnail data
    tower_data = []
    for i, t in enumerate(towers):
        if verbose:
            print(f"  [{i+1}/{len(towers)}] {t['name']}")
        thumb_b64 = ""
        if t['jpg_path'] and os.path.exists(t['jpg_path']):
            thumb_b64 = make_thumbnail_b64(t['jpg_path'])

        screenshot_b64s = []
        for sp in t.get('screenshots', []):
            if os.path.exists(sp):
                screenshot_b64s.append(make_thumbnail_b64(sp))

        tower_data.append({
            'name': t['name'],
            'category': t['category'],
            'volume': t.get('volume', ''),
            'has_multicolor': t['has_multicolor'],
            'thumb': thumb_b64,
            'screenshots': screenshot_b64s,
            'master_stls': t['master_stls'],
            'folder': t['folder'],
        })

    tower_json = json.dumps(tower_data)

    user_data = load_user_data()
    favorites_json = json.dumps(user_data['favorites'])
    tags_json = json.dumps(user_data['tags'])

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Infinite Dice Towers — {catalog_name} Catalog</title>
<style>
@import url('https://fonts.googleapis.com/css2?family=Cinzel:wght@400;700;900&family=Inter:wght@300;400;500;600&display=swap');

:root {{
    --bg-deep: #0a0a0f;
    --bg-card: #14141f;
    --bg-card-hover: #1a1a2e;
    --border: #2a2a3e;
    --border-glow: #6c5ce7;
    --accent: #a29bfe;
    --accent-bright: #6c5ce7;
    --gold: #f0c040;
    --gold-dim: #a08020;
    --multicolor-1: #ff6b6b;
    --multicolor-2: #48dbfb;
    --multicolor-3: #feca57;
    --text: #e0e0f0;
    --text-dim: #8888aa;
    --text-bright: #ffffff;
    --success: #00d2d3;
}}

* {{ margin: 0; padding: 0; box-sizing: border-box; }}

body {{
    background: var(--bg-deep);
    color: var(--text);
    font-family: 'Inter', system-ui, sans-serif;
    min-height: 100vh;
}}

/* ===== HEADER ===== */
.header {{
    text-align: center;
    padding: 3rem 1rem 2rem;
    background: linear-gradient(180deg, #12121f 0%, var(--bg-deep) 100%);
    border-bottom: 1px solid var(--border);
    position: relative;
    overflow: hidden;
}}
.header::before {{
    content: '';
    position: absolute;
    top: 0; left: 50%; transform: translateX(-50%);
    width: 600px; height: 600px;
    background: radial-gradient(circle, rgba(108,92,231,0.08) 0%, transparent 70%);
    pointer-events: none;
}}
.header h1 {{
    font-family: 'Cinzel', serif;
    font-weight: 900;
    font-size: 2.4rem;
    letter-spacing: 0.05em;
    background: linear-gradient(135deg, var(--gold) 0%, #ffe08a 50%, var(--gold-dim) 100%);
    -webkit-background-clip: text;
    -webkit-text-fill-color: transparent;
    background-clip: text;
    margin-bottom: 0.3rem;
}}
.header .subtitle {{
    font-size: 0.95rem;
    color: var(--text-dim);
    letter-spacing: 0.15em;
    text-transform: uppercase;
}}
.header .stats {{
    margin-top: 1.2rem;
    display: flex;
    justify-content: center;
    gap: 2rem;
    flex-wrap: wrap;
}}
.header .stat {{
    text-align: center;
}}
.header .stat .num {{
    font-family: 'Cinzel', serif;
    font-size: 1.8rem;
    font-weight: 700;
    color: var(--accent);
}}
.header .stat .label {{
    font-size: 0.75rem;
    color: var(--text-dim);
    text-transform: uppercase;
    letter-spacing: 0.1em;
}}

/* ===== TOOLBAR ===== */
.toolbar {{
    position: sticky;
    top: 0;
    z-index: 100;
    background: rgba(10,10,15,0.95);
    backdrop-filter: blur(12px);
    border-bottom: 1px solid var(--border);
    padding: 8px 16px;
    display: flex;
    align-items: center;
    gap: 8px;
    flex-wrap: wrap;
}}
.filter-toggle-btn {{
    display: flex;
    align-items: center;
    gap: 6px;
    padding: 7px 14px;
    background: var(--bg-card);
    border: 1px solid var(--border);
    border-radius: 8px;
    color: var(--text);
    font-size: 0.8rem;
    cursor: pointer;
    flex-shrink: 0;
    font-weight: 500;
    font-family: 'Inter', sans-serif;
    transition: all 0.2s;
}}
.filter-toggle-btn:hover {{
    border-color: var(--border-glow);
    background: rgba(108,92,231,0.1);
}}
.filter-badge {{
    background: var(--accent-bright);
    color: var(--text-bright);
    padding: 1px 7px;
    border-radius: 10px;
    font-size: 0.68rem;
    font-weight: 600;
    min-width: 16px;
    text-align: center;
}}
.global-gallery-nav {{
    display: flex;
    flex-shrink: 0;
    gap: 0.3rem;
    align-items: center;
}}
.global-gallery-btn {{
    width: 2.35rem;
    height: 2.35rem;
    padding: 0;
    border: 1px solid var(--border);
    border-radius: 8px;
    background: var(--bg-card);
    color: var(--text-dim);
    font-size: 1.15rem;
    line-height: 1;
    cursor: pointer;
    display: flex;
    align-items: center;
    justify-content: center;
    transition: border-color 0.2s, color 0.2s, background 0.2s;
}}
.global-gallery-btn:hover {{
    border-color: var(--accent);
    color: var(--text);
    background: rgba(108,92,231,0.12);
}}
.global-gallery-btn#global-gallery-reset {{
    font-size: 1.05rem;
}}
.search-box {{
    flex: 1;
    min-width: 180px;
    position: relative;
}}
.search-box input {{
    width: 100%;
    padding: 0.6rem 1rem 0.6rem 2.4rem;
    background: var(--bg-card);
    border: 1px solid var(--border);
    border-radius: 8px;
    color: var(--text);
    font-size: 0.9rem;
    outline: none;
    transition: border-color 0.2s;
}}
.search-box input:focus {{ border-color: var(--accent); }}
.search-box::before {{
    content: '\\1F50D';
    position: absolute;
    left: 0.8rem;
    top: 50%;
    transform: translateY(-50%);
    font-size: 0.85rem;
    opacity: 0.5;
}}
/* ===== SIDEBAR ===== */
.content-wrap {{
    display: flex;
    min-height: calc(100vh - 180px);
}}
.sidebar {{
    width: 256px;
    flex-shrink: 0;
    background: #0c0c18;
    border-right: 1px solid var(--border);
    overflow-y: auto;
    max-height: calc(100vh - 130px);
    position: sticky;
    top: 52px;
}}
.sidebar-section {{
    padding: 14px 16px 12px;
    border-bottom: 1px solid #1e1e30;
}}
.sidebar-section:last-child {{
    border-bottom: none;
    padding-bottom: 16px;
}}
.sidebar-section-header {{
    display: flex;
    align-items: center;
    justify-content: space-between;
    margin-bottom: 10px;
}}
.sidebar-label {{
    font-size: 0.65rem;
    font-weight: 600;
    color: #8888aa;
    text-transform: uppercase;
    letter-spacing: 0.1em;
}}
.sidebar-clear {{
    font-size: 0.65rem;
    color: var(--accent-bright);
    cursor: pointer;
    transition: color 0.2s;
    background: none;
    border: none;
    font-family: 'Inter', sans-serif;
    padding: 0;
}}
.sidebar-clear:hover {{ color: var(--accent); }}
.cb-row {{
    display: flex;
    align-items: center;
    gap: 8px;
    padding: 5px 8px;
    border-radius: 6px;
    cursor: pointer;
    transition: background 0.15s;
}}
.cb-row:hover {{ background: rgba(255,255,255,0.03); }}
.cb-box {{
    width: 15px;
    height: 15px;
    border-radius: 4px;
    border: 1.5px solid #3a3a50;
    background: transparent;
    display: flex;
    align-items: center;
    justify-content: center;
    font-size: 9px;
    transition: all 0.15s;
    flex-shrink: 0;
    color: transparent;
}}
.cb-box.checked-gold {{ border-color: var(--gold); background: var(--gold); color: #000; }}
.cb-box.checked-teal {{ border-color: var(--success); background: var(--success); color: #000; }}
.cb-box.checked-yellow {{ border-color: #feca57; background: #feca57; color: #000; }}
.cb-label {{ font-size: 0.8rem; color: var(--text); flex: 1; }}
.cb-count {{ font-size: 0.72rem; color: #666680; }}
.type-toggle {{
    display: flex;
    background: var(--bg-card);
    border: 1px solid var(--border);
    border-radius: 8px;
    overflow: hidden;
}}
.type-seg {{
    flex: 1;
    padding: 7px 0;
    text-align: center;
    font-size: 0.72rem;
    cursor: pointer;
    color: #8888aa;
    background: transparent;
    border: none;
    border-left: 1px solid var(--border);
    font-family: 'Inter', sans-serif;
    transition: all 0.2s;
}}
.type-seg:first-child {{ border-left: none; }}
.type-seg.active {{ background: var(--accent-bright); color: var(--text-bright); font-weight: 500; }}

/* ===== FILTER CHIPS ===== */
.filter-chips {{
    display: flex;
    flex-wrap: wrap;
    gap: 5px;
    align-items: center;
}}
.filter-chip {{
    display: flex;
    align-items: center;
    gap: 5px;
    padding: 4px 10px;
    border-radius: 16px;
    font-size: 0.72rem;
    white-space: nowrap;
    font-family: 'Inter', sans-serif;
}}
.filter-chip-x {{
    opacity: 0.6;
    cursor: pointer;
    font-size: 0.8rem;
    line-height: 1;
}}
.filter-chip-x:hover {{ opacity: 1; }}
.chip-volume {{ color: #f0c040; background: rgba(240,192,64,0.15); border: 1px solid rgba(240,192,64,0.3); }}
.chip-type {{ color: #a29bfe; background: rgba(162,155,254,0.15); border: 1px solid rgba(162,155,254,0.3); }}
.chip-multicolor {{ color: #feca57; background: rgba(254,202,87,0.15); border: 1px solid rgba(254,202,87,0.3); }}
.chip-favorites {{ color: #f0c040; background: rgba(240,192,64,0.15); border: 1px solid rgba(240,192,64,0.3); }}
.chip-tag {{ color: #00d2d3; background: rgba(0,210,211,0.15); border: 1px solid rgba(0,210,211,0.3); }}
.star-btn {{
    width: 2rem;
    height: 2rem;
    border-radius: 50%;
    border: none;
    background: rgba(10,10,15,0.72);
    color: var(--text-dim);
    font-size: 1.1rem;
    cursor: pointer;
    display: flex;
    align-items: center;
    justify-content: center;
    transition: all 0.2s;
    line-height: 1;
    padding: 0;
    flex-shrink: 0;
}}
.star-btn:hover {{ background: rgba(10,10,15,0.92); color: var(--gold); }}
.star-btn.starred {{ color: var(--gold); }}
.modal-star-btn {{
    background: none;
    border: none;
    color: var(--text-dim);
    font-size: 1.3rem;
    cursor: pointer;
    padding: 0 0.4rem;
    transition: all 0.2s;
    line-height: 1;
}}
.modal-star-btn:hover {{ color: var(--gold); }}
.modal-star-btn.starred {{ color: var(--gold); }}
/* ===== GRID ===== */
.grid {{
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(280px, 1fr));
    gap: 16px;
    padding: 16px;
    flex: 1;
    min-width: 0;
    align-content: start;
}}

/* ===== CARD ===== */
.card {{
    background: var(--bg-card);
    border: 1px solid var(--border);
    border-radius: 12px;
    overflow: hidden;
    transition: all 0.3s ease;
    cursor: default;
    position: relative;
}}
.card:hover {{
    border-color: var(--border-glow);
    transform: translateY(-4px);
    box-shadow: 0 12px 40px rgba(108,92,231,0.15);
}}
.card-img-wrap {{
    position: relative;
    width: 100%;
    aspect-ratio: 1;
    overflow: hidden;
    background: #0d0d18;
    cursor: pointer;
}}
.card-img-wrap img {{
    width: 100%;
    height: 100%;
    object-fit: cover;
    transition: transform 0.4s ease;
}}
.card:hover .card-img-wrap img {{ transform: scale(1.05); }}
.card-img-nav {{
    position: absolute;
    top: 50%;
    transform: translateY(-50%);
    z-index: 2;
    width: 36px;
    height: 44px;
    padding: 0;
    border: none;
    border-radius: 8px;
    background: rgba(255, 255, 255, 0.14);
    color: rgba(255, 255, 255, 0.92);
    font-size: 1.5rem;
    line-height: 1;
    cursor: pointer;
    display: flex;
    align-items: center;
    justify-content: center;
    backdrop-filter: blur(6px);
    -webkit-backdrop-filter: blur(6px);
    transition: background 0.2s, color 0.2s;
}}
.card-img-nav:hover {{
    background: rgba(255, 255, 255, 0.28);
    color: #fff;
}}
.card-img-nav-prev {{ left: 8px; }}
.card-img-nav-next {{ right: 8px; }}
.card-badges {{
    display: flex;
    gap: 0.35rem;
    flex-wrap: wrap;
    margin-top: 0.45rem;
}}
.badge {{
    padding: 0.2rem 0.55rem;
    border-radius: 5px;
    font-size: 0.62rem;
    font-weight: 500;
    text-transform: uppercase;
    letter-spacing: 0.04em;
}}
.badge-category {{
    background: rgba(108,92,231,0.2);
    color: var(--accent);
    border: 1px solid rgba(108,92,231,0.3);
}}
.badge-multicolor {{
    background: linear-gradient(135deg, rgba(255,107,107,0.2), rgba(72,219,251,0.2), rgba(254,202,87,0.2));
    color: var(--gold);
    border: 1px solid rgba(254,202,87,0.3);
    font-weight: 600;
}}
.card-body {{
    padding: 0.9rem 1rem;
}}
.card-name {{
    font-family: 'Cinzel', serif;
    font-weight: 700;
    font-size: 0.95rem;
    color: var(--text-bright);
    margin-bottom: 0.4rem;
    line-height: 1.3;
}}
.card-meta {{
    font-size: 0.75rem;
    color: var(--text-dim);
}}
.card-actions {{
    padding: 0 1rem 0.9rem;
    display: flex;
    gap: 0.5rem;
    flex-wrap: wrap;
    align-items: center;
}}
.card-actions .star-btn {{
    margin-left: auto;
}}
.btn {{
    padding: 0.4rem 0.8rem;
    border-radius: 6px;
    border: 1px solid var(--border);
    background: transparent;
    color: var(--text-dim);
    font-size: 0.72rem;
    cursor: pointer;
    transition: all 0.2s;
    font-family: 'Inter', sans-serif;
    display: flex;
    align-items: center;
    gap: 0.35rem;
}}
.btn:hover {{
    border-color: var(--accent);
    color: var(--text);
    background: rgba(108,92,231,0.1);
}}
.btn-3d {{
    border-color: var(--success);
    color: var(--success);
}}
.btn-3d:hover {{
    background: rgba(0,210,211,0.1);
    border-color: var(--success);
}}

/* ===== 3D VIEWER MODAL ===== */
.modal-overlay {{
    display: none;
    position: fixed;
    inset: 0;
    background: rgba(0,0,0,0.85);
    z-index: 1000;
    align-items: center;
    justify-content: center;
    backdrop-filter: blur(4px);
}}
.modal-overlay.active {{ display: flex; }}
.modal {{
    background: var(--bg-card);
    border: 1px solid var(--border);
    border-radius: 16px;
    width: 90vw;
    max-width: 800px;
    max-height: 90vh;
    overflow: hidden;
    position: relative;
}}
.modal-header {{
    display: flex;
    justify-content: space-between;
    align-items: center;
    padding: 1rem 1.2rem;
    border-bottom: 1px solid var(--border);
}}
.modal-header h2 {{
    font-family: 'Cinzel', serif;
    font-size: 1.1rem;
    color: var(--gold);
}}
.modal-close {{
    background: none;
    border: none;
    color: var(--text-dim);
    font-size: 1.5rem;
    cursor: pointer;
    padding: 0.2rem 0.5rem;
    border-radius: 6px;
    transition: all 0.2s;
}}
.modal-close:hover {{ color: var(--text); background: rgba(255,255,255,0.05); }}
.modal-body {{
    height: 65vh;
    position: relative;
}}
.modal-body canvas {{ width: 100% !important; height: 100% !important; display: block; }}
.modal-controls {{
    position: absolute;
    bottom: 1rem;
    left: 50%;
    transform: translateX(-50%);
    display: flex;
    align-items: center;
    gap: 1rem;
    background: rgba(0,0,0,0.7);
    padding: 0.4rem 1rem;
    border-radius: 20px;
    font-size: 0.75rem;
    opacity: 0.9;
}}
.modal-hint {{
    color: var(--text-dim);
    pointer-events: none;
}}
.modal-auto-rotate {{
    padding: 0.25rem 0.6rem;
    border-radius: 12px;
    border: 1px solid var(--border);
    background: transparent;
    color: var(--text-dim);
    font-size: 0.72rem;
    cursor: pointer;
    transition: all 0.2s;
    font-family: 'Inter', sans-serif;
}}
.modal-auto-rotate:hover {{
    border-color: var(--accent);
    color: var(--text);
}}
.modal-auto-rotate.active {{
    border-color: var(--success);
    color: var(--success);
}}
.modal-loading {{
    position: absolute;
    inset: 0;
    display: flex;
    align-items: center;
    justify-content: center;
    font-size: 0.9rem;
    color: var(--text-dim);
}}
.spinner {{
    width: 30px;
    height: 30px;
    border: 3px solid var(--border);
    border-top-color: var(--accent);
    border-radius: 50%;
    animation: spin 0.8s linear infinite;
    margin-right: 0.8rem;
}}
@keyframes spin {{ to {{ transform: rotate(360deg); }} }}

/* ===== 3D VIEWER SETTINGS PANEL ===== */
.modal-settings-btn {{
    padding: 0.25rem 0.6rem;
    border-radius: 12px;
    border: 1px solid var(--border);
    background: transparent;
    color: var(--text-dim);
    font-size: 0.72rem;
    cursor: pointer;
    transition: all 0.2s;
    font-family: 'Inter', sans-serif;
}}
.modal-settings-btn:hover {{
    border-color: var(--accent);
    color: var(--text);
}}
.modal-settings-btn.active {{
    border-color: var(--accent);
    color: var(--accent);
}}
.settings-panel {{
    position: absolute;
    bottom: 3.5rem;
    right: 1rem;
    background: rgba(10, 10, 18, 0.92);
    border: 1px solid var(--border);
    border-radius: 12px;
    padding: 1rem 1.2rem;
    width: 260px;
    display: none;
    backdrop-filter: blur(12px);
    z-index: 10;
    max-height: 55vh;
    overflow-y: auto;
}}
.settings-panel.open {{ display: block; }}
.settings-panel h3 {{
    font-family: 'Inter', sans-serif;
    font-size: 0.7rem;
    text-transform: uppercase;
    letter-spacing: 0.08em;
    color: var(--text-dim);
    margin: 0 0 0.6rem 0;
}}
.settings-panel h3:not(:first-child) {{
    margin-top: 0.8rem;
    padding-top: 0.8rem;
    border-top: 1px solid var(--border);
}}
.color-swatches {{
    display: flex;
    gap: 0.4rem;
    flex-wrap: wrap;
    margin-bottom: 0.3rem;
}}
.color-swatch {{
    width: 24px;
    height: 24px;
    border-radius: 50%;
    border: 2px solid transparent;
    cursor: pointer;
    transition: all 0.15s;
    box-shadow: 0 1px 3px rgba(0,0,0,0.4);
}}
.color-swatch:hover {{
    transform: scale(1.15);
}}
.color-swatch.active {{
    border-color: #fff;
    box-shadow: 0 0 0 2px var(--accent), 0 1px 4px rgba(0,0,0,0.5);
}}
.setting-row {{
    display: flex;
    align-items: center;
    justify-content: space-between;
    margin: 0.35rem 0;
}}
.setting-row label {{
    font-size: 0.72rem;
    color: var(--text-dim);
    min-width: 70px;
}}
.setting-row input[type="range"] {{
    flex: 1;
    margin: 0 0.5rem;
    accent-color: var(--accent);
    height: 4px;
    cursor: pointer;
}}
.setting-row .setting-val {{
    font-size: 0.65rem;
    color: var(--text-dim);
    min-width: 28px;
    text-align: right;
    font-family: monospace;
}}

/* ===== TAGS ===== */
.badge-tag {{
    background: rgba(0,210,211,0.15);
    color: var(--success);
    border: 1px solid rgba(0,210,211,0.3);
    cursor: default;
    position: relative;
}}
.badge-tag .tag-remove {{
    display: none;
    margin-left: 0.25rem;
    cursor: pointer;
    font-weight: 700;
    opacity: 0.6;
}}
.badge-tag:hover .tag-remove {{ display: inline; }}
.badge-tag .tag-remove:hover {{ opacity: 1; }}
.tag-input-wrap {{
    display: inline-flex;
    align-items: center;
    gap: 0.2rem;
    margin-top: 0.25rem;
}}
.tag-add-btn {{
    padding: 0.15rem 0.45rem;
    border-radius: 5px;
    font-size: 0.62rem;
    font-weight: 500;
    background: transparent;
    color: var(--text-dim);
    border: 1px dashed var(--border);
    cursor: pointer;
    transition: all 0.2s;
}}
.tag-add-btn:hover {{
    border-color: var(--success);
    color: var(--success);
}}
.tag-input {{
    width: 80px;
    padding: 0.15rem 0.35rem;
    border-radius: 5px;
    font-size: 0.62rem;
    background: var(--bg-deep);
    color: var(--text);
    border: 1px solid var(--success);
    outline: none;
    font-family: 'Inter', sans-serif;
}}

/* ===== GRADIENT SWATCHES ===== */
.gradient-swatches {{
    display: flex;
    gap: 0.4rem;
    flex-wrap: wrap;
    margin-bottom: 0.3rem;
}}
.gradient-swatch {{
    width: 24px;
    height: 24px;
    border-radius: 50%;
    border: 2px solid transparent;
    cursor: pointer;
    transition: all 0.15s;
    box-shadow: 0 1px 3px rgba(0,0,0,0.4);
}}
.gradient-swatch:hover {{ transform: scale(1.15); }}
.gradient-swatch.active {{
    border-color: #fff;
    box-shadow: 0 0 0 2px var(--accent), 0 1px 4px rgba(0,0,0,0.5);
}}
.gradient-swatch.none-swatch {{
    background: var(--bg-deep);
    display: flex;
    align-items: center;
    justify-content: center;
    font-size: 0.55rem;
    color: var(--text-dim);
}}

/* ===== EMPTY STATE ===== */
.empty-state {{
    grid-column: 1 / -1;
    text-align: center;
    padding: 4rem 1rem;
    color: var(--text-dim);
}}
.empty-state .icon {{ font-size: 3rem; margin-bottom: 1rem; }}

/* ===== SCREENSHOTS ===== */
.screenshot-section {{
    margin: 0.5rem 1rem 0.2rem;
}}
.screenshot-strip {{
    display: flex;
    gap: 0.5rem;
    overflow-x: auto;
    overflow-y: hidden;
    padding: 0.3rem 0;
    scrollbar-gutter: stable;
}}
.screenshot-strip::-webkit-scrollbar {{
    height: 6px;
}}
.screenshot-strip .thumb {{
    flex-shrink: 0;
    width: 80px;
    height: 80px;
    border-radius: 6px;
    overflow: hidden;
    cursor: pointer;
    border: 2px solid var(--border);
    transition: border-color 0.2s;
}}
.screenshot-strip .thumb:hover {{
    border-color: var(--accent);
}}
.screenshot-strip .thumb img {{
    width: 100%;
    height: 100%;
    object-fit: cover;
}}
.screenshot-upload {{
    margin-top: 0.5rem;
    border: 2px dashed var(--border);
    border-radius: 8px;
    padding: 0.5rem;
    text-align: center;
    font-size: 0.7rem;
    color: var(--text-dim);
    cursor: pointer;
    transition: all 0.2s;
}}
.screenshot-upload:hover {{
    border-color: var(--accent);
    color: var(--text);
}}
/* ===== IMAGE LIGHTBOX ===== */
.lightbox-overlay {{
    display: none;
    position: fixed;
    inset: 0;
    background: rgba(0,0,0,0.9);
    z-index: 2000;
    align-items: center;
    justify-content: center;
}}
.lightbox-overlay.active {{ display: flex; }}
.lightbox-overlay img {{
    max-width: 95vw;
    max-height: 95vh;
    object-fit: contain;
    border-radius: 8px;
}}
.lightbox-overlay .lightbox-close {{
    position: absolute;
    top: 1rem;
    right: 1rem;
    background: var(--bg-card);
    border: 1px solid var(--border);
    color: var(--text);
    font-size: 1.5rem;
    width: 40px;
    height: 40px;
    border-radius: 8px;
    cursor: pointer;
}}

/* ===== SCROLLBAR ===== */
::-webkit-scrollbar {{ width: 8px; }}
::-webkit-scrollbar-track {{ background: var(--bg-deep); }}
::-webkit-scrollbar-thumb {{ background: var(--border); border-radius: 4px; }}
::-webkit-scrollbar-thumb:hover {{ background: var(--text-dim); }}

/* ===== RESPONSIVE ===== */
@media (max-width: 768px) {{
    .sidebar {{
        position: fixed;
        top: 52px;
        left: 0;
        bottom: 0;
        z-index: 99;
        max-height: none;
    }}
    .sidebar-backdrop {{
        position: fixed;
        inset: 0;
        background: rgba(0,0,0,0.5);
        z-index: 98;
    }}
}}
@media (max-width: 600px) {{
    .header h1 {{ font-size: 1.6rem; }}
    .grid {{ grid-template-columns: repeat(auto-fill, minmax(220px, 1fr)); gap: 0.8rem; padding: 0.8rem; }}
}}
</style>
</head>
<body>

<div class="header">
    <h1>Infinite Dice Towers</h1>
    <div class="subtitle">{catalog_name} &mdash; Collection Catalog</div>
    <div class="stats">
        <div class="stat" id="volume-stat" style="display:none"><div class="num" id="volume-count">0</div><div class="label">Volumes</div></div>
        <div class="stat"><div class="num" id="total-count">0</div><div class="label">Towers</div></div>
        <div class="stat"><div class="num" id="multicolor-count">0</div><div class="label">Multicolor</div></div>
        <div class="stat"><div class="num" id="visible-count">0</div><div class="label">Showing</div></div>
    </div>
</div>

<div class="toolbar">
    <button type="button" class="filter-toggle-btn" id="filter-toggle">
        <span style="font-size:1rem;line-height:1">&#9776;</span>
        <span>Filters</span>
        <span class="filter-badge" id="filter-badge" style="display:none">0</span>
    </button>
    <div class="search-box">
        <input type="text" id="search" placeholder="Search towers by name..." autocomplete="off">
    </div>
    <div class="filter-chips" id="filter-chips"></div>
    <div class="global-gallery-nav" role="toolbar" aria-label="Cycle preview image on all towers">
        <button type="button" class="global-gallery-btn" id="global-gallery-prev" title="Previous image on all towers" aria-label="Previous image on all towers">&#8249;</button>
        <button type="button" class="global-gallery-btn" id="global-gallery-reset" title="Reset all tower previews to first image" aria-label="Reset all tower previews">&#8634;</button>
        <button type="button" class="global-gallery-btn" id="global-gallery-next" title="Next image on all towers" aria-label="Next image on all towers">&#8250;</button>
    </div>
</div>

<div class="content-wrap">
    <div class="sidebar" id="sidebar">
        <div class="sidebar-section" style="padding-top:16px">
            <div class="sidebar-section-header">
                <span class="sidebar-label">Quick Filters</span>
            </div>
            <div id="quick-filters"></div>
        </div>
        <div class="sidebar-section">
            <div class="sidebar-section-header">
                <span class="sidebar-label">Tags</span>
                <button class="sidebar-clear" id="clear-tags" style="display:none">Clear</button>
            </div>
            <div id="tag-list"></div>
        </div>
        <div class="sidebar-section">
            <div class="sidebar-section-header">
                <span class="sidebar-label">Type</span>
            </div>
            <div class="type-toggle" id="type-toggle">
                <button type="button" class="type-seg active" data-type="all">All</button>
                <button type="button" class="type-seg" data-type="core">Core</button>
                <button type="button" class="type-seg" data-type="stretch">Stretch</button>
            </div>
        </div>
        <div class="sidebar-section">
            <div class="sidebar-section-header">
                <span class="sidebar-label">Volumes</span>
                <button class="sidebar-clear" id="clear-volumes" style="display:none">Clear</button>
            </div>
            <div id="volume-list"></div>
        </div>
    </div>
    <div class="grid" id="grid"></div>
</div>

<!-- Image Lightbox -->
<div class="lightbox-overlay" id="lightbox">
    <button class="lightbox-close" id="lightbox-close">&times;</button>
    <img id="lightbox-img" src="" alt="Enlarged view">
</div>

<!-- 3D Viewer Modal -->
<div class="modal-overlay" id="modal">
    <div class="modal">
        <div class="modal-header">
            <h2 id="modal-title">Loading...</h2>
            <button class="modal-star-btn" id="modal-star-btn" title="Add to favorites">&#x2605;</button>
            <span style="flex:1"></span>
            <button class="modal-close" id="modal-close">&times;</button>
        </div>
        <div class="modal-body" id="modal-body">
            <div class="modal-loading" id="modal-loading">
                <div class="spinner"></div>
                Loading 3D model...
            </div>
            <div class="settings-panel" id="settings-panel">
                <h3>Model Color</h3>
                <div class="color-swatches" id="color-swatches"></div>
                <h3>Filament Gradient</h3>
                <div class="gradient-swatches" id="gradient-swatches"></div>
                <h3>Material</h3>
                <div class="setting-row">
                    <label>Metalness</label>
                    <input type="range" id="sl-metalness" min="0" max="100" value="15">
                    <span class="setting-val" id="sv-metalness">0.15</span>
                </div>
                <div class="setting-row">
                    <label>Roughness</label>
                    <input type="range" id="sl-roughness" min="0" max="100" value="45">
                    <span class="setting-val" id="sv-roughness">0.45</span>
                </div>
                <div class="setting-row">
                    <label>Clearcoat</label>
                    <input type="range" id="sl-clearcoat" min="0" max="100" value="30">
                    <span class="setting-val" id="sv-clearcoat">0.30</span>
                </div>
                <div class="setting-row">
                    <label>Coat Rough</label>
                    <input type="range" id="sl-clearcoatRoughness" min="0" max="100" value="40">
                    <span class="setting-val" id="sv-clearcoatRoughness">0.40</span>
                </div>
                <h3>Lighting</h3>
                <div class="setting-row">
                    <label>Ambient</label>
                    <input type="range" id="sl-ambient" min="0" max="100" value="50">
                    <span class="setting-val" id="sv-ambient">0.50</span>
                </div>
                <div class="setting-row">
                    <label>Headlamp</label>
                    <input type="range" id="sl-headlamp" min="0" max="200" value="150">
                    <span class="setting-val" id="sv-headlamp">1.50</span>
                </div>
            </div>
            <div class="modal-controls">
                <span class="modal-hint">Click &amp; drag to rotate &bull; Scroll to zoom</span>
                <button type="button" class="modal-auto-rotate" id="modal-auto-rotate">Auto-rotate</button>
                <button type="button" class="modal-settings-btn" id="modal-settings-btn">&#9881; Settings</button>
            </div>
        </div>
    </div>
</div>

<script type="importmap">
{{
    "imports": {{
        "three": "https://cdn.jsdelivr.net/npm/three@0.160.0/build/three.module.js",
        "three/addons/": "https://cdn.jsdelivr.net/npm/three@0.160.0/examples/jsm/"
    }}
}}
</script>

<script type="module">
import * as THREE from 'three';
import {{ OrbitControls }} from 'three/addons/controls/OrbitControls.js';
import {{ STLLoader }} from 'three/addons/loaders/STLLoader.js';

// ===== DATA =====
const TOWERS = {tower_json};

// ===== STATE =====
let sidebarOpen = localStorage.getItem('idt-sidebar') !== 'false';
let activeVolumes = new Set();
let typeFilter = 'all';
let activeMulticolor = false;
let activeFavorites = false;
let activeTags = new Set();
let searchTerm = '';
let screenshotData = {{}};  // towerName -> dataURL

// Global hero image cycle: all cards advance together; per-tower adjust for card arrow buttons
let globalImageCycle = 0;
const towerImageAdjust = {{}};

function buildGalleryForTower(tower) {{
    const imgSrc = tower.thumb ? `data:image/jpeg;base64,${{tower.thumb}}` : '';
    const preLoaded = (tower.screenshots || []).map(s => `data:image/jpeg;base64,${{s}}`);
    const userDrops = Array.isArray(screenshotData[tower.name])
        ? screenshotData[tower.name]
        : (screenshotData[tower.name] ? [screenshotData[tower.name]] : []);
    const extras = [...preLoaded, ...userDrops];
    const gallery = imgSrc ? [imgSrc, ...extras] : [...extras];
    return {{ gallery, extras }};
}}

function heroIndexForTower(towerName, galleryLen) {{
    if (galleryLen <= 1) return 0;
    const adj = towerImageAdjust[towerName] || 0;
    let v = globalImageCycle + adj;
    v %= galleryLen;
    if (v < 0) v += galleryLen;
    return v;
}}

function refreshAllCardHeroImages() {{
    document.querySelectorAll('.card .screenshot-section[data-tower]').forEach(section => {{
        const towerName = section.dataset.tower;
        const tower = TOWERS.find(t => t.name === towerName);
        if (!tower) return;
        const {{ gallery }} = buildGalleryForTower(tower);
        const heroImg = section.closest('.card')?.querySelector('.card-hero-img');
        if (!heroImg || gallery.length === 0) return;
        heroImg.src = gallery[heroIndexForTower(towerName, gallery.length)];
    }});
}}

// Favorites and tags loaded from user_data.yaml at generation time.
// In --serve mode, changes are persisted back to the YAML file via API.
let favoriteTowers = new Set({favorites_json});
let towerTags = {tags_json};
Object.keys(towerTags).forEach(name => {{
    towerTags[name] = [...new Set(towerTags[name].map(t => t.toLowerCase()))];
    if (towerTags[name].length === 0) delete towerTags[name];
}});

function _persistUserData() {{
    fetch('/api/user-data', {{
        method: 'POST',
        headers: {{'Content-Type': 'application/json'}},
        body: JSON.stringify({{favorites: [...favoriteTowers], tags: towerTags}})
    }}).catch(() => {{}});
}}

function saveFavorites() {{ _persistUserData(); }}
function saveTags() {{ _persistUserData(); }}

function addTag(towerName, tag) {{
    tag = tag.trim().toLowerCase();
    if (!tag) return;
    if (!towerTags[towerName]) towerTags[towerName] = [];
    if (!towerTags[towerName].includes(tag)) {{
        towerTags[towerName].push(tag);
        saveTags();
    }}
}}

function removeTag(towerName, tag) {{
    if (!towerTags[towerName]) return;
    towerTags[towerName] = towerTags[towerName].filter(t => t !== tag);
    if (towerTags[towerName].length === 0) delete towerTags[towerName];
    saveTags();
}}

function getAllTags() {{
    const tags = new Set();
    Object.values(towerTags).forEach(arr => arr.forEach(t => tags.add(t)));
    return [...tags].sort();
}}

// ===== VOLUMES & STATS =====
const volumes = [...new Set(TOWERS.map(t => t.volume).filter(Boolean))];
const totalCount = TOWERS.length;
const multicolorCount = TOWERS.filter(t => t.has_multicolor).length;
document.getElementById('total-count').textContent = totalCount;
document.getElementById('multicolor-count').textContent = multicolorCount;
if (volumes.length > 1) {{
    document.getElementById('volume-stat').style.display = '';
    document.getElementById('volume-count').textContent = volumes.length;
}}

const sortedVolumes = [...volumes].sort((a, b) => {{
    const na = parseInt(a.replace(/\\D/g, '')) || 0;
    const nb = parseInt(b.replace(/\\D/g, '')) || 0;
    return nb - na;
}});

// ===== SIDEBAR =====
function toggleSidebar() {{
    sidebarOpen = !sidebarOpen;
    localStorage.setItem('idt-sidebar', sidebarOpen);
    document.getElementById('sidebar').style.display = sidebarOpen ? '' : 'none';
    const bd = document.getElementById('sidebar-backdrop');
    if (bd) bd.style.display = sidebarOpen ? '' : 'none';
}}

document.getElementById('filter-toggle').addEventListener('click', toggleSidebar);
if (!sidebarOpen) document.getElementById('sidebar').style.display = 'none';

function getActiveFilterCount() {{
    return activeVolumes.size
        + (typeFilter !== 'all' ? 1 : 0)
        + (activeMulticolor ? 1 : 0)
        + (activeFavorites ? 1 : 0)
        + activeTags.size;
}}

function buildSidebar() {{
    // Volumes
    const volumeList = document.getElementById('volume-list');
    volumeList.innerHTML = '';
    sortedVolumes.forEach(vol => {{
        const count = TOWERS.filter(t => t.volume === vol).length;
        const checked = activeVolumes.has(vol);
        const row = document.createElement('div');
        row.className = 'cb-row';
        row.innerHTML = `<div class="cb-box${{checked ? ' checked-gold' : ''}}">&#10003;</div><span class="cb-label">${{vol}}</span><span class="cb-count">${{count}}</span>`;
        row.addEventListener('click', () => {{
            if (activeVolumes.has(vol)) activeVolumes.delete(vol);
            else activeVolumes.add(vol);
            buildSidebar(); updateChips(); renderGrid();
        }});
        volumeList.appendChild(row);
    }});
    document.getElementById('clear-volumes').style.display = activeVolumes.size > 0 ? '' : 'none';

    // Type toggle
    document.querySelectorAll('.type-seg').forEach(seg => {{
        seg.classList.toggle('active', seg.dataset.type === typeFilter);
    }});

    // Quick filters
    const qf = document.getElementById('quick-filters');
    qf.innerHTML = '';
    const mcRow = document.createElement('div');
    mcRow.className = 'cb-row';
    mcRow.innerHTML = `<div class="cb-box${{activeMulticolor ? ' checked-yellow' : ''}}">&#10003;</div><span class="cb-label">Multicolor</span><span class="cb-count">${{multicolorCount}}</span>`;
    mcRow.addEventListener('click', () => {{
        activeMulticolor = !activeMulticolor;
        buildSidebar(); updateChips(); renderGrid();
    }});
    qf.appendChild(mcRow);

    const favRow = document.createElement('div');
    favRow.className = 'cb-row';
    const favCount = TOWERS.filter(t => favoriteTowers.has(t.name)).length;
    favRow.innerHTML = `<div class="cb-box${{activeFavorites ? ' checked-gold' : ''}}">&#10003;</div><span class="cb-label">&#9733; Favorites</span><span class="cb-count">${{favCount}}</span>`;
    favRow.addEventListener('click', () => {{
        activeFavorites = !activeFavorites;
        buildSidebar(); updateChips(); renderGrid();
    }});
    qf.appendChild(favRow);

    // Tags
    const tagList = document.getElementById('tag-list');
    tagList.innerHTML = '';
    const allTags = getAllTags();
    allTags.forEach(tag => {{
        const count = TOWERS.filter(t => (towerTags[t.name] || []).includes(tag)).length;
        const checked = activeTags.has(tag);
        const row = document.createElement('div');
        row.className = 'cb-row';
        row.innerHTML = `<div class="cb-box${{checked ? ' checked-teal' : ''}}">&#10003;</div><span class="cb-label">${{tag}}</span><span class="cb-count">${{count}}</span>`;
        row.addEventListener('click', () => {{
            if (activeTags.has(tag)) activeTags.delete(tag);
            else activeTags.add(tag);
            buildSidebar(); updateChips(); renderGrid();
        }});
        tagList.appendChild(row);
    }});
    document.getElementById('clear-tags').style.display = activeTags.size > 0 ? '' : 'none';

    // Badge
    const badge = document.getElementById('filter-badge');
    const cnt = getActiveFilterCount();
    badge.style.display = cnt > 0 ? '' : 'none';
    badge.textContent = cnt;
}}

document.getElementById('clear-volumes').addEventListener('click', (e) => {{
    e.stopPropagation();
    activeVolumes.clear();
    buildSidebar(); updateChips(); renderGrid();
}});
document.getElementById('clear-tags').addEventListener('click', (e) => {{
    e.stopPropagation();
    activeTags.clear();
    buildSidebar(); updateChips(); renderGrid();
}});
document.getElementById('type-toggle').addEventListener('click', (e) => {{
    const seg = e.target.closest('.type-seg');
    if (!seg) return;
    typeFilter = seg.dataset.type;
    buildSidebar(); updateChips(); renderGrid();
}});

// ===== FILTER CHIPS =====
function updateChips() {{
    const container = document.getElementById('filter-chips');
    container.innerHTML = '';
    activeVolumes.forEach(vol => {{
        container.insertAdjacentHTML('beforeend',
            `<span class="filter-chip chip-volume">${{vol}}<span class="filter-chip-x" data-action="remove-volume" data-value="${{vol}}">&times;</span></span>`);
    }});
    if (typeFilter !== 'all') {{
        const label = typeFilter === 'core' ? 'Core' : 'Stretch';
        container.insertAdjacentHTML('beforeend',
            `<span class="filter-chip chip-type">${{label}}<span class="filter-chip-x" data-action="remove-type">&times;</span></span>`);
    }}
    if (activeMulticolor) {{
        container.insertAdjacentHTML('beforeend',
            `<span class="filter-chip chip-multicolor">Multicolor<span class="filter-chip-x" data-action="remove-multicolor">&times;</span></span>`);
    }}
    if (activeFavorites) {{
        container.insertAdjacentHTML('beforeend',
            `<span class="filter-chip chip-favorites">&#9733; Favorites<span class="filter-chip-x" data-action="remove-favorites">&times;</span></span>`);
    }}
    activeTags.forEach(tag => {{
        container.insertAdjacentHTML('beforeend',
            `<span class="filter-chip chip-tag">${{tag}}<span class="filter-chip-x" data-action="remove-tag" data-value="${{tag}}">&times;</span></span>`);
    }});
    container.querySelectorAll('.filter-chip-x').forEach(x => {{
        x.addEventListener('click', (e) => {{
            e.stopPropagation();
            const action = x.dataset.action;
            if (action === 'remove-volume') activeVolumes.delete(x.dataset.value);
            else if (action === 'remove-type') typeFilter = 'all';
            else if (action === 'remove-multicolor') activeMulticolor = false;
            else if (action === 'remove-favorites') activeFavorites = false;
            else if (action === 'remove-tag') activeTags.delete(x.dataset.value);
            buildSidebar(); updateChips(); renderGrid();
        }});
    }});
}}

buildSidebar();
updateChips();

// ===== SEARCH =====
document.getElementById('search').addEventListener('input', (e) => {{
    searchTerm = e.target.value.toLowerCase();
    renderGrid();
}});

document.getElementById('global-gallery-prev').addEventListener('click', () => {{
    globalImageCycle -= 1;
    refreshAllCardHeroImages();
}});
document.getElementById('global-gallery-next').addEventListener('click', () => {{
    globalImageCycle += 1;
    refreshAllCardHeroImages();
}});
document.getElementById('global-gallery-reset').addEventListener('click', () => {{
    globalImageCycle = 0;
    Object.keys(towerImageAdjust).forEach(k => delete towerImageAdjust[k]);
    refreshAllCardHeroImages();
}});

function readFileAsDataURL(file) {{
    return new Promise((resolve, reject) => {{
        const reader = new FileReader();
        reader.onload = (ev) => resolve(ev.target.result);
        reader.onerror = () => reject(new Error('read failed'));
        reader.readAsDataURL(file);
    }});
}}

async function persistScreenshotsForTower(tower, files) {{
    const canHttp = window.location.protocol === 'http:' || window.location.protocol === 'https:';
    const existing = Array.isArray(screenshotData[tower.name]) ? screenshotData[tower.name] : (screenshotData[tower.name] ? [screenshotData[tower.name]] : []);
    const newUrls = [];
    let anySavedToDisk = false;
    for (const file of files) {{
        let dataUrl = null;
        if (canHttp) {{
            try {{
                const q = new URLSearchParams({{ folder: tower.folder, filename: file.name }});
                const res = await fetch('/upload-screenshot?' + q.toString(), {{
                    method: 'POST',
                    body: file,
                    headers: {{ 'Content-Type': file.type || 'application/octet-stream' }},
                }});
                const j = await res.json();
                if (j.ok) {{
                    anySavedToDisk = true;
                    if (j.thumb) dataUrl = `data:image/jpeg;base64,${{j.thumb}}`;
                }}
            }} catch (err) {{
                console.warn('Screenshot upload failed', err);
            }}
        }}
        if (!dataUrl) {{
            try {{ dataUrl = await readFileAsDataURL(file); }} catch (e) {{ continue; }}
        }}
        newUrls.push(dataUrl);
    }}
    if (newUrls.length === 0) return;
    screenshotData[tower.name] = [...existing, ...newUrls];
    if (canHttp && anySavedToDisk) {{
        try {{
            await fetch('/regenerate-catalog', {{ method: 'POST' }});
        }} catch (e) {{
            console.warn('Catalog refresh failed', e);
        }}
    }}
    renderGrid();
}}

// ===== RENDER GRID =====
function renderGrid() {{
    const grid = document.getElementById('grid');
    grid.innerHTML = '';

    const filtered = TOWERS.filter(t => {{
        if (searchTerm && !t.name.toLowerCase().includes(searchTerm)) return false;
        if (activeVolumes.size > 0 && !activeVolumes.has(t.volume)) return false;
        if (typeFilter === 'core' && !t.category.includes('Core Set')) return false;
        if (typeFilter === 'stretch' && !t.category.includes('Stretch Goal')) return false;
        if (activeMulticolor && !t.has_multicolor) return false;
        if (activeFavorites && !favoriteTowers.has(t.name)) return false;
        if (activeTags.size > 0) {{
            const tags = towerTags[t.name] || [];
            if (!tags.some(tag => activeTags.has(tag))) return false;
        }}
        return true;
    }});

    document.getElementById('visible-count').textContent = filtered.length;

    if (filtered.length === 0) {{
        grid.innerHTML = '<div class="empty-state"><div class="icon">&#x1F3F0;</div><div>No towers match your filters</div></div>';
        return;
    }}

    filtered.forEach(tower => {{
        const card = document.createElement('div');
        card.className = 'card';

        const stlCount = tower.master_stls.length;
        const totalSize = tower.master_stls.reduce((a, s) => a + s.size_mb, 0).toFixed(1);

        let badgesHTML = `<span class="badge badge-category">${{tower.category}}</span>`;
        if (tower.has_multicolor) {{
            badgesHTML += `<span class="badge badge-multicolor">Multicolor</span>`;
        }}
        const tTags = towerTags[tower.name] || [];
        tTags.forEach(tag => {{
            badgesHTML += `<span class="badge badge-tag" data-tag="${{tag}}">${{tag}}<span class="tag-remove" data-tower="${{tower.name}}" data-tag="${{tag}}">&times;</span></span>`;
        }});
        badgesHTML += `<button class="tag-add-btn" data-tower="${{tower.name}}">+ tag</button>`;

        const isFav = favoriteTowers.has(tower.name);

        const {{ gallery, extras }} = buildGalleryForTower(tower);
        const heroIdx = heroIndexForTower(tower.name, gallery.length);
        const heroSrc = gallery.length > 0 ? gallery[heroIdx] : '';
        const imgHTML = heroSrc
            ? `<img class="card-hero-img" src="${{heroSrc}}" alt="${{tower.name}}" loading="lazy">`
            : `<div style="width:100%;height:100%;display:flex;align-items:center;justify-content:center;color:var(--text-dim);">No Preview</div>`;
        const navHTML = gallery.length > 1
            ? `<button type="button" class="card-img-nav card-img-nav-prev" aria-label="Previous image">&#8249;</button><button type="button" class="card-img-nav card-img-nav-next" aria-label="Next image">&#8250;</button>`
            : '';

        const stripHTML = extras.length > 0
            ? extras.map(src => `<div class="thumb"><img src="${{src}}" alt="Screenshot" loading="lazy"></div>`).join('')
            : '';
        const stripSection = extras.length > 0
            ? `<div class="screenshot-strip">${{stripHTML}}</div>`
            : '';

        card.innerHTML = `
            <div class="card-img-wrap" onclick="window.open3DViewer('${{tower.name.replace(/'/g, "\\\\'")}}')">
                ${{imgHTML}}
                ${{navHTML}}
            </div>
            <div class="card-body">
                <div class="card-name">${{tower.name}}</div>
                <div class="card-meta">${{stlCount}} master STL${{stlCount !== 1 ? 's' : ''}} &bull; ${{totalSize}} MB</div>
                <div class="card-badges">${{badgesHTML}}</div>
            </div>
            <div class="screenshot-section" data-tower="${{tower.name}}">
                ${{stripSection}}
                <div class="screenshot-upload">Drop or click to add screenshot</div>
            </div>
            <div class="card-actions">
                ${{stlCount > 0 ? `<button class="btn btn-3d" onclick="window.open3DViewer('${{tower.name.replace(/'/g, "\\\\'")}}')">&#x1F4A0; View 3D</button>` : ''}}
                <button class="btn" onclick="window.openFolder('${{tower.folder.replace(/'/g, "\\\\'")}}')">&#x1F4C2; Open Folder</button>
                <button class="star-btn${{isFav ? ' starred' : ''}}" data-tower="${{tower.name}}" title="${{isFav ? 'Remove from favorites' : 'Add to favorites'}}">&#x2605;</button>
            </div>
        `;

        // Star / favorite button
        const starBtn = card.querySelector('.star-btn');
        starBtn.addEventListener('click', (e) => {{
            e.stopPropagation();
            if (favoriteTowers.has(tower.name)) {{
                favoriteTowers.delete(tower.name);
                starBtn.classList.remove('starred');
                starBtn.title = 'Add to favorites';
            }} else {{
                favoriteTowers.add(tower.name);
                starBtn.classList.add('starred');
                starBtn.title = 'Remove from favorites';
            }}
            saveFavorites();
            if (activeFavorites) renderGrid();
        }});

        // Tag remove buttons
        card.querySelectorAll('.tag-remove').forEach(btn => {{
            btn.addEventListener('click', (e) => {{
                e.stopPropagation();
                removeTag(btn.dataset.tower, btn.dataset.tag);
                buildSidebar();
                renderGrid();
            }});
        }});

        // Tag add button
        const tagAddBtn = card.querySelector('.tag-add-btn');
        if (tagAddBtn) {{
            tagAddBtn.addEventListener('click', (e) => {{
                e.stopPropagation();
                const wrap = tagAddBtn.parentNode;
                // Replace button with input
                const input = document.createElement('input');
                input.type = 'text';
                input.className = 'tag-input';
                input.placeholder = 'tag name';
                input.maxLength = 30;
                tagAddBtn.style.display = 'none';
                wrap.appendChild(input);
                input.focus();

                const commitTag = () => {{
                    const val = input.value.trim();
                    if (val) {{
                        addTag(tower.name, val);
                        buildSidebar();
                        renderGrid();
                    }} else {{
                        input.remove();
                        tagAddBtn.style.display = '';
                    }}
                }};
                input.addEventListener('keydown', (ev) => {{
                    if (ev.key === 'Enter') commitTag();
                    if (ev.key === 'Escape') {{ input.remove(); tagAddBtn.style.display = ''; }}
                }});
                input.addEventListener('blur', commitTag);
            }});
        }}

        const heroImg = card.querySelector('.card-hero-img');
        const prevNav = card.querySelector('.card-img-nav-prev');
        const nextNav = card.querySelector('.card-img-nav-next');
        if (heroImg && gallery.length > 1 && prevNav && nextNav) {{
            prevNav.addEventListener('click', (e) => {{
                e.stopPropagation();
                towerImageAdjust[tower.name] = (towerImageAdjust[tower.name] || 0) - 1;
                heroImg.src = gallery[heroIndexForTower(tower.name, gallery.length)];
            }});
            nextNav.addEventListener('click', (e) => {{
                e.stopPropagation();
                towerImageAdjust[tower.name] = (towerImageAdjust[tower.name] || 0) + 1;
                heroImg.src = gallery[heroIndexForTower(tower.name, gallery.length)];
            }});
        }}

        // Thumbnail click -> lightbox
        card.querySelectorAll('.screenshot-strip .thumb').forEach(thumb => {{
            thumb.addEventListener('click', (e) => {{
                e.stopPropagation();
                const img = thumb.querySelector('img');
                if (img && img.src) {{
                    document.getElementById('lightbox-img').src = img.src;
                    document.getElementById('lightbox').classList.add('active');
                }}
            }});
        }});

        // Upload box: drop and click
        const uploadBox = card.querySelector('.screenshot-upload');
        uploadBox.addEventListener('dragover', (e) => {{ e.preventDefault(); uploadBox.style.borderColor = 'var(--accent)'; }});
        uploadBox.addEventListener('dragleave', () => {{ uploadBox.style.borderColor = ''; }});
        uploadBox.addEventListener('drop', (e) => {{
            e.preventDefault();
            uploadBox.style.borderColor = '';
            const files = Array.from(e.dataTransfer.files).filter(f => f.type.startsWith('image/'));
            if (files.length > 0) persistScreenshotsForTower(tower, files);
        }});
        uploadBox.addEventListener('click', (e) => {{
            if (e.target === uploadBox) {{
                const input = document.createElement('input');
                input.type = 'file';
                input.accept = 'image/*';
                input.multiple = true;
                input.onchange = (ev) => {{
                    const files = Array.from(ev.target.files || []).filter(f => f.type.startsWith('image/'));
                    if (files.length > 0) persistScreenshotsForTower(tower, files);
                }};
                input.click();
            }}
        }});

        grid.appendChild(card);
    }});
}}

// ===== 3D VIEWER =====
let scene, camera, renderer, controls, currentMesh, headlamp, ambientLight, currentMaterial;

// ===== SETTINGS PANEL =====
const SWATCH_COLORS = [
    {{ hex: '#cc4444', name: 'Brick Red' }},
    {{ hex: '#e74c3c', name: 'Red' }},
    {{ hex: '#e67e22', name: 'Orange' }},
    {{ hex: '#f1c40f', name: 'Gold' }},
    {{ hex: '#2ecc71', name: 'Green' }},
    {{ hex: '#1abc9c', name: 'Teal' }},
    {{ hex: '#3498db', name: 'Blue' }},
    {{ hex: '#9b59b6', name: 'Purple' }},
    {{ hex: '#e91e8f', name: 'Pink' }},
    {{ hex: '#ecf0f1', name: 'White' }},
    {{ hex: '#95a5a6', name: 'Silver' }},
    {{ hex: '#34495e', name: 'Slate' }},
    {{ hex: '#1a1a2e', name: 'Dark' }},
    {{ hex: '#000000', name: 'Black' }},
];

// Gradient presets simulating longitudinal multi-color filaments
const GRADIENT_PRESETS = [
    {{ name: 'None', colors: null }},
    {{ name: 'Silk Rainbow', colors: ['#e74c3c', '#e67e22', '#f1c40f', '#2ecc71', '#3498db', '#9b59b6'] }},
    {{ name: 'Sunset Gold', colors: ['#e74c3c', '#f39c12', '#f1c40f'] }},
    {{ name: 'Ocean Breeze', colors: ['#0652DD', '#1abc9c', '#00d2d3'] }},
    {{ name: 'Rose Gold', colors: ['#e91e8f', '#f39c12', '#ecf0f1'] }},
    {{ name: 'Forest', colors: ['#27ae60', '#f1c40f', '#27ae60'] }},
    {{ name: 'Lava', colors: ['#c0392b', '#e67e22', '#f1c40f', '#ecf0f1'] }},
    {{ name: 'Galaxy', colors: ['#1a1a2e', '#6c5ce7', '#e91e8f', '#f1c40f'] }},
    {{ name: 'Ice Blue', colors: ['#ecf0f1', '#74b9ff', '#0652DD'] }},
    {{ name: 'Copper Silk', colors: ['#784212', '#e67e22', '#f8c471', '#e67e22', '#784212'] }},
];

let activeGradient = null;  // null = solid color

function applyGradientToGeometry(geometry, gradientColors) {{
    if (!gradientColors || !geometry) return;
    const pos = geometry.attributes.position;
    const count = pos.count;
    const colors = new Float32Array(count * 3);

    geometry.computeBoundingBox();
    const minY = geometry.boundingBox.min.y;
    const maxY = geometry.boundingBox.max.y;
    const range = maxY - minY || 1;

    const stops = gradientColors.map(hex => {{
        const c = new THREE.Color(hex);
        return [c.r, c.g, c.b];
    }});
    const numStops = stops.length;

    for (let i = 0; i < count; i++) {{
        const y = pos.getY(i);
        const t = Math.max(0, Math.min(1, (y - minY) / range));

        // Map t to gradient stops
        const scaledT = t * (numStops - 1);
        const idx = Math.min(Math.floor(scaledT), numStops - 2);
        const frac = scaledT - idx;

        colors[i * 3]     = stops[idx][0] + (stops[idx + 1][0] - stops[idx][0]) * frac;
        colors[i * 3 + 1] = stops[idx][1] + (stops[idx + 1][1] - stops[idx][1]) * frac;
        colors[i * 3 + 2] = stops[idx][2] + (stops[idx + 1][2] - stops[idx][2]) * frac;
    }}

    geometry.setAttribute('color', new THREE.BufferAttribute(colors, 3));
}}

function removeGradientFromGeometry(geometry) {{
    if (geometry && geometry.hasAttribute('color')) {{
        geometry.deleteAttribute('color');
    }}
}}

function refreshGradient() {{
    if (!currentMesh || !currentMaterial) return;
    if (activeGradient) {{
        applyGradientToGeometry(currentMesh.geometry, activeGradient);
        currentMaterial.vertexColors = true;
        currentMaterial.needsUpdate = true;
    }} else {{
        removeGradientFromGeometry(currentMesh.geometry);
        currentMaterial.vertexColors = false;
        currentMaterial.needsUpdate = true;
    }}
}}

(function initSettingsPanel() {{
    const swatchContainer = document.getElementById('color-swatches');
    SWATCH_COLORS.forEach((c, i) => {{
        const el = document.createElement('div');
        el.className = 'color-swatch' + (i === 0 ? ' active' : '');
        el.style.background = c.hex;
        el.title = c.name;
        el.dataset.color = c.hex;
        el.onclick = () => {{
            swatchContainer.querySelectorAll('.color-swatch').forEach(s => s.classList.remove('active'));
            el.classList.add('active');
            if (currentMaterial) currentMaterial.color.set(c.hex);
            // Deactivate gradient when picking a solid color
            activeGradient = null;
            gradContainer.querySelectorAll('.gradient-swatch').forEach(s => s.classList.remove('active'));
            gradContainer.querySelector('.none-swatch').classList.add('active');
            refreshGradient();
        }};
        swatchContainer.appendChild(el);
    }});

    // Gradient swatches
    const gradContainer = document.getElementById('gradient-swatches');
    GRADIENT_PRESETS.forEach((g, i) => {{
        const el = document.createElement('div');
        el.className = 'gradient-swatch' + (i === 0 ? ' active' : '');
        el.title = g.name;
        if (g.colors) {{
            const stopStr = g.colors.map((c, ci) =>
                `${{c}} ${{Math.round(ci / (g.colors.length - 1) * 100)}}%`
            ).join(', ');
            el.style.background = `linear-gradient(180deg, ${{stopStr}})`;
        }} else {{
            el.className += ' none-swatch';
            el.textContent = 'Off';
            el.style.border = '2px solid var(--border)';
        }}
        el.onclick = () => {{
            gradContainer.querySelectorAll('.gradient-swatch').forEach(s => s.classList.remove('active'));
            el.classList.add('active');
            activeGradient = g.colors || null;
            refreshGradient();
        }};
        gradContainer.appendChild(el);
    }});

    // Settings toggle
    const settingsBtn = document.getElementById('modal-settings-btn');
    const settingsPanel = document.getElementById('settings-panel');
    settingsBtn.onclick = () => {{
        settingsPanel.classList.toggle('open');
        settingsBtn.classList.toggle('active', settingsPanel.classList.contains('open'));
    }};

    // Slider wiring
    const sliders = [
        {{ id: 'metalness',          prop: 'metalness',          div: 100 }},
        {{ id: 'roughness',          prop: 'roughness',          div: 100 }},
        {{ id: 'clearcoat',          prop: 'clearcoat',          div: 100 }},
        {{ id: 'clearcoatRoughness', prop: 'clearcoatRoughness', div: 100 }},
    ];
    sliders.forEach(s => {{
        const slider = document.getElementById('sl-' + s.id);
        const valEl  = document.getElementById('sv-' + s.id);
        slider.oninput = () => {{
            const v = slider.value / s.div;
            valEl.textContent = v.toFixed(2);
            if (currentMaterial) currentMaterial[s.prop] = v;
        }};
    }});

    // Ambient intensity
    document.getElementById('sl-ambient').oninput = function() {{
        const v = this.value / 100;
        document.getElementById('sv-ambient').textContent = v.toFixed(2);
        if (ambientLight) ambientLight.intensity = v;
    }};

    // Headlamp intensity
    document.getElementById('sl-headlamp').oninput = function() {{
        const v = this.value / 100;
        document.getElementById('sv-headlamp').textContent = v.toFixed(2);
        if (headlamp) headlamp.intensity = v;
    }};
}})();

window.open3DViewer = function(towerName) {{
    const tower = TOWERS.find(t => t.name === towerName);
    if (!tower || tower.master_stls.length === 0) return;

    const modal = document.getElementById('modal');
    const body = document.getElementById('modal-body');
    const loading = document.getElementById('modal-loading');
    document.getElementById('modal-title').textContent = tower.name;

    // Modal star button
    const modalStar = document.getElementById('modal-star-btn');
    const isStarred = favoriteTowers.has(tower.name);
    modalStar.className = 'modal-star-btn' + (isStarred ? ' starred' : '');
    modalStar.title = isStarred ? 'Remove from favorites' : 'Add to favorites';
    const newModalStar = modalStar.cloneNode(true);
    modalStar.parentNode.replaceChild(newModalStar, modalStar);
    newModalStar.addEventListener('click', () => {{
        if (favoriteTowers.has(tower.name)) {{
            favoriteTowers.delete(tower.name);
            newModalStar.classList.remove('starred');
            newModalStar.title = 'Add to favorites';
        }} else {{
            favoriteTowers.add(tower.name);
            newModalStar.classList.add('starred');
            newModalStar.title = 'Remove from favorites';
        }}
        saveFavorites();
        if (activeFavorites) renderGrid();
    }});

    modal.classList.add('active');
    loading.style.display = 'flex';
    closeSettingsPanel();

    // Clean up previous
    if (renderer) {{
        renderer.dispose();
        if (controls) controls.dispose();
    }}

    // Setup scene
    scene = new THREE.Scene();
    scene.background = new THREE.Color(0x14141f);

    camera = new THREE.PerspectiveCamera(45, body.clientWidth / body.clientHeight, 0.1, 10000);

    renderer = new THREE.WebGLRenderer({{ antialias: true }});
    renderer.setSize(body.clientWidth, body.clientHeight);
    renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2));
    renderer.toneMapping = THREE.ACESFilmicToneMapping;

    // Remove old canvas
    const oldCanvas = body.querySelector('canvas');
    if (oldCanvas) oldCanvas.remove();
    body.appendChild(renderer.domElement);

    controls = new OrbitControls(camera, renderer.domElement);
    controls.enableDamping = true;
    controls.dampingFactor = 0.08;
    controls.autoRotate = false;
    controls.autoRotateSpeed = 1.5;

    const autoRotateBtn = document.getElementById('modal-auto-rotate');
    autoRotateBtn.classList.remove('active');
    autoRotateBtn.onclick = () => {{
        controls.autoRotate = !controls.autoRotate;
        autoRotateBtn.classList.toggle('active', controls.autoRotate);
    }};

    // Lighting: ambient + headlamp that follows camera (always lights the side facing the viewer)
    ambientLight = new THREE.AmbientLight(0x404060, document.getElementById('sl-ambient').value / 100);
    scene.add(ambientLight);
    headlamp = new THREE.DirectionalLight(0xfff0dd, document.getElementById('sl-headlamp').value / 100);
    scene.add(headlamp);

    // Load STL (first master)
    const largestStl = tower.master_stls.reduce((a, b) => b.size_mb > a.size_mb ? b : a);
    const stlPath = largestStl.path;
    const loader = new STLLoader();

    // Use fetch with encoded URI
    fetch('/stl-file?path=' + encodeURIComponent(stlPath))
        .then(r => {{
            if (!r.ok) throw new Error('Could not load STL');
            return r.arrayBuffer();
        }})
        .then(buffer => {{
            const geometry = loader.parse(buffer);
            geometry.computeVertexNormals();
            // STL models are typically Z-up; rotate to Y-up for upright display
            geometry.rotateX(-Math.PI / 2);

            // Read current settings panel values
            const activeColor = document.querySelector('#color-swatches .color-swatch.active');
            currentMaterial = new THREE.MeshPhysicalMaterial({{
                color: activeColor ? activeColor.dataset.color : 0xcc4444,
                metalness: document.getElementById('sl-metalness').value / 100,
                roughness: document.getElementById('sl-roughness').value / 100,
                clearcoat: document.getElementById('sl-clearcoat').value / 100,
                clearcoatRoughness: document.getElementById('sl-clearcoatRoughness').value / 100,
            }});

            if (currentMesh) scene.remove(currentMesh);
            currentMesh = new THREE.Mesh(geometry, currentMaterial);

            // Center and fit
            geometry.computeBoundingBox();
            const box = geometry.boundingBox;
            const center = new THREE.Vector3();
            box.getCenter(center);
            currentMesh.position.sub(center);

            const size = new THREE.Vector3();
            box.getSize(size);
            const maxDim = Math.max(size.x, size.y, size.z);
            const fov = camera.fov * (Math.PI / 180);
            let dist = maxDim / (2 * Math.tan(fov / 2));
            camera.position.set(dist * 0.8, dist * 0.6, dist * 0.8);
            controls.target.set(0, 0, 0);
            camera.near = dist * 0.01;
            camera.far = dist * 20;
            camera.updateProjectionMatrix();
            controls.update();

            scene.add(currentMesh);
            refreshGradient();
            loading.style.display = 'none';
        }})
        .catch(err => {{
            loading.innerHTML = `<div style="text-align:center;color:var(--text-dim);">
                <div style="font-size:2rem;margin-bottom:0.5rem;">&#x26A0;</div>
                <div>Could not load STL file</div>
                <div style="font-size:0.8rem;margin-top:0.3rem;">Run with --serve flag to enable 3D preview</div>
            </div>`;
        }});

    // Animate
    function animate() {{
        if (!modal.classList.contains('active')) return;
        requestAnimationFrame(animate);
        controls.update();
        headlamp.position.copy(camera.position);
        headlamp.target.position.copy(controls.target);
        renderer.render(scene, camera);
    }}
    animate();
}};

// ===== LIGHTBOX CLOSE =====
document.getElementById('lightbox-close').onclick = () => {{
    document.getElementById('lightbox').classList.remove('active');
}};
document.getElementById('lightbox').onclick = (e) => {{
    if (e.target === e.currentTarget) {{
        document.getElementById('lightbox').classList.remove('active');
    }}
}};
document.addEventListener('keydown', (e) => {{
    if (e.key === 'Escape' && document.getElementById('lightbox').classList.contains('active')) {{
        document.getElementById('lightbox').classList.remove('active');
    }}
}});

// ===== MODAL CLOSE =====
function closeSettingsPanel() {{
    document.getElementById('settings-panel').classList.remove('open');
    document.getElementById('modal-settings-btn').classList.remove('active');
}}
document.getElementById('modal-close').onclick = () => {{
    document.getElementById('modal').classList.remove('active');
    closeSettingsPanel();
    if (renderer) renderer.dispose();
    if (controls) controls.dispose();
}};
document.getElementById('modal').onclick = (e) => {{
    if (e.target === e.currentTarget) {{
        document.getElementById('modal').classList.remove('active');
        closeSettingsPanel();
        if (renderer) renderer.dispose();
        if (controls) controls.dispose();
    }}
}};

// ===== OPEN FOLDER (macOS) =====
window.openFolder = function(path) {{
    // This only works when served locally
    fetch('/open-folder?path=' + encodeURIComponent(path)).catch(() => {{}});
}};

// ===== INIT =====
renderGrid();
</script>
</body>
</html>"""

    with open(output_path, 'w', encoding='utf-8') as f:
        f.write(html)

    if verbose:
        print(f"\nCatalog written to: {output_path}")
    return output_path


# Set by serve() so uploads can validate paths and refresh the catalog on disk.
_CATALOG_ROOT = None
_HTML_OUTPUT_PATH = None


def _path_under_catalog_root(candidate, root):
    """Return realpath of candidate if it is a directory under root, else None."""
    try:
        c = os.path.realpath(candidate)
        r = os.path.realpath(root)
        if not os.path.isdir(c):
            return None
        if os.path.commonpath([c, r]) != r:
            return None
        return c
    except (OSError, ValueError):
        return None


def _safe_upload_filename(original):
    base = os.path.basename(original or "")
    base = re.sub(r"[^a-zA-Z0-9._-]", "_", base)
    if not base or base.startswith("."):
        base = f"upload_{int(time.time() * 1000)}.jpg"
    stem, ext = os.path.splitext(base)
    if not ext or len(ext) > 10:
        ext = ".jpg"
        base = stem + ext
    return base[:200]


def _unique_dest_path(dest_dir, filename):
    path = os.path.join(dest_dir, filename)
    if not os.path.exists(path):
        return path
    stem, ext = os.path.splitext(filename)
    n = 1
    while True:
        alt = os.path.join(dest_dir, f"{stem}_{n}{ext}")
        if not os.path.exists(alt):
            return alt
        n += 1


def regenerate_catalog_html():
    if not _CATALOG_ROOT or not _HTML_OUTPUT_PATH:
        return
    towers, is_multi = scan_all(_CATALOG_ROOT)
    generate_html(
        towers, _CATALOG_ROOT, _HTML_OUTPUT_PATH, is_multi_volume=is_multi, verbose=False
    )


class CatalogHandler(http.server.SimpleHTTPRequestHandler):
    """Custom handler that serves STL files, opens folders, and persists screenshot uploads."""

    def _send_json(self, code, obj):
        body = json.dumps(obj).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        path_only = self.path.split("?", 1)[0]
        if path_only == "/regenerate-catalog":
            if not _CATALOG_ROOT:
                self._send_json(503, {"ok": False, "error": "catalog root not configured"})
                return
            try:
                regenerate_catalog_html()
            except Exception as e:
                self._send_json(500, {"ok": False, "error": str(e)})
                return
            self._send_json(200, {"ok": True})
            return

        if path_only == "/api/user-data":
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                self._send_json(400, {"ok": False, "error": "bad content length"})
                return
            body = self.rfile.read(length)
            try:
                data = json.loads(body)
            except (json.JSONDecodeError, ValueError):
                self._send_json(400, {"ok": False, "error": "invalid JSON"})
                return
            try:
                save_user_data(data.get("favorites", []), data.get("tags", {}))
                self._send_json(200, {"ok": True})
            except Exception as e:
                self._send_json(500, {"ok": False, "error": str(e)})
            return

        if path_only != "/upload-screenshot":
            self.send_error(404)
            return

        if not _CATALOG_ROOT:
            self._send_json(503, {"ok": False, "error": "upload disabled (open catalog via --serve)"})
            return

        parsed = urllib.parse.urlparse(self.path)
        qs = urllib.parse.parse_qs(parsed.query)
        folder = (qs.get("folder") or [None])[0]
        filename = (qs.get("filename") or [None])[0]
        if not folder or not filename:
            self._send_json(400, {"ok": False, "error": "missing folder or filename"})
            return

        abs_folder = _path_under_catalog_root(folder, _CATALOG_ROOT)
        if not abs_folder:
            self._send_json(403, {"ok": False, "error": "invalid tower folder"})
            return

        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            self._send_json(400, {"ok": False, "error": "bad content length"})
            return

        max_bytes = 20 * 1024 * 1024
        if length <= 0 or length > max_bytes:
            self._send_json(400, {"ok": False, "error": "file too large or empty"})
            return

        body = self.rfile.read(length)
        if len(body) != length:
            self._send_json(400, {"ok": False, "error": "incomplete upload"})
            return

        uploads_dir = os.path.join(abs_folder, "user_uploads")
        try:
            os.makedirs(uploads_dir, exist_ok=True)
        except OSError:
            self._send_json(500, {"ok": False, "error": "could not create user_uploads"})
            return

        safe_name = _safe_upload_filename(filename)
        dest = _unique_dest_path(uploads_dir, safe_name)
        try:
            with open(dest, "wb") as out:
                out.write(body)
        except OSError:
            self._send_json(500, {"ok": False, "error": "write failed"})
            return

        try:
            thumb_b64 = make_thumbnail_b64(dest)
        except Exception:
            thumb_b64 = ""

        self._send_json(
            200,
            {"ok": True, "thumb": thumb_b64, "saved_as": os.path.basename(dest)},
        )

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)

        if parsed.path == '/stl-file':
            params = urllib.parse.parse_qs(parsed.query)
            file_path = params.get('path', [None])[0]
            if file_path and os.path.isfile(file_path):
                self.send_response(200)
                self.send_header('Content-Type', 'application/octet-stream')
                self.send_header('Access-Control-Allow-Origin', '*')
                file_size = os.path.getsize(file_path)
                self.send_header('Content-Length', str(file_size))
                self.end_headers()
                with open(file_path, 'rb') as f:
                    self.wfile.write(f.read())
            else:
                self.send_error(404, "STL file not found")
            return

        if parsed.path == '/open-folder':
            params = urllib.parse.parse_qs(parsed.query)
            folder = params.get('path', [None])[0]
            if folder and os.path.isdir(folder):
                import subprocess
                subprocess.Popen(['open', folder])  # macOS
                self.send_response(200)
                self.send_header('Content-Type', 'text/plain')
                self.end_headers()
                self.wfile.write(b'OK')
            else:
                self.send_error(404, "Folder not found")
            return

        super().do_GET()

    def log_message(self, format, *args):
        pass  # Suppress request logging


class ReusableTCPServer(socketserver.TCPServer):
    """TCPServer that releases the port immediately on Ctrl+C (SO_REUSEADDR)."""
    allow_reuse_address = True


def serve(html_path, volume_path, port=7294):
    """Start local server and open browser. Refreshes the catalog from disk first."""
    global _CATALOG_ROOT, _HTML_OUTPUT_PATH
    _CATALOG_ROOT = os.path.abspath(os.path.realpath(volume_path))
    _HTML_OUTPUT_PATH = os.path.abspath(html_path)
    towers, is_multi = scan_all(volume_path)
    print(f"Found {len(towers)} towers")
    generate_html(towers, volume_path, html_path, is_multi_volume=is_multi)

    os.chdir(os.path.dirname(os.path.abspath(html_path)))

    handler = CatalogHandler
    with ReusableTCPServer(("", port), handler) as httpd:
        url = f"http://localhost:{port}/{os.path.basename(html_path)}"
        print(f"\n  Serving at: {url}")
        print(f"  Press Ctrl+C to stop\n")
        webbrowser.open(url)
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            print("\nServer stopped.")


def main():
    args = sys.argv[1:]
    do_serve = '--serve' in args
    args = [a for a in args if a != '--serve']

    volume_path = args[0] if args else '.'
    volume_path = os.path.expanduser(volume_path)

    if not os.path.isdir(volume_path):
        print(f"Error: {volume_path} is not a directory")
        sys.exit(1)

    output_dir = os.path.dirname(os.path.abspath(__file__))
    output_path = os.path.join(output_dir, 'dice_tower_catalog.html')

    if do_serve:
        serve(output_path, volume_path)
    else:
        towers, is_multi = scan_all(volume_path)
        print(f"Found {len(towers)} towers")
        generate_html(towers, volume_path, output_path, is_multi_volume=is_multi)
        print("\nTo browse with 3D STL previews, run:")
        print(f"  python3 {os.path.abspath(__file__)} --serve \"{volume_path}\"")


if __name__ == '__main__':
    main()
