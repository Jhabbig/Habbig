#!/usr/bin/env python3
"""Generate app icons. Run: python scripts/generate_icons.py"""
from __future__ import annotations
import shutil, subprocess, tempfile
from pathlib import Path
try:
    from PIL import Image, ImageDraw, ImageFont
except ImportError:
    print("pip install Pillow"); raise

ASSETS = Path(__file__).parent.parent / "app" / "desktop" / "assets"
SCRIPTS = Path(__file__).parent

def generate_app_icon():
    size = 512
    img = Image.new("RGBA", (size, size), "#0f1117")
    draw = ImageDraw.Draw(img)
    draw.rounded_rectangle([20, 20, size-20, size-20], radius=80, fill="#0f1117", outline="#1e2030", width=3)
    bars = [(0.35,"#4b5563"),(0.55,"#6366f1"),(0.45,"#4b5563"),(0.75,"#00ff88"),(0.60,"#3b82f6"),(0.85,"#00ff88")]
    bw, gap, total_w = 40, 20, 6*40+5*20
    sx, by = (size-total_w)//2, size-140
    for i,(h,c) in enumerate(bars):
        x = sx + i*(bw+gap)
        draw.rounded_rectangle([x, by-int(h*240), x+bw, by], radius=8, fill=c)
    draw.ellipse([380,80,420,120], fill="#00ff88"); draw.ellipse([386,86,414,114], fill="#0f1117"); draw.ellipse([394,94,406,106], fill="#00ff88")
    out = ASSETS / "icon.png"; img.save(out, "PNG"); print(f"  {out}"); return out

def generate_icns(png):
    icns = ASSETS / "icon.icns"
    with tempfile.TemporaryDirectory() as tmp:
        iconset = Path(tmp) / "icon.iconset"; iconset.mkdir()
        src = Image.open(png).convert("RGBA")
        for s in [16,32,64,128,256,512]:
            src.resize((s,s), Image.LANCZOS).save(iconset / f"icon_{s}x{s}.png")
            s2 = s*2
            if s2 <= 1024: src.resize((s2,s2), Image.LANCZOS).save(iconset / f"icon_{s}x{s}@2x.png")
        try: subprocess.run(["iconutil","-c","icns",str(iconset),"-o",str(icns)], check=True, capture_output=True); print(f"  {icns}")
        except Exception: shutil.copy(png, icns)

def generate_menubar():
    img = Image.new("RGBA", (22,22), (0,0,0,0))
    draw = ImageDraw.Draw(img)
    for x, top in [(3,8),(7,5),(11,10),(15,3)]: draw.rectangle([x,top,x+2,18], fill="white")
    draw.ellipse([18,2,21,5], fill="white")
    out = ASSETS / "menubar_icon.png"; img.save(out, "PNG"); print(f"  {out}")

def generate_dmg_bg():
    img = Image.new("RGB", (660,400), "#0f1117")
    draw = ImageDraw.Draw(img)
    for x in range(0,660,30): draw.line([(x,0),(x,400)], fill="#13151d")
    for y in range(0,400,30): draw.line([(0,y),(660,y)], fill="#13151d")
    out = SCRIPTS / "dmg_background.png"; img.save(out, "PNG"); print(f"  {out}")

if __name__ == "__main__":
    ASSETS.mkdir(parents=True, exist_ok=True)
    print("Generating icons...")
    png = generate_app_icon(); generate_icns(png); generate_menubar(); generate_dmg_bg()
    print("Done.")
