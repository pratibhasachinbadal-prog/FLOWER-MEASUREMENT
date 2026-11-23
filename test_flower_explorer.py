import csv
import tempfile
import unittest
from pathlib import Path

from flower_explorer import (
    DataError,
    FlowerRecord,
    FlowerStore,
    MEASUREMENTS,
    create_sample_records,
    save_summary_report,
    search_records,
    summarize,
    validate_measurement,
)


class ValidationTests(unittest.TestCase):
    def test_measurement_range_and_non_finite_values(self):
        self.assertEqual(validate_measurement("sepal_length_cm", "2.345"), 2.35)
        for value in ("not-a-number", "NaN", "inf", "1.9", "9.1"):
            with self.subTest(value=value), self.assertRaises(DataError):
                validate_measurement("sepal_length_cm", value)

    def test_record_rejects_unknown_species(self):
        with self.assertRaises(DataError):
            FlowerRecord.create({name: 2 for name in MEASUREMENTS}, "Rose")


class PersistenceTests(unittest.TestCase):
    def test_first_run_seeds_and_persists_sample_data(self):
        with tempfile.TemporaryDirectory() as directory:
            store = FlowerStore(Path(directory))
            self.assertEqual(len(store.records), 36)
            self.assertTrue((Path(directory) / "flowers.json").exists())
            self.assertTrue((Path(directory) / "flowers.csv").exists())
            reloaded = FlowerStore(Path(directory))
            self.assertEqual(len(reloaded.records), 36)

    def test_corrupt_json_is_reported_without_overwrite(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "flowers.json"
            path.write_text("{broken", encoding="utf-8")
            with self.assertRaises(DataError):
                FlowerStore(Path(directory))
            self.assertEqual(path.read_text(encoding="utf-8"), "{broken")

    def test_invalid_utf8_json_is_reported(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "flowers.json"
            path.write_bytes(b"\xff")
            with self.assertRaises(DataError):
                FlowerStore(Path(directory))

    def test_csv_import_reports_bad_rows_and_keeps_valid_rows(self):
        with tempfile.TemporaryDirectory() as directory:
            store = FlowerStore(Path(directory) / "store")
            csv_path = Path(directory) / "incoming.csv"
            with csv_path.open("w", newline="", encoding="utf-8") as file:
                writer = csv.DictWriter(
                    file,
                    fieldnames=("id", *MEASUREMENTS, "species", "created_at"),
                )
                writer.writeheader()
                writer.writerow({
                    "id": "imported1",
                    "sepal_length_cm": "5.1",
                    "sepal_width_cm": "3.5",
                    "petal_length_cm": "1.4",
                    "petal_width_cm": "0.2",
                    "species": "Iris-setosa",
                    "created_at": "2026-01-01T00:00:00+00:00",
                })
                writer.writerow({
                    "id": "invalid1",
                    "sepal_length_cm": "bad",
                    "sepal_width_cm": "3.5",
                    "petal_length_cm": "1.4",
                    "petal_width_cm": "0.2",
                    "species": "Iris-setosa",
                    "created_at": "2026-01-01T00:00:00+00:00",
                })
            count, errors = store.import_csv(csv_path)
            self.assertEqual(count, 1)
            self.assertEqual(len(errors), 1)
            self.assertIn("imported1", {record.id for record in store.records})


class AnalysisTests(unittest.TestCase):
    def test_summary_and_search_include_unlabeled_records(self):
        records = create_sample_records()[:2]
        values = {name: 5 for name in MEASUREMENTS}
        records.append(FlowerRecord.create(values))
        result = summarize(records)
        self.assertEqual(result["total"], 3)
        self.assertEqual(result["groups"]["Unlabeled"]["count"], 1)
        self.assertEqual(len(search_records(records, species="Unlabeled")), 1)
        self.assertEqual(len(search_records(records, keyword="setosa")), 2)

    def test_summary_reports_are_written_in_all_requested_formats(self):
        with tempfile.TemporaryDirectory() as directory:
            paths = save_summary_report(create_sample_records(), Path(directory))
            self.assertEqual({path.suffix for path in paths}, {".md", ".txt", ".csv"})
            self.assertTrue(all(path.stat().st_size > 0 for path in paths))


if __name__ == "__main__":
    unittest.main()
