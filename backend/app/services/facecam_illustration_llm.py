"""Metered multimodal calls. Cache owners serialize access to their cost ledger."""
from __future__ import annotations

import json
import math
import time
from pathlib import Path

import httpx
from openai import APIStatusError

from ..config import settings
from .atomic_files import write_text_atomic
from .openrouter_service import OpenRouterService


def write_json(path: Path, value):
    write_text_atomic(path, json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False))


class IllustrationBudget:
    """Persist reservations before dispatch: a timeout must not reset the budget."""

    _prices: dict = {}

    def __init__(self, directory: Path, limit: float, entry, *, conservative: bool = False):
        self.path = directory / "usage.json"
        self.limit = limit
        self.entry = entry
        self.conservative = conservative

    @classmethod
    def prices(cls, model: str):
        cached = cls._prices.get(model)
        if cached and time.monotonic() - cached[0] < 3600:
            return cached[1]
        # Public catalogue, no generation and no secret in the URL.
        response = httpx.get(settings.openrouter_base_url.rstrip("/") + "/models", timeout=30)
        response.raise_for_status()
        info = next((m for m in response.json()["data"] if m["id"] == model), None)
        if not info or "image" not in info.get("architecture", {}).get("input_modalities", []):
            raise ValueError(f"Le modèle V4 {model} doit accepter les images et publier ses tarifs.")
        rates = {key: float(info["pricing"][key]) for key in ("prompt", "completion")}
        rates["image"] = float(info["pricing"].get("image", rates["prompt"]))
        rates["request"] = float(info["pricing"].get("request", 0))
        if not all(math.isfinite(v) and v >= 0 for v in rates.values()):
            raise ValueError("Tarifs V4 invalides.")
        cls._prices[model] = (time.monotonic(), rates)
        return rates

    def call(self, prompt: str, schema: dict, *, images: list[str] | None = None,
             max_output_tokens: int | None = None, timeout: float | None = None):
        started = time.monotonic()
        rates = self.prices(self.entry.openrouter_id)
        images = images or []
        # Conservative estimate for our bounded 704x888 JPEG contact sheets.
        # This is a local spending guard, not a provider-side billing guarantee.
        max_output = max_output_tokens or self.entry.max_output_tokens or 4096
        text_bytes = len(prompt.encode()) + len(json.dumps(schema).encode())
        input_estimate = text_bytes + 2048 if self.conservative else text_bytes // 2 + 256
        image_estimate = (3072 if self.conservative else 2048) * len(images)
        image_rate = max(rates["prompt"], rates.get("image", rates["prompt"]))
        reserve = (input_estimate * rates["prompt"] + image_estimate * image_rate
                   + max_output * rates["completion"] + rates.get("request", 0)) * (1.1 if self.conservative else 1)
        ledger = json.loads(self.path.read_text()) if self.path.exists() else {"calls": []}
        costs = [c["charged_or_reserved_usd"] for c in ledger["calls"]]
        if any(not isinstance(c, (float, int)) or isinstance(c, bool) or not math.isfinite(c) or c < 0 for c in costs):
            raise ValueError("Journal de coût V4 invalide ; aucun nouvel appel n'a été lancé.")
        spent = sum(costs)
        remaining_timeout = timeout - (time.monotonic() - started) if timeout is not None else None
        if remaining_timeout is not None and remaining_timeout <= 0:
            raise ValueError("Délai de recherche V4 atteint avant l'appel suivant.")
        if spent + reserve > self.limit:
            raise ValueError(
                f"Budget illustrations V4 atteint ({spent:.3f} $ utilisés/réservés, "
                f"{reserve:.3f} $ estimés pour l'appel suivant, plafond {self.limit:.2f} $). "
                "Les analyses terminées sont conservées en cache."
            )
        record = {"model": self.entry.openrouter_id, "charged_or_reserved_usd": reserve,
                  "estimated_usd": reserve, "status": "reserved"}
        ledger["calls"].append(record)
        write_json(self.path, ledger)
        content = [{"type": "text", "text": prompt}] + [
            {"type": "image_url", "image_url": {"url": data}} for data in images]
        extra = {"provider": {"require_parameters": True, "allow_fallbacks": False, "max_price": {
            **{key: rates[key] * 1_000_000 for key in ("prompt", "completion")},
            "request": rates.get("request", 0), "image": rates.get("image", rates["prompt"])}}}
        reasoning = OpenRouterService._build_reasoning(self.entry)
        if reasoning:
            extra["reasoning"] = reasoning
        # No automatic request or model retries: both can multiply paid work.
        options = {"max_retries": 0}
        if remaining_timeout is not None:
            options["timeout"] = min(60, remaining_timeout)
        try:
            response = OpenRouterService._get_client().with_options(**options).chat.completions.create(
                model=self.entry.openrouter_id,
                messages=[{"role": "user", "content": content}],
                max_tokens=max_output,
                response_format={"type": "json_schema", "json_schema": {
                    "name": "facecam_illustrations", "strict": True, "schema": schema}},
                extra_body=extra,
            )
        except APIStatusError as exc:
            error = exc.body if isinstance(exc.body, dict) else {}
            error = error.get("error", error)
            # A documented routing rejection never reached a model. Other failures
            # keep their reservation, since generation may have been billed.
            if exc.status_code == 404 and isinstance(error, dict) and str(error.get("message", "")).startswith("No endpoints found"):
                record.update(status="rejected_before_generation", charged_or_reserved_usd=0, http_status=404)
                write_json(self.path, ledger)
            raise
        usage = response.usage.model_dump() if response.usage else {}
        cost = usage.get("cost")
        if isinstance(cost, (float, int)) and not isinstance(cost, bool) and math.isfinite(cost) and cost >= 0:
            record["charged_or_reserved_usd"] = cost
        record.update(status="received", usage=usage, response_id=response.id)
        write_json(self.path, ledger)
        error = (response.model_extra or {}).get("error")
        if error or not response.choices:
            raise RuntimeError(f"Analyse V4 interrompue par le fournisseur : {error or 'réponse vide'}")
        choice = response.choices[0]
        if choice.finish_reason != "stop":
            raise ValueError(f"Réponse V4 incomplète ({choice.finish_reason}). Les analyses terminées sont conservées.")
        return json.loads(OpenRouterService._strip_json_fence(choice.message.content or ""))
