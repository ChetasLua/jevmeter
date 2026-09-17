"""Build the fictional two-candidate test video used in the README (macOS only: uses `say` for speech).

    python examples/make_mock_video.py            # writes examples/mock_debate.mp4
    jevmeter run examples/mock_debate.mp4 --transcript examples/mock_debate_transcript.txt --speakers REYES,PARK
"""
import os
import subprocess
import tempfile

from PIL import Image, ImageDraw

from jevmeter.util import ffmpeg, font

HERE = os.path.dirname(os.path.abspath(__file__))
VOICES = {"MODERATOR": "Daniel", "REYES": "Fred", "PARK": "Samantha"}


def frame(path):
    W, H = 1920, 1080
    im = Image.new("RGB", (W, H))
    d = ImageDraw.Draw(im)
    for y in range(H):
        d.line((0, y, W, y), fill=(int(20 + 30 * y / H), int(28 + 30 * y / H), int(70 + 50 * y / H)))
    for cx, name, tie in ((480, "MAYOR REYES", (170, 30, 40)), (1440, "COUNCILOR PARK", (40, 60, 150))):
        d.ellipse((cx - 150, 260, cx + 150, 560), fill=(214, 180, 150))
        d.polygon([(cx - 330, 1080), (cx - 250, 640), (cx + 250, 640), (cx + 330, 1080)], fill=(24, 28, 40))
        d.polygon([(cx - 40, 640), (cx + 40, 640), (cx + 18, 900), (cx - 18, 900)], fill=tie)
        tw = font(48, "Heavy").getbbox(name)[2]
        d.rounded_rectangle((cx - tw / 2 - 24, 930, cx + tw / 2 + 24, 1000), 10, fill=(255, 255, 255))
        d.text((cx - tw / 2, 938), name, font=font(48, "Heavy"), fill=(20, 24, 40))
    d.rectangle((954, 0, 966, H), fill=(235, 235, 240))  # broadcast-style divider, exercises the split-screen camera
    d.text((40, 30), "RIVERSIDE MAYORAL DEBATE · fictional example", font=font(34, "Bold"), fill=(255, 255, 255))
    im.save(path)


def main():
    turns = [l.split(": ", 1) for l in open(os.path.join(HERE, "mock_debate_transcript.txt")).read().strip().splitlines()]
    with tempfile.TemporaryDirectory() as tmp:
        files = []
        for i, (sp, text) in enumerate(turns):
            f = os.path.join(tmp, f"{i:02d}.aiff")
            subprocess.run(["say", "-v", VOICES.get(sp, "Daniel"), "-r", "175", "-o", f, text], check=True)
            files.append(f)
        lst = os.path.join(tmp, "list.txt")
        open(lst, "w").write("".join(f"file '{f}'\n" for f in files))
        wav, png = os.path.join(tmp, "speech.wav"), os.path.join(tmp, "frame.png")
        subprocess.run([ffmpeg(), "-loglevel", "error", "-y", "-f", "concat", "-safe", "0", "-i", lst, "-ar", "48000", "-ac", "2", wav], check=True)
        frame(png)
        out = os.path.join(HERE, "mock_debate.mp4")
        subprocess.run([ffmpeg(), "-loglevel", "error", "-y", "-loop", "1", "-framerate", "30", "-i", png, "-i", wav, "-c:v", "libx264",
                        "-tune", "stillimage", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", out], check=True)
        print("wrote", out)


if __name__ == "__main__":
    main()
