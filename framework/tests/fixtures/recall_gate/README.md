# recall_gate test fixtures

Real CPSC data, fetched 2026-11-17 with public read-only GETs, plus synthetic
catalogs. Used by `framework/tests/test_recall_gate*.py`.

| File | Source | What it holds |
|---|---|---|
| `saferproducts_api_delta_max.json` | `https://www.saferproducts.gov/RestWebServices/Recall?format=json&RecallTitle=Delta%20Max` | Byte-for-byte API response: recall 26-010, EcoFlow Delta Max 2000 (model EFD310 only in the description and image captions; `RemedyOptions` = `"R"`). |
| `saferproducts_api_seagull_pro.json` | `...&RecallTitle=Seagull%20Pro` | Recall 25-187, Aiper Seagull Pro robotic pool cleaner (non-power niche; the remedy names the replacement "Aiper Scuba S1"). |
| `saferproducts_api_recalldate_2025-10-08_2025-10-10.json` | `...&RecallDateStart=2025-10-08&RecallDateEnd=2025-10-10` | Every recall announced that week (10): EcoFlow plus nine non-power recalls (robes, torches, pressure cookers, ...). |
| `recalls_recall_listing.sample.csv` | `https://www.cpsc.gov/s3fs-public/recall-data/recalls_recall_listing.csv` (Last-Modified 2026-11-09) | 15 of 10,134 rows, field content verbatim: the power recalls (EcoFlow 26-010, Goal Zero YETI 3000X 26-712, HALO 1000, Predator 2000-Watt, Klein/Blackfire, Goal Zero cable/accessory recalls), the robot recalls (Aiper Seagull Pro, Aiper Elite Pro, Husqvarna Automower), Bose S1 Pro battery packs, NEWDERY (a warning that became recall 26-792), and two non-power recalls. |
| `product_safety_warning_listing.sample.csv` | `https://www.cpsc.gov/s3fs-public/recall-data/product_safety_warning_listing.csv` | 6 of 389 rows: Aeiusny power stations (25-454), ZAUTNKN battery packs "for Dyson vacuums" (24-054, prints ASIN B093Y1KK5Q), a CO-detector warning that prints the SAME ASIN (23-160), INSE stick vacuums, HAIARA power banks (names no model), NEWDERY (25-327, later recall 26-792). |
| `recalls_review_cases.sample.csv` | same recall CSV, fetched 2026-11-17 | 5 rows, field content verbatim, cut for the F8 review findings: Boon "Flair & Flair Elite" (23-272, a name and its longer variant), Thermos "Stainless King 3000 and 3020 ... Sportsman 3010" (26-444, list naming), LG Energy Solution RESU 10H (23-705) and Eguana Evolve (22-058) home energy storage recalls (in the home-backup-power niche), Curtis/Frigidaire minifridges (25-395, "if model number is EFMIS129-B or EFMIS129-C, it is not included in this recall"). |
| `warnings_review_cases.sample.csv` | same warning CSV, fetched 2026-11-17 | 2 rows, verbatim: Ridstar "Q20 and Q20 Pro" (26-337) and "Q20 and Q20 Lite" (26-584) e-bikes. Kept apart from the sample CSVs so the original golden record counts do not move. |
| `recalls_list_naming_cases.sample.csv` | same recall CSV, fetched 2026-11-17 | 5 rows, field content verbatim, cut for the round-2 list-naming finding: elided lists whose shared leading words stand once, Textron "Arctic Cat Prowler Pro / Pro Crew and Tracker Off Road 800SX / 800SX Crew" (25-701) and Tike Tech "Single City X3 and X3 Sport" (11-002), Polaris "RANGER 1000, 1000 Crew, XP 1000, XP 1000 Crew" (23-258); and two negatives, Polaris "RANGER 500, 570 and 1000 RANGER CREW 570 and CREW XP 1000" (20-735, punctuation lost) and Honda "EU2200i, EU2200i Companion and EB2200i" (19-088). |
| `catalog_home_backup_power.json`, `catalog_cleaning_robots.json` | synthetic | Catalog rows in `recall_gate.CatalogItem` shape. Real brand/model names, invented ids and ASINs (`B0RGTEST..`). |
| `site_schema.sql` | synthetic | Minimal brands/models/products/kit_components tables for the throwaway-Postgres tests. |

The CPSC HTML pages (cpsc.gov/Recalls, /Warnings) sit behind a bot wall; the
two CSVs and the SaferProducts API are the machine-readable sources and need no
browser. To refresh: re-run the URLs above and re-cut the same rows.
