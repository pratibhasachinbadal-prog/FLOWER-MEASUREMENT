from __future__ import annotations

import csv
import hashlib
import io
import json
import os
import re
import secrets
import smtplib
from email.message import EmailMessage
from datetime import datetime, timezone
from functools import wraps
from pathlib import Path
from typing import Any, Callable

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
from flask import (
    Flask,
    Response,
    abort,
    flash,
    g,
    make_response,
    redirect,
    render_template,
    request,
    send_file,
    session,
    url_for,
)

from flower_explorer import (
    DataError,
    CSV_FIELDS,
    LIMITS,
    MEASUREMENTS,
    MEASUREMENT_LABELS,
    SPECIES,
    validate_measurement,
)
from flower_web.analytics import (
    CHART_TYPES,
    descriptive_summary,
    make_chart,
    parse_upload,
    pattern_analysis,
    quality_report,
)
from flower_web.database import Database
from flower_web.security import hash_password, verify_password


ROOT = Path(__file__).resolve().parents[1]
NAME_RULE = re.compile(r"^[A-Za-z0-9_]{3,24}$")
EMAIL_RULE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def create_app(config: dict[str, Any] | None = None) -> Flask:
    app = Flask(
        __name__,
        template_folder=str(ROOT / "templates"),
        static_folder=str(ROOT / "static"),
    )
    app.config.update(
        SECRET_KEY=os.environ.get("FLOWER_SECRET_KEY") or secrets.token_hex(32),
        DATABASE=os.environ.get("FLOWER_DATABASE_PATH")
        or str(ROOT / "data" / "flower_explorer.sqlite3"),
        SEED_JSON=str(ROOT / "data" / "iris.json"),
        MAX_CONTENT_LENGTH=8 * 1024 * 1024,
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_SECURE=os.environ.get("FLOWER_COOKIE_SECURE") == "1",
        PERMANENT_SESSION_LIFETIME=8 * 60 * 60,
    )
    if config:
        app.config.update(config)
    database = Database(Path(app.config["DATABASE"]), Path(app.config["SEED_JSON"]))
    app.extensions["flower_database"] = database

    def csrf_token() -> str:
        token = session.get("_csrf_token")
        if not token:
            token = secrets.token_urlsafe(32)
            session["_csrf_token"] = token
        return token

    app.jinja_env.globals["csrf_token"] = csrf_token

    @app.before_request
    def load_user_and_check_csrf() -> Response | None:
        user_id = session.get("user_id")
        g.user = database.get_user(user_id) if isinstance(user_id, int) else None
        if user_id and g.user is None:
            session.clear()
        if request.method == "POST":
            supplied = request.form.get("_csrf_token", "")
            expected = session.get("_csrf_token", "")
            if not expected or not secrets.compare_digest(supplied, expected):
                abort(400, description="Your form session expired. Refresh and try again.")
        return None

    @app.after_request
    def set_security_headers(response: Response) -> Response:
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
        response.headers.setdefault(
            "Content-Security-Policy",
            "default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; "
            "script-src 'self' 'unsafe-inline'; connect-src 'self' data:; font-src 'self' data:",
        )
        return response

    def login_required(view: Callable[..., Any]) -> Callable[..., Any]:
        @wraps(view)
        def wrapped(*args: Any, **kwargs: Any) -> Any:
            if not g.user:
                flash("Please sign in to continue.", "info")
                return redirect(url_for("login", next=request.path))
            return view(*args, **kwargs)

        return wrapped

    def render_workspace(title: str, section: str, **context: Any) -> str:
        return render_template(
            "workspace.html",
            page_title=title,
            section=section,
            current_user=g.user,
            species=SPECIES,
            measurements=MEASUREMENTS,
            measurement_labels=MEASUREMENT_LABELS,
            limits=LIMITS,
            **context,
        )

    def validated_record_form() -> dict[str, object]:
        values: dict[str, object] = {
            name: validate_measurement(name, request.form.get(name, "").strip())
            for name in MEASUREMENTS
        }
        selected_species = request.form.get("species", "").strip()
        if selected_species and selected_species not in SPECIES:
            raise DataError("Choose one of the listed Iris classes or leave it unlabeled.")
        values["species"] = selected_species
        return values

    def safe_records(user_id: int, species_filter: str = "") -> list[dict[str, object]]:
        return database.all_records(user_id, species_filter)

    def send_reset_message(email: str, token: str) -> None:
        host = os.environ.get("FLOWER_SMTP_HOST")
        sender = os.environ.get("FLOWER_SMTP_FROM")
        if not host or not sender:
            raise RuntimeError("Password-reset email is not configured.")
        port = int(os.environ.get("FLOWER_SMTP_PORT", "587"))
        message = EmailMessage()
        message["Subject"] = "Reset your Flower Measurement Explorer password"
        message["From"] = sender
        message["To"] = email
        reset_url = url_for("reset_password", token=token, _external=True)
        message.set_content(
            f"Use this one-time link within 30 minutes to reset your password:\n{reset_url}\n"
        )
        with smtplib.SMTP(host, port, timeout=15) as smtp:
            smtp.starttls()
            username = os.environ.get("FLOWER_SMTP_USERNAME")
            password = os.environ.get("FLOWER_SMTP_PASSWORD")
            if username and password:
                smtp.login(username, password)
            smtp.send_message(message)

    @app.get("/")
    def home() -> str:
        return render_template("welcome.html", page_title="Explore nature through data")

    @app.route("/register", methods=["GET", "POST"])
    def register() -> str | Response:
        if request.method == "POST":
            full_name = request.form.get("full_name", "").strip()
            email = request.form.get("email", "").strip().lower()
            username = request.form.get("username", "").strip()
            password = request.form.get("password", "")
            confirmation = request.form.get("confirm_password", "")
            errors: list[str] = []
            if not full_name or len(full_name) > 80:
                errors.append("Enter your name (up to 80 characters).")
            if not EMAIL_RULE.fullmatch(email):
                errors.append("Enter a valid email address.")
            if not NAME_RULE.fullmatch(username):
                errors.append("Username must be 3-24 letters, numbers, or underscores.")
            if len(password) < 10:
                errors.append("Password must contain at least 10 characters.")
            if len(password) > 1024:
                errors.append("Password must be 1024 characters or fewer.")
            if password != confirmation:
                errors.append("The passwords do not match.")
            if errors:
                for error in errors:
                    flash(error, "error")
            else:
                try:
                    user_id = database.register(full_name, email, username, password)
                except ValueError as exc:
                    flash(str(exc), "error")
                else:
                    session.clear()
                    session.permanent = True
                    session["user_id"] = user_id
                    flash("Your account is ready. Welcome to the Explorer.", "success")
                    return redirect(url_for("dashboard"))
        return render_template("auth.html", page_title="Create your account", mode="register")

    @app.route("/login", methods=["GET", "POST"])
    def login() -> str | Response:
        next_path = request.args.get("next", "")
        if request.method == "POST":
            identifier = request.form.get("identifier", "").strip()
            password = request.form.get("password", "")
            user = database.user_for_login(identifier)
            if not user or not verify_password(password, user["password_hash"]):
                flash("That sign-in did not match our records.", "error")
            else:
                session.clear()
                session.permanent = request.form.get("remember") == "1"
                session["user_id"] = int(user["id"])
                session["_csrf_token"] = secrets.token_urlsafe(32)
                destination = request.form.get("next", "")
                if (
                    not destination.startswith("/")
                    or destination.startswith("//")
                    or "\\" in destination
                ):
                    destination = url_for("dashboard")
                return redirect(destination)
        return render_template(
            "auth.html", page_title="Sign in", mode="login", next_path=next_path
        )

    @app.post("/logout")
    @login_required
    def logout() -> Response:
        session.clear()
        flash("You have been securely signed out.", "success")
        return redirect(url_for("home"))

    @app.route("/forgot-password", methods=["GET", "POST"])
    def forgot_password() -> str | Response:
        if request.method == "POST":
            email = request.form.get("email", "").strip().lower()
            row = database.user_for_login(email) if EMAIL_RULE.fullmatch(email) else None
            if row:
                token = secrets.token_urlsafe(32)
                token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
                database.create_reset_token(int(row["id"]), token_hash)
                try:
                    send_reset_message(email, token)
                except (OSError, RuntimeError, smtplib.SMTPException, ValueError):
                    app.logger.exception("Password reset email could not be sent")
            flash(
                "If the account exists and email delivery is configured, a reset link will arrive shortly.",
                "info",
            )
            return redirect(url_for("forgot_password"))
        return render_template("auth.html", page_title="Reset your password", mode="forgot")

    @app.route("/reset/<token>", methods=["GET", "POST"])
    def reset_password(token: str) -> str | Response:
        if request.method == "POST":
            password = request.form.get("password", "")
            confirmation = request.form.get("confirm_password", "")
            if len(password) < 10 or len(password) > 1024 or password != confirmation:
                flash("Use a matching password with at least 10 characters.", "error")
            else:
                token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
                user_id = database.consume_reset_token(token_hash)
                if user_id is None:
                    flash("That reset link is invalid or has expired.", "error")
                    return redirect(url_for("forgot_password"))
                database.update_password(user_id, password)
                flash("Password updated. You can now sign in.", "success")
                return redirect(url_for("login"))
        return render_template("auth.html", page_title="Choose a new password", mode="reset")

    @app.get("/app")
    @login_required
    def app_home() -> Response:
        return redirect(url_for("dashboard"))

    @app.get("/app/dashboard")
    @login_required
    def dashboard() -> str:
        metrics = database.metrics(int(g.user["id"]))
        records, _ = database.records(int(g.user["id"]), page=1, per_page=6)
        all_rows = safe_records(int(g.user["id"]))
        chart_html = ""
        if all_rows:
            chart_html = make_chart(all_rows, "petal").to_html(
                full_html=False,
                include_plotlyjs="/static/vendor/plotly.min.js",
                config={"displayModeBar": False},
            )
        return render_workspace(
            "Dashboard",
            "dashboard",
            metrics=metrics,
            recent_records=records,
            chart_html=chart_html,
            dataset_count=database.dataset_count(int(g.user["id"])),
            photos=("botanical-hero.jpg", "flower-study.jpg", "petal-detail.jpg"),
        )

    @app.get("/app/explorer")
    @login_required
    def explorer() -> str:
        page = max(request.args.get("page", 1, type=int), 1)
        query = request.args.get("q", "").strip()[:100]
        selected_species = request.args.get("species", "")
        if selected_species not in (*SPECIES, "Unlabeled", ""):
            selected_species = ""
        sort = request.args.get("sort", "created_at")
        descending = request.args.get("direction", "desc") != "asc"
        records, total = database.records(
            int(g.user["id"]),
            query=query,
            species=selected_species,
            sort=sort,
            descending=descending,
            page=page,
        )
        return render_workspace(
            "Flower Explorer",
            "explorer",
            records=records,
            total=total,
            page=page,
            pages=max(1, (total + 11) // 12),
            query=query,
            selected_species=selected_species,
            selected_sort=sort,
        )

    @app.route("/app/add", methods=["GET", "POST"])
    @login_required
    def add_record() -> str | Response:
        if request.method == "POST":
            try:
                values = validated_record_form()
                record_id = database.save_record(int(g.user["id"]), values)
            except (DataError, ValueError) as exc:
                flash(str(exc), "error")
                return render_workspace("Add measurements", "record_form", record=None)
            flash(f"Measurement {record_id} saved.", "success")
            return redirect(url_for("explorer"))
        return render_workspace("Add measurements", "record_form", record=None)

    @app.route("/app/records/<record_id>/edit", methods=["GET", "POST"])
    @login_required
    def edit_record(record_id: str) -> str | Response:
        record = database.get_record(int(g.user["id"]), record_id)
        if not record or record["_owner_id"] != int(g.user["id"]):
            abort(404)
        if request.method == "POST":
            try:
                values = validated_record_form()
                database.save_record(int(g.user["id"]), values, record_id)
            except (DataError, ValueError, LookupError) as exc:
                flash(str(exc), "error")
                return render_workspace("Edit measurement", "record_form", record=record)
            flash("Measurement updated.", "success")
            return redirect(url_for("explorer"))
        return render_workspace("Edit measurement", "record_form", record=record)

    @app.post("/app/records/<record_id>/delete")
    @login_required
    def delete_record(record_id: str) -> Response:
        if not database.delete_record(int(g.user["id"]), record_id):
            abort(404)
        flash("Measurement deleted.", "success")
        return redirect(url_for("explorer"))

    @app.get("/app/datasets")
    @login_required
    def datasets() -> str:
        records, total = database.records(int(g.user["id"]), page=1, per_page=8)
        return render_workspace(
            "Dataset management",
            "datasets",
            records=records,
            total=total,
            datasets=database.datasets(int(g.user["id"])),
            history=database.import_history(int(g.user["id"])),
        )

    @app.route("/app/import", methods=["GET", "POST"])
    @login_required
    def import_dataset() -> str | Response:
        preview = None
        if request.method == "POST":
            action = request.form.get("action", "preview")
            if action == "commit":
                import_id = request.form.get("import_id", "")
                try:
                    result = database.commit_import(int(g.user["id"]), import_id)
                except LookupError as exc:
                    flash(str(exc), "error")
                    return redirect(url_for("import_dataset"))
                flash(
                    f"Import finished: {result['imported']} added, {result['invalid']} invalid, "
                    f"{result['duplicates']} duplicate rows.",
                    "success" if result["imported"] else "info",
                )
                return render_workspace(
                    "Import dataset",
                    "import",
                    result=result,
                    datasets=database.datasets(int(g.user["id"])),
                )
            uploaded = request.files.get("file")
            if not uploaded or not uploaded.filename:
                flash("Choose a CSV or JSON file to preview.", "error")
            else:
                try:
                    rows, errors, duplicates = parse_upload(
                        uploaded.filename, uploaded.read()
                    )
                    import_id = database.stage_import(
                        int(g.user["id"]),
                        Path(uploaded.filename).name[:180],
                        rows,
                        errors,
                        duplicates,
                    )
                    preview = {
                        "id": import_id,
                        "file_name": Path(uploaded.filename).name,
                        "valid": len(rows),
                        "invalid": len(errors),
                        "duplicates": duplicates,
                        "errors": errors[:100],
                        "sample": rows[:5],
                    }
                except (DataError, OSError) as exc:
                    flash(str(exc), "error")
        return render_workspace(
            "Import dataset",
            "import",
            preview=preview,
            datasets=database.datasets(int(g.user["id"])),
        )

    @app.get("/app/visualizations")
    @login_required
    def visualizations() -> str:
        chart_type = request.args.get("chart", "petal")
        if chart_type not in CHART_TYPES:
            chart_type = "petal"
        selected_species = request.args.get("species", "")
        if selected_species not in (*SPECIES, ""):
            selected_species = ""
        records = safe_records(int(g.user["id"]))
        chart_html = ""
        chart_error = ""
        try:
            chart_html = make_chart(records, chart_type, selected_species).to_html(
                full_html=False,
                include_plotlyjs="/static/vendor/plotly.min.js",
                config={"responsive": True, "displaylogo": False},
            )
        except DataError as exc:
            chart_error = str(exc)
        return render_workspace(
            "Visualization lab",
            "visualizations",
            chart_html=chart_html,
            chart_error=chart_error,
            chart_types=CHART_TYPES,
            selected_chart=chart_type,
            selected_species=selected_species,
        )

    @app.get("/app/visualizations/chart.png")
    @login_required
    def chart_png() -> Response:
        chart_type = request.args.get("chart", "petal")
        selected_species = request.args.get("species", "")
        records = safe_records(int(g.user["id"]), selected_species)
        if not records or chart_type not in CHART_TYPES:
            abort(400, description="Select a valid chart and dataset.")
        figure, axes = plt.subplots(figsize=(8, 5), dpi=150)
        colors = {
            "Iris-setosa": "#648d71",
            "Iris-versicolor": "#d4a449",
            "Iris-virginica": "#8f77a8",
            None: "#88928a",
        }
        if chart_type in ("sepal", "petal"):
            first, second = (
                ("sepal_length_cm", "sepal_width_cm")
                if chart_type == "sepal"
                else ("petal_length_cm", "petal_width_cm")
            )
            for class_name in (*SPECIES, None):
                group = [record for record in records if record["species"] == class_name]
                if group:
                    axes.scatter(
                        [record[first] for record in group],
                        [record[second] for record in group],
                        color=colors[class_name],
                        alpha=0.8,
                        label=class_name or "Unlabeled",
                    )
            axes.set_xlabel(MEASUREMENT_LABELS[first] + " (cm)")
            axes.set_ylabel(MEASUREMENT_LABELS[second] + " (cm)")
            axes.legend()
        elif chart_type in ("histogram", "box"):
            columns = [[record[name] for record in records] for name in MEASUREMENTS]
            if chart_type == "histogram":
                for name, values in zip(MEASUREMENTS, columns):
                    axes.hist(values, bins=12, alpha=0.45, label=MEASUREMENT_LABELS[name])
                axes.legend()
            else:
                axes.boxplot(columns, tick_labels=[MEASUREMENT_LABELS[name] for name in MEASUREMENTS])
                axes.tick_params(axis="x", labelrotation=15)
        else:
            names = list(MEASUREMENTS)
            matrix = np.asarray([[record[name] for name in names] for record in records])
            if chart_type == "correlation":
                image = axes.imshow(np.corrcoef(matrix, rowvar=False), vmin=-1, vmax=1, cmap="YlGn")
                figure.colorbar(image, ax=axes, label="Correlation")
                axes.set_xticks(range(len(names)), [MEASUREMENT_LABELS[name] for name in names], rotation=25, ha="right")
                axes.set_yticks(range(len(names)), [MEASUREMENT_LABELS[name] for name in names])
            else:
                means = {
                    class_name: [
                        float(np.mean([row[name] for row in records if row["species"] == class_name]))
                        if any(row["species"] == class_name for row in records)
                        else 0
                        for name in names
                    ]
                    for class_name in (*SPECIES, None)
                }
                x = np.arange(len(names))
                width = 0.18
                for index, (class_name, values) in enumerate(means.items()):
                    if any(values):
                        axes.bar(
                            x + (index - 1.5) * width,
                            values,
                            width,
                            label=class_name or "Unlabeled",
                            color=colors[class_name],
                        )
                axes.set_xticks(x, [MEASUREMENT_LABELS[name] for name in names], rotation=18, ha="right")
                axes.legend()
        axes.set_title(CHART_TYPES[chart_type])
        axes.grid(axis="y", alpha=0.15)
        figure.tight_layout()
        output = io.BytesIO()
        figure.savefig(output, format="png", facecolor="#fbfaf5", bbox_inches="tight")
        plt.close(figure)
        output.seek(0)
        return send_file(
            output,
            mimetype="image/png",
            as_attachment=True,
            download_name=f"flower-{chart_type}.png",
        )

    @app.get("/app/analysis")
    @login_required
    def analysis() -> str:
        records = safe_records(int(g.user["id"]))
        result = None
        chart_html = ""
        analysis_error = ""
        if records:
            try:
                result = pattern_analysis(records)
                import plotly.express as px

                observations = result["observations"]
                import pandas as pd

                frame = pd.DataFrame(observations)
                frame["outlier_label"] = frame["outlier"].map(
                    {True: "Potential outlier", False: "Typical observation"}
                )
                figure = px.scatter(
                    frame,
                    x="pca_x",
                    y="pca_y",
                    color="cluster",
                    symbol="outlier_label",
                    hover_data=["species", "cluster"],
                    color_discrete_sequence=["#648d71", "#d4a449", "#8f77a8"],
                    labels={"pca_x": "Principal component 1", "pca_y": "Principal component 2"},
                    title="Unsupervised measurement clusters",
                )
                chart_html = figure.to_html(
                    full_html=False,
                    include_plotlyjs="/static/vendor/plotly.min.js",
                    config={"responsive": True},
                )
                database.analysis_log(
                    int(g.user["id"]),
                    "K-means / PCA / outlier analysis",
                    json.dumps(
                        {
                            "clusters": result["cluster_counts"],
                            "outliers": result["outlier_count"],
                        }
                    ),
                )
            except (DataError, ValueError) as exc:
                analysis_error = str(exc)
        return render_workspace(
            "AI pattern analysis",
            "analysis",
            result=result,
            chart_html=chart_html,
            analysis_error=analysis_error,
        )

    @app.get("/app/reports")
    @login_required
    def reports() -> str:
        records = safe_records(int(g.user["id"]))
        summary = descriptive_summary(records)
        quality = quality_report(records)
        return render_workspace(
            "Executive summary",
            "reports",
            summary=summary,
            quality=quality,
        )

    @app.get("/app/export/<file_format>")
    @login_required
    def export_report(file_format: str) -> Response:
        records = safe_records(int(g.user["id"]))
        summary = descriptive_summary(records)
        quality = quality_report(records)
        report = {"summary": summary, "quality": quality}
        if file_format == "json":
            body = json.dumps(report, indent=2, allow_nan=False)
            mimetype = "application/json"
        elif file_format == "csv":
            output = io.StringIO(newline="")
            writer = csv.writer(output)
            writer.writerow(
                ("measurement", "mean_cm", "median_cm", "min_cm", "max_cm", "stdev_cm")
            )
            for name in MEASUREMENTS:
                stats = summary["measurements"][name]
                writer.writerow(
                    (
                        MEASUREMENT_LABELS[name],
                        stats["mean"],
                        stats["median"],
                        stats["min"],
                        stats["max"],
                        stats["stdev"],
                    )
                )
            body = output.getvalue()
            mimetype = "text/csv"
        elif file_format in ("md", "txt"):
            lines = [
                "# Flower Measurement Executive Summary",
                "",
                f"Total observations: {summary['total']}",
                f"Data completeness: {quality['completeness']}%",
                f"Potential outliers: {len(quality['outliers'])}",
                f"Duplicate measurements: {quality['duplicates']}",
                "",
                "| Measurement | Mean | Median | Min | Max | Standard deviation |",
                "|---|---:|---:|---:|---:|---:|",
            ]
            for name in MEASUREMENTS:
                stats = summary["measurements"][name]
                values = [
                    f"{stats[key]:.2f}" if stats[key] is not None else "n/a"
                    for key in ("mean", "median", "min", "max", "stdev")
                ]
                lines.append(f"| {MEASUREMENT_LABELS[name]} | " + " | ".join(values) + " |")
            lines.extend(["", "## Class distribution", ""])
            lines.extend(
                f"- {species}: {count}"
                for species, count in {**summary["species"], "Unlabeled": summary["unlabeled"]}.items()
            )
            if file_format == "txt":
                lines = [line.replace("#", "").replace("|", " ") for line in lines]
                mimetype = "text/plain"
            else:
                mimetype = "text/markdown"
            body = "\n".join(lines) + "\n"
        else:
            abort(404)
        return Response(
            body,
            mimetype=mimetype,
            headers={"Content-Disposition": f'attachment; filename="flower-summary.{file_format}"'},
        )

    @app.get("/app/data-export/<file_format>")
    @login_required
    def export_dataset(file_format: str) -> Response:
        records = [
            {key: value for key, value in row.items() if not key.startswith("_")}
            for row in safe_records(int(g.user["id"]))
        ]
        if file_format == "json":
            body = json.dumps(
                {"schema_version": 1, "records": records}, indent=2, allow_nan=False
            )
            mimetype = "application/json"
        elif file_format == "csv":
            output = io.StringIO(newline="")
            writer = csv.DictWriter(output, fieldnames=CSV_FIELDS)
            writer.writeheader()
            writer.writerows(records)
            body = output.getvalue()
            mimetype = "text/csv"
        else:
            abort(404)
        return Response(
            body,
            mimetype=mimetype,
            headers={
                "Content-Disposition": f'attachment; filename="flower-observations.{file_format}"'
            },
        )

    @app.get("/app/backup")
    @login_required
    def account_backup() -> Response:
        user_id = int(g.user["id"])
        payload = {
            "schema_version": 1,
            "exported_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "account": {
                "username": g.user["username"],
                "full_name": g.user["full_name"],
            },
            "records": [
                {key: value for key, value in row.items() if not key.startswith("_")}
                for row in safe_records(user_id)
            ],
        }
        return Response(
            json.dumps(payload, indent=2, allow_nan=False),
            mimetype="application/json",
            headers={"Content-Disposition": 'attachment; filename="flower-account-backup.json"'},
        )

    @app.get("/app/quality")
    @login_required
    def data_quality() -> str:
        records = safe_records(int(g.user["id"]))
        return render_workspace(
            "Data quality monitor",
            "quality",
            quality=quality_report(records),
            import_history=database.import_history(int(g.user["id"])),
        )

    @app.route("/app/settings", methods=["GET", "POST"])
    @login_required
    def settings() -> str | Response:
        if request.method == "POST":
            action = request.form.get("action")
            if action == "profile":
                full_name = request.form.get("full_name", "").strip()
                if not full_name or len(full_name) > 80:
                    flash("Enter a name up to 80 characters.", "error")
                else:
                    database.update_profile(int(g.user["id"]), full_name)
                    flash("Profile updated.", "success")
                    return redirect(url_for("settings"))
            elif action == "password":
                user = database.user_for_login(str(g.user["username"]))
                old_password = request.form.get("current_password", "")
                new_password = request.form.get("new_password", "")
                confirmation = request.form.get("confirm_password", "")
                if not user or not verify_password(old_password, user["password_hash"]):
                    flash("Current password is incorrect.", "error")
                elif len(new_password) < 10 or len(new_password) > 1024 or new_password != confirmation:
                    flash(
                        "New passwords must match and contain 10-1024 characters.",
                        "error",
                    )
                else:
                    database.update_password(int(g.user["id"]), new_password)
                    flash("Password updated.", "success")
                    return redirect(url_for("settings"))
        return render_workspace("Settings", "settings")

    @app.get("/app/about")
    @login_required
    def about() -> str:
        return render_workspace("About the project", "about")

    @app.errorhandler(413)
    def too_large(_error: Exception) -> tuple[str, int]:
        return render_template(
            "error.html",
            page_title="Upload too large",
            message="Uploads are limited to 8 MB.",
        ), 413

    return app


app = create_app()


if __name__ == "__main__":
    app.run(
        host=os.environ.get("FLOWER_HOST", "127.0.0.1"),
        port=int(os.environ.get("FLOWER_PORT", "5000")),
        debug=os.environ.get("FLOWER_DEBUG") == "1",
    )

