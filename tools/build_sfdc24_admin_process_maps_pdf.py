"""Build the nine administration process maps from the frozen Markdown source.

Offline, deterministic ReportLab build. The Mermaid subset is parsed and checked
against nine explicit layouts: a changed node or route fails the build until its
picture is updated. All non-diagram Markdown is rendered by the closed parser;
unknown block syntax fails instead of disappearing from the PDF.
"""
from __future__ import annotations

import hashlib
import html
import math
import os
import re
from dataclasses import dataclass
from pathlib import Path

import reportlab
from reportlab.lib import colors
from reportlab.lib.styles import ParagraphStyle
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen import canvas
from reportlab.platypus import (Flowable, PageBreak, Paragraph,
                                SimpleDocTemplate, Spacer, Table, TableStyle)


ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "docs" / "ADMIN-PROCESS-MAPS-20261007.md"
OUT = ROOT / "docs" / "SFDC24-ADMIN-PROCESS-MAPS-20261007.pdf"
SIDE = OUT.with_suffix(".pdf.sha256")
PAGE_W, PAGE_H = 432, 648  # six-by-nine portrait, comfortable on a phone
MARGIN = 25
MAP_W = PAGE_W - 2 * MARGIN
FONT_DIR = Path(reportlab.__file__).parent / "fonts"
pdfmetrics.registerFont(TTFont("Vera", str(FONT_DIR / "Vera.ttf")))
pdfmetrics.registerFont(TTFont("VeraBd", str(FONT_DIR / "VeraBd.ttf")))
pdfmetrics.registerFont(TTFont("VeraIt", str(FONT_DIR / "VeraIt.ttf")))
pdfmetrics.registerFont(TTFont("VeraBI", str(FONT_DIR / "VeraBI.ttf")))
pdfmetrics.registerFontFamily("Vera", normal="Vera", bold="VeraBd", italic="VeraIt", boldItalic="VeraBI")
INK = colors.HexColor("#17293D")
MUTED = colors.HexColor("#526176")
BLUE = colors.HexColor("#176A9B")
TEAL = colors.HexColor("#087D78")
RED = colors.HexColor("#AB4545")
LINE = colors.HexColor("#C7D3DD")
PALE = colors.HexColor("#F3F7FA")


@dataclass
class Graph:
    nodes: dict[str, str]
    edges: list[tuple[str, str, str, bool]]
    groups: dict[str, str]
    bidirectional: set[tuple[str, str]]


def parse_endpoint(raw: str, nodes: dict[str, str]) -> str:
    match = re.fullmatch(r"([A-Za-z][A-Za-z0-9_]*)(.*)", raw.strip())
    if not match:
        raise ValueError(f"Unrecognized Mermaid endpoint: {raw}")
    ident, wrapper = match.groups()
    if wrapper:
        label = wrapper.strip("[]{}()\" ").replace("<br/>", "\n")
        label = html.unescape(label)
        if not label or (ident in nodes and nodes[ident] and nodes[ident] != label):
            raise ValueError(f"Inconsistent Mermaid node {ident}")
        nodes[ident] = label
    else:
        nodes.setdefault(ident, "")
    return ident


def parse_mermaid(raw: str) -> Graph:
    lines = raw.splitlines()
    if not lines or lines[0].strip() not in ("flowchart TD", "flowchart LR"):
        raise ValueError("Expected flowchart TD or LR")
    nodes: dict[str, str] = {}
    groups: dict[str, str] = {}
    edges = []
    bidirectional = set()
    for line in lines[1:]:
        line = line.strip()
        if not line or line == "end":
            continue
        if line.startswith("subgraph "):
            match = re.fullmatch(r'subgraph\s+([A-Za-z0-9_]+)\["(.*)"\]', line)
            if not match:
                raise ValueError(f"Unrecognized subgraph: {line}")
            groups[match[1]] = match[2]
            nodes[match[1]] = match[2]
            continue
        if not re.search(r"\s(?:-->|-\.->|<-->)", line):
            parse_endpoint(line, nodes)
            continue
        match = re.fullmatch(r"(.+?)\s+(-->|-\.->|<-->)(?:\|(.+?)\|)?\s+(.+)", line)
        if not match:
            raise ValueError(f"Unrecognized Mermaid route: {line}")
        left, operator, label, right = match.groups()
        a = parse_endpoint(left, nodes)
        b = parse_endpoint(right, nodes)
        edges.append((a, b, html.unescape((label or "").strip('"').replace("<br/>", " ")), operator == "-.->"))
        if operator == "<-->":
            bidirectional.add((a, b))
    if len(edges) != len(set(edges)):
        raise ValueError("Duplicate Mermaid route")
    if any(not label for label in nodes.values()):
        raise ValueError("Mermaid route references an undefined node")
    return Graph(nodes, edges, groups, bidirectional)


# Coordinates are x, y-from-top, width, height inside the vector map.
# Group IDs in map 3 are drawn as section headers; their routes are drawn from
# the last/first member node, retaining their explicit Mermaid edge labels.
LAYOUTS = [
    {"height": 506, "nodes": {
        "A": (121, 2, 140, 29), "B": (83, 48, 216, 34),
        "C": (126, 99, 130, 29), "D": (37, 147, 308, 52),
        "E": (125, 218, 132, 29), "F": (2, 267, 105, 61),
        "G": (126, 267, 130, 61), "H": (273, 267, 107, 61),
        "I": (115, 355, 152, 42), "J": (126, 423, 130, 30),
        "K": (115, 474, 152, 30)},
     "routes": ["A>B", "B>C", "C>B", "C>D", "D>E", "E>F", "E>G", "E>H", "G>I", "F>I", "I>J", "J>I", "J>K"]},
    {"height": 330, "nodes": {
        "S": (4, 20, 112, 48), "G": (139, 20, 104, 48),
        "SH": (264, 20, 114, 55), "RD": (260, 133, 118, 51),
        "R1": (114, 255, 155, 57)},
     "routes": ["S>G", "G>SH", "S>SH", "SH>R1", "SH>RD", "RD>R1"],
     "paths": {"S>SH": [(60, 68), (60, 100), (321, 100), (321, 75)],
               "SH>R1": [(321, 75), (321, 108), (190, 108), (190, 255)]},
     "markers": {1:(126, 44), 2:(190, 100), 3:(190, 180),
                 4:(321, 105), 5:(245, 219)}},
    {"height": 530, "nodes": {
        "A1": (8, 26, 174, 33), "A2": (8, 71, 174, 33),
        "A3": (8, 116, 174, 33), "A4": (8, 161, 174, 33),
        "A5": (8, 206, 174, 33), "A6": (8, 251, 174, 40),
        "W": (143, 326, 96, 38),
        "B1": (200, 378, 174, 34), "B2": (200, 423, 174, 34),
        "B3": (200, 468, 174, 34), "B4": (200, 513, 174, 17)},
     "groups": {"IN": (8, 2), "OUT": (200, 354)},
     "routes": ["A1>A2", "A2>A3", "A3>A4", "A4>A5", "A5>A6", "IN>W", "W>OUT", "B1>B2", "B2>B3", "B3>B4"]},
    {"height": 490, "nodes": {
        "P1": (5, 38, 69, 27), "P1a": (84, 27, 103, 75),
        "P2": (5, 146, 69, 27), "P2a": (84, 126, 103, 85),
        "P3": (5, 261, 69, 35), "P3a": (84, 249, 103, 75),
        "P4": (5, 372, 69, 27), "P4a": (84, 359, 103, 75),
        "Q1": (195, 38, 65, 27), "Q1a": (269, 27, 108, 75),
        "Q2": (195, 146, 65, 39), "Q2a": (269, 126, 108, 85),
        "Q3": (195, 261, 65, 35), "Q3a": (269, 249, 108, 75),
        "Q4": (195, 372, 65, 27), "Q4a": (269, 359, 108, 75)},
     "groups": {"BOARD": (5, 3), "REDIS": (195, 3)},
     "routes": ["P1>P1a", "P2>P2a", "P3>P3a", "P4>P4a", "Q1>Q1a", "Q2>Q2a", "Q3>Q3a", "Q4>Q4a"]},
    {"height": 510, "nodes": {
        "F": (3, 204, 145, 89),
        "K1": (205, 7, 174, 61), "K2": (205, 96, 174, 61),
        "K3": (205, 185, 174, 61), "K4": (205, 274, 174, 61),
        "K5": (205, 363, 174, 61), "V": (194, 465, 185, 43)},
     "routes": ["F>K1", "F>K2", "F>K3", "F>K4", "F>K5", "K1>V", "V>F"]},
    {"height": 511, "nodes": {
        "O": (130, 3, 122, 38), "L1": (7, 92, 169, 61),
        "L2": (205, 92, 169, 45), "L3": (7, 218, 169, 45),
        "L4": (205, 218, 169, 45), "D": (139, 337, 104, 43),
        "STOP": (98, 458, 187, 50)},
     "routes": ["O>L1", "O>L2", "O>L3", "O>L4", "L1>D", "D>L1", "D>L2", "D>L3", "D>O", "L3>L1", "L1>STOP", "STOP>L2"],
     "markers": {1:(124, 72), 2:(264, 72), 3:(60, 184), 4:(322, 184),
                 5:(149, 297), 6:(231, 287), 7:(285, 304), 8:(107, 325),
                 9:(190, 182), 10:(95, 185), 11:(135, 425), 12:(319, 422)}},
    {"height": 475, "nodes": {
        "G1": (9, 29, 161, 46), "G2": (206, 29, 167, 46),
        "A1": (9, 127, 161, 53), "A2": (206, 127, 167, 53),
        "R1": (9, 238, 169, 62), "R2": (213, 238, 160, 62),
        "P1": (9, 389, 161, 60), "P2": (206, 389, 167, 60)},
     "groups": {"T1": (9, 3), "T2": (9, 101), "T3": (9, 212),
                "T4": (9, 363)},
     "routes": ["G1>A1", "G2>A2", "A1>R1", "A2>R1", "R1>R2",
                "A1>P1", "A2>P2"],
     "bidirectional": ["R1>R2"],
     "paths": {"A1>P1": [(9, 154), (2, 154), (2, 419), (9, 419)],
               "A2>P2": [(373, 154), (380, 154), (380, 419), (373, 419)]}},
    {"height": 425, "nodes": {
        "SM": (6, 12, 172, 64), "ENV": (204, 12, 172, 64),
        "J": (6, 108, 172, 52), "CLI": (204, 108, 172, 52),
        "SVC": (6, 190, 172, 52), "B": (204, 190, 172, 70),
        "RA": (6, 280, 172, 54), "GH": (204, 280, 172, 54),
        "PR": (204, 356, 172, 52)},
     "routes": ["SM>J", "SM>SVC", "ENV>CLI", "J>B", "CLI>B",
                "J>RA", "GH>PR"],
     "paths": {"SM>SVC": [(6, 44), (1, 44), (1, 216), (6, 216)],
               "J>RA": [(178, 134), (189, 134), (189, 307), (178, 307)]},
     "markers": {1:(90, 92), 2:(24, 177), 3:(290, 92)}},
    {"height": 454, "nodes": {
        "F1": (5, 14, 130, 96), "X1": (154, 14, 222, 96),
        "F2": (5, 133, 130, 77), "X2": (154, 133, 222, 77),
        "F3": (5, 230, 130, 100), "X3": (154, 230, 222, 100),
        "F4": (5, 350, 130, 82), "X4": (154, 350, 222, 82)},
     "routes": ["F1>X1", "F2>X2", "F3>X3", "F4>X4"]},
]
ROUTE_NAMES = [
    {"A":"Owner", "B":"Roster file", "C":"Roster check", "D":"Declaration",
     "E":"Redis decision", "F":"Board path", "G":"Secret grant", "H":"Refusal",
     "I":"Seed", "J":"Verify", "K":"Enrolled"},
    {"S":"Agent", "G":"Gateway", "SH":"Board", "RD":"Redis mirror", "R1":"Readers"},
    {}, {},
    {"F":"Original roster", "K1":"Instance hash", "K2":"Family hash",
     "K3":"Canonical aliases", "K4":"Member set", "K5":"Seed record", "V":"Verify"},
    {"O":"Owner", "L1":"Claude", "L2":"Gemini", "L3":"Codex",
     "L4":"Grok", "D":"Decision", "STOP":"Stop"},
    {},
    {"SM":"Secret Manager", "J":"Cloud Run jobs", "SVC":"Cloud Run services",
     "ENV":"Laptop .env", "CLI":"Local agents", "B":"Board gateway"},
    {},
]

# Pin the complete Mermaid source, including labels, arrow types, and group
# declarations. A layout change must be reviewed before a new source is drawn.
DIAGRAM_SHA256 = (
    "01f942cb9c793d377bf6fd9ed4c36d75c33a891132a2908e3feb95aca8d019c6",
    "2683bf9bb67dcf51b1249ff2533ce820c82065d85d2ab2f4bdc265f0260ce103",
    "a2a3221c09cacd154d8a5d807c6959b593bce4f3166f7f9a7c4e5464b60f4c7d",
    "6ecbddaa937368efddd139d96bfe769dca4c416c3659f849f38ded3785310f90",
    "df0ccce4063ffbc17942919c4741371ca7da7633a6788db1cce2370f3b9f1a2d",
    "ef98d889faf56a39b186ce106c83acf143d1c07aaeb1d5c52b336977b7daf7ee",
    "64b6c155fa9eb7b6cadc7ab361ae1b730ba95cc1b01bf44fdf9d4d1cb5f90711",
    "6e8c5118e4ac64bf239df20f6fd37b75d6e6aebb672e06d4d44b95c14c9a33b9",
    "0b6442433e1dc8a7018d4ab1252f03eeef2288cd1d8d252b4f7010300a955e6a",
)


def diagrams_from_source(source: str) -> tuple[str, list[Graph]]:
    blocks = re.findall(r"```mermaid\n(.*?)\n```", source, flags=re.S)
    if len(blocks) != len(LAYOUTS):
        raise ValueError(f"Expected {len(LAYOUTS)} Mermaid diagrams; found {len(blocks)}")
    if len(DIAGRAM_SHA256) != len(LAYOUTS):
        raise ValueError("Diagram fingerprint count differs from layout count")
    graphs = [parse_mermaid(block) for block in blocks]
    for i, (graph, layout) in enumerate(zip(graphs, LAYOUTS), 1):
        expected_nodes = set(layout["nodes"]) | set(layout.get("groups", {}))
        if set(graph.nodes) != expected_nodes:
            raise ValueError(f"Map {i} node drift: {set(graph.nodes) ^ expected_nodes}")
        routes = [f"{a}>{b}" for a, b, _, _ in graph.edges]
        if sorted(routes) != sorted(layout["routes"]):
            raise ValueError(f"Map {i} route drift: {routes}")
        expected_two_way = {tuple(route.split(">")) for route in layout.get("bidirectional", [])}
        if graph.bidirectional != expected_two_way:
            raise ValueError(f"Map {i} direction drift: {graph.bidirectional}")
        if hashlib.sha256(blocks[i-1].encode("utf-8")).hexdigest() != DIAGRAM_SHA256[i-1]:
            raise ValueError(f"Map {i} Mermaid text drift; update its layout and fingerprint")
    index = 0
    def replace(_: re.Match) -> str:
        nonlocal index
        index += 1
        return f"@@MAP{index}@@"
    return re.sub(r"```mermaid\n.*?\n```", replace, source, flags=re.S), graphs


def markup(value: str) -> str:
    value = html.escape(value.replace("→", "->")).replace("&lt;br&gt;", "<br/>").replace("&lt;br/&gt;", "<br/>")
    value = re.sub(r"`([^`]+)`", r'<font name="Courier">\1</font>', value)
    value = re.sub(r"\*\*([^*]+)\*\*", r"<b>\1</b>", value)
    value = re.sub(r"(?<!\*)\*([^*]+)\*(?!\*)", r"<i>\1</i>", value)
    return value


def para(value: str, size: float = 9.1, leading: float | None = None,
         color=INK, bold: bool = False) -> Paragraph:
    return Paragraph(markup(value), ParagraphStyle(
        "body", fontName="VeraBd" if bold else "Vera",
        fontSize=size, leading=leading or size * 1.38, textColor=color,
        spaceAfter=0, allowWidows=0, allowOrphans=0))


class MapPicture(Flowable):
    def __init__(self, graph: Graph, layout: dict, number: int):
        super().__init__()
        self.graph, self.layout, self.number = graph, layout, number
        self.width, self.height = MAP_W, layout["height"]

    def draw(self):
        c = self.canv
        c.setFillColor(PALE)
        c.roundRect(0, 0, self.width, self.height, 9, stroke=0, fill=1)
        if self.number == 9:
            c.setFillColor(colors.HexColor("#FBE6E4"))
            c.roundRect(2, self.height-116, self.width-4, 112, 7, stroke=0, fill=1)
        for name, (x, top) in self.layout.get("groups", {}).items():
            c.setFont("VeraBd", 8)
            c.setFillColor(TEAL)
            c.drawString(x + 3, self.height - top - 8, self.graph.groups[name].upper())
        route_number = 0
        markers = []
        for a, b, label, dashed in self.graph.edges:
            if label:
                route_number += 1
            point = self._edge(a, b, label, dashed, route_number if label else None)
            if point:
                markers.append((route_number, point))
        for ident, (x, top, w, h) in self.layout["nodes"].items():
            self._node(ident, x, top, w, h)
        for number, (cx, cy) in markers:
            c.setFillColor(colors.white)
            c.setStrokeColor(BLUE)
            c.circle(cx, cy, 6, stroke=1, fill=1)
            c.setFillColor(BLUE)
            c.setFont("VeraBd", 6.4)
            c.drawCentredString(cx, cy-2.2, str(number))

    def _center(self, ident: str) -> tuple[float, float]:
        x, top, w, h = self.layout["nodes"][ident]
        return x + w / 2, self.height - top - h / 2

    def _edge(self, a: str, b: str, label: str, dashed: bool, number: int | None):
        # Mermaid subgraph edges attach to their visible entry/exit node.
        route = f"{a}>{b}"
        a = {"IN": "A6", "OUT": "B4"}.get(a, a)
        b = {"IN": "A1", "OUT": "B1"}.get(b, b)
        x1, y1 = self._center(a)
        x2, y2 = self._center(b)
        xa, ta, wa, ha = self.layout["nodes"][a]
        xb, tb, wb, hb = self.layout["nodes"][b]
        if abs(y2 - y1) > abs(x2 - x1) * .55:
            sign = 1 if y2 > y1 else -1
            y1 += sign * ha / 2
            y2 -= sign * hb / 2
        else:
            sign = 1 if x2 > x1 else -1
            x1 += sign * wa / 2
            x2 -= sign * wb / 2
        c = self.canv
        c.saveState()
        c.setStrokeColor(RED if label.upper() in ("REFUSED", "NO-GO") else BLUE)
        c.setFillColor(RED if label.upper() in ("REFUSED", "NO-GO") else BLUE)
        c.setLineWidth(.85)
        if dashed:
            c.setDash(2, 2)
        manual = self.layout.get("paths", {}).get(route)
        points = [(px, self.height-py) for px, py in manual] if manual else [(x1,y1),(x2,y2)]
        path = c.beginPath()
        path.moveTo(*points[0])
        for point in points[1:]:
            path.lineTo(*point)
        c.drawPath(path, stroke=1, fill=0)
        x2, y2 = points[-1]
        prev_x, prev_y = points[-2]
        angle = math.atan2(y2-prev_y, x2-prev_x)
        def arrow(tip, theta):
            head = c.beginPath()
            head.moveTo(*tip)
            for delta in (2.65, -2.65):
                head.lineTo(tip[0] + 4.3*math.cos(theta+delta),
                            tip[1] + 4.3*math.sin(theta+delta))
            head.close()
            c.drawPath(head, stroke=0, fill=1)
        arrow((x2, y2), angle)
        if (a, b) in self.graph.bidirectional:
            first, second = points[0], points[1]
            arrow(first, math.atan2(first[1]-second[1], first[0]-second[0]))
        c.restoreState()
        if number is not None:
            manual_marker = self.layout.get("markers", {}).get(number)
            return ((manual_marker[0], self.height-manual_marker[1]) if manual_marker
                    else ((points[0][0]+points[-1][0])/2,
                          (points[0][1]+points[-1][1])/2))
        return None

    def _node(self, ident: str, x: float, top: float, w: float, h: float):
        c = self.canv
        y = self.height - top - h
        label = self.graph.nodes[ident]
        critical = ("REFUSED" in label.upper() or "NOT A THING" in label.upper()
                    or label.upper().startswith(("STOP.", "EVERYTHING STOPS.")))
        fill = colors.HexColor("#FFF1F1") if critical else colors.white
        c.setFillColor(fill)
        c.setStrokeColor(LINE)
        c.roundRect(x, y, w, h, 5, stroke=1, fill=1)
        c.setFillColor(TEAL)
        c.roundRect(x, y+h-3, w, 3, 2, stroke=0, fill=1)
        # Font may shrink modestly inside dense permission cards. Overflow is
        # an error, never clipped text.
        for size in (8.0, 7.6, 7.2, 6.8):
            p = para(label.replace("\n", "<br/>"), size, size*1.15)
            _, ph = p.wrap(w-10, h-7)
            if ph <= h-8:
                p.drawOn(c, x+5, y+(h-ph)/2)
                return
        raise ValueError(f"Map {self.number} node {ident} does not fit")


def parse_table(lines: list[str]) -> Table:
    rows = [[part.strip() for part in line.strip().strip("|").split("|")]
            for line in lines]
    if len(rows) < 3 or any(len(row) != len(rows[0]) for row in rows):
        raise ValueError("Malformed Markdown table")
    if not all(re.fullmatch(r":?-{3,}:?", part) for part in rows[1]):
        raise ValueError("Malformed Markdown table divider")
    data = [[para(cell, 7.6 if len(rows[0]) == 3 else 8.2, 10.3,
                  bold=(ri == 0)) for cell in row]
            for ri, row in enumerate([rows[0]] + rows[2:])]
    widths = [75, 112, MAP_W-187] if len(rows[0]) == 3 else [132, MAP_W-132]
    if len(widths) != len(rows[0]):
        raise ValueError("Unsupported table column count")
    table = Table(data, colWidths=widths, repeatRows=1, hAlign="LEFT")
    table.setStyle(TableStyle([
        ("BACKGROUND", (0,0), (-1,0), colors.HexColor("#E4EEF4")),
        ("GRID", (0,0), (-1,-1), .35, LINE),
        ("VALIGN", (0,0), (-1,-1), "TOP"),
        ("LEFTPADDING", (0,0), (-1,-1), 6),
        ("RIGHTPADDING", (0,0), (-1,-1), 6),
        ("TOPPADDING", (0,0), (-1,-1), 3),
        ("BOTTOMPADDING", (0,0), (-1,-1), 3),
    ]))
    return table


def story_from_markdown(text: str, graphs: list[Graph]) -> list:
    lines = text.splitlines()
    story = []
    i = 0
    seen_maps = set()
    while i < len(lines):
        line = lines[i].strip()
        i += 1
        if not line or line == "---":
            continue
        if line.startswith("# "):
            story.extend([para(line[2:], 17, 20, bold=True), Spacer(1, 12)])
        elif line.startswith("## "):
            number = re.match(r"## (\d+)\.", line)
            if number and 1 <= int(number[1]) <= 10:
                story.append(PageBreak())
            story.extend([para(line[3:], 13, 16, bold=True), Spacer(1, 11)])
        elif re.fullmatch(r"@@MAP[1-9]@@", line):
            n = int(line[5:-2])
            if n in seen_maps:
                raise ValueError(f"Duplicate map {n}")
            seen_maps.add(n)
            story.extend([MapPicture(graphs[n-1], LAYOUTS[n-1], n), Spacer(1, 12)])
            if n == 9:
                story.append(PageBreak())
            named = [(a, b, label, dashed) for a, b, label, dashed in graphs[n-1].edges if label]
            if named:
                names = ROUTE_NAMES[n-1]
                if any(a not in names or b not in names for a, b, _, _ in named):
                    raise ValueError(f"Map {n} route key needs a node name")
                story.extend([para("Route labels in the map", 9, bold=True), Spacer(1, 5)])
                for number, (a, b, label, dashed) in enumerate(named, 1):
                    route = f"{number}. {names[a]} → {names[b]}: {label}"
                    if dashed:
                        route += " (dashed read-back route)"
                    story.extend([para(route, 8.6, 11.6), Spacer(1, 4)])
        elif line.startswith("> "):
            block = [line[2:]]
            while i < len(lines) and lines[i].strip().startswith("> "):
                block.append(lines[i].strip()[2:])
                i += 1
            story.extend([para(" ".join(block), 8.8, 12, MUTED), Spacer(1, 9)])
        elif line.startswith("| "):
            block = [line]
            while i < len(lines) and lines[i].strip().startswith("|"):
                block.append(lines[i].strip())
                i += 1
            story.extend([parse_table(block), Spacer(1, 10)])
        elif line.startswith("- ") or re.match(r"\d+\. ", line):
            block = [line]
            while i < len(lines) and lines[i].strip() and not re.match(r"(?:## |---|@@MAP|\| )", lines[i].strip()):
                if lines[i].strip().startswith(("- ", "1. ", "2. ")):
                    break
                block.append(lines[i].strip())
                i += 1
            value = " ".join(block)
            value = re.sub(r"^(?:- |\d+\. )", "• ", value)
            story.extend([para(value), Spacer(1, 5)])
        elif line.startswith("```") or line.startswith("### "):
            raise ValueError(f"Unsupported Markdown block: {line}")
        else:
            block = [line]
            while i < len(lines) and lines[i].strip() and not re.match(r"(?:#|---|@@MAP|\| |- |\d+\. )", lines[i].strip()):
                block.append(lines[i].strip())
                i += 1
            story.extend([para(" ".join(block)), Spacer(1, 9)])
    expected_maps = set(range(1, len(LAYOUTS) + 1))
    if seen_maps != expected_maps:
        raise ValueError(f"Unrendered maps: {expected_maps - seen_maps}")
    return story


def make_canvas(*args, **kwargs):
    kwargs["invariant"] = 1
    return canvas.Canvas(*args, **kwargs)


def build() -> tuple[Path, str]:
    raw = SOURCE.read_bytes()
    source_digest = hashlib.sha256(raw).hexdigest()
    source = raw.decode("utf-8").replace("\r\n", "\n")
    # Prevent an accidental presentation of the sensitive migration gates as
    # complete if a future edit changes the prose without updating this build.
    anchors = ("**OFF**", "**HELD**", "five Codex reviews, five NO-GOs",
               "CAN delete any key", "A REQUEST cannot name one",
               "**PLANNED, unbuilt**", "**PROVEN from Aya's own surface**", "86 ms")
    missing = [word for word in anchors if word not in source]
    if missing:
        raise ValueError(f"Source evidence anchors missing: {missing}")
    text, graphs = diagrams_from_source(source)
    story = story_from_markdown(text, graphs)
    temp = OUT.with_suffix(".pdf.tmp")
    doc = SimpleDocTemplate(
        str(temp), pagesize=(PAGE_W, PAGE_H), leftMargin=MARGIN,
        rightMargin=MARGIN, topMargin=45, bottomMargin=28,
        title="SFDC24 administration process maps | 2026-10-07",
        author="Codex, from Claude's administration maps",
        subject="Nine vector administration process maps; source SHA-256 " + source_digest,
        pageCompression=1,
    )
    def page(c, d):
        c.setFont("VeraBd", 7.8)
        c.setFillColor(TEAL)
        c.drawString(MARGIN, PAGE_H-23, "SFDC24  /  ADMINISTRATION PROCESS MAPS")
        c.setStrokeColor(LINE)
        c.line(MARGIN, PAGE_H-29, PAGE_W-MARGIN, PAGE_H-29)
        c.setFont("Vera", 6.8)
        c.setFillColor(MUTED)
        c.drawString(MARGIN, 17, "Source SHA-256 " + source_digest[:16])
        c.drawRightString(PAGE_W-MARGIN, 17, str(d.page))
    try:
        doc.build(story, onFirstPage=page, onLaterPages=page, canvasmaker=make_canvas)
        os.replace(temp, OUT)
    finally:
        temp.unlink(missing_ok=True)
    digest = hashlib.sha256(OUT.read_bytes()).hexdigest()
    SIDE.write_text(f"{digest}  {OUT.name}\n", encoding="ascii")
    return OUT, digest


if __name__ == "__main__":
    path, digest = build()
    print(f"{path}\nSHA-256 {digest}")
