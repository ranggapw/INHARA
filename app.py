from __future__ import annotations

import csv
import io
import os
import sqlite3
from base64 import b64encode
from collections import Counter
from contextlib import contextmanager
from collections.abc import Iterator
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Iterable

import altair as alt
import streamlit as st
from PIL import Image


DB_PATH = Path(
    os.environ.get(
        "INHARA_DB_PATH", Path(__file__).with_name("inhara_camp.db")
    )
)
LOGO_PATH = Path(__file__).with_name("Logo Inhara.png")
FINE_RATE_PERCENT = 20


def logo_data_uri() -> str:
    with Image.open(LOGO_PATH) as image:
        image = image.convert("RGBA")
        bounds = image.getchannel("A").getbbox()
        if bounds is None:
            raise ValueError(f"Logo tidak memiliki gambar yang terlihat: {LOGO_PATH}")
        cropped = image.crop(bounds)
        image_buffer = io.BytesIO()
        cropped.save(image_buffer, format="PNG")
    encoded = b64encode(image_buffer.getvalue()).decode("ascii")
    return f"data:image/png;base64,{encoded}"


@contextmanager
def connect_db() -> Iterator[sqlite3.Connection]:
    connection = sqlite3.connect(DB_PATH, timeout=10)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    try:
        yield connection
        if connection.in_transaction:
            connection.commit()
    finally:
        connection.close()


def next_business_code(
    connection: sqlite3.Connection, table: str, prefix: str, width: int = 3
) -> str:
    sequence = connection.execute(
        "SELECT seq FROM sqlite_sequence WHERE name = ?", (table,)
    ).fetchone()
    next_id = (sequence["seq"] if sequence else 0) + 1
    return f"{prefix}{next_id:0{width}d}"


def init_db() -> None:
    with connect_db() as connection:
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS categories (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                code TEXT NOT NULL UNIQUE,
                name TEXT NOT NULL UNIQUE,
                description TEXT NOT NULL DEFAULT ''
            );
            CREATE TABLE IF NOT EXISTS equipment (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                code TEXT NOT NULL UNIQUE,
                name TEXT NOT NULL UNIQUE,
                equipment_name TEXT NOT NULL DEFAULT '',
                category_id INTEGER NOT NULL REFERENCES categories(id),
                stock INTEGER NOT NULL CHECK (stock >= 0),
                daily_rate INTEGER NOT NULL CHECK (daily_rate >= 0)
            );
            CREATE TABLE IF NOT EXISTS customers (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                address TEXT NOT NULL,
                phone TEXT NOT NULL UNIQUE
            );
            CREATE TABLE IF NOT EXISTS rental_transactions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                code TEXT NOT NULL UNIQUE,
                customer_id INTEGER NOT NULL REFERENCES customers(id),
                customer_name TEXT NOT NULL,
                customer_address TEXT NOT NULL,
                customer_phone TEXT NOT NULL,
                rent_date TEXT NOT NULL,
                due_date TEXT NOT NULL,
                total INTEGER NOT NULL CHECK (total >= 0),
                status TEXT NOT NULL DEFAULT 'Aktif',
                returned_at TEXT,
                fine INTEGER NOT NULL DEFAULT 0 CHECK (fine >= 0),
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS rental_details (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                transaction_id INTEGER NOT NULL
                    REFERENCES rental_transactions(id) ON DELETE CASCADE,
                equipment_id INTEGER NOT NULL REFERENCES equipment(id),
                quantity INTEGER NOT NULL CHECK (quantity >= 1),
                daily_rate INTEGER NOT NULL CHECK (daily_rate >= 0),
                subtotal INTEGER NOT NULL CHECK (subtotal >= 0),
                UNIQUE (transaction_id, equipment_id)
            );
            CREATE TABLE IF NOT EXISTS equipment_units (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                equipment_id INTEGER NOT NULL
                    REFERENCES equipment(id) ON DELETE CASCADE,
                unit_number INTEGER NOT NULL CHECK (unit_number >= 1),
                unit_code TEXT NOT NULL UNIQUE,
                is_active INTEGER NOT NULL DEFAULT 1 CHECK (is_active IN (0, 1)),
                UNIQUE (equipment_id, unit_number)
            );
            CREATE TABLE IF NOT EXISTS rental_unit_details (
                transaction_id INTEGER NOT NULL
                    REFERENCES rental_transactions(id) ON DELETE CASCADE,
                equipment_unit_id INTEGER NOT NULL
                    REFERENCES equipment_units(id),
                PRIMARY KEY (transaction_id, equipment_unit_id)
            );
            CREATE INDEX IF NOT EXISTS idx_rental_unit_details_unit
                ON rental_unit_details(equipment_unit_id);
            """
        )

        equipment_columns = {
            row["name"] for row in connection.execute("PRAGMA table_info(equipment)")
        }
        if "equipment_name" not in equipment_columns:
            connection.execute(
                "ALTER TABLE equipment ADD COLUMN equipment_name TEXT NOT NULL DEFAULT ''"
            )
            connection.execute(
                "UPDATE equipment SET equipment_name = name WHERE equipment_name = ''"
            )

        if connection.execute("SELECT COUNT(*) FROM categories").fetchone()[0] == 0:
            category_names = ("Tenda", "Perlengkapan Tidur", "Memasak", "Penerangan")
            for name in category_names:
                cursor = connection.execute(
                    "INSERT INTO categories (code, name) VALUES (?, ?)",
                    (next_business_code(connection, "categories", "KAT"), name),
                )

            tent_category = connection.execute(
                "SELECT id FROM categories WHERE name = 'Tenda'"
            ).fetchone()["id"]
            connection.execute(
                """
                INSERT INTO equipment
                    (code, name, equipment_name, category_id, stock, daily_rate)
                VALUES (?, 'Tenda', 'Tenda 4 Orang', ?, 4, 50000)
                """,
                (next_business_code(connection, "equipment", "ALT"), tent_category),
            )

        for equipment in connection.execute(
            "SELECT id, stock FROM equipment ORDER BY id"
        ).fetchall():
            active_count = connection.execute(
                "SELECT COUNT(*) FROM equipment_units WHERE equipment_id = ? AND is_active = 1",
                (equipment["id"],),
            ).fetchone()[0]
            if active_count < equipment["stock"]:
                sync_equipment_units(
                    connection, equipment["id"], equipment["stock"]
                )

        backfill_rental_unit_details(connection)


def rental_days(start: date, due: date) -> int:
    return max(1, (due - start).days)


def calculate_fine(late_days: int, quantity: int, daily_rate: int) -> int:
    amount = late_days * quantity * daily_rate * FINE_RATE_PERCENT
    return (amount + 50) // 100


def create_equipment_units(
    connection: sqlite3.Connection, equipment_id: int, quantity: int
) -> None:
    equipment = connection.execute(
        "SELECT code FROM equipment WHERE id = ?", (equipment_id,)
    ).fetchone()
    if equipment is None:
        raise ValueError("Jenis alat tidak ditemukan.")
    last_number = connection.execute(
        "SELECT COALESCE(MAX(unit_number), 0) FROM equipment_units WHERE equipment_id = ?",
        (equipment_id,),
    ).fetchone()[0]
    connection.executemany(
        """
        INSERT INTO equipment_units (equipment_id, unit_number, unit_code)
        VALUES (?, ?, ?)
        """,
        [
            (equipment_id, number, f"{equipment['code']}-{number:03d}")
            for number in range(last_number + 1, last_number + quantity + 1)
        ],
    )


def sync_equipment_units(
    connection: sqlite3.Connection, equipment_id: int, target_stock: int
) -> None:
    active_count = connection.execute(
        "SELECT COUNT(*) FROM equipment_units WHERE equipment_id = ? AND is_active = 1",
        (equipment_id,),
    ).fetchone()[0]
    if target_stock > active_count:
        inactive_units = connection.execute(
            """
            SELECT id FROM equipment_units
            WHERE equipment_id = ? AND is_active = 0
            ORDER BY unit_number
            LIMIT ?
            """,
            (equipment_id, target_stock - active_count),
        ).fetchall()
        connection.executemany(
            "UPDATE equipment_units SET is_active = 1 WHERE id = ?",
            [(row["id"],) for row in inactive_units],
        )
        remaining = target_stock - active_count - len(inactive_units)
        if remaining:
            create_equipment_units(connection, equipment_id, remaining)
    elif target_stock < active_count:
        units_to_deactivate = active_count - target_stock
        unused_units = connection.execute(
            """
            SELECT u.id
            FROM equipment_units u
            WHERE u.equipment_id = ? AND u.is_active = 1
              AND NOT EXISTS (
                  SELECT 1
                  FROM rental_unit_details ru
                  JOIN rental_transactions t ON t.id = ru.transaction_id
                  WHERE ru.equipment_unit_id = u.id AND t.returned_at IS NULL
              )
            ORDER BY u.unit_number DESC
            LIMIT ?
            """,
            (equipment_id, units_to_deactivate),
        ).fetchall()
        if len(unused_units) != units_to_deactivate:
            raise ValueError(
                "Stok tidak dapat dikurangi karena unit masih disewa "
                "atau memiliki jadwal penyewaan."
            )
        connection.executemany(
            "UPDATE equipment_units SET is_active = 0 WHERE id = ?",
            [(row["id"],) for row in unused_units],
        )


def backfill_rental_unit_details(connection: sqlite3.Connection) -> None:
    details = connection.execute(
        """
        SELECT d.transaction_id, d.equipment_id, d.quantity,
               t.rent_date, t.due_date, t.returned_at
        FROM rental_details d
        JOIN rental_transactions t ON t.id = d.transaction_id
        ORDER BY t.rent_date, t.id, d.equipment_id
        """
    ).fetchall()
    for detail in details:
        assigned_count = connection.execute(
            """
            SELECT COUNT(*)
            FROM rental_unit_details ru
            JOIN equipment_units u ON u.id = ru.equipment_unit_id
            WHERE ru.transaction_id = ? AND u.equipment_id = ?
            """,
            (detail["transaction_id"], detail["equipment_id"]),
        ).fetchone()[0]
        missing_count = detail["quantity"] - assigned_count
        while missing_count > 0:
            units = connection.execute(
                """
                SELECT id FROM equipment_units
                WHERE equipment_id = ? AND is_active = 1
                ORDER BY unit_number
                """,
                (detail["equipment_id"],),
            ).fetchall()
            available_units = get_available_equipment_unit_ids(
                connection,
                detail["equipment_id"],
                date.fromisoformat(detail["rent_date"]),
                date.fromisoformat(detail["due_date"]),
            )
            unit_id = next(
                (unit["id"] for unit in units if unit["id"] in available_units),
                None,
            )
            if unit_id is None:
                equipment = connection.execute(
                    "SELECT stock FROM equipment WHERE id = ?",
                    (detail["equipment_id"],),
                ).fetchone()
                create_equipment_units(connection, detail["equipment_id"], 1)
                unit_id = connection.execute(
                    """
                    SELECT id FROM equipment_units
                    WHERE equipment_id = ?
                    ORDER BY unit_number DESC LIMIT 1
                    """,
                    (detail["equipment_id"],),
                ).fetchone()["id"]
                connection.execute(
                    "UPDATE equipment SET stock = ? WHERE id = ?",
                    (equipment["stock"] + 1, detail["equipment_id"]),
                )
            connection.execute(
                """
                INSERT INTO rental_unit_details (transaction_id, equipment_unit_id)
                VALUES (?, ?)
                """,
                (detail["transaction_id"], unit_id),
            )
            missing_count -= 1


def _exclusive_end(start: date, end: date) -> date:
    return max(end, start + timedelta(days=1))


def available_stock(
    stock: int,
    start: date,
    due: date,
    bookings: Iterable[tuple[date, date, date | None, int]],
) -> int:
    """Return the minimum stock available at any point in the requested period."""
    period_end = _exclusive_end(start, due)
    intervals: list[tuple[date, date, int]] = []

    for booking_start, planned_end, returned_at, quantity in bookings:
        actual_end = (
            returned_at + timedelta(days=1)
            if returned_at is not None
            else date.max
        )
        booking_end = _exclusive_end(booking_start, actual_end)
        if booking_start < period_end and booking_end > start:
            intervals.append((booking_start, booking_end, quantity))

    checkpoints = {start}
    checkpoints.update(
        booking_start
        for booking_start, _, _ in intervals
        if start < booking_start < period_end
    )
    most_reserved = max(
        (
            sum(
                quantity
                for booking_start, booking_end, quantity in intervals
                if booking_start <= checkpoint < booking_end
            )
            for checkpoint in checkpoints
        ),
        default=0,
    )
    return max(0, stock - most_reserved)


def get_available_equipment_unit_ids(
    connection: sqlite3.Connection,
    equipment_id: int,
    start: date,
    due: date,
) -> list[int]:
    units = connection.execute(
        """
        SELECT id FROM equipment_units
        WHERE equipment_id = ? AND is_active = 1
        ORDER BY unit_number
        """,
        (equipment_id,),
    ).fetchall()
    available_ids = []
    for unit in units:
        rows = connection.execute(
            """
            SELECT t.rent_date, t.due_date, t.returned_at
            FROM rental_unit_details ru
            JOIN rental_transactions t ON t.id = ru.transaction_id
            WHERE ru.equipment_unit_id = ?
            """,
            (unit["id"],),
        ).fetchall()
        bookings = [
            (
                date.fromisoformat(row["rent_date"]),
                date.fromisoformat(row["due_date"]),
                date.fromisoformat(row["returned_at"])
                if row["returned_at"]
                else None,
                1,
            )
            for row in rows
        ]
        if available_stock(1, start, due, bookings):
            available_ids.append(unit["id"])
    return available_ids


def get_available_stock(
    equipment_id: int, start: date, due: date
) -> int:
    with connect_db() as connection:
        return len(
            get_available_equipment_unit_ids(connection, equipment_id, start, due)
        )


def equipment_unit_status(
    unit_id: int, is_active: bool, today: date
) -> str:
    if not is_active:
        return "Tidak aktif"
    with connect_db() as connection:
        bookings = connection.execute(
            """
            SELECT t.rent_date, t.due_date, t.returned_at
            FROM rental_unit_details ru
            JOIN rental_transactions t ON t.id = ru.transaction_id
            WHERE ru.equipment_unit_id = ?
            ORDER BY t.rent_date
            """,
            (unit_id,),
        ).fetchall()
    parsed_bookings = [
        (
            date.fromisoformat(row["rent_date"]),
            date.fromisoformat(row["due_date"]),
            date.fromisoformat(row["returned_at"])
            if row["returned_at"]
            else None,
            1,
        )
        for row in bookings
    ]
    for booking in parsed_bookings:
        if not available_stock(1, today, today, [booking]):
            start, due, returned_at, _ = booking
            if returned_at == today:
                return "Dikembalikan hari ini"
            if returned_at is None and due < today:
                return "Terlambat"
            if start <= today:
                return "Sedang disewa"
    if any(start > today for start, _, _, _ in parsed_bookings):
        return "Akan datang"
    return "Tersedia"


def load_equipment_unit_rows(today: date) -> list[dict[str, object]]:
    with connect_db() as connection:
        units = connection.execute(
            """
            SELECT u.id, u.unit_code, u.is_active,
                   e.code AS equipment_code, e.name AS equipment_type,
                   e.equipment_name, e.daily_rate, c.name AS category
            FROM equipment_units u
            JOIN equipment e ON e.id = u.equipment_id
            JOIN categories c ON c.id = e.category_id
            ORDER BY e.name, e.equipment_name, u.unit_number
            """
        ).fetchall()
    return [
        {
            "Kode unit": unit["unit_code"],
            "Jenis alat": unit["equipment_type"],
            "Nama alat": unit["equipment_name"],
            "Kategori": unit["category"],
            "Status": equipment_unit_status(
                unit["id"], bool(unit["is_active"]), today
            ),
            "Tarif / hari": rupiah(unit["daily_rate"]),
        }
        for unit in units
    ]


def load_stock_summary(today: date) -> list[dict[str, object]]:
    with connect_db() as connection:
        equipment = connection.execute(
            """
            SELECT e.id, e.name, e.equipment_name, e.stock, c.name AS category
            FROM equipment e
            JOIN categories c ON c.id = e.category_id
            ORDER BY e.name, e.equipment_name
            """
        ).fetchall()
    summary = []
    for item in equipment:
        available = get_available_stock(item["id"], today, today)
        summary.append(
            {
                "Jenis alat": item["name"],
                "Nama alat": item["equipment_name"],
                "Kategori": item["category"],
                "Stok total": item["stock"],
                "Sedang disewa": item["stock"] - available,
                "Sisa tersedia": available,
            }
        )
    return summary


def render_stock_summary() -> None:
    with st.container(key="page_panel_stock_summary"):
        st.markdown('<div class="section-heading">Ringkasan stok hari ini</div>', unsafe_allow_html=True)
        st.markdown('<p class="section-description">Jumlah perlengkapan yang tersedia untuk operasional.</p>', unsafe_allow_html=True)
        summary = load_stock_summary(date.today())
        if summary:
            st.dataframe(summary, hide_index=True, width="stretch")
        else:
            st.info("Belum ada data alat.")


def rupiah(amount: int) -> str:
    return f"Rp{amount:,}".replace(",", ".")


def transaction_status(row: sqlite3.Row) -> str:
    if row["status"] == "Selesai":
        return "Dikembalikan"
    today = date.today()
    rent_date = date.fromisoformat(row["rent_date"])
    due_date = date.fromisoformat(row["due_date"])
    if rent_date > today:
        return "Akan datang"
    if due_date < today:
        return "Terlambat"
    return "Sedang disewa"


def load_transactions() -> list[sqlite3.Row]:
    with connect_db() as connection:
        return connection.execute(
            """
            SELECT * FROM rental_transactions
            ORDER BY created_at DESC, id DESC
            """
        ).fetchall()


def load_transaction_details(transaction_id: int) -> list[sqlite3.Row]:
    with connect_db() as connection:
        return connection.execute(
            """
            SELECT e.code, e.name, e.equipment_name,
                   d.quantity, d.daily_rate, d.subtotal,
                   GROUP_CONCAT(u.unit_code, ', ') AS unit_codes
            FROM rental_details d
            JOIN equipment e ON e.id = d.equipment_id
            LEFT JOIN rental_unit_details ru ON ru.transaction_id = d.transaction_id
            LEFT JOIN equipment_units u
                ON u.id = ru.equipment_unit_id AND u.equipment_id = e.id
            WHERE d.transaction_id = ?
            GROUP BY d.id
            ORDER BY e.name
            """,
            (transaction_id,),
        ).fetchall()


def add_page_style() -> None:
    st.markdown(
        """
        <style>
        :root {
            --forest: #1b765e;
            --forest-light: #2a9274;
            --moss: #e8eee7;
            --amber: #e6a84b;
            --ink: #293a33;
            --muted: #718078;
            --paper: #ffffff;
            --line: #dce5dd;
        }
        .stApp {
            background:
                radial-gradient(ellipse at 84% 0%, rgba(220, 232, 219, .52), transparent 32rem),
                #f3f5ef;
            color: var(--ink);
        }
        [data-testid="stHeader"] { background: transparent; }
        [data-testid="stSidebar"] {
            background: linear-gradient(165deg, #246c56 0%, #1d604c 62%, #174e3f 100%);
            border-right: 1px solid rgba(255, 255, 255, .08);
        }
        [data-testid="stSidebar"] * { color: #f3f5ef; }
        [data-testid="stSidebar"] [data-testid="stMarkdownContainer"] p {
            color: rgba(243, 245, 239, .7);
        }
        [data-testid="stSidebar"] [data-testid="stButton"] > button {
            justify-content: flex-start;
            min-height: 2.8rem;
            padding: .68rem .8rem;
            border: 1px solid transparent;
            border-radius: 11px;
            background: transparent;
            color: #f3f5ef;
            text-align: left;
            transition: background .18s ease, border-color .18s ease;
        }
        [data-testid="stSidebar"] [data-testid="stButton"] > button:hover {
            background: rgba(255, 255, 255, .09);
            transform: none;
        }
        [data-testid="stSidebar"] [data-testid="stButton"] > button[kind="primary"] {
            border-color: rgba(255, 255, 255, .45);
            background: #e4f4ec;
            box-shadow: 0 5px 16px rgba(4, 43, 31, .12);
            color: #1b765e !important;
        }
        .block-container { padding-top: 1.15rem; padding-bottom: 2rem; }
        .st-key-app_surface {
            min-height: calc(100vh - 3.2rem);
            padding: 1.25rem 1.35rem 1.5rem;
            border: 1px solid #d9e3d8;
            border-radius: 22px;
            background:
                radial-gradient(ellipse at 86% 0%, rgba(255, 255, 255, .76), transparent 28rem),
                linear-gradient(145deg, #e9efe7 0%, #e2eae1 100%);
            box-shadow: inset 0 1px 0 rgba(255, 255, 255, .62);
        }
        h1, h2, h3 { color: var(--forest); letter-spacing: -.025em; }
        h1 { font-weight: 750; }
        [data-testid="stCaptionContainer"] { color: var(--muted); }
        .page-heading {
            display: flex;
            justify-content: space-between;
            align-items: center;
            gap: 1rem;
            margin: .1rem 0 1.15rem;
            padding: 0 0 .2rem .9rem;
            border-left: 4px solid #42b889;
        }
        .page-title {
            margin: 0;
            color: #1c624f;
            font-size: clamp(1.55rem, 2.5vw, 2rem);
            font-weight: 800;
            letter-spacing: -.04em;
        }
        .page-subtitle { margin: .3rem 0 0; color: var(--muted); font-size: .9rem; }
        .page-date {
            flex: 0 0 auto;
            padding: .55rem .85rem;
            border: 1px solid #d5eae1;
            border-radius: 10px;
            color: var(--forest);
            background: rgba(255, 255, 255, .82);
            font-size: .8rem;
            font-weight: 650;
        }
        [class*="st-key-page_panel_"] {
            margin: .25rem 0 1rem;
            padding: 1.1rem 1.2rem 1.05rem;
            border: 1px solid rgba(219, 235, 228, .95);
            border-radius: 17px;
            background: rgba(255, 255, 255, .94);
            box-shadow: 0 9px 26px rgba(31, 83, 66, .065);
        }
        [class*="st-key-page_panel_"] [data-testid="stForm"] {
            margin: .5rem 0;
            background: #f7fcf9;
        }
        .section-heading {
            margin: 0 0 .25rem;
            color: var(--forest);
            font-size: 1.05rem;
            font-weight: 750;
        }
        .section-description { margin: 0 0 .8rem; color: var(--muted); font-size: .82rem; }
        .brand-card {
            overflow: hidden;
            margin: .1rem 0 1.45rem;
            padding: 1rem .9rem .8rem;
            border: 1px solid rgba(255, 255, 255, .13);
            border-radius: 17px;
            background: rgba(255, 255, 255, .08);
            text-align: center;
        }
        .brand-logo-wrap {
            display: grid;
            place-items: center;
            min-height: 76px;
            padding: .8rem;
            border-radius: 12px;
            background: #fff;
        }
        .brand-logo {
            display: block;
            width: min(100%, 210px);
            height: auto;
        }
        .brand-name {
            color: #1b765e;
            font-size: 1.15rem;
            font-weight: 800;
            letter-spacing: .08em;
        }
        .brand-caption {
            margin: .7rem 0 0;
            color: rgba(243, 245, 239, .72);
            font-size: .69rem;
            font-weight: 700;
            letter-spacing: .11em;
        }
        .dashboard-heading {
            display: flex;
            justify-content: space-between;
            align-items: center;
            gap: 1rem;
            margin: .25rem 0 1rem;
        }
        .dashboard-title {
            margin: 0;
            color: #1c624f;
            font-size: clamp(1.55rem, 2.5vw, 2rem);
            font-weight: 800;
            letter-spacing: -.04em;
        }
        .dashboard-subtitle {
            margin: .3rem 0 0;
            color: var(--muted);
            font-size: .9rem;
        }
        .dashboard-date {
            flex: 0 0 auto;
            padding: .55rem .85rem;
            border: 1px solid var(--line);
            border-radius: 10px;
            color: var(--forest);
            background: rgba(255, 255, 255, .72);
            font-size: .8rem;
            font-weight: 650;
        }
        [class*="st-key-gear_card_"] {
            min-height: 140px;
            padding: 1rem 1rem .75rem;
            border: 1px solid var(--line);
            border-radius: 16px;
            background: linear-gradient(145deg, #fff 0%, #f7fffb 100%);
            box-shadow: 0 8px 22px rgba(31, 83, 66, .055);
            transition: transform .18s ease, box-shadow .18s ease;
        }
        [class*="st-key-gear_card_"]:hover {
            transform: translateY(-2px);
            box-shadow: 0 12px 26px rgba(31, 83, 66, .1);
        }
        [class*="st-key-dashboard_panel_"] {
            min-height: 265px;
            padding: .95rem 1rem .75rem;
            border: 1px solid var(--line);
            border-radius: 17px;
            background: rgba(255, 255, 255, .9);
            box-shadow: 0 8px 24px rgba(31, 83, 66, .045);
        }
        .gear-category {
            color: #35896d;
            font-size: .7rem;
            font-weight: 750;
            letter-spacing: .1em;
            text-transform: uppercase;
        }
        .gear-name {
            margin: .3rem 0 .15rem;
            color: #285346;
            font-size: 1rem;
            font-weight: 750;
        }
        .gear-meta { color: var(--muted); font-size: .78rem; }
        .section-kicker {
            margin: .5rem 0 .6rem;
            color: #58756b;
            font-size: .73rem;
            font-weight: 750;
            letter-spacing: .1em;
            text-transform: uppercase;
        }
        div[data-testid="stMetric"] {
            min-height: 102px;
            padding: 1rem 1.1rem;
            border: 1px solid var(--line);
            border-radius: 15px;
            background: var(--paper);
            box-shadow: 0 6px 18px rgba(31, 83, 66, .045);
            transition: transform .18s ease, box-shadow .18s ease;
        }
        div[data-testid="stMetric"]:hover {
            transform: translateY(-2px);
            box-shadow: 0 10px 24px rgba(31, 83, 66, .09);
        }
        div[data-testid="stMetricLabel"] { color: var(--muted); font-weight: 600; }
        div[data-testid="stMetricValue"] { color: var(--forest); font-weight: 800; }
        [data-testid="stDataFrame"], [data-testid="stTable"] {
            overflow: hidden;
            border: 1px solid var(--line);
            border-radius: 12px;
            background: var(--paper);
        }
        [data-testid="stForm"] {
            padding: 1.25rem;
            border: 1px solid var(--line);
            border-radius: 16px;
            background: rgba(255, 255, 255, .78);
        }
        [data-testid="stTextInput"] input,
        [data-testid="stTextArea"] textarea,
        [data-testid="stNumberInput"] input,
        [data-testid="stDateInput"] input,
        [data-testid="stSelectbox"] [data-baseweb="select"] > div,
        [data-testid="stMultiSelect"] [data-baseweb="select"] > div {
            border-color: #d9e8e1;
            border-radius: 10px;
            background: #edf3ee;
        }
        [data-testid="stTextInput"] input,
        [data-testid="stTextArea"] textarea,
        [data-testid="stNumberInput"] input,
        [data-testid="stDateInput"] input,
        [data-testid="stSelectbox"] input[role="combobox"],
        [data-testid="stMultiSelect"] input {
            color: #243a34 !important;
            caret-color: #1b765e !important;
            -webkit-text-fill-color: #243a34 !important;
            background: transparent !important;
        }
        [data-testid="stSelectbox"] [data-baseweb="select"],
        [data-testid="stMultiSelect"] [data-baseweb="select"] {
            background: #edf3ee !important;
            border-radius: 10px;
        }
        [data-testid="stTextInput"] input::placeholder,
        [data-testid="stTextArea"] textarea::placeholder {
            color: #718078 !important;
            -webkit-text-fill-color: #718078 !important;
            opacity: 1;
        }
        [data-testid="stSelectbox"] [data-baseweb="select"] *,
        [data-testid="stMultiSelect"] [data-baseweb="select"] * {
            color: #243a34 !important;
        }
        [role="listbox"] {
            color: #243a34 !important;
            background: #fff !important;
        }
        [role="option"] {
            color: #243a34 !important;
            background: #fff !important;
        }
        [role="option"][aria-selected="true"],
        [role="option"]:hover {
            color: #174e3f !important;
            background: #e8eee7 !important;
        }
        [data-testid="stCheckbox"] input {
            accent-color: #1b765e;
        }
        [data-testid="stCheckbox"] label {
            color: #293a33 !important;
        }
        [data-testid="stSelectbox"] label { color: var(--ink); }
        [data-testid="stTextInput"] input:focus,
        [data-testid="stTextArea"] textarea:focus,
        [data-testid="stNumberInput"] input:focus {
            border-color: #35a980;
            box-shadow: 0 0 0 1px #35a980;
        }
        [data-testid="stTabs"] [role="tablist"] {
            gap: .35rem;
            padding: .35rem;
            border: 1px solid #d5e8df;
            border-radius: 13px;
            background: rgba(255, 255, 255, .58);
        }
        [data-testid="stTabs"] button[role="tab"] {
            min-height: 2.5rem;
            padding: .45rem .9rem;
            border-radius: 9px;
        }
        [data-testid="stTabs"] button[aria-selected="true"] {
            color: #1b765e;
            background: #fff;
            box-shadow: 0 2px 8px rgba(31, 83, 66, .09);
        }
        [data-testid="stExpander"] {
            overflow: hidden;
            border: 1px solid #e0ece6;
            border-radius: 12px;
            background: #f9fcfa;
        }
        .stButton > button, .stDownloadButton > button,
        [data-testid="stFormSubmitButton"] > button {
            border-radius: 10px;
            font-weight: 650;
            transition: transform .15s ease, box-shadow .15s ease;
        }
        [data-testid="stFormSubmitButton"] > button[kind="primary"],
        .stButton > button[kind="primary"] {
            border-color: #1b765e;
            background: linear-gradient(135deg, #2a9274, #1b765e);
            color: white;
        }
        .stButton > button:hover, .stDownloadButton > button:hover,
        [data-testid="stFormSubmitButton"] > button:hover {
            transform: translateY(-1px);
            box-shadow: 0 6px 16px rgba(23, 60, 50, .12);
        }
        [data-testid="stTabs"] button[role="tab"] { font-weight: 650; }
        [data-testid="stTabs"] button[aria-selected="true"] { color: var(--forest); }
        @media (max-width: 700px) {
            .block-container { padding-top: 1.25rem; }
            .dashboard-heading { align-items: flex-start; }
            .page-heading { align-items: flex-start; }
            .dashboard-date { font-size: .7rem; }
            .page-date { font-size: .7rem; }
            [class*="st-key-dashboard_panel_"] { min-height: unset; }
            .st-key-app_surface { padding: .9rem .75rem 1rem; border-radius: 16px; }
            [class*="st-key-page_panel_"] { padding: .85rem .75rem; border-radius: 14px; }
        }
        </style>
        """,
        unsafe_allow_html=True,
    )


def render_page_heading(title: str, subtitle: str) -> None:
    today = date.today()
    st.markdown(
        f"""
        <div class="page-heading">
            <div>
                <h1 class="page-title">{title}</h1>
                <p class="page-subtitle">{subtitle}</p>
            </div>
            <div class="page-date">{today.strftime("%d-%m-%Y")}</div>
        </div>
        """,
        unsafe_allow_html=True,
    )


def render_dashboard() -> None:
    render_dashboard_content()


def render_dashboard_content() -> None:
    today = date.today()
    render_page_heading(
        "Ringkasan",
        "Pantau aktivitas dan perlengkapan INHARA CAMP.",
    )
    with connect_db() as connection:
        categories_count = connection.execute(
            "SELECT COUNT(*) FROM categories"
        ).fetchone()[0]
        equipment_count = connection.execute(
            "SELECT COUNT(*) FROM equipment"
        ).fetchone()[0]
        units_count = connection.execute(
            "SELECT COALESCE(SUM(stock), 0) FROM equipment"
        ).fetchone()[0]

    transactions = load_transactions()
    current = [row for row in transactions if row["status"] != "Selesai"]
    overdue = [row for row in current if transaction_status(row) == "Terlambat"]
    stock_summary = load_stock_summary(today)
    available_count = sum(int(item["Sisa tersedia"]) for item in stock_summary)

    metrics = st.columns(4)
    metrics[0].metric("Penyewaan aktif", len(current))
    metrics[1].metric("Unit tersedia", available_count, f"dari {units_count} unit")
    metrics[2].metric("Terlambat", len(overdue))
    metrics[3].metric("Kategori alat", categories_count, f"{equipment_count} jenis alat")

    if overdue:
        st.warning(f"Ada {len(overdue)} transaksi yang terlambat dikembalikan.")

    st.markdown('<div class="section-kicker">Perlengkapan pilihan</div>', unsafe_allow_html=True)
    if stock_summary:
        gear_columns = st.columns(3)
        for index, item in enumerate(stock_summary[:3]):
            stock_total = int(item["Stok total"])
            available = int(item["Sisa tersedia"])
            ratio = available / stock_total if stock_total else 0
            with gear_columns[index].container(key=f"gear_card_{index}"):
                st.markdown(
                    f'<div class="gear-category">{item["Kategori"]} · {item["Jenis alat"]}</div>'
                    f'<div class="gear-name">{item["Nama alat"]}</div>'
                    f'<div class="gear-meta">{available} dari {stock_total} unit tersedia</div>',
                    unsafe_allow_html=True,
                )
                st.progress(ratio, text=f"{available}/{stock_total} unit tersedia")
    else:
        st.info("Belum ada data alat. Tambahkan perlengkapan pada menu Data alat.")

    activity_col, status_col = st.columns((1.45, 1))
    with activity_col:
        with st.container(key="dashboard_panel_activity"):
            st.markdown('<div class="section-kicker">Aktivitas terbaru</div>', unsafe_allow_html=True)
            st.subheader("Penyewaan terkini")
            rows = [
                {
                    "Kode": row["code"],
                    "Penyewa": row["customer_name"],
                    "Periode": f"{row['rent_date']} — {row['due_date']}",
                    "Status": transaction_status(row),
                    "Total": rupiah(row["total"]),
                }
                for row in transactions[:5]
            ]
            if rows:
                st.dataframe(rows, hide_index=True, width="stretch")
            else:
                st.info("Belum ada transaksi penyewaan.")
    with status_col:
        with st.container(key="dashboard_panel_status"):
            st.markdown('<div class="section-kicker">Kondisi inventaris</div>', unsafe_allow_html=True)
            st.subheader("Status unit alat")
            inventory = load_equipment_unit_rows(today)
            status_counts = Counter(str(row["Status"]) for row in inventory)
            if status_counts:
                status_data = [
                    {"Status": status, "Jumlah": count}
                    for status, count in status_counts.items()
                ]
                status_chart = (
                    alt.Chart(alt.Data(values=status_data))
                    .mark_arc(innerRadius=48, cornerRadius=4)
                    .encode(
                        theta=alt.Theta("Jumlah:Q"),
                        color=alt.Color(
                            "Status:N",
                            scale=alt.Scale(
                                range=["#22b994", "#e7ae4b", "#e56d67", "#7698df", "#9aaea6", "#4d6c61"]
                            ),
                            legend=alt.Legend(orient="bottom", title=None),
                        ),
                        tooltip=["Status:N", "Jumlah:Q"],
                    )
                    .properties(height=220)
                )
                st.altair_chart(status_chart, width="stretch")
            else:
                st.info("Belum ada data unit alat.")

    trend_col, inventory_col = st.columns((1, 1))
    with trend_col:
        with st.container(key="dashboard_panel_trend"):
            st.markdown('<div class="section-kicker">Tren operasional</div>', unsafe_allow_html=True)
            st.subheader("Penyewaan & pengembalian")
            month_names = ("Jan", "Feb", "Mar", "Apr", "Mei", "Jun", "Jul", "Agu", "Sep", "Okt", "Nov", "Des")
            months = []
            for offset in range(5, -1, -1):
                month_index = today.year * 12 + today.month - 1 - offset
                year, month_zero = divmod(month_index, 12)
                months.append((year, month_zero + 1))
            rental_counts = Counter()
            return_counts = Counter()
            for transaction in transactions:
                rented = date.fromisoformat(transaction["rent_date"])
                rental_counts[(rented.year, rented.month)] += 1
                if transaction["returned_at"]:
                    returned = date.fromisoformat(transaction["returned_at"])
                    return_counts[(returned.year, returned.month)] += 1
            month_order = [month_names[month - 1] for _, month in months]
            trend_data = [
                {"Bulan": month_names[month - 1], "Aktivitas": activity, "Jumlah": count}
                for year, month in months
                for activity, count in (
                    ("Penyewaan", rental_counts[(year, month)]),
                    ("Pengembalian", return_counts[(year, month)]),
                )
            ]
            trend_chart = (
                alt.Chart(alt.Data(values=trend_data))
                .mark_line(point=alt.OverlayMarkDef(filled=True, size=48), strokeWidth=2.5)
                .encode(
                    x=alt.X("Bulan:N", sort=month_order, axis=alt.Axis(title=None, labelAngle=0)),
                    y=alt.Y("Jumlah:Q", axis=alt.Axis(title=None, tickMinStep=1)),
                    color=alt.Color(
                        "Aktivitas:N",
                        scale=alt.Scale(domain=["Penyewaan", "Pengembalian"], range=["#18ae8a", "#e7ad45"]),
                        legend=alt.Legend(orient="bottom", title=None),
                    ),
                    tooltip=["Bulan:N", "Aktivitas:N", "Jumlah:Q"],
                )
                .properties(height=230)
            )
            st.altair_chart(trend_chart, width="stretch")
    with inventory_col:
        with st.container(key="dashboard_panel_stock"):
            st.markdown('<div class="section-kicker">Ketersediaan alat</div>', unsafe_allow_html=True)
            st.subheader("Ringkasan stok")
            if stock_summary:
                st.dataframe(
                    [
                        {
                            "Perlengkapan": item["Nama alat"],
                            "Kategori": item["Kategori"],
                            "Tersedia": f'{item["Sisa tersedia"]} / {item["Stok total"]}',
                        }
                        for item in stock_summary
                    ],
                    hide_index=True,
                    width="stretch",
                )
            else:
                st.info("Belum ada data stok.")


def render_categories() -> None:
    render_page_heading(
        "Kategori alat",
        "Kelola kelompok kategori perlengkapan camping.",
    )
    st.markdown('<div class="section-heading">Kategori alat</div>', unsafe_allow_html=True)
    st.markdown('<p class="section-description">Kelompokkan perlengkapan camping agar inventaris lebih rapi.</p>', unsafe_allow_html=True)
    def load_categories() -> list[sqlite3.Row]:
        with connect_db() as connection:
            return connection.execute(
                "SELECT * FROM categories ORDER BY name"
            ).fetchall()

    categories = load_categories()

    with st.expander("Tambah kategori", expanded=not categories):
        with st.form("add_category", clear_on_submit=True):
            name = st.text_input("Jenis kategori")
            description = st.text_input("Nama alat")
            submitted = st.form_submit_button("Simpan kategori", type="primary")
        if submitted:
            if not name.strip():
                st.error("Jenis kategori wajib diisi.")
            else:
                try:
                    with connect_db() as connection:
                        connection.execute(
                            "INSERT INTO categories (code, name, description) VALUES (?, ?, ?)",
                            (
                                next_business_code(connection, "categories", "KAT"),
                                name.strip(),
                                description.strip(),
                            ),
                        )
                    categories = load_categories()
                    st.success("Kategori berhasil ditambahkan.")
                except sqlite3.IntegrityError:
                    st.error("Jenis kategori tersebut sudah digunakan.")

    if not categories:
        st.info("Belum ada kategori. Tambahkan kategori terlebih dahulu.")
        return

    labels = {row["id"]: f"{row['code']} — {row['name']}" for row in categories}
    selected_id = st.selectbox(
        "Pilih kategori untuk diubah atau dihapus",
        options=list(labels),
        format_func=lambda value: labels[value],
        key="category_to_edit",
    )
    selected = next(row for row in categories if row["id"] == selected_id)
    left, right = st.columns([3, 1])
    with left:
        with st.form("edit_category"):
            new_name = st.text_input(
                "Jenis kategori",
                value=selected["name"],
                key=f"edit_category_name_{selected_id}",
            )
            new_description = st.text_input(
                "Nama alat",
                value=selected["description"],
                key=f"edit_category_description_{selected_id}",
            )
            save = st.form_submit_button("Simpan perubahan")
        if save:
            if not new_name.strip():
                st.error("Jenis kategori wajib diisi.")
            else:
                try:
                    with connect_db() as connection:
                        cursor = connection.execute(
                            "UPDATE categories SET name = ?, description = ? WHERE id = ?",
                            (new_name.strip(), new_description.strip(), selected_id),
                        )
                    if cursor.rowcount != 1:
                        st.error("Kategori tidak ditemukan, perubahan tidak disimpan.")
                    else:
                        categories = load_categories()
                        st.success("Kategori berhasil diperbarui.")
                except sqlite3.IntegrityError:
                    st.error("Jenis kategori tersebut sudah digunakan.")

    st.dataframe(
        [
            {
                "Kode": row["code"],
                "Jenis kategori": row["name"],
                "Nama alat": row["description"] or "—",
            }
            for row in categories
        ],
        hide_index=True,
        width="stretch",
    )

    with right:
        st.write("")
        st.write("")
        confirm_delete = st.checkbox(
            "Yakin ingin menghapus kategori ini?",
            key=f"confirm_category_delete_{selected_id}",
        )
        if st.button("Hapus kategori", disabled=not confirm_delete):
            try:
                with connect_db() as connection:
                    connection.execute(
                        "DELETE FROM categories WHERE id = ?", (selected_id,)
                    )
                st.success("Kategori berhasil dihapus.")
                st.rerun()
            except sqlite3.IntegrityError:
                st.error("Kategori tidak bisa dihapus selama masih memiliki alat.")


def render_equipment() -> None:
    st.markdown('<div class="section-heading">Perlengkapan camping</div>', unsafe_allow_html=True)
    st.markdown('<p class="section-description">Kelola stok, status unit, dan tarif setiap perlengkapan.</p>', unsafe_allow_html=True)
    with connect_db() as connection:
        categories = connection.execute(
            "SELECT id, name FROM categories ORDER BY name"
        ).fetchall()
        equipment = connection.execute(
            """
            SELECT e.*, c.name AS category_name
            FROM equipment e JOIN categories c ON c.id = e.category_id
            ORDER BY e.name, e.equipment_name
            """
        ).fetchall()

    if not categories:
        st.info("Tambahkan kategori terlebih dahulu sebelum menambah alat.")
        return

    category_ids = [row["id"] for row in categories]
    category_names = {row["id"]: row["name"] for row in categories}
    with st.expander("Tambah alat", expanded=not equipment):
        with st.form("add_equipment", clear_on_submit=True):
            equipment_type = st.text_input("Jenis alat")
            equipment_name = st.text_input("Nama alat")
            category_id = st.selectbox(
                "Kategori", category_ids, format_func=lambda value: category_names[value]
            )
            stock = st.number_input("Jumlah stok", min_value=0, step=1, value=1)
            daily_rate = st.number_input(
                "Tarif sewa per hari (Rp)", min_value=0, step=1000, value=0
            )
            submitted = st.form_submit_button("Simpan alat", type="primary")
        if submitted:
            if not equipment_type.strip() or not equipment_name.strip():
                st.error("Jenis alat dan nama alat wajib diisi.")
            else:
                try:
                    with connect_db() as connection:
                        cursor = connection.execute(
                            """
                            INSERT INTO equipment
                                (code, name, equipment_name, category_id, stock, daily_rate)
                            VALUES (?, ?, ?, ?, ?, ?)
                            """,
                            (
                                next_business_code(connection, "equipment", "ALT"),
                                equipment_type.strip(),
                                equipment_name.strip(),
                                category_id,
                                stock,
                                daily_rate,
                            ),
                        )
                        sync_equipment_units(
                            connection, cursor.lastrowid, int(stock)
                        )
                    st.success("Alat berhasil ditambahkan.")
                    st.rerun()
                except ValueError as error:
                    st.error(str(error))
                except sqlite3.IntegrityError:
                    st.error("Jenis alat tersebut sudah digunakan.")

    if not equipment:
        return

    st.dataframe(
        load_equipment_unit_rows(date.today()),
        hide_index=True,
        width="stretch",
    )

    labels = {
        row["id"]: f"{row['code']} — {row['name']} · {row['equipment_name']}"
        for row in equipment
    }
    selected_id = st.selectbox(
        "Pilih alat untuk diubah atau dihapus",
        options=list(labels),
        format_func=lambda value: labels[value],
        key="equipment_to_edit",
    )
    selected = next(row for row in equipment if row["id"] == selected_id)
    left, right = st.columns([3, 1])
    with left:
        with st.form("edit_equipment"):
            new_type = st.text_input(
                "Jenis alat",
                value=selected["name"],
                key=f"edit_equipment_type_{selected_id}",
            )
            new_equipment_name = st.text_input(
                "Nama alat",
                value=selected["equipment_name"],
                key=f"edit_equipment_name_{selected_id}",
            )
            new_category = st.selectbox(
                "Kategori",
                category_ids,
                index=category_ids.index(selected["category_id"]),
                format_func=lambda value: category_names[value],
                key=f"edit_equipment_category_{selected_id}",
            )
            new_stock = st.number_input(
                "Jumlah stok",
                min_value=0,
                step=1,
                value=selected["stock"],
                key=f"edit_equipment_stock_{selected_id}",
            )
            new_rate = st.number_input(
                "Tarif sewa per hari (Rp)",
                min_value=0,
                step=1000,
                value=selected["daily_rate"],
                key=f"edit_equipment_rate_{selected_id}",
            )
            save = st.form_submit_button("Simpan perubahan")
        if save:
            if not new_type.strip() or not new_equipment_name.strip():
                st.error("Jenis alat dan nama alat wajib diisi.")
            else:
                try:
                    with connect_db() as connection:
                        sync_equipment_units(
                            connection, selected_id, int(new_stock)
                        )
                        connection.execute(
                            """
                            UPDATE equipment
                            SET name = ?, equipment_name = ?, category_id = ?,
                                stock = ?, daily_rate = ?
                            WHERE id = ?
                            """,
                            (
                                new_type.strip(),
                                new_equipment_name.strip(),
                                new_category,
                                new_stock,
                                new_rate,
                                selected_id,
                            ),
                        )
                    st.success("Data alat berhasil diperbarui.")
                    st.rerun()
                except ValueError as error:
                    st.error(str(error))
                except sqlite3.IntegrityError:
                    st.error("Jenis alat tersebut sudah digunakan.")
    with right:
        st.write("")
        st.write("")
        confirm_delete = st.checkbox(
            "Yakin ingin menghapus alat ini?",
            key=f"confirm_equipment_delete_{selected_id}",
        )
        if st.button("Hapus alat", disabled=not confirm_delete):
            try:
                with connect_db() as connection:
                    connection.execute(
                        "DELETE FROM equipment WHERE id = ?", (selected_id,)
                    )
                st.success("Alat berhasil dihapus.")
                st.rerun()
            except sqlite3.IntegrityError:
                st.error("Alat tidak bisa dihapus karena sudah tercatat di transaksi.")


def render_inventory() -> None:
    render_page_heading(
        "Data alat",
        "Kelola stok, status unit, dan tarif perlengkapan camping.",
    )
    stock_summary = load_stock_summary(date.today())
    with connect_db() as connection:
        category_count = connection.execute("SELECT COUNT(*) FROM categories").fetchone()[0]
        equipment_count = connection.execute("SELECT COUNT(*) FROM equipment").fetchone()[0]
        total_units = connection.execute(
            "SELECT COALESCE(SUM(stock), 0) FROM equipment"
        ).fetchone()[0]
    available_units = sum(int(item["Sisa tersedia"]) for item in stock_summary)
    stats = st.columns(4)
    stats[0].metric("Kategori", category_count)
    stats[1].metric("Jenis alat", equipment_count)
    stats[2].metric("Total unit", total_units)
    stats[3].metric("Siap disewa", available_units)
    with st.container(key="page_panel_equipment"):
        render_equipment()


def render_customers() -> None:
    render_page_heading(
        "Identitas penyewa",
        "Catat dan kelola nama, alamat, serta nomor telepon penyewa.",
    )
    with connect_db() as connection:
        customers = connection.execute(
            "SELECT id, name, address, phone FROM customers ORDER BY name, phone"
        ).fetchall()

    st.metric("Penyewa terdaftar", len(customers))
    with st.expander("Tambah penyewa", expanded=not customers):
        with st.form("add_customer", clear_on_submit=True):
            name = st.text_input("Nama penyewa")
            phone = st.text_input("Nomor telepon")
            address = st.text_area("Alamat")
            submitted = st.form_submit_button("Simpan penyewa", type="primary")
        if submitted:
            if not name.strip() or not phone.strip() or not address.strip():
                st.error("Nama, nomor telepon, dan alamat penyewa wajib diisi.")
            else:
                try:
                    with connect_db() as connection:
                        connection.execute(
                            "INSERT INTO customers (name, address, phone) VALUES (?, ?, ?)",
                            (name.strip(), address.strip(), phone.strip()),
                        )
                    st.success("Identitas penyewa berhasil ditambahkan.")
                    st.rerun()
                except sqlite3.IntegrityError:
                    st.error("Nomor telepon tersebut sudah terdaftar.")

    if not customers:
        st.info("Belum ada penyewa terdaftar.")
        return

    st.dataframe(
        [
            {
                "Nama penyewa": row["name"],
                "Nomor telepon": row["phone"],
                "Alamat": row["address"],
            }
            for row in customers
        ],
        hide_index=True,
        width="stretch",
    )

    labels = {
        row["id"]: f'{row["name"]} — {row["phone"]}'
        for row in customers
    }
    selected_id = st.selectbox(
        "Pilih penyewa untuk diubah atau dihapus",
        options=list(labels),
        format_func=lambda value: labels[value],
        key="customer_to_edit",
    )
    selected = next(row for row in customers if row["id"] == selected_id)
    edit_col, delete_col = st.columns([3, 1])
    with edit_col:
        with st.form("edit_customer"):
            new_name = st.text_input(
                "Nama penyewa",
                value=selected["name"],
                key=f"edit_customer_name_{selected_id}",
            )
            new_phone = st.text_input(
                "Nomor telepon",
                value=selected["phone"],
                key=f"edit_customer_phone_{selected_id}",
            )
            new_address = st.text_area(
                "Alamat",
                value=selected["address"],
                key=f"edit_customer_address_{selected_id}",
            )
            save = st.form_submit_button("Simpan perubahan")
        if save:
            if not new_name.strip() or not new_phone.strip() or not new_address.strip():
                st.error("Nama, nomor telepon, dan alamat penyewa wajib diisi.")
            else:
                try:
                    with connect_db() as connection:
                        connection.execute(
                            """
                            UPDATE customers
                            SET name = ?, address = ?, phone = ?
                            WHERE id = ?
                            """,
                            (
                                new_name.strip(),
                                new_address.strip(),
                                new_phone.strip(),
                                selected_id,
                            ),
                        )
                    st.success("Identitas penyewa berhasil diperbarui.")
                    st.rerun()
                except sqlite3.IntegrityError:
                    st.error("Nomor telepon tersebut sudah digunakan penyewa lain.")
    with delete_col:
        st.write("")
        st.write("")
        confirm_delete = st.checkbox(
            "Yakin ingin menghapus penyewa ini?",
            key=f"confirm_customer_delete_{selected_id}",
        )
        if st.button("Hapus penyewa", disabled=not confirm_delete):
            try:
                with connect_db() as connection:
                    connection.execute(
                        "DELETE FROM customers WHERE id = ?", (selected_id,)
                    )
                st.success("Identitas penyewa berhasil dihapus.")
                st.rerun()
            except sqlite3.IntegrityError:
                st.error("Penyewa tidak bisa dihapus karena sudah memiliki transaksi.")


def save_rental(
    customer_name: str,
    customer_address: str,
    customer_phone: str,
    start: date,
    due: date,
    quantities: dict[int, int],
) -> str:
    if due < start:
        raise ValueError("Tanggal kembali tidak boleh lebih awal dari tanggal sewa.")
    if not quantities:
        raise ValueError("Pilih setidaknya satu alat untuk disewakan.")

    with connect_db() as connection:
        connection.execute("BEGIN IMMEDIATE")
        customer = connection.execute(
            "SELECT id FROM customers WHERE phone = ?", (customer_phone,)
        ).fetchone()
        if customer:
            customer_id = customer["id"]
            connection.execute(
                "UPDATE customers SET name = ?, address = ? WHERE id = ?",
                (customer_name, customer_address, customer_id),
            )
        else:
            cursor = connection.execute(
                "INSERT INTO customers (name, address, phone) VALUES (?, ?, ?)",
                (customer_name, customer_address, customer_phone),
            )
            customer_id = cursor.lastrowid

        items = []
        total = 0
        for equipment_id, quantity in quantities.items():
            item = connection.execute(
                """
                SELECT id, name, equipment_name, stock, daily_rate
                FROM equipment WHERE id = ?
                """,
                (equipment_id,),
            ).fetchone()
            if item is None:
                raise ValueError("Salah satu alat yang dipilih sudah tidak tersedia.")
            available_unit_ids = get_available_equipment_unit_ids(
                connection, equipment_id, start, due
            )
            if quantity < 1 or quantity > len(available_unit_ids):
                raise ValueError(
                    f"Stok {item['equipment_name']} tidak cukup untuk periode tersebut. "
                    f"Yang tersedia: {len(available_unit_ids)} unit."
                )
            subtotal = quantity * rental_days(start, due) * item["daily_rate"]
            items.append(
                (
                    equipment_id,
                    quantity,
                    item["daily_rate"],
                    subtotal,
                    available_unit_ids[:quantity],
                )
            )
            total += subtotal

        code = next_business_code(connection, "rental_transactions", "TRX", 6)
        connection.execute(
            """
            INSERT INTO rental_transactions (
                code, customer_id, customer_name, customer_address, customer_phone,
                rent_date, due_date, total, status, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'Aktif', ?)
            """,
            (
                code,
                customer_id,
                customer_name,
                customer_address,
                customer_phone,
                start.isoformat(),
                due.isoformat(),
                total,
                datetime.now().isoformat(timespec="seconds"),
            ),
        )
        transaction_id = connection.execute(
            "SELECT id FROM rental_transactions WHERE code = ?", (code,)
        ).fetchone()["id"]
        connection.executemany(
            """
            INSERT INTO rental_details
                (transaction_id, equipment_id, quantity, daily_rate, subtotal)
            VALUES (?, ?, ?, ?, ?)
            """,
            [
                (transaction_id, equipment_id, quantity, rate, subtotal)
                for equipment_id, quantity, rate, subtotal, _ in items
            ],
        )
        connection.executemany(
            """
            INSERT INTO rental_unit_details (transaction_id, equipment_unit_id)
            VALUES (?, ?)
            """,
            [
                (transaction_id, unit_id)
                for _, _, _, _, unit_ids in items
                for unit_id in unit_ids
            ],
        )
    return code


def render_new_rental() -> None:
    render_page_heading(
        "Penyewaan baru",
        "Isi identitas penyewa, pilih perlengkapan, dan tentukan periode sewa.",
    )

    today = date.today()
    with st.container(key="page_panel_rental_dates"):
        st.markdown('<div class="section-heading">Periode penyewaan</div>', unsafe_allow_html=True)
        st.markdown('<p class="section-description">Tentukan tanggal mulai dan rencana pengembalian alat.</p>', unsafe_allow_html=True)
        date_col1, date_col2 = st.columns(2)
        with date_col1:
            start = st.date_input("Tanggal sewa", value=today, key="rent_start_date")
        with date_col2:
            due = st.date_input(
                "Rencana tanggal kembali",
                value=today + timedelta(days=1),
                key="rent_due_date",
            )
    if due < start:
        st.error("Tanggal kembali tidak boleh lebih awal daripada tanggal sewa.")
        return

    with connect_db() as connection:
        equipment = connection.execute(
            """
            SELECT e.id, e.code, e.name, e.equipment_name, e.stock,
                   e.daily_rate, c.name AS category
            FROM equipment e JOIN categories c ON c.id = e.category_id
            ORDER BY e.name, e.equipment_name
            """
        ).fetchall()

    if not equipment:
        st.info("Tambahkan data alat sebelum membuat penyewaan.")
        return

    available_by_id = {
        row["id"]: get_available_stock(row["id"], start, due)
        for row in equipment
    }
    available_units = sum(available_by_id.values())
    available_types = sum(1 for quantity in available_by_id.values() if quantity > 0)
    rental_overview = st.columns(3)
    rental_overview[0].metric("Lama sewa", f"{rental_days(start, due)} hari")
    rental_overview[1].metric("Jenis tersedia", available_types)
    rental_overview[2].metric("Unit tersedia", available_units)
    labels = {
        row["id"]: (
            f"{row['code']} — {row['name']} · {row['equipment_name']} · tersedia "
            f"{available_by_id[row['id']]}/{row['stock']} · {rupiah(row['daily_rate'])}/hari"
        )
        for row in equipment
        if available_by_id[row["id"]] > 0
    }
    if not labels:
        st.warning("Tidak ada alat yang tersedia untuk periode ini.")
        return

    version = st.session_state.get("rental_form_version", 0)
    with st.container(key="page_panel_rental_items"):
        st.markdown('<div class="section-heading">Pilih perlengkapan</div>', unsafe_allow_html=True)
        st.markdown('<p class="section-description">Stok ditampilkan sesuai periode yang Anda pilih.</p>', unsafe_allow_html=True)
        selected_ids = st.multiselect(
            "Pilih alat",
            options=list(labels),
            format_func=lambda value: labels[value],
            key=f"rental_items_{version}",
        )
    equipment_by_id = {row["id"]: row for row in equipment}
    quantities: dict[int, int] = {}
    total = 0
    days = rental_days(start, due)

    if selected_ids:
        with st.container(key="page_panel_rental_calculation"):
            st.markdown(f'<div class="section-heading">Rincian biaya · {days} hari</div>', unsafe_allow_html=True)
            for equipment_id in selected_ids:
                item = equipment_by_id[equipment_id]
                quantity = st.number_input(
                    f"Jumlah {item['equipment_name']} (maks. {available_by_id[equipment_id]})",
                    min_value=1,
                    max_value=available_by_id[equipment_id],
                    value=1,
                    step=1,
                    key=f"rental_quantity_{version}_{equipment_id}",
                )
                quantities[equipment_id] = quantity
                subtotal = quantity * days * item["daily_rate"]
                total += subtotal
                st.caption(
                    f"{quantity} × {rupiah(item['daily_rate'])} × {days} hari = {rupiah(subtotal)}"
                )
            st.metric("Total biaya sewa", rupiah(total))
    else:
        st.info("Pilih satu atau beberapa alat untuk melihat perhitungan biaya.")

    with st.container(key="page_panel_rental_customer"):
        st.markdown('<div class="section-heading">Identitas penyewa</div>', unsafe_allow_html=True)
        st.markdown('<p class="section-description">Masukkan kontak penyewa untuk mencatat transaksi.</p>', unsafe_allow_html=True)
        with st.form(f"rental_customer_{version}"):
            customer_col1, customer_col2 = st.columns(2)
            with customer_col1:
                customer_name = st.text_input("Nama penyewa")
                customer_phone = st.text_input("Nomor telepon")
            with customer_col2:
                customer_address = st.text_area("Alamat")
            submitted = st.form_submit_button("Simpan penyewaan", type="primary")

    if submitted:
        if not customer_name.strip() or not customer_phone.strip() or not customer_address.strip():
            st.error("Nama, nomor telepon, dan alamat penyewa wajib diisi.")
        elif not quantities:
            st.error("Pilih setidaknya satu alat untuk disewakan.")
        else:
            try:
                code = save_rental(
                    customer_name.strip(),
                    customer_address.strip(),
                    customer_phone.strip(),
                    start,
                    due,
                    quantities,
                )
                st.session_state.last_saved_rental = code
                st.session_state.rental_form_version = version + 1
                st.rerun()
            except ValueError as error:
                st.error(str(error))

    if "last_saved_rental" in st.session_state:
        st.success(f"Penyewaan {st.session_state.pop('last_saved_rental')} berhasil disimpan.")


def return_transaction(transaction: sqlite3.Row, actual_return: date) -> int:
    if transaction["status"] == "Selesai":
        raise ValueError("Transaksi ini sudah selesai.")
    start = date.fromisoformat(transaction["rent_date"])
    if actual_return < start:
        raise ValueError("Tanggal pengembalian tidak boleh sebelum tanggal sewa.")
    details = load_transaction_details(transaction["id"])
    late_days = max(
        0,
        (actual_return - date.fromisoformat(transaction["due_date"])).days,
    )
    fine = sum(
        calculate_fine(late_days, row["quantity"], row["daily_rate"])
        for row in details
    )
    with connect_db() as connection:
        connection.execute(
            """
            UPDATE rental_transactions
            SET status = 'Selesai', returned_at = ?, fine = ?
            WHERE id = ? AND status = 'Aktif'
            """,
            (actual_return.isoformat(), fine, transaction["id"]),
        )
    return fine


def render_transaction_details(transaction: sqlite3.Row) -> None:
    transaction_id = transaction["id"]
    with st.container(key=f"page_panel_transaction_{transaction_id}"):
        st.markdown(
            f'<div class="section-heading">Rincian transaksi · {transaction["code"]}</div>',
            unsafe_allow_html=True,
        )
        customer_col, date_col, total_col = st.columns(3)
        customer_col.metric("Penyewa", transaction["customer_name"])
        date_col.metric(
            "Periode",
            f'{transaction["rent_date"]} → {transaction["due_date"]}',
        )
        total_col.metric("Total sewa", rupiah(transaction["total"]))
        st.caption(
            f'{transaction["customer_phone"]} · {transaction["customer_address"]} · '
            f'Status: {transaction_status(transaction)}'
        )
        details = load_transaction_details(transaction_id)
        st.dataframe(
            [
                {
                    "Kode unit": row["unit_codes"] or "—",
                    "Jenis alat": row["name"],
                    "Nama alat": row["equipment_name"],
                    "Jumlah": row["quantity"],
                    "Tarif / hari": rupiah(row["daily_rate"]),
                    "Subtotal": rupiah(row["subtotal"]),
                }
                for row in details
            ],
            hide_index=True,
            width="stretch",
        )


def render_transactions() -> None:
    render_page_heading(
        "Transaksi",
        "Telusuri riwayat penyewaan, status, dan rincian perlengkapan.",
    )
    transactions = load_transactions()
    current = [row for row in transactions if row["status"] != "Selesai"]
    overdue = [row for row in current if transaction_status(row) == "Terlambat"]
    returned = [row for row in transactions if row["status"] == "Selesai"]
    stats = st.columns(4)
    stats[0].metric("Semua transaksi", len(transactions))
    stats[1].metric("Sedang berjalan", len(current))
    stats[2].metric("Terlambat", len(overdue))
    stats[3].metric("Dikembalikan", len(returned))
    render_stock_summary()
    if not transactions:
        with st.container(key="page_panel_transaction_empty"):
            st.info("Belum ada transaksi. Buat transaksi pertama di menu Penyewaan baru.")
        return

    status_options = [
        "Semua status",
        "Akan datang",
        "Sedang disewa",
        "Terlambat",
        "Dikembalikan",
    ]
    with st.container(key="page_panel_transaction_filters"):
        st.markdown('<div class="section-heading">Cari & filter transaksi</div>', unsafe_allow_html=True)
        search_col, status_col = st.columns((2, 1))
        with search_col:
            search = st.text_input("Cari kode transaksi, nama, atau nomor telepon")
        with status_col:
            selected_status = st.selectbox("Filter status", status_options)
    visible = [
        row
        for row in transactions
        if (
            not search.strip()
            or search.strip().lower()
            in f"{row['code']} {row['customer_name']} {row['customer_phone']}".lower()
        )
        and (
            selected_status == "Semua status"
            or transaction_status(row) == selected_status
        )
    ]

    table = [
        {
            "Kode": row["code"],
            "Penyewa": row["customer_name"],
            "Telepon": row["customer_phone"],
            "Tanggal sewa": row["rent_date"],
            "Rencana kembali": row["due_date"],
            "Total sewa": rupiah(row["total"]),
            "Denda": rupiah(row["fine"]),
            "Status": transaction_status(row),
        }
        for row in visible
    ]
    with st.container(key="page_panel_transaction_list"):
        st.markdown('<div class="section-heading">Daftar transaksi</div>', unsafe_allow_html=True)
        st.dataframe(table, hide_index=True, width="stretch")
        csv_buffer = io.StringIO()
        if table:
            writer = csv.DictWriter(csv_buffer, fieldnames=table[0].keys())
            writer.writeheader()
            writer.writerows(table)
        st.download_button(
            "Unduh riwayat CSV",
            data=csv_buffer.getvalue(),
            file_name="riwayat_inhara_camp.csv",
            mime="text/csv",
            disabled=not table,
        )

    if not visible:
        st.info("Tidak ada transaksi yang cocok dengan pencarian atau filter ini.")
        return

    transaction_labels = {
        row["id"]: f"{row['code']} — {row['customer_name']} ({transaction_status(row)})"
        for row in visible
    }
    transaction_id = st.selectbox(
        "Pilih transaksi untuk melihat rincian",
        options=list(transaction_labels),
        format_func=lambda value: transaction_labels[value],
    )
    transaction = next(row for row in visible if row["id"] == transaction_id)
    render_transaction_details(transaction)
    if transaction["returned_at"]:
        st.success(
            f"Dikembalikan pada {transaction['returned_at']} · "
            f"Denda: {rupiah(transaction['fine'])}"
        )


def render_returns() -> None:
    render_page_heading(
        "Pengembalian",
        "Proses pengembalian alat dan periksa denda keterlambatan.",
    )
    transactions = load_transactions()
    pending = [row for row in transactions if row["status"] != "Selesai"]
    overdue = [row for row in pending if transaction_status(row) == "Terlambat"]
    due_today = [
        row for row in pending
        if date.fromisoformat(row["due_date"]) == date.today()
    ]
    stats = st.columns(3)
    stats[0].metric("Menunggu pengembalian", len(pending))
    stats[1].metric("Terlambat", len(overdue))
    stats[2].metric("Jatuh tempo hari ini", len(due_today))
    render_stock_summary()
    if not pending:
        with st.container(key="page_panel_returns_empty"):
            st.success("Tidak ada transaksi yang menunggu pengembalian.")
        return

    labels = {
        row["id"]: (
            f"{row['code']} — {row['customer_name']} "
            f"({transaction_status(row)})"
        )
        for row in pending
    }
    with st.container(key="page_panel_return_selection"):
        st.markdown('<div class="section-heading">Pilih transaksi</div>', unsafe_allow_html=True)
        transaction_id = st.selectbox(
            "Pilih transaksi untuk diproses",
            options=list(labels),
            format_func=lambda value: labels[value],
        )
    transaction = next(row for row in pending if row["id"] == transaction_id)
    render_transaction_details(transaction)

    rent_date = date.fromisoformat(transaction["rent_date"])
    if rent_date > date.today():
        st.info("Pengembalian dapat dicatat mulai tanggal sewa.")
        return

    with st.container(key="page_panel_return_form"):
        st.markdown('<div class="section-heading">Catat pengembalian</div>', unsafe_allow_html=True)
        st.markdown('<p class="section-description">Denda dihitung otomatis jika terlambat.</p>', unsafe_allow_html=True)
        with st.form(f"return_{transaction_id}"):
            actual_return = st.date_input(
                "Tanggal pengembalian aktual",
                value=date.today(),
                min_value=rent_date,
                max_value=date.today(),
            )
            submitted = st.form_submit_button("Catat pengembalian", type="primary")
    if submitted:
        try:
            fine = return_transaction(transaction, actual_return)
            st.success(
                f"Pengembalian tercatat. Denda: {rupiah(fine)} "
                f"(20% tarif harian per unit untuk setiap hari keterlambatan)."
            )
            st.rerun()
        except ValueError as error:
            st.error(str(error))


def main() -> None:
    st.set_page_config(
        page_title="INHARA CAMP | Administrasi",
        layout="wide",
        initial_sidebar_state="expanded",
    )
    add_page_style()
    init_db()
    logo = (
        f'<img class="brand-logo" src="{logo_data_uri()}" alt="Logo Inhara Camp">'
        if LOGO_PATH.is_file()
        else '<div class="brand-name">INHARA CAMP</div>'
    )
    pages = [
        "Ringkasan",
        "Data alat",
        "Kategori alat",
        "Identitas penyewa",
        "Penyewaan baru",
        "Transaksi",
        "Pengembalian",
    ]
    with st.sidebar:
        st.markdown(
            f"""
            <div class="brand-card">
                <div class="brand-logo-wrap">{logo}</div>
                <div class="brand-caption">ADMINISTRASI PENYEWAAN</div>
            </div>
            """,
            unsafe_allow_html=True,
        )
        for menu_page in pages:
            if st.button(
                menu_page,
                key=f"nav_{menu_page}",
                type=(
                    "primary"
                    if st.session_state.get("main_menu", "Ringkasan") == menu_page
                    else "secondary"
                ),
                width="stretch",
            ):
                st.session_state["main_menu"] = menu_page
        st.caption("Data tersimpan di SQLite lokal")

    with st.container(key="app_surface"):
        page = st.session_state.get("main_menu", "Ringkasan")
        if page == "Ringkasan":
            render_dashboard()
        elif page == "Data alat":
            render_inventory()
        elif page == "Kategori alat":
            render_categories()
        elif page == "Identitas penyewa":
            render_customers()
        elif page == "Penyewaan baru":
            render_new_rental()
        elif page == "Transaksi":
            render_transactions()
        else:
            render_returns()


if __name__ == "__main__":
    main()
