from __future__ import annotations

import math
import random

from reportlab.lib import colors
from reportlab.pdfbase import pdfmetrics
from reportlab.platypus import Flowable

from app.utils.rendering import text


def pdf_word_cloud_flowable(word_cloud: dict, width: float):
    """Desenha no PDF a mesma linguagem visual da nuvem exibida na web."""
    words = list(word_cloud.get("words") or [])[:45]
    if not words:
        return None

    class WordCloudFlowable(Flowable):
        def __init__(self):
            super().__init__()
            self.width = float(width)
            self.height = max(205.0, self.width * 0.49)
            self._placements = self._layout_words()

        @staticmethod
        def _seed(value: str) -> int:
            hash_value = 2166136261
            for char in value:
                hash_value ^= ord(char)
                hash_value = (hash_value * 16777619) & 0xFFFFFFFF
            return hash_value

        def _font_size(self, count: float, min_count: float, max_count: float) -> float:
            if max_count <= min_count:
                return 24.0
            low, high = 9.5, 40.0
            lower = math.sqrt(max(1.0, min_count))
            upper = math.sqrt(max(1.0, max_count))
            current = math.sqrt(max(1.0, count))
            ratio = (current - lower) / max(0.0001, upper - lower)
            return low + (high - low) * max(0.0, min(1.0, ratio))

        @staticmethod
        def _intersects(box, others, padding=1.0):
            x1, y1, x2, y2 = box
            for ox1, oy1, ox2, oy2 in others:
                if not (
                    x2 + padding < ox1 or x1 - padding > ox2
                    or y2 + padding < oy1 or y1 - padding > oy2
                ):
                    return True
            return False

        def _layout_words(self):
            counts = [max(1.0, float(item.get("count") or 1)) for item in words]
            min_count, max_count = min(counts), max(counts)
            strong_palette = ["#075B9A", "#6536A5", "#247A3C", "#007C73", "#A87400"]
            soft_palette = ["#6F9FC2", "#9A83BC", "#79A783", "#6FAAA5", "#B9A36A"]
            seed_text = "|".join(f"{item.get('word', '')}:{item.get('count', 0)}" for item in words)
            rng = random.Random(self._seed(seed_text))
            placed_boxes, placements = [], []
            inner_left, inner_bottom = 12.0, 12.0
            inner_right, inner_top = self.width - 12.0, self.height - 12.0
            center_x, center_y = self.width / 2.0, self.height / 2.0

            for index, item in enumerate(words):
                label = text(item.get("word")).strip()
                if not label:
                    continue
                size = self._font_size(max(1.0, float(item.get("count") or 1)), min_count, max_count)
                rotation = 90 if index % 9 == 0 else (-90 if index % 13 == 0 else 0)
                is_top = index < 5
                color = strong_palette[index] if is_top else soft_palette[(index - 5) % len(soft_palette)]
                found = None
                trial_size = size
                for _shrink in range(8):
                    text_width = pdfmetrics.stringWidth(label, "Helvetica-Bold", trial_size)
                    text_height = trial_size * 1.05
                    box_width = text_height if rotation else text_width
                    box_height = text_width if rotation else text_height
                    phase = rng.random() * 0.7
                    for step in range(2200):
                        angle = phase + step * 0.29
                        radius = 0.48 * angle
                        x = center_x + radius * math.cos(angle)
                        y = center_y + radius * math.sin(angle) * 0.72
                        box = (x - box_width / 2.0, y - box_height / 2.0, x + box_width / 2.0, y + box_height / 2.0)
                        if box[0] < inner_left or box[1] < inner_bottom or box[2] > inner_right or box[3] > inner_top:
                            continue
                        if self._intersects(box, placed_boxes):
                            continue
                        found = (x, y, trial_size, rotation, color, is_top, label, box)
                        break
                    if found:
                        break
                    trial_size *= 0.90
                    if trial_size < 7.8:
                        break
                if found:
                    placements.append(found)
                    placed_boxes.append(found[-1])
            return placements

        def draw(self):
            canvas = self.canv
            canvas.saveState()
            canvas.setFillColor(colors.HexColor("#F8FBFD"))
            canvas.setStrokeColor(colors.HexColor("#DBE4EC"))
            canvas.setLineWidth(0.7)
            canvas.roundRect(0, 0, self.width, self.height, 10, fill=1, stroke=1)
            for x, y, size, rotation, color, _is_top, label, _box in self._placements:
                canvas.saveState()
                canvas.setFillColor(colors.HexColor(color))
                canvas.setFont("Helvetica-Bold", size)
                canvas.translate(x, y)
                if rotation:
                    canvas.rotate(rotation)
                canvas.drawCentredString(0, -size * 0.32, label)
                canvas.restoreState()
            canvas.restoreState()

    return WordCloudFlowable()
