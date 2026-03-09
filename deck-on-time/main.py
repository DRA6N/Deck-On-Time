import asyncio
import json
import os
import re
import subprocess
from pathlib import Path
from typing import Any, Dict, List, Optional

import decky


DEFAULT_DEVICE_CANDIDATES = [
    "/devmon/nvme01",   # your requested path
    "/dev/nvme0",
    "/dev/nvme0n1",
]


class Plugin:
    def __init__(self):
        self.settings_dir = Path(decky.DECKY_PLUGIN_SETTINGS_DIR)
        self.settings_dir.mkdir(parents=True, exist_ok=True)

        self.state_path = self.settings_dir / "deck_on_time_state.json"
        self.state: Dict[str, Any] = {
            "imported_hours": 0,
            "imported_from_device": None,
            "smart_import_done": False,
            "tracked_seconds": 0.0,
            "last_boot_id": None,
            "last_uptime_seconds": None,
            "last_error": None,
        }

        self._tick_task: Optional[asyncio.Task] = None

    def _load_state(self) -> None:
        if self.state_path.exists():
            try:
                loaded = json.loads(self.state_path.read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    self.state.update(loaded)
            except Exception as exc:
                decky.logger.error(f"Failed to load state: {exc}")

    def _save_state(self) -> None:
        try:
            self.state_path.write_text(
                json.dumps(self.state, indent=2),
                encoding="utf-8"
            )
        except Exception as exc:
            decky.logger.error(f"Failed to save state: {exc}")

    def _read_boot_id(self) -> Optional[str]:
        try:
            return Path("/proc/sys/kernel/random/boot_id").read_text(encoding="utf-8").strip()
        except Exception as exc:
            decky.logger.error(f"Failed to read boot_id: {exc}")
            return None

    def _read_uptime_seconds(self) -> Optional[float]:
        try:
            raw = Path("/proc/uptime").read_text(encoding="utf-8").strip()
            return float(raw.split()[0])
        except Exception as exc:
            decky.logger.error(f"Failed to read /proc/uptime: {exc}")
            return None

    def _smartctl_candidates(self) -> List[str]:
        candidates: List[str] = []

        preferred = self.state.get("preferred_device")
        if isinstance(preferred, str) and preferred.strip():
            candidates.append(preferred.strip())

        for item in DEFAULT_DEVICE_CANDIDATES:
            if item not in candidates:
                candidates.append(item)

        try:
            proc = subprocess.run(
                ["smartctl", "--scan-open"],
                capture_output=True,
                text=True,
                check=False
            )
            if proc.stdout:
                for line in proc.stdout.splitlines():
                    line = line.strip()
                    if not line.startswith("/dev"):
                        continue
                    device = line.split()[0]
                    if device not in candidates:
                        candidates.append(device)
        except FileNotFoundError:
            pass
        except Exception as exc:
            decky.logger.warning(f"smartctl --scan-open failed: {exc}")

        return candidates

    def _extract_hours_from_json(self, data: Dict[str, Any]) -> Optional[int]:
        # Common smartctl JSON patterns
        if isinstance(data.get("power_on_time"), dict):
            hours = data["power_on_time"].get("hours")
            if isinstance(hours, (int, float)):
                return int(hours)

        if isinstance(data.get("power_on_hours"), (int, float)):
            return int(data["power_on_hours"])

        # Some output may put values in nested NVMe structures
        nvme = data.get("nvme_smart_health_information_log")
        if isinstance(nvme, dict):
            # Not standard everywhere, but worth trying
            hours = nvme.get("power_on_hours")
            if isinstance(hours, (int, float)):
                return int(hours)

        return None

    def _extract_hours_from_text(self, text: str) -> Optional[int]:
        patterns = [
            r"Power On Hours\s*:\s*(\d+)",
            r"Power_On_Hours\s*.*?(\d+)\s*$",
            r"power on hours\s*:\s*(\d+)",
            r"Power on Hours\s*:\s*(\d+)",
        ]

        for pattern in patterns:
            match = re.search(pattern, text, re.IGNORECASE | re.MULTILINE)
            if match:
                return int(match.group(1))

        return None

    def _smartctl_read_hours(self, device: str) -> Optional[int]:
        # Try JSON first
        try:
            proc = subprocess.run(
                ["smartctl", "-j", "-a", device],
                capture_output=True,
                text=True,
                check=False
            )

            combined = (proc.stdout or "") + "\n" + (proc.stderr or "")
            if proc.stdout:
                try:
                    data = json.loads(proc.stdout)
                    hours = self._extract_hours_from_json(data)
                    if hours is not None:
                        return hours
                except json.JSONDecodeError:
                    pass

            hours = self._extract_hours_from_text(combined)
            if hours is not None:
                return hours
        except FileNotFoundError:
            self.state["last_error"] = "smartctl is not installed or not available in PATH."
            return None
        except Exception as exc:
            decky.logger.error(f"smartctl JSON read failed for {device}: {exc}")

        # Fallback to plain text
        try:
            proc = subprocess.run(
                ["smartctl", "-a", device],
                capture_output=True,
                text=True,
                check=False
            )
            combined = (proc.stdout or "") + "\n" + (proc.stderr or "")
            hours = self._extract_hours_from_text(combined)
            if hours is not None:
                return hours
        except FileNotFoundError:
            self.state["last_error"] = "smartctl is not installed or not available in PATH."
            return None
        except Exception as exc:
            decky.logger.error(f"smartctl text read failed for {device}: {exc}")

        return None

    async def _import_smart_hours(self, force: bool = False) -> Dict[str, Any]:
        if self.state.get("smart_import_done") and not force:
            return {
                "ok": True,
                "imported_hours": int(self.state.get("imported_hours", 0)),
                "device": self.state.get("imported_from_device"),
                "message": "SMART hours already imported."
            }

        candidates = self._smartctl_candidates()
        decky.logger.info(f"Trying SMART import candidates: {candidates}")

        for device in candidates:
            hours = self._smartctl_read_hours(device)
            if hours is not None:
                self.state["imported_hours"] = int(hours)
                self.state["imported_from_device"] = device
                self.state["smart_import_done"] = True
                self.state["last_error"] = None
                self._save_state()
                return {
                    "ok": True,
                    "imported_hours": int(hours),
                    "device": device,
                    "message": f"Imported SMART hours from {device}"
                }

        self.state["last_error"] = (
            "Could not read SMART power-on hours. "
            "The device path may differ, or smartctl may be unavailable."
        )
        self._save_state()
        return {
            "ok": False,
            "message": self.state["last_error"],
            "candidates": candidates
        }

    async def force_import_smart_hours(self) -> Dict[str, Any]:
        return await self._import_smart_hours(force=True)

    async def set_preferred_device(self, device: str) -> Dict[str, Any]:
        device = (device or "").strip()
        self.state["preferred_device"] = device or None
        self._save_state()
        return {"ok": True, "preferred_device": self.state["preferred_device"]}

    async def get_status(self) -> Dict[str, Any]:
        imported_hours = int(self.state.get("imported_hours", 0))
        tracked_seconds = float(self.state.get("tracked_seconds", 0.0))

        return {
            "imported_hours": imported_hours,
            "tracked_seconds": tracked_seconds,
            "total_seconds_estimate": imported_hours * 3600 + tracked_seconds,
            "imported_from_device": self.state.get("imported_from_device"),
            "smart_import_done": bool(self.state.get("smart_import_done", False)),
            "preferred_device": self.state.get("preferred_device"),
            "last_error": self.state.get("last_error"),
        }

    async def reset_tracked_time(self) -> Dict[str, Any]:
        self.state["tracked_seconds"] = 0.0
        self.state["last_error"] = None
        self._save_state()
        return {"ok": True}

    async def clear_smart_import(self) -> Dict[str, Any]:
        self.state["imported_hours"] = 0
        self.state["imported_from_device"] = None
        self.state["smart_import_done"] = False
        self.state["last_error"] = None
        self._save_state()
        return {"ok": True}

    async def _tick_loop(self) -> None:
        while True:
            try:
                boot_id = self._read_boot_id()
                uptime = self._read_uptime_seconds()

                if boot_id is None or uptime is None:
                    await asyncio.sleep(30)
                    continue

                last_boot_id = self.state.get("last_boot_id")
                last_uptime = self.state.get("last_uptime_seconds")

                if last_boot_id != boot_id or last_uptime is None:
                    self.state["last_boot_id"] = boot_id
                    self.state["last_uptime_seconds"] = uptime
                    self._save_state()
                else:
                    delta = uptime - float(last_uptime)
                    if delta < 0:
                        delta = 0

                    # guard against weird jumps
                    if delta > 600:
                        delta = 0

                    self.state["tracked_seconds"] = float(self.state.get("tracked_seconds", 0.0)) + delta
                    self.state["last_uptime_seconds"] = uptime
                    self._save_state()

            except Exception as exc:
                decky.logger.error(f"Tick loop error: {exc}")

            await asyncio.sleep(30)

    async def _main(self):
        decky.logger.info("Deck On Time backend starting")
        self._load_state()

        if not self.state.get("smart_import_done", False):
            result = await self._import_smart_hours(force=False)
            decky.logger.info(f"Initial SMART import result: {result}")

        self._tick_task = asyncio.create_task(self._tick_loop())

    async def _unload(self):
        decky.logger.info("Deck On Time unloading")
        if self._tick_task:
            self._tick_task.cancel()
            try:
                await self._tick_task
            except asyncio.CancelledError:
                pass
        self._save_state()