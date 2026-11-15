"""Read-only health tracking for optional robot device ROS2 interfaces."""
from __future__ import annotations

import copy
import threading
import time
from collections import deque
from typing import Any, Callable

from ..aruco import ARUCO_TOPIC, ArucoPreview, waiting_aruco
from .truth import Status, json_safe

STALE_SECONDS = 2.0
RATE_WINDOW_SECONDS = 5.0

VISION_TOPICS = {
    "detectionStatus": {
        "topic": "/fisheye_aruco/detection_status",
        "topicType": "std_msgs/msg/String",
    },
    "detectedIds": {
        "topic": "/fisheye_aruco/detected_ids",
        "topicType": "std_msgs/msg/Int32MultiArray",
    },
    "boardPose": {
        "topic": "/fisheye_aruco/board_pose",
        "topicType": "geometry_msgs/msg/PoseStamped",
    },
    "dockingState": {
        "topic": "/aruco_docking/state",
        "topicType": "std_msgs/msg/String",
    },
    "arucoPreview": {
        "topic": ARUCO_TOPIC,
        "topicType": "std_msgs/msg/String",
    },
}


class TopicSample:
    """Keep one topic's latest value without inventing missing data."""

    def __init__(self, topic: str, topic_type: str,
                 clock: Callable[[], float] = time.monotonic, stale_seconds=STALE_SECONDS):
        self.topic = topic
        self.topic_type = topic_type
        self.clock = clock
        self.stale_seconds = stale_seconds
        self.publisher_count = 0
        self.observed_types: list[str] = []
        self.contract_conflict: str | None = None
        self.message_count = 0
        self.last_monotonic: float | None = None
        self.value: Any = None
        self.samples: deque[float] = deque()

    def set_graph(self, publisher_count: int, observed_types: list[str]) -> None:
        self.publisher_count = max(0, int(publisher_count))
        self.observed_types = sorted(set(observed_types))
        invalid = [kind for kind in self.observed_types if kind != self.topic_type]
        self.contract_conflict = (
            f"Contract expects {self.topic_type}; graph reports {', '.join(invalid)}"
            if invalid else None
        )

    def receive(self, value: Any) -> None:
        now = self.clock()
        self.value = json_safe(copy.deepcopy(value))
        self.message_count += 1
        self.last_monotonic = now
        self.samples.append(now)
        self._trim(now)

    def snapshot(self) -> dict[str, Any]:
        now = self.clock()
        self._trim(now)
        age = None if self.last_monotonic is None else max(0.0, now - self.last_monotonic)
        if self.contract_conflict:
            status = Status.ERROR
            reason = self.contract_conflict
        elif self.last_monotonic is None:
            if self.publisher_count:
                status = Status.NO_DATA
                reason = "Publisher visible; no message received"
            else:
                status = Status.WAITING_UPSTREAM
                reason = "Publisher not discovered"
        elif ((self.stale_seconds is not None and age > self.stale_seconds)
              or (self.stale_seconds is None and not self.publisher_count)):
            status = Status.STALE
            reason = "Publisher gone or last message expired"
        else:
            status = Status.AVAILABLE
            reason = None
        return {
            "topic": self.topic,
            "topicType": self.topic_type,
            "status": status.value,
            "reason": reason,
            "publisherCount": self.publisher_count,
            "observedTypes": list(self.observed_types),
            "contractConflict": self.contract_conflict,
            "messageCount": self.message_count,
            "ageS": age,
            "hz": self._rate(),
            "value": copy.deepcopy(self.value),
        }

    def _trim(self, now: float) -> None:
        cutoff = now - RATE_WINDOW_SECONDS
        while self.samples and self.samples[0] < cutoff:
            self.samples.popleft()

    def _rate(self) -> float | None:
        if len(self.samples) < 2:
            return None
        elapsed = self.samples[-1] - self.samples[0]
        return None if elapsed <= 0 else (len(self.samples) - 1) / elapsed


class DeviceInterfaceStore:
    """Aggregate optional device topics into one truthful web snapshot."""

    def __init__(self, clock: Callable[[], float] = time.monotonic, wall_clock=time.time):
        self.clock = clock
        self.lock = threading.RLock()
        self.aruco_preview = ArucoPreview(clock, wall_clock)
        self.topics = {
            key: TopicSample(spec["topic"], spec["topicType"], clock)
            for key, spec in VISION_TOPICS.items()
        }

    def set_graph(self, key: str, publisher_count: int,
                  observed_types: list[str]) -> None:
        with self.lock:
            self.topics[key].set_graph(publisher_count, observed_types)

    def receive_detection_status(self, value: str) -> None:
        with self.lock:
            self.topics["detectionStatus"].receive(value.strip() or None)

    def receive_detected_ids(self, values: list[int]) -> None:
        clean = [int(value) for value in values
                 if isinstance(value, int) and not isinstance(value, bool)]
        with self.lock:
            self.topics["detectedIds"].receive(clean)

    def receive_board_pose(self, value: dict[str, Any]) -> None:
        with self.lock:
            self.topics["boardPose"].receive(value)

    def receive_docking_state(self, value: str) -> None:
        with self.lock:
            self.topics["dockingState"].receive(value.strip() or None)

    def receive_aruco_preview(self, value: str) -> None:
        with self.lock:
            if self.aruco_preview.receive(value):
                self.topics["arucoPreview"].receive(self.aruco_preview.snapshot())

    def snapshot(self) -> dict[str, Any]:
        with self.lock:
            topics_by_key = {key: item.snapshot() for key, item in self.topics.items()}
            preview = self.aruco_preview.snapshot()
            preview_topic = topics_by_key["arucoPreview"]
            if preview_topic["status"] != Status.AVAILABLE.value:
                preview = waiting_aruco(preview_topic["status"], preview["ageMs"])
            elif preview["status"] != Status.AVAILABLE.value:
                preview_topic.update(status=preview["status"], reason="Marker summary invalid or expired")
            preview_topic["value"] = copy.deepcopy(preview)
        detection_topic = topics_by_key["detectionStatus"]
        pose_topic = topics_by_key["boardPose"]
        ids_topic = topics_by_key["detectedIds"]
        docking_topic = topics_by_key["dockingState"]
        detection_value = detection_topic["value"]
        detection_ok = detection_value in ("ok", "ok_single")
        if detection_topic["status"] != Status.AVAILABLE.value:
            target_status = detection_topic["status"]
        elif not detection_ok:
            target_status = Status.NO_DATA.value
        else:
            target_status = pose_topic["status"]
        return json_safe({
            "visionDocking": {
                "status": detection_topic["status"],
                "targetStatus": target_status,
                "source": "ROS2 read-only device interfaces",
                "detectionState": detection_value,
                "detectedIds": ids_topic["value"],
                "boardPose": pose_topic["value"] if detection_ok else None,
                "dockingState": docking_topic["value"],
                "arucoPreview": preview,
                "topics": topics_by_key,
            }
        })

    def topic_snapshots(self) -> dict[str, dict[str, Any]]:
        snapshot = self.snapshot()["visionDocking"]["topics"]
        return {item["topic"]: item for item in snapshot.values()}
