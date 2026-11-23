import csv
import io
import json
import re
import tempfile
import unittest
from pathlib import Path

from flower_web.analytics import parse_upload, pattern_analysis
from flower_web.app import create_app
from flower_web.database import Database
from flower_web.security import verify_password


ROOT = Path(__file__).resolve().parents[1]


def csrf_from(response):
    match = re.search(rb'name="_csrf_token" value="([^"]+)"', response.data)
    if not match:
        raise AssertionError("CSRF token not found in rendered form")
    return match.group(1).decode("ascii")


class WebAppTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.database_path = Path(self.temporary.name) / "web.sqlite3"
        self.app = create_app(
            {
                "TESTING": True,
                "SECRET_KEY": "test-key-not-for-production",
                "DATABASE": str(self.database_path),
                "SEED_JSON": str(ROOT / "data" / "iris.json"),
            }
        )
        self.client = self.app.test_client()

    def tearDown(self):
        self.temporary.cleanup()

    def register(self, client=None, username="botanist", email="botanist@example.org"):
        client = client or self.client
        response = client.get("/register")
        return client.post(
            "/register",
            data={
                "_csrf_token": csrf_from(response),
                "full_name": "Ava Botanist",
                "email": email,
                "username": username,
                "password": "PetalGarden2026!",
                "confirm_password": "PetalGarden2026!",
            },
            follow_redirects=True,
        )

    def post_token(self, path, data=None, client=None, **extra):
        client = client or self.client
        response = client.get(path)
        payload = {"_csrf_token": csrf_from(response), **(data or {}), **extra}
        return client.post(path, data=payload, follow_redirects=False)

    def add_observation(self, client=None, **overrides):
        values = {
            "sepal_length_cm": "8.0",
            "sepal_width_cm": "4.0",
            "petal_length_cm": "6.0",
            "petal_width_cm": "2.5",
            "species": "Iris-virginica",
        }
        values.update(overrides)
        return self.post_token("/app/add", values, client=client)

    def test_auth_registration_login_and_password_hashing(self):
        response = self.register()
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Dashboard", response.data)
        database = self.app.extensions["flower_database"]
        row = database.user_for_login("botanist")
        self.assertTrue(row["password_hash"].startswith("scrypt$"))
        self.assertNotEqual(row["password_hash"], "PetalGarden2026!")
        self.assertTrue(verify_password("PetalGarden2026!", row["password_hash"]))

        duplicate_client = self.app.test_client()
        duplicate = self.register(
            duplicate_client,
            username="another",
            email="botanist@example.org",
        )
        self.assertIn(b"already registered", duplicate.data)
        duplicate_client = self.app.test_client()
        duplicate = self.register(
            duplicate_client,
            username="botanist",
            email="different@example.org",
        )
        self.assertIn(b"already registered", duplicate.data)

        guest = self.app.test_client()
        login_page = guest.get("/login")
        wrong_password = guest.post(
            "/login",
            data={
                "_csrf_token": csrf_from(login_page),
                "identifier": "botanist",
                "password": "incorrect",
            },
            follow_redirects=True,
        )
        self.assertIn(b"did not match", wrong_password.data)
        login_page = guest.get("/login")
        login = guest.post(
            "/login",
            data={
                "_csrf_token": csrf_from(login_page),
                "identifier": "botanist@example.org",
                "password": "PetalGarden2026!",
                "remember": "1",
            },
            follow_redirects=True,
        )
        self.assertEqual(login.status_code, 200)
        self.assertIn(b"Dashboard", login.data)

    def test_auth_pages_render_botanical_tabs_and_password_strength(self):
        login_page = self.app.test_client().get("/login")
        self.assertIn(b"assets/flowers/purple-iris.jpg", login_page.data)
        self.assertIn(b"Discover Nature Through Data", login_page.data)
        self.assertIn(b"Analyze Flower Data", login_page.data)
        self.assertIn(b"Visualize Patterns", login_page.data)
        self.assertIn(b"Explore with AI", login_page.data)
        self.assertIn(b"Manage Datasets", login_page.data)
        self.assertIn(b'aria-current="page">Sign in', login_page.data)

        register_page = self.app.test_client().get("/register")
        self.assertIn(b'aria-current="page">Create account', register_page.data)
        self.assertIn(b'data-strength-input', register_page.data)
        self.assertIn(b'data-strength-fill', register_page.data)
        self.assertEqual(register_page.data.count(b'data-password-toggle'), 2)

    def test_public_and_workspace_pages_receive_background_classes(self):
        for path, body_class in (
            ("/", "public-home"),
            ("/login", "public-login"),
            ("/register", "public-register"),
        ):
            with self.subTest(path=path):
                response = self.client.get(path)
                self.assertEqual(response.status_code, 200)
                self.assertIn(f'<body class="public-page {body_class}">'.encode(), response.data)

        self.register()
        pages = (
            ("/app/dashboard", "dashboard"),
            ("/app/explorer", "explorer"),
            ("/app/add", "record_form"),
            ("/app/visualizations", "visualizations"),
            ("/app/analysis", "analysis"),
            ("/app/datasets", "datasets"),
            ("/app/import", "import"),
            ("/app/reports", "reports"),
            ("/app/quality", "quality"),
            ("/app/settings", "settings"),
            ("/app/about", "about"),
        )
        for path, section in pages:
            with self.subTest(path=path):
                response = self.client.get(path)
                self.assertEqual(response.status_code, 200)
                self.assertIn(f'<body class="has-sidebar page-{section}">'.encode(), response.data)

        css = (ROOT / "static" / "app.css").read_text(encoding="utf-8")
        page_classes = ("public-home", "public-login", "public-register") + tuple(
            f"page-{section}" for _, section in pages
        )
        for page_class in page_classes:
            with self.subTest(page_class=page_class):
                self.assertIn(f"body.{page_class}", css)

        image_paths = re.findall(r"""url\(["']?(/static/[^"')]+)""", css)
        for image_path in image_paths:
            with self.subTest(image_path=image_path):
                self.assertTrue((ROOT / "static" / image_path.removeprefix("/static/")).is_file())

    def test_csrf_protects_state_changing_routes(self):
        self.register()
        response = self.client.post("/app/add", data={"sepal_length_cm": "5"})
        self.assertEqual(response.status_code, 400)

    def test_web_measurement_validation_and_logout(self):
        self.register()
        response = self.post_token(
            "/app/add",
            {
                "sepal_length_cm": "-1",
                "sepal_width_cm": "3.2",
                "petal_length_cm": "1.5",
                "petal_width_cm": "0.2",
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"between 2 and 9 cm", response.data)
        database = self.app.extensions["flower_database"]
        self.assertEqual(database.metrics(database.user_for_login("botanist")["id"])["total"], 150)
        explorer = self.client.get("/app/explorer")
        logout = self.client.post(
            "/logout", data={"_csrf_token": csrf_from(explorer)}, follow_redirects=True
        )
        self.assertIn(b"securely signed out", logout.data)
        self.assertEqual(self.client.get("/app/dashboard").status_code, 302)

    def test_user_scoped_crud_dashboard_and_backup(self):
        self.register()
        before = self.client.get("/app/dashboard")
        self.assertEqual(before.status_code, 200)
        self.assertIn(b"150", before.data)
        self.assertIn(b"Average sepal length", before.data)

        self.add_observation()
        database = self.app.extensions["flower_database"]
        user = database.user_for_login("botanist")
        owned = next(
            row for row in database.all_records(user["id"])
            if row["_owner_id"] == user["id"]
        )
        record_id = owned["id"]
        edit_path = f"/app/records/{record_id}/edit"
        self.post_token(
            edit_path,
            {
                "sepal_length_cm": "7.9",
                "sepal_width_cm": "3.9",
                "petal_length_cm": "5.9",
                "petal_width_cm": "2.4",
                "species": "Iris-virginica",
            },
        )
        updated = database.get_record(user["id"], record_id)
        self.assertEqual(updated["sepal_length_cm"], 7.9)
        self.assertEqual(database.metrics(user["id"])["total"], 151)

        other_client = self.app.test_client()
        self.register(other_client, username="second_user", email="second@example.org")
        other_user = database.user_for_login("second_user")
        self.assertIsNone(database.get_record(other_user["id"], record_id))
        denied = other_client.get(edit_path)
        self.assertEqual(denied.status_code, 404)

        backup = self.client.get("/app/backup")
        payload = json.loads(backup.data)
        self.assertEqual(len(payload["records"]), 151)
        self.assertNotIn("password_hash", backup.data.decode("utf-8"))
        explorer_page = self.client.get("/app/explorer")
        deleted = self.client.post(
            f"/app/records/{record_id}/delete",
            data={"_csrf_token": csrf_from(explorer_page)},
        )
        self.assertEqual(deleted.status_code, 302)
        self.assertIsNone(database.get_record(user["id"], record_id))

    def test_import_preview_commits_only_valid_nonduplicate_rows(self):
        self.register()
        content = (
            "id,sepal_length,sepal_width,petal_length,petal_width,species\n"
            "field-001,8.0,4.0,6.0,2.5,Iris-virginica\n"
            "field-001,8.0,4.0,6.0,2.5,Iris-virginica\n"
            "field-002,bad,4.0,6.0,2.5,Iris-virginica\n"
        ).encode("utf-8")
        page = self.client.get("/app/import")
        response = self.client.post(
            "/app/import",
            data={
                "_csrf_token": csrf_from(page),
                "action": "preview",
                "file": (io.BytesIO(content), "field-notes.csv"),
            },
            content_type="multipart/form-data",
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"valid rows", response.data)
        self.assertIn(b"invalid rows", response.data)
        import_id = re.search(rb'name="import_id" value="([^"]+)"', response.data).group(1).decode()
        token = csrf_from(response)
        complete = self.client.post(
            "/app/import",
            data={"_csrf_token": token, "action": "commit", "import_id": import_id},
            follow_redirects=True,
        )
        self.assertIn(b"1 added, 1 invalid, 1 duplicate", complete.data)
        database = self.app.extensions["flower_database"]
        self.assertEqual(database.dataset_count(database.user_for_login("botanist")["id"]), 2)

    def test_json_import_parser_handles_real_iris_record_and_rejects_bad_schema(self):
        source = (ROOT / "data" / "iris.json").read_bytes()
        records, errors, duplicates = parse_upload("iris.json", source)
        self.assertEqual(len(records), 150)
        self.assertEqual(errors, [])
        self.assertEqual(duplicates, 0)
        with self.assertRaises(ValueError):
            parse_upload("bad.json", b'{"flowers": []}')
        malicious = (
            b'id,sepal_length_cm,sepal_width_cm,petal_length_cm,petal_width_cm\n'
            b'<script>,5,3,1,0.2\n'
        )
        parsed, errors, _ = parse_upload("safe.csv", malicious)
        self.assertEqual(errors, [])
        self.assertNotIn("<script>", parsed[0]["id"])

    def test_interactive_charts_png_ai_reports_and_quality(self):
        self.register()
        self.assertEqual(self.client.get("/app/visualizations").status_code, 200)
        plotly_asset = self.client.get("/static/vendor/plotly.min.js")
        self.assertEqual(plotly_asset.status_code, 200)
        self.assertGreater(len(plotly_asset.data), 1_000_000)
        plotly_asset.close()
        chart = self.client.get("/app/visualizations/chart.png?chart=petal")
        self.assertEqual(chart.status_code, 200)
        self.assertEqual(chart.mimetype, "image/png")
        self.assertEqual(self.client.get("/app/analysis").status_code, 200)
        self.assertEqual(self.client.get("/app/quality").status_code, 200)
        self.assertEqual(self.client.get("/app/reports").status_code, 200)
        exported_json = self.client.get("/app/data-export/json")
        self.assertEqual(len(json.loads(exported_json.data)["records"]), 150)
        self.assertEqual(self.client.get("/app/data-export/csv").mimetype, "text/csv")
        for file_format, mimetype in (
            ("csv", "text/csv"),
            ("json", "application/json"),
            ("md", "text/markdown"),
            ("txt", "text/plain"),
        ):
            response = self.client.get(f"/app/export/{file_format}")
            self.assertEqual(response.status_code, 200)
            self.assertTrue(response.mimetype.startswith(mimetype))

    def test_password_reset_page_does_not_disclose_account_existence(self):
        response = self.post_token(
            "/forgot-password",
            {"email": "not-registered@example.org"},
            client=self.app.test_client(),
        )
        self.assertEqual(response.status_code, 302)
        page = self.app.test_client().get("/forgot-password", follow_redirects=True)
        self.assertEqual(page.status_code, 200)


class DatabaseTests(unittest.TestCase):
    def test_sqlite_persistence_and_account_separation(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "records.sqlite3"
            first = Database(path, ROOT / "data" / "iris.json")
            first.register("A User", "a@example.org", "user_a", "long-password-a")
            self.assertEqual(first.metrics(1)["total"], 150)
            second = Database(path, ROOT / "data" / "iris.json")
            self.assertEqual(second.metrics(1)["total"], 150)

    def test_deterministic_pattern_analysis(self):
        with (ROOT / "data" / "iris.json").open(encoding="utf-8") as file:
            records = json.load(file)["records"]
        result = pattern_analysis(records)
        self.assertEqual(len(result["observations"]), 150)
        self.assertEqual(sum(result["cluster_counts"].values()), 150)
        self.assertIn("species labels", result["explainer"])
        self.assertEqual(pattern_analysis(records)["cluster_counts"], result["cluster_counts"])


if __name__ == "__main__":
    unittest.main()
