from __future__ import annotations

import csv
import io
import json
import re
import statistics
import uuid
from datetime import datetime, timezone
from typing import Any

import numpy as np
import plotly.express as px
import plotly.graph_objects as go
from plotly.subplots import make_subplots

from flower_explorer import (
    DataError,
    FlowerRecord,
    LIMITS,
    MEASUREMENTS,
    MEASUREMENT_LABELS,
    SPECIES,
    validate_measurement,
)


CHART_TYPES = {
    "sepal": "Sepal length vs width",
    "petal": "Petal length vs width",
    "distribution": "Species distribution",
    "comparison": "Measurement comparison",
    "correlation": "Correlation heatmap",
    "histogram": "Measurement histograms",
    "box": "Measurement box plots",
    "pairwise": "Pairwise comparisons",
}

_ALIASES = {
    "sepallength": "sepal_length_cm",
    "sepallengthcm": "sepal_length_cm",
    "sepal_length": "sepal_length_cm",
    "sepalslength": "sepal_length_cm",
    "sepalwidth": "sepal_width_cm",
    "sepalwidthcm": "sepal_width_cm",
    "sepal_width": "sepal_width_cm",
    "petallength": "petal_length_cm",
    "petallengthcm": "petal_length_cm",
    "petal_length": "petal_length_cm",
    "petalwidth": "petal_width_cm",
    "petalwidthcm": "petal_width_cm",
    "petal_width": "petal_width_cm",
    "class": "species",
    "target": "species",
    "variety": "species",
}


def _column_name(raw: str) -> str:
    key = "".join(character for character in raw.casefold() if character.isalnum())
    return _ALIASES.get(key, key)


def parse_upload(filename: str, content: bytes) -> tuple[list[dict[str, Any]], list[str], int]:
    if not filename or len(content) > 8 * 1024 * 1024:
        raise DataError("Choose a file smaller than 8 MB.")
    suffix = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    try:
        text = content.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise DataError("The uploaded file must be UTF-8 encoded.") from exc

    try:
        if suffix == "csv":
            reader = csv.DictReader(io.StringIO(text))
            if not reader.fieldnames:
                raise DataError("The CSV file needs a header row.")
            if len({_column_name(name) for name in reader.fieldnames}) != len(reader.fieldnames):
                raise DataError("The CSV contains duplicate or ambiguous column names.")
            raw_rows = list(reader)
        elif suffix == "json":
            payload = json.loads(text)
            if isinstance(payload, dict):
                raw_rows = payload.get("records")
                if raw_rows is None:
                    raise DataError("JSON object must contain a records list.")
            else:
                raw_rows = payload
            if not isinstance(raw_rows, list):
                raise DataError("JSON must contain a list of records.")
        else:
            raise DataError("Upload a .csv or .json file.")
    except (csv.Error, json.JSONDecodeError) as exc:
        raise DataError(f"Could not parse the upload: {exc}") from exc
    if len(raw_rows) > 10000:
        raise DataError("A single import is limited to 10,000 rows.")

    normalized_rows: list[dict[str, Any]] = []
    errors: list[str] = []
    seen_ids: dict[str, tuple[object, ...]] = {}
    duplicates = 0
    for row_number, raw_row in enumerate(raw_rows, start=2):
        if not isinstance(raw_row, dict):
            errors.append(f"Row {row_number}: expected an object.")
            continue
        if None in raw_row:
            errors.append(f"Row {row_number}: contains unexpected extra CSV fields.")
            continue
        mapped = {_column_name(str(key)): value for key, value in raw_row.items() if key is not None}
        source_id = str(mapped.get("id") or "").strip()
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", source_id):
            source_id = uuid.uuid4().hex[:8]
        record_data: dict[str, Any] = {
            "id": source_id,
            "created_at": str(
                mapped.get("created_at")
                or datetime.now(timezone.utc).isoformat(timespec="seconds")
            ),
            "species": mapped.get("species"),
        }
        missing: list[str] = []
        for name in MEASUREMENTS:
            if name in mapped:
                record_data[name] = mapped[name]
            else:
                missing.append(MEASUREMENT_LABELS[name])
        if missing:
            errors.append(
                f"Row {row_number}: missing measurement columns {', '.join(missing)}."
            )
            continue
        try:
            record = FlowerRecord.from_mapping(record_data)
        except DataError as exc:
            errors.append(f"Row {row_number}: {exc}")
            continue
        if mapped.get("id"):
            signature = tuple(
                [*(getattr(record, name) for name in MEASUREMENTS), record.species]
            )
            previous = seen_ids.get(record.id)
            if previous is not None:
                if signature == previous:
                    duplicates += 1
                else:
                    errors.append(
                        f"Row {row_number}: record ID '{record.id}' is repeated with different measurements."
                    )
                continue
            seen_ids[record.id] = signature
        normalized_rows.append(record.__dict__)
    return normalized_rows, errors, duplicates


def descriptive_summary(records: list[dict[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {"total": len(records), "measurements": {}}
    for name in MEASUREMENTS:
        values = [float(record[name]) for record in records]
        result["measurements"][name] = {
            "mean": statistics.fmean(values) if values else None,
            "median": statistics.median(values) if values else None,
            "min": min(values) if values else None,
            "max": max(values) if values else None,
            "stdev": statistics.stdev(values) if len(values) > 1 else 0.0 if values else None,
        }
    result["species"] = {
        species: sum(record.get("species") == species for record in records)
        for species in SPECIES
    }
    result["unlabeled"] = sum(record.get("species") is None for record in records)
    result["class_means"] = {}
    for species in (*SPECIES, None):
        group = [record for record in records if record.get("species") == species]
        result["class_means"][species or "Unlabeled"] = {
            name: statistics.fmean(float(record[name]) for record in group) if group else None
            for name in MEASUREMENTS
        }
    return result


def quality_report(records: list[dict[str, Any]]) -> dict[str, Any]:
    signatures: set[tuple[object, ...]] = set()
    duplicate_count = 0
    invalid_rows: list[str] = []
    for record in records:
        signature = tuple(
            [*(round(float(record[name]), 2) for name in MEASUREMENTS), record.get("species")]
        )
        if signature in signatures:
            duplicate_count += 1
        signatures.add(signature)
        for name in MEASUREMENTS:
            try:
                validate_measurement(name, record[name])
            except DataError as exc:
                invalid_rows.append(f"{record['id']}: {exc}")
    outliers: set[str] = set()
    for name in MEASUREMENTS:
        values = np.asarray([float(record[name]) for record in records], dtype=float)
        if len(values) < 4:
            continue
        first, third = np.percentile(values, [25, 75])
        spread = third - first
        flagged = (values < first - 1.5 * spread) | (values > third + 1.5 * spread)
        outliers.update(
            records[index]["id"] for index in np.flatnonzero(flagged)
        )
    possible_values = max(1, len(records) * len(MEASUREMENTS))
    missing_count = sum(
        record.get(name) in (None, "") for record in records for name in MEASUREMENTS
    )
    return {
        "total": len(records),
        "missing": missing_count,
        "invalid": invalid_rows,
        "duplicates": duplicate_count,
        "outliers": sorted(outliers),
        "completeness": round(100 * (1 - missing_count / possible_values), 1),
    }


def make_chart(records: list[dict[str, Any]], chart_type: str, species_filter: str = "") -> Any:
    if chart_type not in CHART_TYPES:
        raise DataError("Choose a supported chart.")
    selected = [
        record
        for record in records
        if not species_filter or record.get("species") == species_filter
    ]
    if not selected:
        raise DataError("There are no observations for this chart and filter.")
    import pandas as pd

    frame = pd.DataFrame(selected)
    frame["species_label"] = frame["species"].fillna("Unlabeled")
    palette = {
        "Iris-setosa": "#648d71",
        "Iris-versicolor": "#d4a449",
        "Iris-virginica": "#8f77a8",
        "Unlabeled": "#88928a",
    }
    if chart_type == "sepal":
        fig = px.scatter(
            frame,
            x="sepal_length_cm",
            y="sepal_width_cm",
            color="species_label",
            color_discrete_map=palette,
            hover_data=["id", "petal_length_cm", "petal_width_cm"],
            labels={
                "sepal_length_cm": "Sepal length (cm)",
                "sepal_width_cm": "Sepal width (cm)",
                "species_label": "Class",
            },
            title=CHART_TYPES[chart_type],
        )
    elif chart_type == "petal":
        fig = px.scatter(
            frame,
            x="petal_length_cm",
            y="petal_width_cm",
            color="species_label",
            color_discrete_map=palette,
            hover_data=["id", "sepal_length_cm", "sepal_width_cm"],
            labels={
                "petal_length_cm": "Petal length (cm)",
                "petal_width_cm": "Petal width (cm)",
                "species_label": "Class",
            },
            title=CHART_TYPES[chart_type],
        )
    elif chart_type == "distribution":
        counts = frame["species_label"].value_counts().rename_axis("Class").reset_index(name="Count")
        fig = px.pie(counts, names="Class", values="Count", hole=0.56, color="Class", color_discrete_map=palette, title=CHART_TYPES[chart_type])
    elif chart_type == "comparison":
        means = frame.groupby("species_label", as_index=False)[list(MEASUREMENTS)].mean()
        melted = means.melt(id_vars="species_label", var_name="Measurement", value_name="Average (cm)")
        melted["Measurement"] = melted["Measurement"].map(MEASUREMENT_LABELS)
        fig = px.bar(melted, x="species_label", y="Average (cm)", color="Measurement", barmode="group", title=CHART_TYPES[chart_type])
    elif chart_type == "correlation":
        corr = frame[list(MEASUREMENTS)].corr()
        fig = px.imshow(
            corr,
            text_auto=".2f",
            color_continuous_scale=["#f4f0e5", "#9ab19a", "#315e4b"],
            zmin=-1,
            zmax=1,
            title=CHART_TYPES[chart_type],
            labels={"x": "Measurement", "y": "Measurement", "color": "Correlation"},
        )
        fig.update_xaxes(ticktext=[MEASUREMENT_LABELS[name] for name in MEASUREMENTS], tickvals=list(MEASUREMENTS))
        fig.update_yaxes(ticktext=[MEASUREMENT_LABELS[name] for name in MEASUREMENTS], tickvals=list(MEASUREMENTS))
    elif chart_type == "histogram":
        fig = make_subplots(rows=2, cols=2, subplot_titles=[MEASUREMENT_LABELS[name] for name in MEASUREMENTS])
        for index, name in enumerate(MEASUREMENTS):
            fig.add_trace(
                go.Histogram(
                    x=frame[name],
                    marker_color="#547d63",
                    name=MEASUREMENT_LABELS[name],
                    showlegend=False,
                ),
                row=index // 2 + 1,
                col=index % 2 + 1,
            )
        fig.update_layout(title=CHART_TYPES[chart_type], height=580)
    elif chart_type == "box":
        long_frame = frame.melt(
            id_vars="species_label",
            value_vars=list(MEASUREMENTS),
            var_name="Measurement",
            value_name="Measurement (cm)",
        )
        long_frame["Measurement"] = long_frame["Measurement"].map(MEASUREMENT_LABELS)
        fig = px.box(
            long_frame,
            x="Measurement",
            y="Measurement (cm)",
            color="species_label",
            color_discrete_map=palette,
            title=CHART_TYPES[chart_type],
        )
    else:
        fig = px.scatter_matrix(
            frame,
            dimensions=list(MEASUREMENTS),
            color="species_label",
            color_discrete_map=palette,
            hover_data=["id"],
            title=CHART_TYPES[chart_type],
        )
    fig.update_layout(
        template="plotly_white",
        paper_bgcolor="rgba(0,0,0,0)",
        plot_bgcolor="rgba(255,255,255,.55)",
        font={"family": "Inter, Segoe UI, sans-serif", "color": "#294538"},
        margin={"l": 28, "r": 22, "t": 65, "b": 35},
    )
    return fig


def pattern_analysis(records: list[dict[str, Any]]) -> dict[str, Any]:
    if len(records) < 3:
        raise DataError("At least three records are needed for clustering.")
    matrix = np.asarray(
        [[float(record[name]) for name in MEASUREMENTS] for record in records],
        dtype=float,
    )
    cluster_count = min(3, len(records))
    engine = "scikit-learn"
    outlier_method = "Isolation Forest"
    try:
        from sklearn.cluster import KMeans
        from sklearn.decomposition import PCA
        from sklearn.ensemble import IsolationForest
        from sklearn.neighbors import NearestNeighbors
        from sklearn.preprocessing import StandardScaler

        scaled = StandardScaler().fit_transform(matrix)
        clusters = KMeans(n_clusters=cluster_count, random_state=42, n_init=10).fit(scaled)
        labels = clusters.labels_
        coordinates = PCA(n_components=2, random_state=42).fit_transform(scaled)
        outlier_labels = IsolationForest(
            random_state=42, contamination="auto"
        ).fit_predict(scaled)
        neighbors = NearestNeighbors(n_neighbors=min(4, len(records))).fit(scaled)
        distances, indexes = neighbors.kneighbors(scaled)
    except (ImportError, OSError):
        engine = "NumPy fallback"
        outlier_method = "Robust-distance review"
        scaled = _standardize(matrix)
        labels, centers = _deterministic_kmeans(scaled, cluster_count)
        coordinates = _principal_components(scaled)
        outlier_labels = _robust_outlier_labels(scaled)
        distances, indexes = _nearest_neighbors(scaled, min(4, len(records)))
        clusters = None
    observations = []
    for index, record in enumerate(records):
        nearest = [
            {
                "id": records[neighbor]["id"],
                "distance": round(float(distance), 3),
                "species": records[neighbor].get("species") or "Unlabeled",
            }
            for distance, neighbor in zip(distances[index][1:], indexes[index][1:])
        ]
        observations.append(
            {
                "id": record["id"],
                "species": record.get("species") or "Unlabeled",
                "cluster": int(labels[index]) + 1,
                "pca_x": float(coordinates[index, 0]),
                "pca_y": float(coordinates[index, 1]),
                "outlier": outlier_labels[index] == -1,
                "nearest": nearest,
            }
        )
    return {
        "observations": observations,
        "cluster_counts": {
            f"Cluster {index + 1}": int(count)
            for index, count in enumerate(np.bincount(labels, minlength=cluster_count))
        },
        "outlier_count": int(np.sum(outlier_labels == -1)),
        "engine": engine,
        "outlier_method": outlier_method,
        "explainer": (
            f"{engine} standardizes and groups measurements with K-means without using "
            f"species labels. PCA compresses the measurements to two axes, and "
            f"{outlier_method} highlights unusual combinations. These unsupervised "
            "results are not botanical species classifications."
        ),
    }


def _standardize(matrix: np.ndarray) -> np.ndarray:
    center = matrix.mean(axis=0)
    scale = matrix.std(axis=0)
    scale[scale == 0] = 1
    return (matrix - center) / scale


def _deterministic_kmeans(
    matrix: np.ndarray, cluster_count: int
) -> tuple[np.ndarray, np.ndarray]:
    indexes = np.linspace(0, len(matrix) - 1, cluster_count, dtype=int)
    centers = matrix[indexes].copy()
    labels = np.zeros(len(matrix), dtype=int)
    for _ in range(100):
        distances = np.sum((matrix[:, None, :] - centers[None, :, :]) ** 2, axis=2)
        next_labels = np.argmin(distances, axis=1)
        next_centers = centers.copy()
        for index in range(cluster_count):
            members = matrix[next_labels == index]
            if len(members):
                next_centers[index] = members.mean(axis=0)
        if np.array_equal(next_labels, labels) and np.allclose(next_centers, centers):
            labels = next_labels
            centers = next_centers
            break
        labels = next_labels
        centers = next_centers
    return labels, centers


def _principal_components(matrix: np.ndarray) -> np.ndarray:
    covariance = np.cov(matrix, rowvar=False)
    eigenvalues, eigenvectors = np.linalg.eigh(covariance)
    axes = eigenvectors[:, np.argsort(eigenvalues)[::-1][:2]]
    return matrix @ axes


def _robust_outlier_labels(matrix: np.ndarray) -> np.ndarray:
    median = np.median(matrix, axis=0)
    deviation = np.median(np.abs(matrix - median), axis=0)
    deviation[deviation == 0] = 1
    scores = np.sqrt(np.mean(((matrix - median) / (1.4826 * deviation)) ** 2, axis=1))
    score_median = float(np.median(scores))
    score_mad = float(np.median(np.abs(scores - score_median)))
    limit = score_median + max(3 * 1.4826 * score_mad, 1.0)
    return np.where(scores > limit, -1, 1)


def _nearest_neighbors(
    matrix: np.ndarray, count: int
) -> tuple[np.ndarray, np.ndarray]:
    distances = np.sqrt(np.sum((matrix[:, None, :] - matrix[None, :, :]) ** 2, axis=2))
    indexes = np.argsort(distances, axis=1)[:, :count]
    ordered_distances = np.take_along_axis(distances, indexes, axis=1)
    return ordered_distances, indexes

