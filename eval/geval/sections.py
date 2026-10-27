"""The five result sections: which metrics each reports, how they read, what limits go beside them.

Each metric: (key, label, unit, better, source key in the per-unit store). `better` is "lower",
"higher" or "near1" (SCHEMA.md). Every condition string starts with how the numbers were obtained
(measured, derived or borrowed).
"""
from __future__ import annotations

OPEN = [
    ("lat_mae", "Lateral error", "m/s²", "lower"),
    ("lat_mae_bl", "Lateral error at the model's own best lead", "m/s²", "lower"),
    ("lat_corr", "Lateral correlation with the car's later motion", "", "higher"),
    ("lat_gain", "Lateral gain (1 = same size as the car's motion)", "", "near1"),
    ("lat_rate_rms", "Lateral action rate, RMS (smoothness)", "m/s³", "lower"),
    ("lat_best_lag", "Lead of the lateral request over the car's motion", "s", "lower"),
    ("lon_mae", "Longitudinal error", "m/s²", "lower"),
    ("lon_corr", "Longitudinal correlation", "", "higher"),
    ("lon_brake_mae", "Longitudinal error while braking", "m/s²", "lower"),
    ("lon_rate_rms", "Longitudinal action rate, RMS (smoothness)", "m/s³", "lower"),
    ("v3_mae", "Plan speed error at 3 s", "m/s", "lower"),
]

# Values a metric cannot resolve, left out of its mean and pairs: a best-correlating lag at either edge
# of its search (openloop.best_lag searches 0 to 1.5 s) is only a bound on the true lag.
CENSORED = {"lat_best_lag": (0.0, 1.5)}

CLOSED = [
    ("xtrack_2s", "Distance from the recorded path at 2 s", "m", "lower", "absx_2s"),
    ("xtrack_4s", "Distance from the recorded path at 4 s", "m", "lower", "absx_4s"),
    ("xtrack_7s", "Distance from the recorded path at 7.2 s", "m", "lower", "absx_7.2s"),
    ("fail_1m", "Share of episodes ever more than 1 m off the path", "", "lower", "fail_1m"),
    ("lat_jerk", "Requested lateral jerk, RMS", "m/s³", "lower", "rms_lat_jerk"),
    ("long_jerk", "Requested longitudinal jerk, RMS", "m/s³", "lower", "rms_long_jerk"),
    ("speed_err", "Speed error against the recorded speed, RMS", "m/s", "lower", "rms_speed_err"),
]

# Recovery: one block of metrics per test; the store holds them under "<test>:<source key>".
RECOVERY = [
    ("heading_xtrack_2s", "2° off heading: distance from the path at 2 s", "m", "lower", "heading:absx_2s"),
    ("heading_xtrack_4s", "2° off heading: distance from the path at 4 s", "m", "lower", "heading:absx_4s"),
    ("heading_lat_jerk", "2° off heading: requested lateral jerk, RMS", "m/s³", "lower", "heading:rms_lat_jerk"),
    ("offcentre_xtrack_2s", "0.5 m off centre: distance from the path at 2 s", "m", "lower", "offcentre:absx_2s"),
    ("offcentre_xtrack_4s", "0.5 m off centre: distance from the path at 4 s", "m", "lower", "offcentre:absx_4s"),
    ("offcentre_lat_jerk", "0.5 m off centre: requested lateral jerk, RMS", "m/s³", "lower", "offcentre:rms_lat_jerk"),
    ("brake_ahead_4s", "Braking lead: distance ahead of the recorded car at 4 s", "m", "lower", "brake:along_4s"),
    ("brake_gap_min", "Braking lead: smallest gap to the lead (derived)", "m", "higher", "brake:gap_min"),
    ("brake_speed_err", "Braking lead: speed error against the recorded speed, RMS", "m/s", "lower", "brake:rms_speed_err"),
]

SECTIONS = {
    "public_open_loop": {"unit": "segment", "metrics": [m + (m[0],) for m in OPEN], "public": True},
    "public_closed_loop": {"unit": "segment", "metrics": CLOSED, "public": True},
    "truck_open_loop": {"unit": "window", "metrics": [m + (m[0],) for m in OPEN], "public": False},
    "truck_closed_loop": {"unit": "window", "metrics": CLOSED, "public": False},
    "truck_recovery": {"unit": "window", "metrics": RECOVERY, "public": False},
}

CONDITIONS = {
    "public_open_loop": (
        "Measured. comma's public two-camera segments (openpilot CI data), native intrinsics and the "
        "device's own calibration, both cameras; every model on the same decoded frames. Truth is what "
        "the recording car did next: carState speed and acceleration, yaw rate from the device's "
        "cameraOdometry. Segments where the car never moves have no data."),
    "public_closed_loop": (
        "Measured. comma's worldmodel-4B generates each next frame at 5 Hz from 9 history frames, 5 "
        "recorded anchor frames and the car's pose; the model drives comma's ideal car (the requested "
        "curvature and acceleration happen at once) for 7.2 s from the recorded start. Distance is to "
        "the recording car's own path. One window per segment, nearest the middle, where the car holds at "
        "least 5 m/s for the window and the 4.8 s before it; segments with no such stretch are left out. "
        "Seeds 0 and 1, averaged per segment."),
    "truck_open_loop": (
        "Measured. The G55's recorded drives replayed as the truck runs the model: narrow camera only, "
        "road-plane warp to 1.45 m (plane:1.45). Truth is the truck's CAN speed and yaw rate. "
        "Window = unit."),
    "truck_closed_loop": (
        "Measured. worldmodel-4B as in the public closed loop, but the model drives a simulation of the "
        "G55's measured steering and powertrain response instead of the ideal car. Lane-following "
        "windows from the G55's drives, day and night; distance is to the driver's recorded path; "
        "seeds 0 and 1, averaged per window."),
    "truck_recovery": "Measured. The truck closed loop with a disturbance while the model drives; only the tests listed ran.",
}

RECOVERY_TESTS = {
    "heading": "2° off heading: an extra 2°/s yaw for the first second.",
    "offcentre": ("0.5 m off centre: an extra yaw-rate pulse, out and back over the first 2 s, that would move "
                  "an uncorrected car 0.5 m sideways with no net heading change."),
    "brake": ("Braking lead: windows where the car ahead braked; the gap is derived from the lead distance "
              "seen on the recording minus how far the model ran ahead of the recorded car."),
}

LIMITS_COMMON = [
    "Open-loop scores compare a model's action with what the recording driver or car did; a model that "
    "drives differently but well is scored as wrong. comma's RL residual makes the lateral action "
    "smoother, and earlier than its base's by 0.10 to 0.26 s (measured on the G55 and on 16 of comma's "
    "cars); both score as error here. Measured on the 2026-10-27 deep dive: open-loop accuracy did not "
    "predict closed-loop driving.",
    "Closed-loop numbers come from a generative world model, not a car. In a separate test on the G55's "
    "windows, worldmodel-4B followed an added yaw command through 2 s but showed 0 to 20% of an added "
    "lateral offset 3 to 4 s later; past about 4 s the recorded anchor frames also pull every model "
    "back toward the recorded path. The front-page ranking uses 2 s.",
    "Intervals are 95% percentile bootstraps that resample whole drives (the drive a public segment was "
    "cut from, the recorded drive a truck window comes from), so segments of one drive count once. Below "
    "10 drives there is no interval: the truck sections come from fewer drives and report means and win counts only.",
    "Paired deltas are model minus baseline on the units both have (for lateral gain, the change in its "
    "distance from 1); wins count units where the model is better in the metric's direction.",
    "The lead of the lateral request is the shift, searched from 0 to 1.5 s, at which the request best "
    "correlates with the car's later lateral motion; a larger value means the request comes earlier. A "
    "value at either edge is censored and left out. Plan speed error at 3 s reads the plan head, which "
    "does not separate models built on one frozen base: it is identical on every segment for the four "
    "f22e06e5 models, and for Cinque v2 and v3.",
]

LIMITS = {
    "truck_open_loop": "Truck open loop pools few windows (camera mounts changed between drives); read it as "
                       "a check that a model works on the truck's camera, not a ranking.",
    "truck_closed_loop": "Truck closed loop: the steering and powertrain simulation is fitted to the G55, not "
                         "validated on it in closed loop; the reference is the driver's own line, not the lane "
                         "centre.",
    "truck_recovery": "Recovery tests are disturbances rendered by the world model (yaw it renders; a one-frame "
                      "jump it does not, so none is used). The braking-lead test is not built yet: the drives the "
                      "truck closed loop can use hold no window where a car ahead brakes.",
}
