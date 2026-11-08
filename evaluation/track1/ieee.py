"""Read-only IEEE-CIS adapter without dataset download or source-row output."""

from __future__ import annotations

import csv
import os
from pathlib import Path
from typing import Iterable

from .models import EvaluationDataset, EvaluationRecord, EvaluationTruth
from .tokens import optional_hmac_token


TRANSACTION_FILE = "train_transaction.csv"
IDENTITY_FILE = "train_identity.csv"
REQUIRED_TRANSACTION_COLUMNS = {"TransactionID", "TransactionDT", "isFraud"}


def default_ieee_data_dir() -> Path:
    configured = os.getenv("TD_IEEE_CIS_DIR") or os.getenv("TD_EVALUATION_DATA_ROOT")
    root = Path(configured).expanduser() if configured else Path.home() / ".ticket-defender" / "private-data"
    return root if root.name == "ieee-cis" else root / "ieee-cis"


def _read_rows(path: Path, *, limit: int | None = None) -> tuple[list[dict[str, str]], tuple[str, ...]]:
    with path.open("r", newline="", encoding="utf-8-sig") as stream:
        reader = csv.DictReader(stream)
        if reader.fieldnames is None:
            raise ValueError(f"CSV header is missing: {path.name}")
        rows: list[dict[str, str]] = []
        for row in reader:
            rows.append(dict(row))
            if limit is not None and len(rows) >= limit:
                break
    return rows, tuple(reader.fieldnames)


def _proxy_token(tenant_id: str, signal_type: str, row: dict[str, str], names: Iterable[str]) -> str | None:
    return optional_hmac_token(tenant_id, signal_type, (row.get(name) for name in names))


def _split_by_time(records: list[EvaluationRecord]) -> dict[str, str]:
    """Stable 70/15/15 chronological split; no label participates in splitting."""

    ordered = sorted(records, key=lambda record: (record.transaction_dt, record.ticket_id))
    total = len(ordered)
    train_end = int(total * 0.70)
    validation_end = train_end + int(total * 0.15)
    split: dict[str, str] = {}
    for index, record in enumerate(ordered):
        split[record.ticket_id] = "train" if index < train_end else "validation" if index < validation_end else "test"
    return split


def load_ieee_cis(
    dataset_dir: Path, *, max_rows: int | None, tenant_id: str = "tenant-ieee-local"
) -> tuple[EvaluationDataset, dict[str, str]]:
    """Make a local evaluation dataset from user-provided CSVs.

    Only the first explicit ``max_rows`` transaction rows are read. This avoids
    an implicit full 590k-row run. The source files remain unmodified.
    """

    if max_rows is not None and max_rows <= 0:
        raise ValueError("max_rows must be greater than zero; full runs require an explicit value")
    transaction_path = dataset_dir / TRANSACTION_FILE
    identity_path = dataset_dir / IDENTITY_FILE
    missing = [path.name for path in (transaction_path, identity_path) if not path.is_file()]
    if missing:
        raise FileNotFoundError(", ".join(missing))
    transactions, transaction_columns = _read_rows(transaction_path, limit=max_rows)
    missing_columns = REQUIRED_TRANSACTION_COLUMNS - set(transaction_columns)
    if missing_columns:
        raise ValueError(f"train_transaction.csv is missing columns: {', '.join(sorted(missing_columns))}")
    transaction_ids = {row["TransactionID"] for row in transactions}
    if len(transaction_ids) != len(transactions) or "" in transaction_ids:
        raise ValueError("TransactionID must be non-empty and unique in the selected transaction sample")

    identities, identity_columns = _read_rows(identity_path)
    if "TransactionID" not in identity_columns:
        raise ValueError("train_identity.csv is missing TransactionID")
    identity_map: dict[str, dict[str, str]] = {}
    for identity in identities:
        transaction_id = identity.get("TransactionID", "")
        if transaction_id in transaction_ids:
            if transaction_id in identity_map:
                raise ValueError("train_identity.csv has duplicate selected TransactionID")
            identity_map[transaction_id] = identity

    records: list[EvaluationRecord] = []
    labels: dict[str, int] = {}
    for row in transactions:
        transaction_id = row["TransactionID"]
        try:
            transaction_dt = int(float(row["TransactionDT"]))
            label = int(row["isFraud"])
        except (TypeError, ValueError) as error:
            raise ValueError("TransactionDT and isFraud must be numeric in selected rows") from error
        if label not in (0, 1):
            raise ValueError("isFraud must be 0 or 1 in selected rows")
        identity = identity_map.get(transaction_id, {})
        # isFraud is deliberately absent from all signal construction below.
        signals = {
            "payment_hash": _proxy_token(tenant_id, "payment_hash", row, ("card1", "card2", "card3", "card4", "card5", "card6")),
            "address_hash": _proxy_token(tenant_id, "address_hash", row, ("addr1", "addr2")),
            "device_id_hash": _proxy_token(tenant_id, "device_id_hash", identity, ("DeviceType", "DeviceInfo")),
            "ip_hash": None,  # IEEE-CIS does not provide an actual IP column.
        }
        ticket_id = f"ieee-{transaction_id}"
        records.append(EvaluationRecord(
            ticket_id=ticket_id,
            account_id=f"ieee-account-{transaction_id}",
            reservation_no=f"ieee-reservation-{transaction_id}",
            tenant_id=tenant_id,
            event_id="ieee-cis-online-transaction",
            transaction_dt=transaction_dt,
            signals=signals,
        ))
        labels[ticket_id] = label
    split = _split_by_time(records)
    dataset = EvaluationDataset(
        name="IEEE_CIS",
        records=tuple(records),
        truth=EvaluationTruth(investigation_labels=labels, relationship_groups={}),
        metadata={
            "source": "user_local_ieee_cis",
            "max_rows": str(max_rows) if max_rows is not None else "FULL_EXPLICIT",
            "split": "TransactionDT chronological 70/15/15",
            "ip": "null",
        },
    )
    return dataset, split


def holdout_test_dataset(dataset: EvaluationDataset, split: dict[str, str]) -> EvaluationDataset:
    """Return only the chronological test partition for final baseline measurement."""

    records = tuple(record for record in dataset.records if split[record.ticket_id] == "test")
    if dataset.truth is None:
        return EvaluationDataset(dataset.name, records, None, dataset.metadata)
    labels = {record.ticket_id: dataset.truth.investigation_labels[record.ticket_id] for record in records}
    return EvaluationDataset(
        name="IEEE_CIS_TEST",
        records=records,
        truth=EvaluationTruth(investigation_labels=labels, relationship_groups={}),
        metadata={**dataset.metadata, "evaluated_partition": "test"},
    )
