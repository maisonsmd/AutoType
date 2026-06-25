#!/usr/bin/env python3
"""Generate AutoType.icns from PyQt6 rendering (no external image assets needed)."""
import os
import subprocess
import sys
import tempfile

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QColor, QFont, QImage, QPainter
from PyQt6.QtWidgets import QApplication

app = QApplication(sys.argv)


def make_icon(size: int) -> QImage:
    img = QImage(size, size, QImage.Format.Format_ARGB32_Premultiplied)
    img.fill(0)
    p = QPainter(img)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.setBrush(QColor("#439425"))
    p.setPen(Qt.PenStyle.NoPen)
    r = max(size // 8, 4)
    p.drawRoundedRect(0, 0, size, size, r, r)
    p.setPen(QColor("white"))
    f = QFont("Arial", int(size * 0.42), QFont.Weight.Bold)
    p.setFont(f)
    p.drawText(img.rect(), Qt.AlignmentFlag.AlignCenter, "AT")
    p.end()
    return img


ICONSET_SPECS = [
    ("icon_16x16.png", 16),
    ("icon_16x16@2x.png", 32),
    ("icon_32x32.png", 32),
    ("icon_32x32@2x.png", 64),
    ("icon_128x128.png", 128),
    ("icon_128x128@2x.png", 256),
    ("icon_256x256.png", 256),
    ("icon_256x256@2x.png", 512),
    ("icon_512x512.png", 512),
    ("icon_512x512@2x.png", 1024),
]

out_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "AutoType.icns")

with tempfile.TemporaryDirectory() as tmp:
    iconset = os.path.join(tmp, "AutoType.iconset")
    os.makedirs(iconset)
    for filename, size in ICONSET_SPECS:
        make_icon(size).save(os.path.join(iconset, filename))
    subprocess.run(
        ["iconutil", "-c", "icns", iconset, "-o", out_path],
        check=True,
    )

print(f"Generated {out_path}")
