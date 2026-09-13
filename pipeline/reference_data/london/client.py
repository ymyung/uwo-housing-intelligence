"""Small dependency-free ArcGIS REST client with deterministic pagination."""
from __future__ import annotations

import json
import time
from collections.abc import Callable
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import urlopen

from .datasets import DatasetSpec


class ArcGISRequestError(RuntimeError):
    pass


class ArcGISClient:
    def __init__(self, *, timeout_seconds: float = 30, retries: int = 3, opener: Callable[..., Any] = urlopen):
        self.timeout_seconds, self.retries, self._opener = timeout_seconds, retries, opener

    def _request(self, url: str, params: dict[str, object]) -> dict[str, Any]:
        request_url = f"{url}?{urlencode(params)}"
        failure: Exception | None = None
        for attempt in range(self.retries):
            try:
                with self._opener(request_url, timeout=self.timeout_seconds) as response:
                    payload = json.loads(response.read().decode("utf-8-sig"))
                if payload.get("error"):
                    raise ArcGISRequestError(str(payload["error"]))
                return payload
            except (HTTPError, URLError, TimeoutError, OSError, ValueError, ArcGISRequestError) as error:
                failure = error
                if attempt + 1 < self.retries:
                    time.sleep(0.25 * (2 ** attempt))
        raise ArcGISRequestError(f"ArcGIS request failed after {self.retries} attempts: {failure}") from failure

    def metadata(self, spec: DatasetSpec) -> dict[str, Any]:
        return self._request(spec.source_url, {"f": "json"})

    def feature_count(self, spec: DatasetSpec) -> int:
        payload = self._request(
            f"{spec.source_url}/query",
            {"where": "1=1", "returnCountOnly": "true", "f": "json"},
        )
        return int(payload["count"])

    def download(self, spec: DatasetSpec) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        metadata = self.metadata(spec)
        query_url = f"{spec.source_url}/query"
        offset, page_size, features = 0, min(int(metadata.get("maxRecordCount", 1000)), 1000), []
        expected_count = self.feature_count(spec)
        while True:
            page = self._request(query_url, {
                "where": "1=1", "outFields": "*", "returnGeometry": "true", "f": "json",
                "resultOffset": offset, "resultRecordCount": page_size,
                "orderByFields": f"{spec.source_id_field} ASC", "outSR": 26917,
            })
            batch = page.get("features", [])
            features.extend(batch)
            if not batch:
                if page.get("exceededTransferLimit"):
                    raise ArcGISRequestError(f"{spec.name}: empty ArcGIS page before transfer completed")
                break
            if not page.get("exceededTransferLimit"):
                break
            offset += len(batch)
        if len(features) != expected_count:
            raise ArcGISRequestError(
                f"{spec.name}: acquired {len(features)} feature(s), expected {expected_count}"
            )
        return metadata, features
