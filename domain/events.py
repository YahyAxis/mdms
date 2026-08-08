"""
Domain Events & PySide6 Qt Signal Bridge
"""

import threading
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Type
from PySide6.QtCore import QObject, Signal
from domain.models import TelemetryEvent

@dataclass
class Event:
    pass

@dataclass
class LogEvent(Event):
    message: str
    level: str = "INFO"

@dataclass
class IngestionFinishedEvent(Event):
    imported_count: int
    duration_sec: float

@dataclass
class CrawlerTelemetryEvent(Event):
    active_seed: str
    pending_queue_size: int
    total_crawled_count: int
    active_throttle_rate_sec: float
    circuit_breaker_states_json: str

class AppSignalBridge(QObject):
    log_emitted = Signal(LogEvent)
    telemetry_updated = Signal(TelemetryEvent)
    ingestion_finished = Signal(IngestionFinishedEvent)
    crawler_telemetry_updated = Signal(CrawlerTelemetryEvent)

signals = AppSignalBridge()

class EventBus:
    def __init__(self) -> None:
        self._subscribers: Dict[Type[Event], List[Callable[[Any], None]]] = {}
        self._lock = threading.Lock()

    def subscribe(self, event_type: Type[Event], handler: Callable[[Any], None]) -> None:
        with self._lock:
            if event_type not in self._subscribers:
                self._subscribers[event_type] = []
            if handler not in self._subscribers[event_type]:
                self._subscribers[event_type].append(handler)

    def publish(self, event: Event) -> None:
        with self._lock:
            handlers = list(self._subscribers.get(type(event), []))

        for handler in handlers:
            try:
                handler(event)
            except Exception:
                pass

        if isinstance(event, LogEvent):
            signals.log_emitted.emit(event)
        elif isinstance(event, TelemetryEvent):
            signals.telemetry_updated.emit(event)
        elif isinstance(event, IngestionFinishedEvent):
            signals.ingestion_finished.emit(event)
        elif isinstance(event, CrawlerTelemetryEvent):
            signals.crawler_telemetry_updated.emit(event)

event_bus = EventBus()
