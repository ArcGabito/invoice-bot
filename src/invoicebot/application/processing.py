"""Procesamiento de documentos: escaneo de carpetas, extracción, validación y reintentos."""

from __future__ import annotations

import os
import time
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from functools import partial
from pathlib import Path
from threading import Lock
from typing import Any

from invoicebot.application.deps import Deps
from invoicebot.application.exchange_rates import lookup_exchange_rate
from invoicebot.application.ports import AiExtractionResult, TextExtraction
from invoicebot.domain.duplicates import business_key
from invoicebot.domain.invoice import (
    DataSource,
    DocumentStatus,
    NewIssue,
    Severity,
    SkipReason,
    has_invoice_data,
    normalize_invoice,
)
from invoicebot.domain.validation import (
    VALIDATION_RULES_VERSION,
    status_from_issues,
    validate_invoice,
)
from invoicebot.errors import DocumentReadError, ExtractionError, error_message
from invoicebot.infrastructure.ai.prompt import PROMPT_VERSION
from invoicebot.infrastructure.documents.scanner import XML_MIME_TYPE, discover_files, hash_file
from invoicebot.infrastructure.documents.ubl import UBL_PARSER_VERSION

#: Reintentos inmediatos dentro de una misma ejecución cuando el JSON no es válido.
INVALID_OUTPUT_RETRIES = 1

#: Fallos que no se arreglan repitiendo la operación (solo con --force).
PERMANENT_ERROR_KINDS = frozenset(
    {
        "not_invoice",
        "refused",
        "unreadable",
        "provider_auth",
        "provider_config",
        "file_missing",
        "file_changed",
    }
)


@dataclass
class ProcessResult:
    #: extracted | needs_ocr | failed | skipped
    outcome: str
    validation_status: str | None = None
    data_source: str | None = None
    possible_duplicate: bool = False
    invoice_id: int | None = None
    error: str | None = None
    kind: str | None = None
    reason: str | None = None


@dataclass
class ScanSummary:
    found: int = 0
    unsupported: list[str] = field(default_factory=list)
    queued: int = 0
    extracted: int = 0
    valid: int = 0
    warning: int = 0
    error: int = 0
    failed: int = 0
    needs_ocr: int = 0
    from_xml: int = 0
    linked_to_xml: int = 0
    skipped: int = 0
    already_processed: int = 0
    previously_failed: int = 0
    exact_duplicates: int = 0
    possible_duplicates: int = 0
    failures: list[dict[str, str]] = field(default_factory=list)
    retry_skipped: list[dict[str, str]] = field(default_factory=list)
    invoice_ids: list[int] = field(default_factory=list)
    #: Documentos enviados a la cola (`documents.process`) en lugar de procesarse aquí.
    dispatched: int = 0
    duration_ms: int = 0

    def add(self, result: ProcessResult, file_path: str) -> None:
        if result.outcome == "extracted":
            self.extracted += 1
            if result.validation_status in ("valid", "warning", "error"):
                setattr(self, result.validation_status, getattr(self, result.validation_status) + 1)
            if result.data_source == DataSource.XML:
                self.from_xml += 1
            if result.possible_duplicate:
                self.possible_duplicates += 1
            if result.invoice_id is not None:
                self.invoice_ids.append(result.invoice_id)
        elif result.outcome == "needs_ocr":
            self.needs_ocr += 1
        elif result.outcome == "skipped":
            if result.reason == SkipReason.PRINTED_REPRESENTATION:
                self.linked_to_xml += 1
            else:
                self.skipped += 1
        else:
            self.failed += 1
            self.failures.append({"file": file_path, "message": result.error or ""})

    def merge(self, other: ScanSummary) -> ScanSummary:
        merged = ScanSummary()
        for name, value in asdict(self).items():
            other_value = getattr(other, name)
            setattr(merged, name, value + other_value)
        return merged

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


Progress = Callable[[int, int, str, ProcessResult], None]
#: Recibe un documento registrado y lo encola para que lo procese un worker.
Dispatch = Callable[[dict[str, Any]], None]


def _ocr_issues(
    extraction: TextExtraction, use_vision: bool, min_confidence: int
) -> list[NewIssue]:
    """Constancia de que los datos salieron de una imagen (y aviso si la confianza es baja)."""
    if use_vision:
        return [
            NewIssue(
                "OCR_SOURCE",
                Severity.INFO,
                "Datos leídos de la imagen con visión (Claude), sin texto nativo",
            )
        ]
    if extraction.method != "ocr" or extraction.ocr is None:
        return []
    engine, confidence = extraction.ocr.engine, extraction.ocr.confidence
    issues = [
        NewIssue(
            "OCR_SOURCE",
            Severity.INFO,
            f"Datos leídos por OCR ({engine}) de una imagen o un escaneo",
            None,
            f"confianza {confidence} %",
        )
    ]
    if confidence is not None and confidence < min_confidence:
        issues.append(
            NewIssue(
                "OCR_LOW_CONFIDENCE",
                Severity.WARNING,
                "El OCR leyó el documento con poca confianza: compara los datos con la imagen",
                f">= {min_confidence} %",
                f"{confidence} %",
            )
        )
    return issues


def _extract_with_ai(
    deps: Deps,
    *,
    document: dict[str, Any],
    attempt: int,
    run: dict[str, Any],
    call: Callable[[], AiExtractionResult],
) -> tuple[dict[str, Any], int] | tuple[None, tuple[str, str]]:
    """Llama a la IA y registra cada intento; repite una vez si la salida no cumple el esquema."""
    base = {"document_id": document["id"], "attempt": attempt, **run}
    tries = 0
    while True:
        started = time.monotonic()
        try:
            result = call()
        except Exception as error:
            err = (
                error
                if isinstance(error, ExtractionError)
                else ExtractionError(error_message(error))
            )
            deps.repository.insert_extraction_run(
                **base,
                status="failed",
                error_kind=err.kind,
                error_message=err.message,
                raw_response=err.raw_response,
                duration_ms=int((time.monotonic() - started) * 1000),
            )
            if err.kind == "invalid_output" and tries < INVALID_OUTPUT_RETRIES:
                tries += 1
                deps.logger.info(
                    "salida inválida, se repite la extracción",
                    extra={"document_id": document["id"]},
                )
                continue
            return None, (err.kind, err.message)
        run_id = deps.repository.insert_extraction_run(
            **{**base, "model": result.model},
            status="success",
            raw_response=result.raw,
            input_tokens=result.usage.input_tokens,
            output_tokens=result.usage.output_tokens,
            duration_ms=int((time.monotonic() - started) * 1000),
        )
        return result.data, run_id


def process_document(deps: Deps, *, document: dict[str, Any], file_path: str) -> ProcessResult:
    """Datos (XML o texto + IA) → normalización → validación → SQL Server.

    Nunca lanza por un fallo del documento: lo registra y devuelve el resultado,
    para que el lote continúe.
    """
    repo, log = deps.repository, deps.logger
    doc_id = document["id"]
    attempt = repo.start_attempt(doc_id)

    def fail(kind: str, message: str) -> ProcessResult:
        repo.update_document(
            doc_id, status=DocumentStatus.FAILED, error_kind=kind, error_message=message
        )
        log.warning("documento fallido: %s", message, extra={"document_id": doc_id, "kind": kind})
        return ProcessResult("failed", error=message, kind=kind)

    def skip(reason: str, message: str) -> ProcessResult:
        repo.update_document(
            doc_id,
            processed=True,
            status=DocumentStatus.SKIPPED,
            skip_reason=reason,
            error_message=message,
        )
        log.info("documento omitido", extra={"document_id": doc_id, "reason": reason})
        return ProcessResult("skipped", reason=reason)

    try:
        extraction = deps.document_router.extract_text(
            file_path=file_path, mime_type=document["mime_type"]
        )
    except DocumentReadError as error:
        return fail("unreadable", error.message)
    except Exception as error:
        return fail("text_extraction", error_message(error))

    # Sin texto legible: con visión de Claude el documento se envía tal cual.
    vision = deps.vision_extractor if extraction.needs_ocr else None
    use_vision = vision is not None
    repo.update_document(
        doc_id,
        text_method="ocr" if use_vision else extraction.method,
        text_chars=extraction.chars,
        page_count=extraction.page_count,
    )
    if extraction.needs_ocr and not use_vision:
        repo.update_document(
            doc_id,
            status=DocumentStatus.NEEDS_OCR,
            error_message="El OCR no encontró texto legible: revisa la calidad de la imagen o del escaneo"
            if extraction.ocr_attempted
            else None,
        )
        log.info("sin texto suficiente: requiere OCR", extra={"document_id": doc_id})
        return ProcessResult("needs_ocr")

    data: dict[str, Any]
    if vision is not None:
        out = _extract_with_ai(
            deps,
            document=document,
            attempt=attempt,
            run={
                "provider": f"{vision.name}-vision",
                "model": vision.model,
                "prompt_version": PROMPT_VERSION,
                "text_method": "ocr",
            },
            call=lambda: vision.extract_from_file(
                file_path=file_path,
                mime_type=document["mime_type"],
                file_name=document["file_name"],
            ),
        )
        if out[0] is None:
            return fail(*out[1])
        data, run_id = out
        data_source = DataSource.AI
    elif extraction.structured is not None:
        structured = extraction.structured
        if structured.kind == "cdr":
            return skip(
                SkipReason.CDR,
                "Constancia de recepción (CDR) de SUNAT: acompaña al comprobante, no es uno",
            )
        if structured.kind == "other":
            return skip(
                SkipReason.UNSUPPORTED_XML,
                f"XML que no es un comprobante UBL (raíz: {structured.root})",
            )
        data = structured.data
        data_source = DataSource.XML
        run_id = repo.insert_extraction_run(
            document_id=doc_id,
            attempt=attempt,
            provider="ubl-xml",
            prompt_version=UBL_PARSER_VERSION,
            text_method=extraction.method,
            input_chars=extraction.chars,
            status="success",
            duration_ms=0,
        )
    else:
        provider = deps.ai_provider
        out = _extract_with_ai(
            deps,
            document=document,
            attempt=attempt,
            run={
                "provider": provider.name,
                "model": provider.model,
                "prompt_version": PROMPT_VERSION,
                "text_method": extraction.method,
                "input_chars": len(extraction.text or ""),
            },
            call=lambda: provider.extract(
                text=extraction.text or "",
                file_name=document["file_name"],
                ocr=extraction.method == "ocr",
            ),
        )
        if out[0] is None:
            return fail(*out[1])
        data, run_id = out
        data_source = DataSource.AI

    # La IA propone y el código decide: si el modelo duda pero entregó un
    # comprobante completo, se procesa y queda para revisión.
    if data_source == DataSource.AI and not data.get("is_invoice") and not has_invoice_data(data):
        return fail("not_invoice", "El modelo indica que el documento no es un comprobante de pago")

    normalized = normalize_invoice(data)
    invoice = normalized.invoice
    exchange_rate = lookup_exchange_rate(repo, invoice)
    _, issues = validate_invoice(
        invoice,
        normalization_issues=normalized.normalization_issues,
        tolerance_cents=deps.config.amount_tolerance_cents,
        today=deps.today,
        exchange_rate=exchange_rate,
    )
    issues.extend(_ocr_issues(extraction, use_vision, deps.config.ocr.min_confidence))
    if data_source == DataSource.AI and not data.get("is_invoice"):
        issues.append(
            NewIssue(
                "MODEL_DOUBTED_INVOICE",
                Severity.WARNING,
                "El modelo no reconoció el documento como comprobante, pero extrajo tipo, número, "
                "emisor y total: confirma que lo sea",
            )
        )
    note_code = "XML_NOTE" if data_source == DataSource.XML else "MODEL_NOTE"
    notes = [NewIssue(note_code, Severity.INFO, str(n)[:500]) for n in data.get("notes") or []]

    # Buscar duplicados y guardar en la misma transacción, con la clave reservada:
    # dos copias del mismo comprobante procesadas a la vez se ven como duplicado.
    key = business_key(invoice)
    with repo.transaction() as tx:
        if key:
            tx.lock_business_key(key)
        matches = tx.find_by_business_key(key, doc_id) if key else []
        all_issues = list(issues)
        if matches:
            all_issues.append(
                NewIssue(
                    "POSSIBLE_DUPLICATE",
                    Severity.WARNING,
                    "Existe otro documento con el mismo emisor, tipo, serie y número",
                    None,
                    "; ".join(f"#{m['document_id']} {m['file_name']}" for m in matches),
                )
            )
        all_issues.extend(notes)
        status = status_from_issues(all_issues)
        invoice_id = tx.save_invoice_result(
            document_id=doc_id,
            extraction_run_id=run_id,
            invoice=invoice,
            validation_status=status,
            rules_version=VALIDATION_RULES_VERSION,
            business_key=key,
            data_source=data_source,
            issues=all_issues,
            exchange_rate=exchange_rate,
        )
    log.info(
        "documento extraído", extra={"document_id": doc_id, "status": status, "source": data_source}
    )
    return ProcessResult(
        "extracted",
        validation_status=status,
        data_source=data_source,
        possible_duplicate=bool(matches),
        invoice_id=invoice_id,
    )


def _classify_existing(
    document: dict[str, Any],
    file_path: str,
    summary: ScanSummary,
    enqueue: Callable[[], None],
    ocr_available: bool,
) -> None:
    """Con OCR disponible, lo que quedó "requiere OCR" se vuelve a procesar."""
    is_copy = document["file_path"] != file_path
    if is_copy:
        summary.exact_duplicates += 1
    status = document["status"]
    if status in (DocumentStatus.EXTRACTED, DocumentStatus.SKIPPED):
        if not is_copy:
            summary.already_processed += 1
    elif status == DocumentStatus.FAILED:
        if not is_copy:
            summary.previously_failed += 1
    elif status == DocumentStatus.NEEDS_OCR:
        if ocr_available and not is_copy:
            enqueue()
        elif not is_copy:
            summary.needs_ocr += 1
    else:
        # pending o processing: un documento nuevo o una ejecución interrumpida.
        enqueue()


def _run_queue(
    deps: Deps,
    items: Sequence[tuple[dict[str, Any], str, str]],
    summary: ScanSummary,
    on_progress: Progress | None,
    dispatch: Dispatch | None = None,
) -> None:
    """XML primero (para vincular sus PDF), luego el resto, con concurrencia limitada.

    Con ``dispatch`` (modo colas) los XML se leen aquí, porque son rápidos y sin IA, y
    así sus PDF se vinculan; el resto se encola para que lo procesen los workers.
    """
    lock = Lock()
    done = 0

    def work(item: tuple[dict[str, Any], str, str]) -> None:
        nonlocal done
        document, file_path, mime_type = item
        try:
            sibling = (
                None if mime_type == XML_MIME_TYPE else _extracted_xml_sibling(deps, file_path)
            )
            if sibling is not None:
                result = _link_to_xml(deps, document, sibling)
            else:
                result = process_document(deps, document=document, file_path=file_path)
        except Exception as error:  # Un fallo de infraestructura no detiene el lote.
            deps.logger.exception("error inesperado al procesar %s", file_path)
            result = ProcessResult("failed", error=error_message(error), kind="unknown")
        with lock:
            summary.add(result, file_path)
            done += 1
            current = done
        if on_progress:
            on_progress(current, len(items), file_path, result)

    xml = [i for i in items if i[2] == XML_MIME_TYPE]
    others = [i for i in items if i[2] != XML_MIME_TYPE]
    workers = max(1, deps.config.concurrency)
    if dispatch is not None:
        for item in xml:
            work(item)
        for document, file_path, _mime in others:
            sibling = _extracted_xml_sibling(deps, file_path)
            if sibling is not None:
                summary.add(_link_to_xml(deps, document, sibling), file_path)
            else:
                dispatch(document)
                summary.dispatched += 1
        return
    for group in (xml, others):
        if not group:
            continue
        if workers == 1 or len(group) == 1:
            for item in group:
                work(item)
        else:
            with ThreadPoolExecutor(
                max_workers=min(workers, len(group)), thread_name_prefix="invoicebot"
            ) as pool:
                list(pool.map(work, group))


def _extracted_xml_sibling(deps: Deps, file_path: str) -> dict[str, Any] | None:
    """XML extraído con el mismo nombre base y en la misma carpeta que el archivo."""
    path = Path(file_path)
    for ext in (".xml", ".XML"):
        xml = deps.repository.find_document_by_path(str(path.with_suffix(ext)))
        if xml and xml["status"] == DocumentStatus.EXTRACTED:
            return xml
    return None


def _link_to_xml(
    deps: Deps, document: dict[str, Any], xml_document: dict[str, Any]
) -> ProcessResult:
    deps.repository.update_document(
        document["id"],
        processed=True,
        status=DocumentStatus.SKIPPED,
        skip_reason=SkipReason.PRINTED_REPRESENTATION,
        linked_document_id=xml_document["id"],
        error_message=f"Representación impresa de {xml_document['file_name']}: los datos se toman del XML",
    )
    return ProcessResult("skipped", reason=SkipReason.PRINTED_REPRESENTATION)


def scan_invoices(
    deps: Deps,
    *,
    root: str,
    recursive: bool = False,
    dry_run: bool = False,
    on_discovered: Callable[[ScanSummary], None] | None = None,
    on_progress: Progress | None = None,
    dispatch: Dispatch | None = None,
) -> ScanSummary:
    """Procesa los comprobantes de una carpeta (o un archivo). Es idempotente.

    Fase 1 (secuencial): descubre, calcula SHA-256 y registra cada documento una
    sola vez. Fase 2 (concurrente): procesa primero los XML y después el resto;
    un PDF con el mismo nombre que un XML ya extraído se vincula sin IA.
    """
    started = time.monotonic()
    repo = deps.repository
    summary = ScanSummary()
    ocr_available = bool(deps.document_router.can_ocr or deps.vision_extractor)
    discovery = discover_files(root, recursive=recursive)
    summary.found = len(discovery.supported)
    summary.unsupported = discovery.unsupported
    # Con colas, un documento "en proceso" puede estar en manos de otro worker: no se toca.
    if not dry_run and dispatch is None:
        reset = repo.reset_interrupted()
        if reset:
            deps.logger.info("documentos interrumpidos devueltos a pendiente: %s", reset)

    queue: list[tuple[dict[str, Any], str, str]] = []
    queued_ids: set[int] = set()
    seen_in_dry_run: set[str] = set()
    for item in discovery.supported:
        try:
            sha256, size = hash_file(item.file_path)
        except OSError as error:
            summary.failed += 1
            summary.failures.append(
                {"file": item.file_path, "message": f"No se pudo leer el archivo: {error}"}
            )
            continue
        existing = repo.find_document_by_sha(sha256)
        if dry_run:
            if existing:
                _classify_existing(
                    existing,
                    item.file_path,
                    summary,
                    partial(queue.append, ({}, item.file_path, item.mime_type)),
                    ocr_available,
                )
            elif sha256 in seen_in_dry_run:
                summary.exact_duplicates += 1
            else:
                seen_in_dry_run.add(sha256)
                queue.append(({}, item.file_path, item.mime_type))
            continue
        document = existing or repo.insert_document(
            sha256=sha256,
            file_path=item.file_path,
            file_name=os.path.basename(item.file_path),
            mime_type=item.mime_type,
            file_size=size,
        )
        repo.record_occurrence(document["id"], item.file_path)

        def enqueue(
            doc: dict[str, Any] = document, path: str = item.file_path, mime: str = item.mime_type
        ) -> None:
            if doc["id"] in queued_ids:
                return
            queued_ids.add(doc["id"])
            queue.append((doc, path if doc["file_path"] == path else doc["file_path"], mime))

        _classify_existing(document, item.file_path, summary, enqueue, ocr_available)

    summary.queued = len(queue)
    if on_discovered:
        on_discovered(summary)
    if not dry_run:
        _run_queue(deps, queue, summary, on_progress, dispatch)
    summary.duration_ms = int((time.monotonic() - started) * 1000)
    return summary


def _locate_file(deps: Deps, document: dict[str, Any]) -> tuple[str | None, str, str]:
    """Una copia del archivo que siga existiendo con el mismo SHA-256."""
    paths = [
        document["file_path"],
        *(o["file_path"] for o in deps.repository.get_occurrences(document["id"])),
    ]
    saw_changed = False
    for candidate in dict.fromkeys(paths):
        if not os.path.exists(candidate):
            continue
        if hash_file(candidate)[0] == document["sha256"]:
            return candidate, "", ""
        saw_changed = True
    if saw_changed:
        return None, "file_changed", "El archivo cambió de contenido desde que se registró"
    return None, "file_missing", "El archivo ya no existe en ninguna de las rutas registradas"


def retry_documents(
    deps: Deps,
    *,
    failed: bool = True,
    pending: bool = True,
    force: bool = False,
    on_progress: Progress | None = None,
    dispatch: Dispatch | None = None,
) -> ScanSummary:
    """Reintenta documentos fallidos y recupera los de una ejecución interrumpida."""
    started = time.monotonic()
    repo = deps.repository
    summary = ScanSummary()
    if dispatch is None:
        repo.reset_interrupted()
    statuses = [
        s for s, on in ((DocumentStatus.FAILED, failed), (DocumentStatus.PENDING, pending)) if on
    ]
    candidates = repo.list_documents_by_status(statuses)
    queue: list[tuple[dict[str, Any], str, str]] = []
    for document in candidates:
        if document["status"] == DocumentStatus.FAILED and not force:
            if document["attempts"] >= deps.config.max_attempts:
                summary.retry_skipped.append(
                    {
                        "file": document["file_path"],
                        "reason": f"alcanzó el máximo de {deps.config.max_attempts} intentos",
                    }
                )
                continue
            if document["error_kind"] in PERMANENT_ERROR_KINDS:
                summary.retry_skipped.append(
                    {
                        "file": document["file_path"],
                        "reason": f"error no recuperable ({document['error_kind']})",
                    }
                )
                continue
        path, kind, message = _locate_file(deps, document)
        if path is None:
            repo.update_document(
                document["id"], status=DocumentStatus.FAILED, error_kind=kind, error_message=message
            )
            summary.failed += 1
            summary.failures.append({"file": document["file_path"], "message": message})
            continue
        queue.append((document, path, document["mime_type"]))
    summary.found = len(candidates)
    summary.queued = len(queue)
    _run_queue(deps, queue, summary, on_progress, dispatch)
    summary.duration_ms = int((time.monotonic() - started) * 1000)
    return summary


def process_document_by_id(deps: Deps, document_id: int) -> ProcessResult:
    """Procesa un documento ya registrado (lo usa el worker de la cola `extract`).

    Es idempotente: si otro intento ya lo terminó, no hace nada.
    """
    document = deps.repository.get_document(document_id)
    if document is None:
        return ProcessResult(
            "failed", error=f"No existe el documento {document_id}", kind="missing"
        )
    if document["status"] in (DocumentStatus.EXTRACTED, DocumentStatus.SKIPPED):
        return ProcessResult("skipped", reason="already_processed")
    path, kind, message = _locate_file(deps, document)
    if path is None:
        deps.repository.update_document(
            document_id, status=DocumentStatus.FAILED, error_kind=kind, error_message=message
        )
        return ProcessResult("failed", error=message, kind=kind)
    if document["mime_type"] != XML_MIME_TYPE:
        sibling = _extracted_xml_sibling(deps, path)
        if sibling is not None:
            return _link_to_xml(deps, document, sibling)
    return process_document(deps, document=document, file_path=path)
