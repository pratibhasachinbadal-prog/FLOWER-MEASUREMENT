"""Flower Measurement Explorer: a local, menu-driven measurement analysis tool."""

from __future__ import annotations

import argparse
import csv
import json
import math
import random
import sys
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable


SPECIES = ("Iris-setosa", "Iris-versicolor", "Iris-virginica")
MEASUREMENTS = (
    "sepal_length_cm",
    "sepal_width_cm",
    "petal_length_cm",
    "petal_width_cm",
)
MEASUREMENT_LABELS = {
    "sepal_length_cm": "Sepal length",
    "sepal_width_cm": "Sepal width",
    "petal_length_cm": "Petal length",
    "petal_width_cm": "Petal width",
}
LIMITS = {
    "sepal_length_cm": (2.0, 9.0),
    "sepal_width_cm": (1.0, 5.0),
    "petal_length_cm": (0.1, 7.5),
    "petal_width_cm": (0.1, 3.5),
}
CSV_FIELDS = ("id", *MEASUREMENTS, "species", "created_at")
DATA_FILENAME = "flowers.json"
CSV_FILENAME = "flowers.csv"


class DataError(ValueError):
    """Raised when a stored or imported record does not match the data schema."""


@dataclass(frozen=True)
class FlowerRecord:
    id: str
    sepal_length_cm: float
    sepal_width_cm: float
    petal_length_cm: float
    petal_width_cm: float
    species: str | None
    created_at: str

    @classmethod
    def create(
        cls, values: dict[str, float], species: str | None = None
    ) -> "FlowerRecord":
        normalized_species = species.strip() if species else None
        if normalized_species and normalized_species not in SPECIES:
            raise DataError(f"Species must be one of: {', '.join(SPECIES)}.")
        return cls(
            id=uuid.uuid4().hex[:8],
            **values,
            species=normalized_species or None,
            created_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        )

    @classmethod
    def from_mapping(cls, row: dict[str, object]) -> "FlowerRecord":
        try:
            record_id = str(row["id"]).strip()
            created_at = str(row["created_at"]).strip()
            species_value = row.get("species")
            species = str(species_value).strip() if species_value else None
            values = {
                name: validate_measurement(name, row[name])
                for name in MEASUREMENTS
            }
        except (KeyError, TypeError, ValueError) as exc:
            raise DataError(f"Missing or invalid record field: {exc}") from exc
        if not record_id:
            raise DataError("Record ID cannot be blank.")
        if not created_at:
            raise DataError("Record creation date cannot be blank.")
        if species and species not in SPECIES:
            raise DataError(f"Unknown species '{species}'.")
        return cls(
            id=record_id,
            **values,
            species=species or None,
            created_at=created_at,
        )


def validate_measurement(name: str, raw_value: object) -> float:
    if name not in LIMITS:
        raise DataError(f"Unknown measurement '{name}'.")
    try:
        value = float(raw_value)
    except (TypeError, ValueError) as exc:
        raise DataError(f"{MEASUREMENT_LABELS[name]} must be a number.") from exc
    if not math.isfinite(value):
        raise DataError(f"{MEASUREMENT_LABELS[name]} must be a finite number.")
    low, high = LIMITS[name]
    if not low <= value <= high:
        raise DataError(
            f"{MEASUREMENT_LABELS[name]} must be between {low:g} and {high:g} cm."
        )
    return round(value, 2)


def create_sample_records() -> list[FlowerRecord]:
    """Build 36 reproducible, plausible demo measurements across three species."""
    randomizer = random.Random(118)
    profiles = (
        ("Iris-setosa", (5.00, 3.43, 1.46, 0.25), (0.34, 0.36, 0.16, 0.08)),
        ("Iris-versicolor", (5.94, 2.77, 4.26, 1.33), (0.50, 0.30, 0.45, 0.19)),
        ("Iris-virginica", (6.59, 2.97, 5.55, 2.03), (0.62, 0.32, 0.55, 0.27)),
    )
    records: list[FlowerRecord] = []
    for species, means, deviations in profiles:
        for _ in range(12):
            values = {
                name: validate_measurement(
                    name, round(randomizer.gauss(mean, deviation), 2)
                )
                for name, mean, deviation in zip(MEASUREMENTS, means, deviations)
            }
            records.append(FlowerRecord.create(values, species))
    return records


class FlowerStore:
    def __init__(self, data_dir: Path):
        self.data_dir = data_dir
        self.json_path = data_dir / DATA_FILENAME
        self.csv_path = data_dir / CSV_FILENAME
        try:
            self.data_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise DataError(f"Could not create data directory {data_dir}: {exc}") from exc
        if self.json_path.exists():
            self.records = self.load_json()
        else:
            self.records = create_sample_records()
            self.save_json()
            self.export_csv()

    def load_json(self) -> list[FlowerRecord]:
        try:
            payload = json.loads(self.json_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise DataError(f"Could not read {self.json_path}: {exc}") from exc
        if not isinstance(payload, dict) or payload.get("schema_version") != 1:
            raise DataError(f"{self.json_path} has an unsupported data format.")
        rows = payload.get("records")
        if not isinstance(rows, list):
            raise DataError(f"{self.json_path} must contain a records list.")
        try:
            records = [FlowerRecord.from_mapping(row) for row in rows]
        except (DataError, TypeError) as exc:
            raise DataError(f"Invalid record in {self.json_path}: {exc}") from exc
        ids = [record.id for record in records]
        if len(ids) != len(set(ids)):
            raise DataError(f"{self.json_path} contains duplicate record IDs.")
        return records

    def save_json(self) -> None:
        payload = {
            "schema_version": 1,
            "records": [asdict(record) for record in self.records],
        }
        temporary_path = self.json_path.with_suffix(".json.tmp")
        try:
            temporary_path.write_text(
                json.dumps(payload, indent=2) + "\n", encoding="utf-8"
            )
            temporary_path.replace(self.json_path)
        except OSError as exc:
            raise DataError(f"Could not save {self.json_path}: {exc}") from exc

    def add(self, record: FlowerRecord) -> None:
        if any(existing.id == record.id for existing in self.records):
            raise DataError(f"Duplicate record ID '{record.id}'.")
        self.records.append(record)
        try:
            self.save_json()
        except DataError:
            self.records.pop()
            raise

    def export_csv(self, path: Path | None = None) -> Path:
        target = path or self.csv_path
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            with target.open("w", newline="", encoding="utf-8") as file:
                writer = csv.DictWriter(file, fieldnames=CSV_FIELDS)
                writer.writeheader()
                for record in self.records:
                    writer.writerow(asdict(record))
        except OSError as exc:
            raise DataError(f"Could not export {target}: {exc}") from exc
        return target

    def import_csv(self, path: Path) -> tuple[int, list[str]]:
        imported: list[FlowerRecord] = []
        errors: list[str] = []
        try:
            with path.open("r", newline="", encoding="utf-8-sig") as file:
                reader = csv.DictReader(file)
                missing = set(CSV_FIELDS) - set(reader.fieldnames or ())
                if missing:
                    raise DataError(
                        f"CSV is missing required columns: {', '.join(sorted(missing))}."
                    )
                for row_number, row in enumerate(reader, start=2):
                    try:
                        imported.append(FlowerRecord.from_mapping(row))
                    except DataError as exc:
                        errors.append(f"Row {row_number}: {exc}")
        except (OSError, UnicodeError, csv.Error) as exc:
            raise DataError(f"Could not import {path}: {exc}") from exc

        known_ids = {record.id for record in self.records}
        unique_imports: list[FlowerRecord] = []
        for record in imported:
            if record.id in known_ids:
                errors.append(f"Record {record.id}: duplicate ID; skipped.")
            else:
                known_ids.add(record.id)
                unique_imports.append(record)
        if unique_imports:
            self.records.extend(unique_imports)
            try:
                self.save_json()
            except DataError:
                del self.records[-len(unique_imports):]
                raise
            self.export_csv()
        return len(unique_imports), errors


def summarize(records: Iterable[FlowerRecord]) -> dict[str, object]:
    items = list(records)
    measurements = {
        name: (
            sum(getattr(record, name) for record in items) / len(items)
            if items
            else None
        )
        for name in MEASUREMENTS
    }
    grouped: dict[str, dict[str, object]] = {}
    for species in SPECIES:
        group = [record for record in items if record.species == species]
        grouped[species] = {
            "count": len(group),
            "means": {
                name: (
                    sum(getattr(record, name) for record in group) / len(group)
                    if group
                    else None
                )
                for name in MEASUREMENTS
            },
        }
    grouped["Unlabeled"] = {
        "count": sum(record.species is None for record in items),
        "means": {
            name: (
                sum(
                    getattr(record, name)
                    for record in items
                    if record.species is None
                )
                / sum(record.species is None for record in items)
                if any(record.species is None for record in items)
                else None
            )
            for name in MEASUREMENTS
        },
    }
    return {"total": len(items), "means": measurements, "groups": grouped}


def search_records(
    records: Iterable[FlowerRecord], keyword: str = "", species: str = ""
) -> list[FlowerRecord]:
    term = keyword.casefold().strip()
    category = species.casefold().strip()
    return [
        record
        for record in records
        if (not category or (record.species or "unlabeled").casefold() == category)
        and (
            not term
            or term in record.id.casefold()
            or term in (record.species or "unlabeled").casefold()
            or any(term in f"{getattr(record, key):g}" for key in MEASUREMENTS)
        )
    ]


def print_records(records: Iterable[FlowerRecord]) -> None:
    items = list(records)
    if not items:
        print("No matching records.")
        return
    print(f"{'ID':<10} {'Sepal L':>8} {'Sepal W':>8} {'Petal L':>8} {'Petal W':>8}  Species")
    print("-" * 75)
    for record in items:
        print(
            f"{record.id:<10} {record.sepal_length_cm:>8.2f} "
            f"{record.sepal_width_cm:>8.2f} {record.petal_length_cm:>8.2f} "
            f"{record.petal_width_cm:>8.2f}  {record.species or 'Unlabeled'}"
        )


def print_summary(records: Iterable[FlowerRecord]) -> None:
    result = summarize(records)
    print(f"\nExecutive summary - {result['total']} records")
    print("Overall averages (cm):")
    for name in MEASUREMENTS:
        value = result["means"][name]  # type: ignore[index]
        print(f"  {MEASUREMENT_LABELS[name]:<15} {value:.2f}" if value else f"  {MEASUREMENT_LABELS[name]:<15} n/a")
    print("\nClass patterns:")
    print(f"{'Class':<20} {'Count':>5} " + " ".join(f"{MEASUREMENT_LABELS[name]:>13}" for name in MEASUREMENTS))
    for species, values in result["groups"].items():  # type: ignore[union-attr]
        means = values["means"]
        formatted = " ".join(
            f"{means[name]:>13.2f}" if means[name] is not None else f"{'n/a':>13}"
            for name in MEASUREMENTS
        )
        print(f"{species:<20} {values['count']:>5} {formatted}")
    print("\nExploration note: compare petal length and width by class; setosa typically")
    print("has smaller petals, while versicolor and virginica show larger measurements.")


def save_summary_report(records: Iterable[FlowerRecord], output_dir: Path) -> list[Path]:
    items = list(records)
    summary = summarize(items)
    output_dir.mkdir(parents=True, exist_ok=True)
    markdown_path = output_dir / "flower_summary.md"
    text_path = output_dir / "flower_summary.txt"
    csv_path = output_dir / "flower_summary.csv"
    lines = [
        "# Flower Measurement Summary",
        "",
        f"Records analyzed: {summary['total']}",
        "",
        "## Overall averages (cm)",
        "",
        "| Measurement | Average |",
        "|---|---:|",
    ]
    for name in MEASUREMENTS:
        value = summary["means"][name]  # type: ignore[index]
        lines.append(f"| {MEASUREMENT_LABELS[name]} | {value:.2f} |" if value is not None else f"| {MEASUREMENT_LABELS[name]} | n/a |")
    lines.extend(["", "## Class patterns", "", "| Class | Count | " + " | ".join(MEASUREMENT_LABELS[n] for n in MEASUREMENTS) + " |", "|---|---:|" + "---:|" * len(MEASUREMENTS)])
    for species, values in summary["groups"].items():  # type: ignore[union-attr]
        means = values["means"]
        averages = " | ".join(
            f"{means[name]:.2f}" if means[name] is not None else "n/a"
            for name in MEASUREMENTS
        )
        lines.append(f"| {species} | {values['count']} | {averages} |")
    report = "\n".join(lines) + "\n"
    try:
        markdown_path.write_text(report, encoding="utf-8")
        text_path.write_text(report.replace("#", "").replace("|", " "), encoding="utf-8")
        with csv_path.open("w", newline="", encoding="utf-8") as file:
            writer = csv.writer(file)
            writer.writerow(("class", "count", *MEASUREMENTS))
            for species, values in summary["groups"].items():  # type: ignore[union-attr]
                writer.writerow(
                    (species, values["count"], *(values["means"][name] for name in MEASUREMENTS))
                )
    except OSError as exc:
        raise DataError(f"Could not write summary reports: {exc}") from exc
    return [markdown_path, text_path, csv_path]


def create_chart(records: Iterable[FlowerRecord], output_path: Path) -> Path:
    try:
        import matplotlib.pyplot as plt
    except ImportError as exc:
        raise DataError(
            "Matplotlib is not installed. Install it with: python -m pip install matplotlib"
        ) from exc
    items = list(records)
    if not items:
        raise DataError("There are no records to plot.")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    fig, axes = plt.subplots(1, 2, figsize=(11, 4.5))
    colors = dict(zip(SPECIES, ("#4e9f71", "#e2a74f", "#8064a2")))
    for species in (*SPECIES, None):
        group = [record for record in items if record.species == species]
        if group:
            label = species or "Unlabeled"
            axes[0].scatter(
                [record.petal_length_cm for record in group],
                [record.petal_width_cm for record in group],
                label=label,
                color=colors.get(species, "#777777"),
                alpha=0.8,
            )
    axes[0].set(xlabel="Petal length (cm)", ylabel="Petal width (cm)", title="Petal measurements by class")
    axes[0].legend(fontsize="small")

    result = summarize(items)
    species_groups = [name for name in SPECIES if result["groups"][name]["count"]]  # type: ignore[index]
    x_positions = list(range(len(species_groups)))
    width = 0.36
    for offset, (name, color) in enumerate((("sepal_length_cm", "#68a0cf"), ("petal_length_cm", "#ef8354"))):
        means = [result["groups"][species]["means"][name] for species in species_groups]  # type: ignore[index]
        axes[1].bar(
            [x + (offset - 0.5) * width for x in x_positions],
            means,
            width,
            label=MEASUREMENT_LABELS[name],
            color=color,
        )
    axes[1].set_xticks(x_positions, [name.replace("Iris-", "") for name in species_groups])
    axes[1].set(ylabel="Average length (cm)", title="Average length by class")
    axes[1].legend(fontsize="small")
    fig.tight_layout()
    try:
        fig.savefig(output_path, dpi=150)
    except OSError as exc:
        raise DataError(f"Could not save chart {output_path}: {exc}") from exc
    finally:
        plt.close(fig)
    return output_path


def prompt_measurement(name: str) -> float:
    low, high = LIMITS[name]
    while True:
        raw = input(f"{MEASUREMENT_LABELS[name]} in cm ({low:g}-{high:g}): ").strip()
        try:
            return validate_measurement(name, raw)
        except DataError as exc:
            print(f"Invalid input: {exc}")


def add_record(store: FlowerStore) -> None:
    values = {name: prompt_measurement(name) for name in MEASUREMENTS}
    print("Species options: " + ", ".join(SPECIES) + " (or press Enter for unlabeled)")
    while True:
        species = input("Species: ").strip()
        if not species or species in SPECIES:
            break
        print(f"Invalid input. Choose one of: {', '.join(SPECIES)}.")
    record = FlowerRecord.create(values, species)
    store.add(record)
    store.export_csv()
    print(f"Saved flower record {record.id}.")


def run_menu(store: FlowerStore) -> None:
    print("\n" + "=" * 58)
    print("  FLOWER MEASUREMENT EXPLORER")
    print("  Explore measurement patterns before classification")
    print("=" * 58)
    while True:
        print(
            "\n1. Add measurement   2. List records   3. Search / filter\n"
            "4. Executive summary 5. Save charts    6. Export reports\n"
            "7. Import CSV        0. Exit"
        )
        choice = input("Choose an option: ").strip()
        try:
            if choice == "1":
                add_record(store)
            elif choice == "2":
                print_records(store.records)
            elif choice == "3":
                keyword = input("Keyword (ID, species, or measurement): ")
                species = input("Class filter (exact species, Unlabeled, or blank): ")
                matches = search_records(store.records, keyword, species)
                print(f"{len(matches)} matching record(s).")
                print_records(matches)
            elif choice == "4":
                print_summary(store.records)
            elif choice == "5":
                path = create_chart(store.records, store.data_dir / "flower_measurements.png")
                print(f"Chart saved to {path}.")
            elif choice == "6":
                paths = save_summary_report(store.records, store.data_dir)
                print("Reports saved: " + ", ".join(str(path) for path in paths))
            elif choice == "7":
                source = Path(input("CSV file path: ").strip().strip('"'))
                count, errors = store.import_csv(source)
                print(f"Imported {count} record(s).")
                for error in errors:
                    print(f"  Warning: {error}")
            elif choice == "0":
                print("Goodbye.")
                return
            else:
                print("Choose one of the listed menu options.")
        except (DataError, OSError) as exc:
            print(f"Operation failed: {exc}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Explore and analyze flower measurements.")
    parser.add_argument(
        "--data-dir",
        type=Path,
        default=Path(__file__).resolve().parent / "data",
        help="Directory for local JSON, CSV, charts, and reports.",
    )
    args = parser.parse_args(argv)
    try:
        store = FlowerStore(args.data_dir)
    except DataError as exc:
        print(f"Startup failed: {exc}", file=sys.stderr)
        return 1
    run_menu(store)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
