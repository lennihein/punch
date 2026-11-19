# Punch! — ATOSS Time Tracking & PDF Export Rules

When generating PDF time tracking reports or summaries for ATOSS:

## 1. Terminology & Mapping
- **Locations**:
  - `office` → `Anwesenheit`
  - `DG` → `dg`
  - `?` or `-` → `ma` (Homeoffice / Remote)
  - `ARZT` → `Arzt`
  - *(empty / none specified)* → Leave empty (do not fill in a default location if none was given in the export)
- **Absences**:
  - `K` → `Krank` (Title Case)
  - `F` → `Feiertag` (Title Case)
  - `ZA` → `ZA` (All-caps)
  - `U` → `Urlaub`

## 2. Table Layout & Styling
- **Columns**: `Datum`, `Tag`, `Arbeitszeiten`, `Standort / Status`.
- **Omit `GESAMT`**: Do not include daily total hours unless explicitly requested.
- **Week Separation**: Insert a visible horizontal line (`table.hline(stroke: 1.2pt + rgb("#555555"))`) between calendar weeks to assist manual data entry.
- **Data Row Text Weight**: Keep all status labels and locations in regular font weight (do not bold `Krank`, `Feiertag`, etc.).

## 3. Page Formatting & Typography
- **Font**: Use **Inter** (`font: ("Inter", "Roboto", "DejaVu Sans")`).
- **Section Headings**: Minimalist month headings (e.g., `= Mai 2026`, font size `12.5pt` bold) with `12pt` spacing before tables.
- **No Headers/Footers**: Omit page numbers and top banners (`header: none`, `footer: none`).
- **Ink-Saving**: Keep background fills minimal/light gray (`#f2f2f2` for headers only), white pages, and thin cell borders (`0.3pt`).
