# USRP X310 + TwinRX Configuration

Short guide for configuring a **USRP X310 with TwinRX** on Ubuntu and MATLAB.

See `instructions/` for more info


## Recording I/Q samples (Python + UHD)

`x310usrp_record.py` streams the TwinRX channels straight to disk as [SigMF](https://sigmf.org) recordings. Defaults: GPS L1 on RX0 at 4 MS/s, GPS L5 on RX1 at 20 MS/s, 25 dB gain on both, external 10 MHz reference.

### One-time setup

The UHD Python bindings come from apt (`python3-uhd`) and only work with the system Python 3.12, so the uv venv must use it with system site-packages:

```bash
rm -rf .venv
uv venv --python /usr/bin/python3 --system-site-packages
uv sync
```

### Record

```bash
uv run x310usrp_record.py --duration 5                       # L1 + L5, 5 s
uv run x310usrp_record.py --freq 1575.42e6 --channels 0      # L1 only
uv run x310usrp_record.py --iq-rate 4e6 20e6 --gain 25 --name test1  # per-channel rates; see --help
```

Output in `recordings/`, per channel:

- `<name>_ch<k>.sigmf-data`: interleaved int8 I/Q (`ci8`), 2 bytes per sample
- `<name>_ch<k>.sigmf-meta`: JSON with sample rate, center frequency, gain, start time and any overflow gaps

Over 1 GbE, keep the sum of the channels' rates ≲ 25 MS/s (the default 4 + 20 MS/s in 8-bit `sc8` is 48 MB/s, about half the link; ~14.4 GB per 5-min recording). X310 rates are 200 MHz / integer, so the requested rate may be coerced. The script prints the actual value.

### Load

Python:

```python
import numpy as np
x = np.fromfile("recordings/<name>_ch0.sigmf-data", dtype=np.int8).astype(np.float32).view(np.complex64)
```

MATLAB:

```matlab
fid = fopen("recordings/<name>_ch0.sigmf-data"); raw = fread(fid, [2 Inf], "int8=>double"); fclose(fid);
x = complex(raw(1,:), raw(2,:)).';
```
