"""Single point positioning (SPP) from a gnss-sdr observables dump: L1-only or dual-frequency ionosphere-free.

gnss-sdr v0.0.20 computes a dual-frequency position only for L1+L5, and its iono-free solver kept failing and
resetting the observables on our recordings; for L1+L2C it never uses L2C at all. This module computes the
position itself from what gnss-sdr already produced and that we trust (stage by stage, see gnss_sdr_report.py):
  - observables.mat: per epoch and channel, the pseudorange, PRN and validity flag, RX_time (receiver time, GPS TOW s)
  - gps_ephemeris.xml: the LNAV broadcast ephemeris and satellite clock decoded by the L1 C/A telemetry decoder

Per epoch (every --step, default 1 s): for each satellite valid on the bands used, the iono-free pseudorange
P_IF = (f1^2 P1 - f2^2 P2) / (f1^2 - f2^2) (or P1 alone for L1-only), carrier-smoothed with a Hatch filter (window
SMOOTH_S): the iono-free combination triples the code noise, and smoothing it with the same combination of the
carrier phases (which gnss-sdr measures too) removes most of it without ionospheric divergence, since the
combination has no ionosphere left; the filter restarts at every gap or cycle slip. Then the satellite position at transmit time
(IS-GPS-200 orbit + Earth rotation during the travel time), the satellite clock (af0..af2 + relativistic term;
-TGD for L1-only, as the broadcast clock refers to the L1/L2 P(Y) iono-free combination), a Saastamoinen
troposphere, then weighted least squares for x, y, z and the receiver clock. A common inter-band receiver bias
(e.g. P_L5 - P_L1 ~ -700 m on our TwinRX) is the same for every satellite, so it ends up in the receiver clock.
"""

import xml.etree.ElementTree as ET
from pathlib import Path

import numpy as np

C = 299_792_458.0
MU = 3.986005e14  # WGS-84 Earth gravitational constant [m^3/s^2], IS-GPS-200
OMEGA_E = 7.2921151467e-5  # Earth rotation rate [rad/s]
F_REL = -4.442807633e-10  # relativistic clock correction constant [s/sqrt(m)]
FREQ = {"1C": 1575.42e6, "2S": 1227.60e6, "L5": 1176.45e6}
WGS84_A = 6378137.0
WGS84_F = 1 / 298.257223563
ELEV_MASK_DEG = 10.0
HALF_WEEK = 302400.0
SMOOTH_S = 100.0  # Hatch filter window [s]
SLIP_M = 10.0  # code-minus-carrier jump [m] between epochs taken as a cycle slip (restarts the filter)
OBS_FIELDS = ["RX_time", "Pseudorange_m", "PRN", "Flag_valid_pseudorange", "Carrier_phase_cycles"]


def load_observables(mat_path, n_channels):
    """OBS_FIELDS of gnss-sdr's observables.mat (MAT v7.3), each as a (n_channels, n_epochs) array."""
    import h5py
    with h5py.File(mat_path, "r") as f:
        data = {k: np.asarray(f[k], dtype=float) for k in OBS_FIELDS if k in f}
    for k, v in data.items():
        if v.shape[0] != n_channels and v.shape[-1] == n_channels:  # HDF5 stores MATLAB arrays transposed
            data[k] = v.T
    return data


def load_lnav_ephemeris(xml_path):
    """{PRN: {field: float}} from gnss-sdr's gps_ephemeris.xml (boost serialization of its ephemeris map)."""
    root = ET.parse(xml_path).getroot()
    eph = {}
    for item in root.iter("item"):
        second = item.find("second")
        if second is None or second.find("sqrtA") is None:
            continue
        rec = {child.tag: float(child.text) for child in second if child.text and _is_number(child.text)}
        eph[int(rec["PRN"])] = rec
    return eph


def _is_number(text):
    try:
        float(text)
    except ValueError:
        return False
    return True


def _wrap_week(dt):
    return dt - 2 * HALF_WEEK * np.round(dt / (2 * HALF_WEEK))


def sat_clock(e, t):
    """Satellite clock offset [s] at GPS TOW t (without TGD and the relativistic term)."""
    dt = _wrap_week(t - e["toc"])
    return e["af0"] + e["af1"] * dt + e["af2"] * dt ** 2


def sat_position(e, t):
    """ECEF position [m] of the satellite at GPS TOW t, and the relativistic clock term [s] (IS-GPS-200 20.3.3.4.3)."""
    a = e["sqrtA"] ** 2
    n = np.sqrt(MU / a ** 3) + e["delta_n"]
    tk = _wrap_week(t - e["toe"])
    m = e["M_0"] + n * tk
    ecc_anom = m
    for _ in range(10):
        ecc_anom = m + e["ecc"] * np.sin(ecc_anom)
    nu = np.arctan2(np.sqrt(1 - e["ecc"] ** 2) * np.sin(ecc_anom), np.cos(ecc_anom) - e["ecc"])
    phi = nu + e["omega"]
    du = e["Cus"] * np.sin(2 * phi) + e["Cuc"] * np.cos(2 * phi)
    dr = e["Crs"] * np.sin(2 * phi) + e["Crc"] * np.cos(2 * phi)
    di = e["Cis"] * np.sin(2 * phi) + e["Cic"] * np.cos(2 * phi)
    u = phi + du
    r = a * (1 - e["ecc"] * np.cos(ecc_anom)) + dr
    i = e["i_0"] + di + e["idot"] * tk
    x_orb, y_orb = r * np.cos(u), r * np.sin(u)
    omega = e["OMEGA_0"] + (e["OMEGAdot"] - OMEGA_E) * tk - OMEGA_E * e["toe"]
    pos = np.array([x_orb * np.cos(omega) - y_orb * np.cos(i) * np.sin(omega),
                    x_orb * np.sin(omega) + y_orb * np.cos(i) * np.cos(omega),
                    y_orb * np.sin(i)])
    rel = F_REL * e["ecc"] * e["sqrtA"] * np.sin(ecc_anom)
    return pos, rel


def ecef_to_llh(xyz):
    """ECEF [m] -> (lat [deg], lon [deg], height [m]) on WGS-84."""
    x, y, z = xyz
    e2 = WGS84_F * (2 - WGS84_F)
    lon = np.arctan2(y, x)
    p = np.hypot(x, y)
    lat = np.arctan2(z, p * (1 - e2))
    for _ in range(10):
        n = WGS84_A / np.sqrt(1 - e2 * np.sin(lat) ** 2)
        h = p / np.cos(lat) - n
        lat = np.arctan2(z, p * (1 - e2 * n / (n + h)))
    return np.degrees(lat), np.degrees(lon), h


def llh_to_ecef(lat_deg, lon_deg, h):
    lat, lon = np.radians(lat_deg), np.radians(lon_deg)
    e2 = WGS84_F * (2 - WGS84_F)
    n = WGS84_A / np.sqrt(1 - e2 * np.sin(lat) ** 2)
    return np.array([(n + h) * np.cos(lat) * np.cos(lon), (n + h) * np.cos(lat) * np.sin(lon),
                     (n * (1 - e2) + h) * np.sin(lat)])


def enu_matrix(lat_deg, lon_deg):
    """Rows: east, north, up unit vectors in ECEF at (lat, lon)."""
    lat, lon = np.radians(lat_deg), np.radians(lon_deg)
    return np.array([[-np.sin(lon), np.cos(lon), 0],
                     [-np.sin(lat) * np.cos(lon), -np.sin(lat) * np.sin(lon), np.cos(lat)],
                     [np.cos(lat) * np.cos(lon), np.cos(lat) * np.sin(lon), np.sin(lat)]])


def saastamoinen(lat_deg, h, elev):
    """Tropospheric delay [m] (Saastamoinen, standard atmosphere, as RTKLIB's tropmodel with humi=0.7)."""
    if h < -100 or h > 1e4 or elev <= 0:
        return 0.0
    hgt = max(h, 0.0)
    pres = 1013.25 * (1 - 2.2557e-5 * hgt) ** 5.2568
    temp = 15.0 - 6.5e-3 * hgt + 273.16
    e = 6.108 * 0.7 * np.exp((17.15 * temp - 4684.0) / (temp - 38.45))
    z = np.pi / 2 - elev
    trph = 0.0022768 * pres / (1 - 0.00266 * np.cos(2 * np.radians(lat_deg)) - 0.00028 * hgt / 1e3) / np.cos(z)
    trpw = 0.002277 * (1255.0 / temp + 0.05) * e / np.cos(z)
    return trph + trpw


def solve_epoch(meas, eph, x0, use_tgd):
    """meas: [(PRN, pseudorange [m], rx_time [s])]. Returns (xyz, clock [m], used PRNs, elevations [rad], azimuths [rad])
    or None. Iterated weighted least squares with an elevation mask once a position is known."""
    x = np.array(x0, dtype=float)
    clk = 0.0
    for it in range(10):
        rows, res, prns, elevs, azs = [], [], [], [], []
        llh = ecef_to_llh(x) if np.linalg.norm(x) > 1e6 else None
        for prn, pr, t_rx in meas:
            e = eph[prn]
            t_tx = t_rx - pr / C
            dts = sat_clock(e, t_tx)
            sat, rel = sat_position(e, t_tx - dts)
            dts += rel - (e.get("TGD", 0.0) if use_tgd else 0.0)
            # Earth rotation during the signal travel time (Sagnac)
            tau = np.linalg.norm(sat - x) / C
            rot = OMEGA_E * tau
            sat = np.array([np.cos(rot) * sat[0] + np.sin(rot) * sat[1],
                            -np.sin(rot) * sat[0] + np.cos(rot) * sat[1], sat[2]])
            los = sat - x
            rng = np.linalg.norm(los)
            trop, elev, az = 0.0, np.pi / 2, 0.0
            if llh is not None:
                enu = enu_matrix(llh[0], llh[1]) @ (los / rng)
                elev = np.arcsin(enu[2])
                az = np.arctan2(enu[0], enu[1])
                if it > 0 and np.degrees(elev) < ELEV_MASK_DEG:
                    continue
                trop = saastamoinen(llh[0], llh[2], elev)
            res.append(pr - (rng + clk - C * dts + trop))
            rows.append([-los[0] / rng, -los[1] / rng, -los[2] / rng, 1.0])
            prns.append(prn)
            elevs.append(elev)
            azs.append(az)
        if len(rows) < 4:
            return None
        g = np.array(rows)
        w = np.diag(np.sin(np.maximum(elevs, np.radians(5))) ** 2) if llh is not None else np.eye(len(rows))
        dx = np.linalg.lstsq(np.sqrt(w) @ g, np.sqrt(w) @ np.array(res), rcond=None)[0]
        x += dx[:3]
        clk += dx[3]
        if np.linalg.norm(dx[:3]) < 1e-4:
            break
    resid = np.array(res) - g @ dx
    if np.sqrt(np.mean(resid ** 2)) > 50:  # a gross outlier is left: reject the epoch
        return None
    q = np.linalg.inv(g.T @ g)
    return x, clk, prns, np.array(elevs), np.array(azs), q


def combined_measurements(obs, bands, eph, second_band, smooth_s):
    """Per epoch: {PRN: pseudorange} of the satellites valid on L1 (and second_band), iono-free if second_band;
    Hatch-smoothed over smooth_s seconds when smooth_s > 0 and carrier phases are available."""
    rx_time = obs["RX_time"].max(axis=0)
    valid = obs["Flag_valid_pseudorange"] > 0
    n_ch, n_ep = valid.shape
    phase = obs.get("Carrier_phase_cycles")
    dt = np.median(np.diff(rx_time[rx_time > 0])) if np.any(rx_time > 0) else 0.02
    win = max(1.0, smooth_s / dt) if smooth_s > 0 and phase is not None else 1.0
    f1 = FREQ["1C"]
    f2 = FREQ[second_band] if second_band else None
    state = {}  # PRN -> (smoothed P, previous carrier [m], count)
    out = []
    for k in range(n_ep):
        code, carr = {}, {}
        for ch in range(n_ch):
            if not valid[ch, k] or bands[ch] not in ("1C", second_band):
                continue
            prn = int(obs["PRN"][ch, k])
            if prn not in eph:
                continue
            lam = C / FREQ[bands[ch]]
            code.setdefault(prn, {})[bands[ch]] = obs["Pseudorange_m"][ch, k]
            if phase is not None:
                carr.setdefault(prn, {})[bands[ch]] = phase[ch, k] * lam
        meas = {}
        for prn, c in code.items():
            if "1C" not in c or (second_band and second_band not in c):
                continue
            if second_band:
                a, b = f1 ** 2 / (f1 ** 2 - f2 ** 2), f2 ** 2 / (f1 ** 2 - f2 ** 2)
                p = a * c["1C"] - b * c[second_band]
                ph = (a * carr[prn]["1C"] - b * carr[prn][second_band]) if prn in carr and len(carr[prn]) == 2 else None
            else:
                p, ph = c["1C"], carr.get(prn, {}).get("1C")
            prev = state.get(prn)
            if ph is None or prev is None or prev[1] is None:
                state[prn] = (p, ph, 1)
            else:
                ps, ph_prev, n = prev
                pred = ps + (ph - ph_prev)  # carrier phase (as a range) follows the range change without code noise
                if abs(p - pred) > SLIP_M:  # cycle slip or reacquisition: restart from the raw code
                    state[prn] = (p, ph, 1)
                else:
                    n = min(n + 1, win)
                    state[prn] = (p / n + (n - 1) / n * pred, ph, n)
            meas[prn] = state[prn][0]
        for prn in [p for p in state if p not in meas]:  # gap: the filter restarts when the satellite returns
            del state[prn]
        out.append(meas)
    return rx_time, out


def solve(obs, bands, eph, step_s=1.0, second_band=None, smooth_s=SMOOTH_S):
    """obs: dict of (n_channels, n_epochs) arrays (load_observables); bands: band code per channel ('1C', '2S',
    'L5'). second_band None -> L1-only, else iono-free L1 + second_band; smooth_s: Hatch window (0: raw code).
    Returns a list of per-epoch dicts (t, xyz, llh, n_sats, pdop, prns, elev, az)."""
    epochs = []
    if obs["RX_time"].shape[1] < 2:
        return epochs
    rx_time, per_epoch = combined_measurements(obs, bands, eph, second_band, smooth_s)
    dt = np.median(np.diff(rx_time[rx_time > 0])) if np.any(rx_time > 0) else 0.02
    every = max(1, int(round(step_s / dt)))
    x = np.zeros(3)
    for k in range(0, len(per_epoch), every):
        t = rx_time[k]
        meas = [(prn, p, t) for prn, p in per_epoch[k].items()]
        if len(meas) < 4:
            continue
        sol = solve_epoch(meas, eph, x, use_tgd=second_band is None)
        if sol is None:
            continue
        x, clk, prns, elev, az, q = sol
        epochs.append({"t": float(t), "xyz": x.copy(), "llh": ecef_to_llh(x), "n_sats": len(prns),
                       "pdop": float(np.sqrt(np.trace(q[:3, :3]))), "prns": prns, "elev": elev, "az": az})
    return epochs


def enu_errors(epochs, ref_llh):
    """(n, 3) east/north/up errors [m] of the solutions with respect to the reference (lat, lon, h)."""
    if not epochs:
        return np.zeros((0, 3))
    ref = llh_to_ecef(*ref_llh)
    r = enu_matrix(ref_llh[0], ref_llh[1])
    return np.array([r @ (e["xyz"] - ref) for e in epochs])


if __name__ == "__main__":
    import argparse
    import sys

    sys.path.insert(0, str(Path(__file__).parent))
    from compare_pseudoranges import channel_counts

    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("run_dir", type=Path)
    p.add_argument("--ref", type=float, nargs=3, default=[-23.209433, -45.874875, 596.42], help="lat lon h")
    a = p.parse_args()
    n_1c, band, n_2 = channel_counts(a.run_dir)
    code = {"L2C": "2S", "L5": "L5"}[band]
    obs = load_observables(a.run_dir / "observables.mat", n_1c + n_2)
    eph = load_lnav_ephemeris(a.run_dir / "gps_ephemeris.xml")
    bands = ["1C"] * n_1c + [code] * n_2
    for label, sb, sm in (("L1 only", None, 0), (f"iono-free L1+{band}, raw", code, 0),
                          (f"iono-free L1+{band}, smoothed {SMOOTH_S:g} s", code, SMOOTH_S)):
        sols = solve(obs, bands, eph, second_band=sb, smooth_s=sm)
        enu = enu_errors(sols, a.ref)
        if len(enu):
            print(f"{label}: {len(sols)} epochs, mean ENU {enu.mean(0).round(2)} m, std {enu.std(0).round(2)} m, "
                  f"sats median {np.median([s['n_sats'] for s in sols]):.0f}")
        else:
            print(f"{label}: no solution")
