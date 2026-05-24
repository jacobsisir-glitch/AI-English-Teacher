"""Generate placeholder PNG slides for PPT mode testing."""
import sys
import io
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
import json
from pathlib import Path
from PIL import Image, ImageDraw, ImageFont

MANIFEST_PATH = Path(__file__).resolve().parent.parent / "frontend" / "slides" / "manifest.json"
SLIDES_DIR = Path(__file__).resolve().parent.parent / "frontend" / "slides" / "sentence_patterns"
W, H = 1280, 720

STAGE_COLORS = {
    "hook":  ("#1a1a2e", "#e94560"),  # dark navy, accent red
    "core":  ("#16213e", "#0f3460"),  # dark blue, medium blue
    "quiz":  ("#1a1a2e", "#533483"),  # dark navy, purple accent
}

FONT_SIZE_TITLE = 48
FONT_SIZE_SUBTITLE = 24
FONT_SIZE_ID = 18


def get_font(size: int):
    # Try common Chinese-friendly fonts on Windows
    candidates = ["msyh.ttc", "simhei.ttf", "simsun.ttc", "arial.ttf"]
    fonts_dir = Path("C:/Windows/Fonts")
    for name in candidates:
        fp = fonts_dir / name
        if fp.exists():
            return ImageFont.truetype(str(fp), size)
    return ImageFont.load_default()


def main():
    with open(MANIFEST_PATH, "r", encoding="utf-8") as f:
        manifest = json.load(f)

    SLIDES_DIR.mkdir(parents=True, exist_ok=True)

    font_title = get_font(FONT_SIZE_TITLE)
    font_sub = get_font(FONT_SIZE_SUBTITLE)
    font_id = get_font(FONT_SIZE_ID)

    created = []
    for slide in manifest["slides"]:
        slide_id = slide["id"]
        title = slide["title"]
        stage = slide.get("stage", "core")
        bg, accent = STAGE_COLORS.get(stage, ("#16213e", "#0f3460"))

        img = Image.new("RGB", (W, H), bg)
        draw = ImageDraw.Draw(img)

        # Accent bar at top
        draw.rectangle([0, 0, W, 6], fill=accent)

        # Slide ID at bottom-right
        draw.text(
            (W - 220, H - 50),
            f"ID: {slide_id}  |  stage: {stage}",
            fill="#888888",
            font=font_id,
        )

        # Course title subtitle
        draw.text(
            (80, H - 100),
            "AI English Grammar — 五大基本句型入门",
            fill="#666666",
            font=font_sub,
        )

        # Main title — centered large text
        bbox = draw.textbbox((0, 0), title, font=font_title)
        tw = bbox[2] - bbox[0]
        draw.text(
            ((W - tw) / 2, (H - 60) / 2),
            title,
            fill="#ffffff",
            font=font_title,
        )

        # Quiz badge
        if stage == "quiz":
            badge_text = "Q 练习 / Quiz"
            draw.text(
                (80, H - 150),
                badge_text,
                fill=accent,
                font=font_sub,
            )
            # Also show question if present
            question = slide.get("question", "")
            if question:
                draw.text(
                    (80, H - 195),
                    question,
                    fill="#aaaaaa",
                    font=font_sub,
                )

        # "core" badge
        if stage == "core":
            draw.text(
                (80, H - 150),
                "核心讲解 / Core",
                fill=accent,
                font=font_sub,
            )

        # Save
        img_path = SLIDES_DIR / f"{slide_id}.png"
        img.save(str(img_path), "PNG")
        created.append(str(img_path))
        print(f"  [OK] {img_path.name}")

    print(f"\nGenerated {len(created)} placeholder slides in {SLIDES_DIR}")


if __name__ == "__main__":
    main()
