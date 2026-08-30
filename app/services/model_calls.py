from __future__ import annotations

from time import perf_counter

from sqlalchemy.orm import Session

from app.models import ModelCall


def start_model_timer() -> float:
    return perf_counter()


def elapsed_ms(started_at: float) -> int:
    return max(0, round((perf_counter() - started_at) * 1000))


def add_model_call(
    db: Session,
    *,
    user_id: str | None,
    feature: str,
    provider: str,
    model: str,
    success: bool,
    started_at: float,
    input_tokens: int | None = None,
    output_tokens: int | None = None,
    error_code: str | None = None,
    reference_id: str | None = None,
) -> ModelCall:
    call = ModelCall(
        user_id=user_id,
        feature=feature,
        provider=provider,
        model=model,
        success=success,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        latency_ms=elapsed_ms(started_at),
        error_code=error_code,
        reference_id=reference_id,
    )
    db.add(call)
    return call
