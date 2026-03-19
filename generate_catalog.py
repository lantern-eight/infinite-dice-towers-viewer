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
import threading
import urllib.parse
from pathlib import Path
from io import BytesIO

try:
    from PIL import Image
    HAS_PIL = True
except ImportError:
    HAS_PIL = False
    print("Warning: Pillow not installed. Thumbnails will use full-size images.")
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
    if (d / "Master").is_dir():
        return True
    if list(d.glob("*.stl")) or list(d.glob("*.jpg")) or list(d.glob("*.jpeg")):
        return True
    return False


def scan_tower_dir(tower_dir, category, volume_name=None):
    """Scan a single tower directory and return a tower dict."""
    name = tower_dir.name
    jpgs = list(tower_dir.glob("*.jpg")) + list(tower_dir.glob("*.jpeg")) + list(tower_dir.glob("*.png"))
    jpg_path = jpgs[0] if jpgs else None

    master_dir = tower_dir / "Master"
    master_stls = sorted(master_dir.rglob("*.stl")) if master_dir.exists() else []

    threemf_files = list(tower_dir.glob("*MultiColor*.3mf")) + list(tower_dir.glob("*.3mf"))
    has_multicolor = len(threemf_files) > 0
    threemf_path = threemf_files[0] if threemf_files else None

    # Find any image files in the tower dir (screenshots) - no specific naming required
    image_extensions = ("*.png", "*.jpg", "*.jpeg", "*.webp", "*.gif")
    all_images = []
    for ext in image_extensions:
        all_images.extend(tower_dir.glob(ext))
    all_images = sorted(set(all_images))
    # Exclude main thumbnail to avoid showing it twice
    if jpg_path:
        all_images = [p for p in all_images if p != jpg_path]
    screenshots = all_images

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
        'jpg_path': str(jpg_path) if jpg_path else None,
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


def generate_html(towers, root_path, output_path, is_multi_volume=False):
    """Generate the self-contained HTML catalog."""
    catalog_name = Path(root_path).name or "Catalog"
    print(f"Generating catalog for {len(towers)} towers...")

    # Build thumbnail data
    tower_data = []
    for i, t in enumerate(towers):
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
    background: rgba(10,10,15,0.92);
    backdrop-filter: blur(12px);
    border-bottom: 1px solid var(--border);
    padding: 0.8rem 1.5rem;
    display: flex;
    align-items: center;
    gap: 1rem;
    flex-wrap: wrap;
}}
.search-box {{
    flex: 1;
    min-width: 200px;
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
.filter-pills {{
    display: flex;
    gap: 0.4rem;
    flex-wrap: wrap;
}}
.pill {{
    padding: 0.4rem 0.9rem;
    border-radius: 20px;
    border: 1px solid var(--border);
    background: transparent;
    color: var(--text-dim);
    font-size: 0.78rem;
    cursor: pointer;
    transition: all 0.2s;
    font-family: 'Inter', sans-serif;
    white-space: nowrap;
}}
.pill:hover {{ border-color: var(--accent); color: var(--text); }}
.pill.active {{
    background: var(--accent-bright);
    border-color: var(--accent-bright);
    color: var(--text-bright);
}}
.pill.multicolor-pill {{
    border: 1.5px solid transparent;
    background-image: linear-gradient(var(--bg-deep), var(--bg-deep)),
                      linear-gradient(135deg, var(--multicolor-1), var(--multicolor-2), var(--multicolor-3));
    background-origin: border-box;
    background-clip: padding-box, border-box;
    color: var(--text);
}}
.pill.multicolor-pill.active {{
    background: linear-gradient(135deg, var(--multicolor-1), var(--multicolor-2), var(--multicolor-3));
    border-color: transparent;
    color: #000;
    font-weight: 600;
}}
.pill.favorites-pill.active {{
    background: var(--gold);
    border-color: var(--gold);
    color: #000;
    font-weight: 600;
}}
.star-btn {{
    position: absolute;
    top: 0.5rem;
    right: 0.5rem;
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
    z-index: 2;
    line-height: 1;
    padding: 0;
}}
.star-btn:hover {{ background: rgba(10,10,15,0.92); color: var(--gold); }}
.star-btn.starred {{ color: var(--gold); }}
.pill.volume-pill.active {{
    background: var(--gold);
    border-color: var(--gold);
    color: #000;
    font-weight: 600;
}}

/* ===== GRID ===== */
.grid {{
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(280px, 1fr));
    gap: 1.2rem;
    padding: 1.5rem;
    max-width: 1600px;
    margin: 0 auto;
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
}}
.card-img-wrap img {{
    width: 100%;
    height: 100%;
    object-fit: cover;
    transition: transform 0.4s ease;
}}
.card:hover .card-img-wrap img {{ transform: scale(1.05); }}
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
    <div class="search-box">
        <input type="text" id="search" placeholder="Search towers by name..." autocomplete="off">
    </div>
    <div class="filter-pills" id="filters"></div>
</div>

<div class="grid" id="grid"></div>

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
let activeCategories = new Set();  // empty = all categories; non-empty = filter to these
let activeMulticolor = false;
let activeFavorites = false;
let searchTerm = '';
let screenshotData = {{}};  // towerName -> dataURL

// Favorites persistence via localStorage
let favoriteTowers = new Set();
try {{
    const saved = JSON.parse(localStorage.getItem('favoriteTowers') || '[]');
    favoriteTowers = new Set(saved);
}} catch(e) {{}}

function saveFavorites() {{
    try {{
        localStorage.setItem('favoriteTowers', JSON.stringify([...favoriteTowers]));
    }} catch(e) {{}}
}}

// Try to load saved screenshots from memory
try {{
    const saved = window.__screenshots || {{}};
    Object.assign(screenshotData, saved);
}} catch(e) {{}}

// ===== CATEGORIES & VOLUMES =====
const volumes = [...new Set(TOWERS.map(t => t.volume).filter(Boolean))];
const categories = ['All', ...new Set(TOWERS.map(t => t.category))];
const totalCount = TOWERS.length;
const multicolorCount = TOWERS.filter(t => t.has_multicolor).length;
document.getElementById('total-count').textContent = totalCount;
document.getElementById('multicolor-count').textContent = multicolorCount;
if (volumes.length > 1) {{
    document.getElementById('volume-stat').style.display = '';
    document.getElementById('volume-count').textContent = volumes.length;
}}

let activeVolumes = new Set();  // empty = all volumes; non-empty = filter to these

// ===== BUILD FILTER PILLS =====
const filtersEl = document.getElementById('filters');

function updateVolumePillStates() {{
    document.querySelectorAll('.pill.volume-pill').forEach(p => {{
        const isAll = p.textContent === 'All Volumes';
        p.classList.toggle('active', isAll ? activeVolumes.size === 0 : activeVolumes.has(p.textContent));
    }});
}}

function updateCategoryPillStates() {{
    document.querySelectorAll('.pill.category-pill').forEach(p => {{
        const isAll = p.textContent === 'All';
        p.classList.toggle('active', isAll ? activeCategories.size === 0 : activeCategories.has(p.textContent));
    }});
}}

// Multicolor filter pill (first)
const mcPill = document.createElement('button');
mcPill.className = 'pill multicolor-pill';
mcPill.textContent = 'Multicolor Only';
mcPill.onclick = () => {{
    activeMulticolor = !activeMulticolor;
    mcPill.classList.toggle('active');
    renderGrid();
}};
filtersEl.appendChild(mcPill);

// Favorites filter pill
const favPill = document.createElement('button');
favPill.className = 'pill favorites-pill';
favPill.textContent = '★ Favorites';
favPill.onclick = () => {{
    activeFavorites = !activeFavorites;
    favPill.classList.toggle('active');
    renderGrid();
}};
filtersEl.appendChild(favPill);

// Visual separator before volume/category pills
const sepA = document.createElement('span');
sepA.style.cssText = 'width:1px;height:24px;background:var(--border);margin:0 0.3rem;';
filtersEl.appendChild(sepA);

// Volume pills (only shown when multiple volumes exist)
if (volumes.length > 1) {{
    const volAll = document.createElement('button');
    volAll.className = 'pill volume-pill active';
    volAll.textContent = 'All Volumes';
    volAll.onclick = () => {{
        activeVolumes.clear();
        updateVolumePillStates();
        renderGrid();
    }};
    filtersEl.appendChild(volAll);

    volumes.forEach(vol => {{
        const pill = document.createElement('button');
        pill.className = 'pill volume-pill';
        pill.textContent = vol;
        pill.onclick = () => {{
            if (activeVolumes.has(vol)) {{
                activeVolumes.delete(vol);
            }} else {{
                activeVolumes.add(vol);
            }}
            updateVolumePillStates();
            renderGrid();
        }};
        filtersEl.appendChild(pill);
    }});

    // Visual separator
    const sep = document.createElement('span');
    sep.style.cssText = 'width:1px;height:24px;background:var(--border);margin:0 0.3rem;';
    filtersEl.appendChild(sep);
}}

// Category pills
categories.forEach(cat => {{
    const pill = document.createElement('button');
    pill.className = 'pill category-pill' + (cat === 'All' ? ' active' : '');
    pill.textContent = cat;
    pill.onclick = () => {{
        if (cat === 'All') {{
            activeCategories.clear();
        }} else {{
            if (activeCategories.has(cat)) {{
                activeCategories.delete(cat);
            }} else {{
                activeCategories.add(cat);
            }}
        }}
        updateCategoryPillStates();
        renderGrid();
    }};
    filtersEl.appendChild(pill);
}});

// ===== SEARCH =====
document.getElementById('search').addEventListener('input', (e) => {{
    searchTerm = e.target.value.toLowerCase();
    renderGrid();
}});

// ===== RENDER GRID =====
function renderGrid() {{
    const grid = document.getElementById('grid');
    grid.innerHTML = '';

    const filtered = TOWERS.filter(t => {{
        if (activeVolumes.size > 0 && !activeVolumes.has(t.volume)) return false;
        if (activeCategories.size > 0 && !activeCategories.has(t.category)) return false;
        if (activeMulticolor && !t.has_multicolor) return false;
        if (activeFavorites && !favoriteTowers.has(t.name)) return false;
        if (searchTerm && !t.name.toLowerCase().includes(searchTerm)) return false;
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

        const imgSrc = tower.thumb ? `data:image/jpeg;base64,${{tower.thumb}}` : '';
        const imgHTML = imgSrc
            ? `<img src="${{imgSrc}}" alt="${{tower.name}}" loading="lazy">`
            : `<div style="width:100%;height:100%;display:flex;align-items:center;justify-content:center;color:var(--text-dim);">No Preview</div>`;
        const isFav = favoriteTowers.has(tower.name);

        // Build all screenshots: pre-loaded + user drops (screenshotData as array)
        const preLoaded = (tower.screenshots || []).map(s => `data:image/jpeg;base64,${{s}}`);
        const userDrops = Array.isArray(screenshotData[tower.name])
            ? screenshotData[tower.name]
            : (screenshotData[tower.name] ? [screenshotData[tower.name]] : []);
        const allScreenshots = [...preLoaded, ...userDrops];

        const stripHTML = allScreenshots.length > 0
            ? allScreenshots.map(src => `<div class="thumb"><img src="${{src}}" alt="Screenshot" loading="lazy"></div>`).join('')
            : '';
        const stripSection = allScreenshots.length > 0
            ? `<div class="screenshot-strip">${{stripHTML}}</div>`
            : '';

        card.innerHTML = `
            <div class="card-img-wrap">
                ${{imgHTML}}
                <button class="star-btn${{isFav ? ' starred' : ''}}" data-tower="${{tower.name}}" title="${{isFav ? 'Remove from favorites' : 'Add to favorites'}}">&#x2605;</button>
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
            if (files.length > 0) {{
                const existing = Array.isArray(screenshotData[tower.name]) ? screenshotData[tower.name] : (screenshotData[tower.name] ? [screenshotData[tower.name]] : []);
                let loaded = 0;
                files.forEach(file => {{
                    const reader = new FileReader();
                    reader.onload = (ev) => {{
                        existing.push(ev.target.result);
                        loaded++;
                        if (loaded === files.length) {{
                            screenshotData[tower.name] = existing;
                            window.__screenshots = screenshotData;
                            renderGrid();
                        }}
                    }};
                    reader.readAsDataURL(file);
                }});
            }}
        }});
        uploadBox.addEventListener('click', (e) => {{
            if (e.target === uploadBox) {{
                const input = document.createElement('input');
                input.type = 'file';
                input.accept = 'image/*';
                input.multiple = true;
                input.onchange = (ev) => {{
                    const files = Array.from(ev.target.files || []);
                    if (files.length > 0) {{
                        const existing = Array.isArray(screenshotData[tower.name]) ? screenshotData[tower.name] : (screenshotData[tower.name] ? [screenshotData[tower.name]] : []);
                        let loaded = 0;
                        files.forEach(file => {{
                            const reader = new FileReader();
                            reader.onload = (ev) => {{
                                existing.push(ev.target.result);
                                loaded++;
                                if (loaded === files.length) {{
                                    screenshotData[tower.name] = existing;
                                    window.__screenshots = screenshotData;
                                    renderGrid();
                                }}
                            }};
                            reader.readAsDataURL(file);
                        }});
                    }}
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
        }};
        swatchContainer.appendChild(el);
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

    print(f"\nCatalog written to: {output_path}")
    return output_path


class CatalogHandler(http.server.SimpleHTTPRequestHandler):
    """Custom handler that serves STL files and opens folders."""

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


def serve(html_path, port=7294):
    """Start local server and open browser."""
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

    towers, is_multi = scan_all(volume_path)
    print(f"Found {len(towers)} towers")

    generate_html(towers, volume_path, output_path, is_multi_volume=is_multi)

    if do_serve:
        serve(output_path)
    else:
        print("\nTo browse with 3D STL previews, run:")
        print(f"  python3 {os.path.abspath(__file__)} --serve \"{volume_path}\"")


if __name__ == '__main__':
    main()
