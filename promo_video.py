"""
Winkly Promotional Video Generator
Creates a 30-second vertical (1080x1920) promo video for social media.
"""
import os
import math
import numpy as np
from PIL import Image, ImageDraw, ImageFont
from moviepy import (
    ImageClip, CompositeVideoClip, TextClip, 
    concatenate_videoclips, ColorClip,
    vfx
)

# ─── Config ──────────────────────────────────────────────────────────────────
W, H = 1080, 1920  # 9:16 vertical (Reels/TikTok/YouTube Shorts)
FPS = 30
OUTPUT = r"C:\Users\suraj\winkly_bot\promo_video.mp4"

# Colors
BG_DARK = (10, 10, 26)       # Deep navy
BG_MID = (20, 15, 40)        # Slightly lighter
PINK = (255, 45, 85)         # Dating pink
PURPLE = (123, 47, 247)      # Accent purple
WHITE = (255, 255, 255)
LIGHT_GRAY = (200, 200, 210)
SOFT_WHITE = (240, 240, 245)
HEART_RED = (255, 50, 80)

# Fonts
FONT_BOLD = r"C:\Windows\Fonts\segoeuib.ttf"
FONT_REG = r"C:\Windows\Fonts\segoeui.ttf"
FONT_LIGHT = r"C:\Windows\Fonts\calibri.ttf"

# ─── Helpers ─────────────────────────────────────────────────────────────────

def make_gradient_bg(w, h, color_top, color_bottom):
    """Create a vertical gradient background."""
    img = Image.new('RGB', (w, h))
    draw = ImageDraw.Draw(img)
    for y in range(h):
        ratio = y / h
        r = int(color_top[0] * (1 - ratio) + color_bottom[0] * ratio)
        g = int(color_top[1] * (1 - ratio) + color_bottom[1] * ratio)
        b = int(color_top[2] * (1 - ratio) + color_bottom[2] * ratio)
        draw.line([(0, y), (w, y)], fill=(r, g, b))
    return img

def make_frame_with_text(bg_img, lines, y_start=400, font_size=48, color=WHITE, line_spacing=80, align='center', emoji_size=60):
    """Draw multi-line text on a background image."""
    img = bg_img.copy()
    draw = ImageDraw.Draw(img)
    y = y_start
    
    for line in lines:
        if not line.strip():
            y += line_spacing // 2
            continue
        
        # Check for emoji/colored text markers
        is_emoji_line = line.startswith('emoji:')
        is_accent = line.startswith('accent:')
        is_small = line.startswith('small:')
        
        if is_emoji_line:
            line = line[6:]
            fs = emoji_size
            fc = PINK
            fn = FONT_BOLD
        elif is_accent:
            line = line[7:]
            fs = font_size - 4
            fc = PINK
            fn = FONT_BOLD
        elif is_small:
            line = line[6:]
            fs = font_size - 16
            fc = LIGHT_GRAY
            fn = FONT_REG
        else:
            fs = font_size
            fc = color
            fn = FONT_BOLD
        
        font = ImageFont.truetype(fn, fs)
        bbox = draw.textbbox((0, 0), line, font=font)
        tw = bbox[2] - bbox[0]
        
        if align == 'center':
            x = (W - tw) // 2
        else:
            x = 80
        
        draw.text((x, y), line, fill=fc, font=font)
        y += line_spacing
    
    return img

def make_heart_icon(size=120, color=HEART_RED):
    """Create a simple heart icon using Pillow."""
    img = Image.new('RGBA', (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    
    # Draw a heart shape using two circles and a triangle
    cx, cy = size // 2, size // 2
    r = size // 4
    
    # Top two circles
    draw.ellipse([cx - r - 5, cy - r - 10, cx + 5, cy + r - 10], fill=color + (255,))
    draw.ellipse([cx - 5, cy - r - 10, cx + r + 5, cy + r - 10], fill=color + (255,))
    
    # Bottom triangle
    draw.polygon([
        (cx - r - 5, cy),
        (cx + r + 5, cy),
        (cx, cy + r + 15)
    ], fill=color + (255,))
    
    return img

def make_icon_circle(emoji_text, size=100, bg_color=PINK):
    """Create a circular icon with emoji-like text."""
    img = Image.new('RGBA', (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    
    # Draw circle background
    draw.ellipse([0, 0, size - 1, size - 1], fill=bg_color + (200,))
    
    # Draw text in center
    font = ImageFont.truetype(FONT_BOLD, size // 3)
    bbox = draw.textbbox((0, 0), emoji_text, font=font)
    tw = bbox[2] - bbox[0]
    th = bbox[3] - bbox[1]
    x = (size - tw) // 2
    y = (size - th) // 2
    draw.text((x, y), emoji_text, fill=WHITE + (255,), font=font)
    
    return img

# ─── Create Video ────────────────────────────────────────────────────────────

def create_video():
    print("Creating Winkly promotional video...")
    print(f"  Resolution: {W}x{H}")
    print(f"  FPS: {FPS}")
    
    # Create gradient backgrounds
    bg_dark = make_gradient_bg(W, H, BG_DARK, (15, 10, 35))
    bg_accent = make_gradient_bg(W, H, (30, 15, 50), BG_DARK)
    
    clips = []
    
    # ─── Scene 1: Title Card (0-3s) ─────────────────────────────────────────
    print("  Scene 1: Title card...")
    frame1 = bg_dark.copy()
    draw1 = ImageDraw.Draw(frame1)
    
    # Draw large heart
    heart = make_heart_icon(200, HEART_RED)
    frame1.paste(heart, (W // 2 - 100, 600), heart)
    
    # Draw "Winkly" title
    font_title = ImageFont.truetype(FONT_BOLD, 120)
    bbox = draw1.textbbox((0, 0), "Winkly", font=font_title)
    tw = bbox[2] - bbox[0]
    draw1.text(((W - tw) // 2, 850), "Winkly", fill=WHITE, font=font_title)
    
    # Draw tagline
    font_tag = ImageFont.truetype(FONT_REG, 36)
    tagline = "Meet genuine people nearby"
    bbox = draw1.textbbox((0, 0), tagline, font=font_tag)
    tw = bbox[2] - bbox[0]
    draw1.text(((W - tw) // 2, 1000), tagline, fill=LIGHT_GRAY, font=font_tag)
    
    # Draw decorative line
    draw1.line([(W // 2 - 100, 1080), (W // 2 + 100, 1080)], fill=PINK, width=3)
    
    clip1 = ImageClip(np.array(frame1)).with_duration(3)
    clip1 = clip1.with_effects([vfx.CrossFadeIn(0.5)])
    clips.append(clip1)
    
    # ─── Scene 2: Problem Statement (3-6s) ──────────────────────────────────
    print("  Scene 2: Problem statement...")
    frame2 = bg_accent.copy()
    frame2 = make_frame_with_text(
        frame2, [
            "Stop endless swiping.",
            "",
            "accent:Start real conversations.",
            "",
            "small:Winkly helps you discover",
            "small:genuine people nearby.",
        ],
        y_start=650, font_size=52, line_spacing=90
    )
    
    clip2 = ImageClip(np.array(frame2)).with_duration(3)
    clip2 = clip2.with_effects([vfx.CrossFadeIn(0.5)])
    clips.append(clip2)
    
    # ─── Scene 3: Feature - Verified (6-10s) ────────────────────────────────
    print("  Scene 3: Verified feature...")
    frame3 = bg_dark.copy()
    draw3 = ImageDraw.Draw(frame3)
    
    # Shield icon
    shield = make_icon_circle("V", 120, (0, 180, 100))
    frame3.paste(shield, (W // 2 - 60, 500), shield)
    
    frame3 = make_frame_with_text(
        frame3, [
            "",
            "accent:Verified through Telegram",
            "",
            "small:Your Telegram account helps",
            "small:confirm you're a real user.",
            "small:No selfie uploads required.",
        ],
        y_start=680, font_size=48, line_spacing=80
    )
    
    clip3 = ImageClip(np.array(frame3)).with_duration(4)
    clip3 = clip3.with_effects([vfx.CrossFadeIn(0.5)])
    clips.append(clip3)
    
    # ─── Scene 4: Feature - Nearby (10-14s) ─────────────────────────────────
    print("  Scene 4: Nearby feature...")
    frame4 = bg_accent.copy()
    draw4 = ImageDraw.Draw(frame4)
    
    # Location icon
    loc = make_icon_circle("L", 120, PINK)
    frame4.paste(loc, (W // 2 - 60, 500), loc)
    
    frame4 = make_frame_with_text(
        frame4, [
            "",
            "accent:Meet people nearby",
            "",
            "small:Discover people around you",
            "small:based on your preferences.",
            "small:Not random profiles far away.",
        ],
        y_start=680, font_size=48, line_spacing=80
    )
    
    clip4 = ImageClip(np.array(frame4)).with_duration(4)
    clip4 = clip4.with_effects([vfx.CrossFadeIn(0.5)])
    clips.append(clip4)
    
    # ─── Scene 5: Feature - Quick (14-18s) ──────────────────────────────────
    print("  Scene 5: Quick setup...")
    frame5 = bg_dark.copy()
    draw5 = ImageDraw.Draw(frame5)
    
    # Lightning icon
    lightning = make_icon_circle("!", 120, PURPLE)
    frame5.paste(lightning, (W // 2 - 60, 500), lightning)
    
    frame5 = make_frame_with_text(
        frame5, [
            "",
            "accent:Ready in under a minute",
            "",
            "small:Create your profile quickly",
            "small:and start meeting people.",
            "small:No app download needed.",
        ],
        y_start=680, font_size=48, line_spacing=80
    )
    
    clip5 = ImageClip(frame5).with_duration(4)
    clip5 = clip5.with_effects([vfx.CrossFadeIn(0.5)])
    clips.append(clip5)
    
    # ─── Scene 6: Privacy (18-22s) ──────────────────────────────────────────
    print("  Scene 6: Privacy...")
    frame6 = bg_accent.copy()
    draw6 = ImageDraw.Draw(frame6)
    
    # Lock icon
    lock = make_icon_circle("P", 120, (0, 150, 255))
    frame6.paste(lock, (W // 2 - 60, 500), lock)
    
    frame6 = make_frame_with_text(
        frame6, [
            "",
            "accent:Your privacy, your choice",
            "",
            "small:Your identity isn't revealed",
            "small:automatically. You decide what",
            "small:to share and when.",
        ],
        y_start=680, font_size=48, line_spacing=80
    )
    
    clip6 = ImageClip(frame6).with_duration(4)
    clip6 = clip6.with_effects([vfx.CrossFadeIn(0.5)])
    clips.append(clip6)
    
    # ─── Scene 7: CTA (22-27s) ──────────────────────────────────────────────
    print("  Scene 7: Call to action...")
    frame7 = bg_dark.copy()
    draw7 = ImageDraw.Draw(frame7)
    
    # Large heart
    heart2 = make_heart_icon(160, HEART_RED)
    frame7.paste(heart2, (W // 2 - 80, 550), heart2)
    
    # CTA text
    font_cta = ImageFont.truetype(FONT_BOLD, 56)
    lines = [
        "Find your match",
        "on Winkly",
    ]
    y = 750
    for line in lines:
        bbox = draw7.textbbox((0, 0), line, font=font_cta)
        tw = bbox[2] - bbox[0]
        draw7.text(((W - tw) // 2, y), line, fill=WHITE, font=font_cta)
        y += 80
    
    # Bot URL
    font_url = ImageFont.truetype(FONT_REG, 32)
    url = "t.me/Winkly_dating_bot"
    bbox = draw7.textbbox((0, 0), url, font=font_url)
    tw = bbox[2] - bbox[0]
    draw7.text(((W - tw) // 2, 1000), url, fill=PINK, font=font_url)
    
    # Decorative line
    draw7.line([(W // 2 - 120, 1060), (W // 2 + 120, 1060)], fill=PINK, width=2)
    
    # Subtitle
    font_sub = ImageFont.truetype(FONT_REG, 28)
    sub = "Simple. Private. Genuine."
    bbox = draw7.textbbox((0, 0), sub, font=font_sub)
    tw = bbox[2] - bbox[0]
    draw7.text(((W - tw) // 2, 1100), sub, fill=LIGHT_GRAY, font=font_sub)
    
    clip7 = ImageClip(frame7).with_duration(5)
    clip7 = clip7.with_effects([vfx.CrossFadeIn(0.5)])
    clips.append(clip7)
    
    # ─── Assemble ────────────────────────────────────────────────────────────
    print("  Assembling video...")
    final = concatenate_videoclips(clips, method="compose")
    final = final.with_duration(sum(c.duration for c in clips))
    
    print(f"  Total duration: {final.duration:.1f}s")
    print(f"  Writing to: {OUTPUT}")
    
    final.write_videofile(
        OUTPUT,
        fps=FPS,
        codec='libx264',
        audio=False,
        preset='medium',
        threads=4,
        logger='bar'
    )
    
    print(f"\n  Video saved to: {OUTPUT}")
    print(f"  File size: {os.path.getsize(OUTPUT) / 1024 / 1024:.1f} MB")
    return OUTPUT

if __name__ == '__main__':
    create_video()
