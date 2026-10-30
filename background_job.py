"""
Arka planda tek seferde bir iş çalıştıran, ilerlemesi sorgulanabilen (ilerleme çubuğu) ve durdurulabilen genel iş altyapısı.
stats_sync.py (toplu istatistik) ve prediction_batch.py (toplu Gemini analizi) bunu kullanır.
"""
import threading
import time
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional, Sequence

class StopJob(Exception):
    """Worker bu hatayı fırlatırsa iş hemen durur (örn. Gemini ayarı kapatıldı)."""

class BackgroundJob:
    def __init__(self, name: str, outcomes: Sequence[str], delay_seconds: float = 0.0,
                 max_consecutive_errors: Optional[int] = None, workers: int = 1):
        """
        outcomes: worker'ın dönebileceği sonuç anahtarları (örn. 'updated', 'empty'); her biri durumda sayaç olarak tutulur.
        max_consecutive_errors: art arda bu kadar hata olursa iş 'error' durumuyla durur (örn. API kotası doldu).
        workers: aynı anda işlenen öğe sayısı (her worker thread'i ortak kuyruktan sıradaki öğeyi alır).
        delay_seconds: her worker'ın bir öğeyi bitirdikten sonraki beklemesi.
        """
        self.name = name
        self.outcomes = tuple(outcomes)
        self.delay_seconds = delay_seconds
        self.max_consecutive_errors = max_consecutive_errors
        self.workers = max(1, workers)
        self._lock = threading.Lock()
        self._cancel = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._state: Dict[str, Any] = {"status": "idle"}

    @staticmethod
    def _now() -> str:
        return datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    def _update(self, **fields: Any) -> None:
        with self._lock:
            self._state.update(fields)

    def _snapshot(self) -> Dict[str, Any]:
        state = dict(self._state)
        total, processed = state.get("total") or 0, state.get("processed") or 0
        state["percent"] = round(processed / total * 100, 1) if total else (100.0 if state["status"] == "done" else 0.0)
        state["eta_seconds"] = None
        started_ts = state.pop("started_ts", None)
        if state["status"] == "running" and processed and started_ts:
            state["eta_seconds"] = int((time.time() - started_ts) / processed * (total - processed))
        return state

    def status(self) -> Dict[str, Any]:
        """status: idle / running / done / cancelled / error; sayaçlar, yüzde ve tahmini kalan süre (eta_seconds)."""
        with self._lock:
            return self._snapshot()

    def is_running(self) -> bool:
        with self._lock:
            return self._state.get("status") == "running"

    def start(self, items: List[Dict[str, Any]], worker: Callable[[Dict[str, Any]], str],
              label: Callable[[Dict[str, Any]], str]) -> Dict[str, Any]:
        """İşi başlatır. Zaten çalışıyorsa yeni iş başlatmaz ve mevcut durumu döner ('started': False)."""
        with self._lock:
            if self._state.get("status") == "running":
                return {**self._snapshot(), "started": False}
            self._cancel.clear()
            self._state = {
                "status": "running" if items else "done", "total": len(items), "processed": 0, "errors": 0,
                **{key: 0 for key in self.outcomes},
                "current": None, "started_at": self._now(), "started_ts": time.time(),
                "finished_at": None if items else self._now(), "message": None,
            }
            if items:
                self._state["workers"] = min(self.workers, len(items))
                self._thread = threading.Thread(target=self._run, args=(items, worker, label), daemon=True, name=self.name)
                self._thread.start()
            return {**self._snapshot(), "started": bool(items)}

    def _run(self, items: List[Dict[str, Any]], worker: Callable[[Dict[str, Any]], str],
             label: Callable[[Dict[str, Any]], str]) -> None:
        """Öğeleri 'workers' sayıda thread ile işler; iptal, StopJob veya art arda hata sınırında yeni öğe alınmaz."""
        pending = list(items)
        control: Dict[str, Any] = {"consecutive_errors": 0, "final_status": None, "message": None}

        def next_item() -> Optional[Dict[str, Any]]:
            with self._lock:
                if self._cancel.is_set() or control["final_status"] or not pending:
                    return None
                item = pending.pop(0)
                self._state["current"] = label(item)
                return item

        def worker_loop() -> None:
            while True:
                item = next_item()
                if item is None:
                    return
                try:
                    outcome = worker(item)
                    with self._lock:
                        control["consecutive_errors"] = 0
                        if outcome in self.outcomes:
                            self._state[outcome] += 1
                        self._state["processed"] += 1
                except StopJob as e:
                    with self._lock:
                        control["final_status"], control["message"] = "error", str(e)
                    return
                except Exception as e:
                    with self._lock:
                        control["consecutive_errors"] += 1
                        self._state["errors"] += 1
                        self._state["processed"] += 1
                        self._state["message"] = f"Son hata: {e}"
                        if self.max_consecutive_errors and control["consecutive_errors"] >= self.max_consecutive_errors:
                            control["final_status"] = "error"
                            control["message"] = f"Art arda {control['consecutive_errors']} hata nedeniyle durduruldu. Son hata: {e}"
                if self.delay_seconds:
                    time.sleep(self.delay_seconds)

        threads = [threading.Thread(target=worker_loop, daemon=True, name=f"{self.name}-{i}")
                   for i in range(min(self.workers, len(items)))]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        final_status = control["final_status"] or ("cancelled" if self._cancel.is_set() and pending else "done")
        self._update(status=final_status, finished_at=self._now(), current=None,
                     **({"message": control["message"]} if control["message"] else {}))

    def cancel(self) -> Dict[str, Any]:
        """Çalışan işi, o an işlenen öğe bittikten sonra durdurur."""
        self._cancel.set()
        return self.status()

    def wait(self, timeout: Optional[float] = None) -> None:
        """Testler için: işin bitmesini bekler."""
        if self._thread is not None:
            self._thread.join(timeout)
