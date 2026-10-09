"""Make-believe stock to try Platen on: a small warehouse database, the saved
queries over it, and label and page templates bound to those queries.

    python -m app.demo                       # warehouse at ./demo-warehouse.sqlite
    python -m app.demo --warehouse /data/demo.sqlite

Everything it makes has an id starting with ``demo-`` and lives in the
``Demo`` folder, so it is easy to find and to delete. Running it again
rebuilds the warehouse and saves the drafts over themselves; a template is
only published again when its draft has changed. Every name, address and
number in it is invented.
"""

from __future__ import annotations

import argparse
import base64
import io
import sqlite3
import sys
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from db import models as db
from db import session as dbsession

from .models import Template
from .pages import PageTemplate

DATASOURCE = "demo-warehouse"
FOLDER = "Demo"
TODAY = date.today()

SENDER = ("Platen Demo Warehouse", "12 Example Street", "WENDOUREE VIC 3355")


# ------------------------------------------------------------------ the data

CUSTOMERS = [
    # code, name, address, suburb, state, postcode, has a logo
    ("BAKER", "Lal Lal Bakery Supplies", "4 Flour Mill Road", "Buninyong", "VIC", "3357", True),
    ("HARDY", "Sebastopol Hardware Co", "88 Albert Street", "Sebastopol", "VIC", "3356", True),
    ("COAST", "Torquay Surf & Outdoor", "2/17 Bell Street", "Torquay", "VIC", "3228", True),
    ("NORTH", "Bendigo Workwear", "301 High Street", "Golden Square", "VIC", "3555", False),
    ("RIVER", "Echuca Paddle Steamers Kiosk", "1 Murray Esplanade", "Echuca", "VIC", "3564", True),
    ("HILLS", "Daylesford Pantry", "56 Vincent Street", "Daylesford", "VIC", "3460", True),
    ("TASSY", "Zoë Ngô Imports", "9 Salamanca Place", "Hobart", "TAS", "7000", True),
]

PRODUCTS = [
    # sku, description, unit, weight kg, bin, price
    ("FLR-25-PLN", "Plain flour 25 kg sack", "SACK", 25.0, "A-01-02", 31.50),
    ("FLR-10-WHL", "Wholemeal flour 10 kg", "BAG", 10.0, "A-01-03", 18.90),
    ("YST-500-DRY", "Dry yeast 500 g", "PKT", 0.5, "A-02-01", 9.75),
    ("BOX-S-KRFT", "Kraft carton small 300x200x150", "EA", 0.2, "B-04-01", 1.20),
    ("BOX-L-KRFT", "Kraft carton large 600x400x400", "EA", 0.6, "B-04-02", 2.85),
    ("TPE-48-BRN", "Packing tape 48 mm brown", "ROLL", 0.3, "B-05-01", 3.40),
    ("LBL-100150", "Thermal labels 100x150, 500/roll", "ROLL", 1.1, "B-05-03", 24.00),
    ("GLV-NIT-L", "Nitrile gloves large, box of 100", "BOX", 0.6, "C-01-01", 12.95),
    ("VST-HV-XL", "Hi-vis vest XL", "EA", 0.2, "C-02-04", 8.50),
    ("BTS-STL-10", "Steel-cap boots size 10", "PR", 1.8, "C-03-01", 129.00),
    ("SNS-SPF50", "Sunscreen SPF50+ 1 L pump", "EA", 1.1, "C-04-02", 22.00),
    ("WAX-SURF-C", "Surf wax, cold water", "EA", 0.1, "D-01-01", 4.50),
    ("LEA-7FT", "Surf leash 7 ft", "EA", 0.3, "D-01-02", 39.95),
    ("TEA-BLK-1K", "Black tea leaf 1 kg", "BAG", 1.0, "A-03-01", 27.50),
    ("HNY-JAR-500", "Local honey 500 g jar", "JAR", 0.7, "A-03-04", 11.00),
]

ORDERS = [
    # order no, customer, days ago, carrier, service, status, lines [(sku, qty)]
    ("ORD-10421", "BAKER", 0, "StarTrack", "Express", "packed",
     [("FLR-25-PLN", 8), ("FLR-10-WHL", 6), ("YST-500-DRY", 12)]),
    ("ORD-10422", "HARDY", 0, "Australia Post", "Parcel Post", "packed",
     [("TPE-48-BRN", 24), ("BOX-L-KRFT", 40), ("GLV-NIT-L", 5)]),
    ("ORD-10423", "COAST", 0, "Couriers Please", "Standard", "packed",
     [("WAX-SURF-C", 60), ("LEA-7FT", 10), ("SNS-SPF50", 12)]),
    ("ORD-10424", "NORTH", 0, "StarTrack", "Premium", "packed",
     [("VST-HV-XL", 30), ("BTS-STL-10", 4), ("GLV-NIT-L", 10)]),
    ("ORD-10425", "RIVER", 1, "Australia Post", "Express Post", "despatched",
     [("TEA-BLK-1K", 6), ("HNY-JAR-500", 24)]),
    ("ORD-10426", "HILLS", 0, "StarTrack", "Express", "picking",
     [("HNY-JAR-500", 48), ("TEA-BLK-1K", 10), ("FLR-10-WHL", 4), ("YST-500-DRY", 6)]),
    ("ORD-10427", "TASSY", 0, "Toll", "Road Express", "picking",
     [("TEA-BLK-1K", 20), ("BOX-S-KRFT", 100), ("TPE-48-BRN", 12), ("LBL-100150", 4),
      ("WAX-SURF-C", 30), ("SNS-SPF50", 6)]),
]

ASSETS = [
    # tag, description, serial, location
    ("Q7-000101", "Zebra ZT411 label printer", "99J204100561", "Despatch bench 1"),
    ("Q7-000102", "Zebra ZD421 desktop printer", "D2J213904417", "Returns desk"),
    ("Q7-000117", "Honeywell CT40 handheld", "21040B4A8F", "Charging rack A"),
    ("Q7-000118", "Honeywell CT40 handheld", "21040B4B02", "Charging rack A"),
    ("Q7-000130", "Dell Latitude 5440", "7XK2QZ3", "Despatch office"),
    ("Q7-000144", "Pallet jack 2.5 t", "PJ-25-8812", "Dock 2"),
    ("Q7-000145", "Walkie stacker 1.2 t", "WS-12-0457", "Dock 1"),
    ("Q7-000152", "Avery Weigh-Tronix floor scale", "WT-600-33190", "Despatch bench 2"),
]

COMPANY_PREFIX = "9312345"          # an invented GS1 company prefix


def _check_digit(digits: str) -> str:
    """GS1 mod 10, the one GTINs and SSCCs share."""
    total = sum(int(d) * (3 if i % 2 == 0 else 1) for i, d in enumerate(reversed(digits)))
    return str((10 - total % 10) % 10)


def gtin13(n: int) -> str:
    body = f"{COMPANY_PREFIX}{n:05d}"
    return body + _check_digit(body)


def sscc(n: int) -> str:
    body = f"3{COMPANY_PREFIX}{n:09d}"
    return body + _check_digit(body)


def logo_png(text: str) -> bytes:
    """A stand-in customer logo: initials in a ring, black on white."""
    img = Image.new("L", (160, 160), 255)
    draw = ImageDraw.Draw(img)
    draw.ellipse((6, 6, 154, 154), outline=0, width=10)
    draw.ellipse((30, 30, 130, 130), fill=0)
    draw.text((80, 80), text, fill=255, anchor="mm", font_size=44)
    out = io.BytesIO()
    img.save(out, "PNG")
    return out.getvalue()


def build_warehouse(path: Path) -> None:
    if path.exists():
        path.unlink()
    con = sqlite3.connect(path)
    con.executescript("""
        create table customer (
            code text primary key, name text, address1 text, suburb text,
            state text, postcode text, logo text);
        create table product (
            sku text primary key, description text, gtin text, unit text,
            weight_kg real, bin text, price real);
        create table batch (
            sku text, batch_no text, made_on text, best_before text,
            primary key (sku, batch_no));
        create table bin (
            location text primary key, zone text, aisle text, bay text, level text);
        create table sales_order (
            order_no text primary key, customer_code text, order_date text,
            carrier text, service text, status text);
        create table order_line (
            order_no text, line_no integer, sku text, qty integer,
            primary key (order_no, line_no));
        create table carton (
            consignment_no text primary key, order_no text, carton_no integer,
            carton_count integer, weight_kg real, despatch_date text);
        create table pallet (
            sscc text primary key, order_no text, cartons integer, weight_kg real,
            despatch_date text);
        create table asset (
            asset_tag text primary key, description text, serial text,
            location text, purchased text);
    """)

    for code, name, addr, suburb, state, pc, has_logo in CUSTOMERS:
        logo = base64.b64encode(logo_png(code[:2])).decode() if has_logo else None
        con.execute("insert into customer values (?,?,?,?,?,?,?)",
                    (code, name, addr, suburb, state, pc, logo))

    for i, (sku, desc, unit, kg, location, price) in enumerate(PRODUCTS, start=1):
        con.execute("insert into product values (?,?,?,?,?,?,?)",
                    (sku, desc, gtin13(i), unit, kg, location, price))
        for b in range(2):
            made = TODAY - timedelta(days=30 * (b + 1) + i)
            con.execute("insert into batch values (?,?,?,?)",
                        (sku, f"B{made:%y%m%d}{i:02d}", made.isoformat(),
                         (made + timedelta(days=365)).isoformat()))

    for zone in "ABCD":
        for aisle in range(1, 6):
            for level in range(1, 5):
                con.execute("insert into bin values (?,?,?,?,?)",
                            (f"{zone}-{aisle:02d}-{level:02d}", zone, f"{aisle:02d}",
                             "01", f"{level:02d}"))

    weights = {p[0]: p[3] for p in PRODUCTS}
    consignment = 4200100
    pallet_no = 1
    for order_no, cust, ago, carrier, service, status, lines in ORDERS:
        day = (TODAY - timedelta(days=ago)).isoformat()
        con.execute("insert into sales_order values (?,?,?,?,?,?)",
                    (order_no, cust, day, carrier, service, status))
        for n, (sku, qty) in enumerate(lines, start=1):
            con.execute("insert into order_line values (?,?,?,?)", (order_no, n, sku, qty))
        if status == "picking":
            continue
        total_kg = sum(weights[sku] * qty for sku, qty in lines)
        cartons = max(1, min(6, round(total_kg / 20)))
        for c in range(1, cartons + 1):
            consignment += 7
            con.execute("insert into carton values (?,?,?,?,?,?)",
                        (f"PLT{consignment}", order_no, c, cartons,
                         round(total_kg / cartons, 1), day))
        if cartons >= 3:
            con.execute("insert into pallet values (?,?,?,?,?)",
                        (sscc(pallet_no), order_no, cartons, round(total_kg + 22, 1), day))
            pallet_no += 1

    for n, (tag, desc, serial, where) in enumerate(ASSETS):
        con.execute("insert into asset values (?,?,?,?,?)",
                    (tag, desc, serial, where,
                     (TODAY - timedelta(days=200 + 41 * n)).isoformat()))
    con.commit()
    con.close()


# ------------------------------------------------------------------ queries

QUERIES: list[dict[str, Any]] = [
    {
        "id": "demo-cartons", "name": "Cartons for an order",
        "sql": """
            select c.consignment_no, c.carton_no, c.carton_count, c.weight_kg,
                   c.despatch_date, o.order_no, o.carrier, o.service,
                   cu.name as consignee_name, cu.address1, cu.suburb, cu.state,
                   cu.postcode, cu.logo
            from carton c
            join sales_order o on o.order_no = c.order_no
            join customer cu on cu.code = o.customer_code
            where o.order_no = :order_no
            order by c.carton_no""",
        "parameters": [{"name": "order_no", "label": "Order number", "default": "ORD-10421"}],
    },
    {
        "id": "demo-cartons-day", "name": "Every carton going out on a day",
        "sql": """
            select c.consignment_no, c.carton_no, c.carton_count, c.weight_kg,
                   c.despatch_date, o.order_no, o.carrier, o.service,
                   cu.name as consignee_name, cu.address1, cu.suburb, cu.state,
                   cu.postcode, cu.logo
            from carton c
            join sales_order o on o.order_no = c.order_no
            join customer cu on cu.code = o.customer_code
            where c.despatch_date = :despatch_date
            order by o.order_no, c.carton_no""",
        "parameters": [{"name": "despatch_date", "label": "Despatch date (YYYY-MM-DD)",
                        "default": TODAY.isoformat()}],
    },
    {
        "id": "demo-products", "name": "Products by SKU",
        "sql": """
            select sku, description, gtin, unit, weight_kg, bin, price
            from product
            where sku like :sku_starts || '%'
            order by sku""",
        "parameters": [{"name": "sku_starts", "label": "SKU starts with (blank for all)",
                        "default": ""}],
    },
    {
        "id": "demo-batches", "name": "Batches of a product",
        "sql": """
            select b.sku, p.description, p.gtin, b.batch_no, b.made_on, b.best_before
            from batch b join product p on p.sku = b.sku
            where b.sku = :sku
            order by b.made_on desc""",
        "parameters": [{"name": "sku", "label": "SKU", "default": "HNY-JAR-500"}],
    },
    {
        "id": "demo-bins", "name": "Bin locations in a zone",
        "sql": """
            select location, zone, aisle, bay, level
            from bin where zone = :zone order by location""",
        "parameters": [{"name": "zone", "label": "Zone (A to D)", "default": "A"}],
    },
    {
        "id": "demo-pallets", "name": "Pallets going out on a day",
        "sql": """
            select p.sscc, p.cartons, p.weight_kg, p.despatch_date, o.order_no,
                   o.carrier, cu.name as consignee_name, cu.suburb, cu.state, cu.postcode
            from pallet p
            join sales_order o on o.order_no = p.order_no
            join customer cu on cu.code = o.customer_code
            where p.despatch_date = :despatch_date
            order by p.sscc""",
        "parameters": [{"name": "despatch_date", "label": "Despatch date (YYYY-MM-DD)",
                        "default": TODAY.isoformat()}],
    },
    {
        "id": "demo-assets", "name": "Asset register",
        "sql": """
            select asset_tag, description, serial, location, purchased
            from asset order by asset_tag""",
        "parameters": [],
    },
    {
        "id": "demo-order-lines", "name": "Order lines with status",
        "sql": """
            select o.order_no, o.order_date, o.carrier, o.service, o.status,
                   cu.name as customer_name, cu.address1, cu.suburb, cu.state, cu.postcode,
                   l.line_no, l.sku, p.description, p.unit, p.bin, l.qty, p.price,
                   round(l.qty * p.price, 2) as line_total
            from sales_order o
            join customer cu on cu.code = o.customer_code
            join order_line l on l.order_no = o.order_no
            join product p on p.sku = l.sku
            where o.status = :status
            order by o.order_no, l.line_no""",
        "parameters": [{"name": "status", "label": "Status (picking, packed, despatched)",
                        "default": "packed"}],
    },
    {
        "id": "demo-pick-lines", "name": "Lines to pick, by bin",
        "sql": """
            select p.bin, l.sku, p.description, p.unit, l.qty, o.order_no,
                   cu.name as customer_name
            from order_line l
            join sales_order o on o.order_no = l.order_no
            join customer cu on cu.code = o.customer_code
            join product p on p.sku = l.sku
            where o.status = 'picking'
            order by p.bin, o.order_no""",
        "parameters": [],
    },
]


# ------------------------------------------------------------------ templates

def _text(name: str, value: str, x: float, y: float, w: float, h: float,
          pt: float, **kw: Any) -> dict[str, Any]:
    return {"kind": "text", "name": name, "value": value,
            "box": {"x": x, "y": y, "w": w, "h": h}, "height_pt": pt, **kw}


def _line(name: str, x: float, y: float, w: float, h: float = 0.0,
          thickness: float = 0.5) -> dict[str, Any]:
    return {"kind": "line", "name": name, "box": {"x": x, "y": y, "w": w, "h": h},
            "thickness_mm": thickness}


LABELS: list[dict[str, Any]] = [
    {
        "id": "demo-shipping", "name": "Demo shipping label 100×150",
        "width_mm": 100, "height_mm": 150, "query": "demo-cartons",
        "elements": [
            {"kind": "image", "name": "customer logo", "value": "{{ logo }}",
             "box": {"x": 4, "y": 4, "w": 20, "h": 20}, "on_missing": "blank"},
            _text("from heading", "FROM", 28, 4, 68, 4, 7),
            _text("sender", SENDER[0], 28, 8, 68, 5, 10),
            _text("sender address", f"{SENDER[1]}, {SENDER[2]}", 28, 13, 68, 4, 8),
            _text("carrier", "{{ carrier | upper }} {{ service | upper }}",
                  28, 18, 68, 5, 11),
            _line("rule 1", 4, 26, 92),
            _text("to heading", "TO", 4, 29, 20, 4, 8),
            _text("consignee", "{{ consignee_name }}", 4, 34, 92, 7, 16),
            _text("address", "{{ address1 }}", 4, 42, 92, 6, 13),
            _text("suburb", "{{ suburb | upper }} {{ state }} {{ postcode }}",
                  4, 49, 92, 8, 20),
            _line("rule 2", 4, 60, 92),
            _text("order", "Order {{ order_no }}", 4, 63, 50, 5, 11),
            _text("carton", "CARTON {{ carton_no }} OF {{ carton_count }}",
                  50, 63, 46, 6, 14, align="right"),
            _text("weight", "{{ weight_kg | round:1 }} kg", 4, 70, 50, 5, 11),
            _text("despatched", "Despatched {{ despatch_date }}", 50, 70, 46, 5, 9,
                  align="right"),
            _line("rule 3", 4, 77, 92),
            {"kind": "barcode", "name": "consignment barcode", "value": "{{ consignment_no }}",
             "symbology": "code128", "module_dots": 3,
             "box": {"x": 8, "y": 82, "w": 84, "h": 28}},
            _line("rule 4", 4, 120, 92),
            {"kind": "qr", "name": "tracking qr",
             "value": "https://track.example.com/{{ consignment_no }}", "magnification": 4,
             "box": {"x": 4, "y": 124, "w": 22, "h": 22}},
            _text("tracking note", "Scan to track this carton", 30, 128, 66, 5, 9),
            _text("tracking no", "{{ consignment_no }}", 30, 134, 66, 7, 16),
        ],
    },
    {
        "id": "demo-product", "name": "Demo product label 50×25",
        "width_mm": 50, "height_mm": 25, "query": "demo-products",
        "elements": [
            _text("description", "{{ description }}", 2, 1.5, 46, 4, 8),
            _text("sku", "{{ sku }}", 2, 6, 30, 4, 10),
            _text("price", "${{ price | round:2 }}", 30, 6, 18, 4, 10, align="right"),
            {"kind": "barcode", "name": "gtin barcode", "value": "{{ gtin }}",
             "symbology": "code128", "module_dots": 2,
             "box": {"x": 3, "y": 11, "w": 44, "h": 8}},
        ],
    },
    {
        "id": "demo-batch", "name": "Demo batch label 50×30 (Data Matrix)",
        "width_mm": 50, "height_mm": 30, "query": "demo-batches",
        "elements": [
            {"kind": "datamatrix", "name": "gs1 data matrix",
             "value": "01{{ gtin | pad:14 }}10{{ batch_no }}", "module_dots": 5,
             "box": {"x": 2, "y": 3, "w": 16, "h": 16}},
            _text("description", "{{ description }}", 21, 2, 27, 8, 7, multiline=True),
            _text("batch", "Batch {{ batch_no }}", 21, 11, 27, 4, 8),
            _text("made", "Made {{ made_on }}", 21, 16, 27, 4, 7),
            _text("best before", "BEST BEFORE {{ best_before }}", 2, 22, 46, 5, 11),
        ],
    },
    {
        "id": "demo-bin", "name": "Demo bin location 100×50",
        "width_mm": 100, "height_mm": 50, "query": "demo-bins",
        "elements": [
            _text("zone", "ZONE {{ zone }}", 4, 3, 40, 5, 10),
            _text("location", "{{ location }}", 4, 9, 92, 14, 40, align="centre"),
            {"kind": "barcode", "name": "location barcode", "value": "{{ location }}",
             "symbology": "code128", "module_dots": 3, "human_readable": "none",
             "box": {"x": 10, "y": 28, "w": 80, "h": 16}},
            _text("detail", "Aisle {{ aisle }} · Bay {{ bay }} · Level {{ level }}",
                  4, 45, 92, 4, 8, align="centre"),
        ],
    },
    {
        "id": "demo-asset", "name": "Demo asset tag 50×25",
        "width_mm": 50, "height_mm": 25, "query": "demo-assets",
        "elements": [
            {"kind": "box", "name": "border", "box": {"x": 1, "y": 1, "w": 48, "h": 23},
             "thickness_mm": 0.4},
            {"kind": "qr", "name": "asset qr", "value": "{{ asset_tag }}", "magnification": 3,
             "box": {"x": 3, "y": 4, "w": 16, "h": 16}},
            _text("owner", "PLATEN DEMO ASSET", 21, 3, 27, 3, 6),
            _text("tag", "{{ asset_tag }}", 21, 7, 27, 5, 12),
            _text("description", "{{ description }}", 21, 13, 27, 6, 6, multiline=True),
            _text("serial", "S/N {{ serial }}", 21, 20, 27, 3, 6),
        ],
    },
    {
        "id": "demo-pallet", "name": "Demo pallet SSCC 100×150 (GS1-128)",
        "width_mm": 100, "height_mm": 150, "query": "demo-pallets",
        "elements": [
            _text("sender", SENDER[0], 4, 4, 92, 5, 10),
            _text("heading", "SSCC PALLET LABEL", 4, 10, 92, 6, 14),
            _line("rule 1", 4, 18, 92),
            _text("sscc caption", "SSCC", 4, 21, 30, 4, 8),
            _text("sscc", "{{ sscc }}", 4, 26, 92, 7, 18),
            _text("consignee", "{{ consignee_name }}", 4, 36, 92, 6, 13),
            _text("destination", "{{ suburb | upper }} {{ state }} {{ postcode }}",
                  4, 43, 92, 6, 13),
            _text("order", "Order {{ order_no }} · {{ carrier }}", 4, 51, 92, 5, 10),
            _text("contents", "{{ cartons }} cartons · {{ weight_kg | round:1 }} kg gross",
                  4, 57, 92, 5, 10),
            _line("rule 2", 4, 65, 92),
            {"kind": "barcode", "name": "sscc barcode", "value": "00{{ sscc }}",
             "symbology": "gs1_128", "module_dots": 3, "human_readable": "none",
             "box": {"x": 6, "y": 72, "w": 88, "h": 32}},
            _text("sscc under bars", "(00) {{ sscc }}", 6, 107, 88, 6, 13),
            _line("rule 3", 4, 118, 92),
            _text("despatched", "Despatched {{ despatch_date }}", 4, 122, 92, 5, 10),
        ],
    },
]

PAGES: list[dict[str, Any]] = [
    {
        "id": "demo-packing-slip", "name": "Demo packing slip (A4)",
        "query": "demo-order-lines", "group_by": "order_no",
        "header": [
            {"kind": "text", "value": SENDER[0], "size_pt": 14, "bold": True},
            {"kind": "text", "value": f"{SENDER[1]}, {SENDER[2]}", "size_pt": 9},
            {"kind": "rule"},
        ],
        "body": [
            {"kind": "text", "value": "Packing slip {{ order_no }}", "size_pt": 18,
             "bold": True},
            {"kind": "space", "height_mm": 2},
            {"kind": "barcode", "value": "{{ order_no }}", "height_mm": 10},
            {"kind": "space", "height_mm": 4},
            {"kind": "text", "value": "Deliver to", "size_pt": 8, "bold": True},
            {"kind": "text", "value": "{{ customer_name }}", "size_pt": 12},
            {"kind": "text", "value": "{{ address1 }}"},
            {"kind": "text", "value": "{{ suburb | upper }} {{ state }} {{ postcode }}"},
            {"kind": "space", "height_mm": 4},
            {"kind": "text",
             "value": "Ordered {{ order_date }} · {{ carrier }} {{ service }}", "size_pt": 9},
            {"kind": "space", "height_mm": 4},
            {"kind": "table", "columns": [
                {"title": "Line", "value": "{{ line_no }}", "width_mm": 12},
                {"title": "SKU", "value": "{{ sku }}", "width_mm": 32},
                {"title": "Description", "value": "{{ description }}"},
                {"title": "Unit", "value": "{{ unit }}", "width_mm": 16},
                {"title": "Qty", "value": "{{ qty }}", "width_mm": 14, "align": "right"},
                {"title": "Total", "value": "${{ line_total | round:2 }}", "width_mm": 24,
                 "align": "right"},
            ]},
            {"kind": "space", "height_mm": 8},
            {"kind": "text", "value": "Checked by ____________________", "size_pt": 9},
        ],
        "footer": [
            {"kind": "rule"},
            {"kind": "text", "value": "{{ order_no }} · page {{ page }} of {{ pages }} · "
                                      "made-up data from python -m app.demo",
             "size_pt": 7, "align": "centre"},
        ],
    },
    {
        "id": "demo-pick-list", "name": "Demo pick list, by bin (A4)",
        "query": "demo-pick-lines",
        "header": [
            {"kind": "text", "value": "Pick list", "size_pt": 16, "bold": True},
            {"kind": "text", "value": "Every order still picking, walked in bin order",
             "size_pt": 9},
            {"kind": "rule"},
        ],
        "body": [
            {"kind": "table", "size_pt": 10, "columns": [
                {"title": "Bin", "value": "{{ bin }}", "width_mm": 22},
                {"title": "SKU", "value": "{{ sku }}", "width_mm": 30},
                {"title": "Description", "value": "{{ description }}"},
                {"title": "Qty", "value": "{{ qty }} {{ unit }}", "width_mm": 22,
                 "align": "right"},
                {"title": "Order", "value": "{{ order_no }}", "width_mm": 24},
                {"title": "Picked", "value": "", "width_mm": 16},
            ]},
        ],
        "footer": [
            {"kind": "text", "value": "Page {{ page }} of {{ pages }}", "size_pt": 7,
             "align": "right"},
        ],
    },
]


# ------------------------------------------------------------------ saving

def _publish_if_changed(s: Session, row: db.Template, definition: dict[str, Any]) -> int:
    latest = row.versions[-1] if row.versions else None
    if latest is not None:
        frozen = {**latest.definition, "version": 0}
        if frozen == {**definition, "version": 0}:
            return latest.version
    version = (latest.version + 1) if latest else 1
    s.add(db.TemplateVersion(template=row, version=version,
                             definition={**definition, "version": version}))
    s.add(db.AuditLog(actor="app.demo", action="publish", entity="template",
                      entity_id=row.id, detail={"version": version}))
    return version


def seed(s: Session, warehouse: Path) -> list[str]:
    said: list[str] = []
    ds = s.get(db.DataSource, DATASOURCE) or db.DataSource(id=DATASOURCE)
    # read-only at the file, so the demo keeps to invariant 2 like a real source
    ds.name, ds.label, ds.kind = "Demo warehouse", "Made-up stock, orders and cartons", "sql"
    ds.url = f"sqlite:///file:{warehouse.resolve()}?mode=ro&uri=true"
    ds.headers, ds.pool_size = {}, 2
    s.add(ds)
    s.flush()

    for q in QUERIES:
        row = s.get(db.SavedQuery, q["id"]) or db.SavedQuery(id=q["id"])
        row.datasource_id, row.name, row.row_path = DATASOURCE, q["name"], ""
        row.sql = "\n".join(line[12:] if line.startswith(" " * 12) else line
                            for line in q["sql"].strip("\n").splitlines()).strip()
        row.parameters.clear()
        s.flush()
        row.parameters.extend(
            db.QueryParameter(name=p["name"], type="text", label=p["label"], position=i,
                              default=p["default"], ask_at_print=True)
            for i, p in enumerate(q["parameters"]))
        s.add(row)
    s.flush()
    said.append(f"data source {DATASOURCE} with {len(QUERIES)} saved queries")

    for spec in LABELS:
        t = Template.model_validate({**spec, "folder": FOLDER, "datasource": DATASOURCE,
                                     "dpi": 203})
        row = s.get(db.Template, t.id) or db.Template(id=t.id, kind="label")
        row.name, row.folder, row.kind, row.page = t.name, t.folder, "label", None
        row.width_mm, row.height_mm, row.dpi, row.darkness = (
            t.width_mm, t.height_mm, t.dpi, t.darkness)
        row.datasource_id, row.query_id = t.datasource, t.query
        row.elements = [e.model_dump(mode="json") for e in t.elements]
        s.add(row)
        s.flush()
        v = _publish_if_changed(s, row, t.model_dump(mode="json"))
        said.append(f"label {t.id} v{v}")

    for spec in PAGES:
        p = PageTemplate.model_validate({**spec, "folder": FOLDER, "datasource": DATASOURCE})
        row = s.get(db.Template, p.id) or db.Template(id=p.id, kind="page", dpi=203)
        row.name, row.folder, row.kind, row.elements = p.name, p.folder, "page", []
        row.datasource_id, row.query_id = p.datasource, p.query
        row.width_mm, row.height_mm = p.size_mm
        row.page = p.model_dump(mode="json", exclude={"id", "name", "version", "folder",
                                                      "datasource", "query"})
        s.add(row)
        s.flush()
        v = _publish_if_changed(s, row, p.model_dump(mode="json"))
        said.append(f"page {p.id} v{v}")

    s.commit()
    return said


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.demo")
    parser.add_argument("--warehouse", default="demo-warehouse.sqlite", type=Path,
                        help="where to write the made-up warehouse database "
                             "(the API has to be able to read it at this path)")
    args = parser.parse_args(argv)

    try:
        build_warehouse(args.warehouse)
    except OSError as exc:
        print(f"could not write the demo warehouse at {args.warehouse}: {exc}",
              file=sys.stderr)
        return 1
    print(f"wrote {args.warehouse.resolve()}")

    try:
        dbsession.engine()
        with dbsession.SessionLocal() as s:
            for line in seed(s, args.warehouse):
                print(line)
    except OperationalError:
        # never the URL itself: it carries the password (invariant 17)
        url = dbsession.engine().url
        print(f"could not reach Platen's own database at {url.host or url.database}:"
              f"{url.port or ''} — set DATABASE_URL to the one the API uses. For the "
              "compose stack, run this inside it instead:\n"
              "  docker compose exec api python -m app.demo --warehouse /tmp/demo.sqlite",
              file=sys.stderr)
        return 1
    print(f"everything is in the {FOLDER!r} folder; ids start with 'demo-'")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
