# USRP X310 + TwinRX Configuration

Short guide for configuring a **USRP X310 with TwinRX** on Ubuntu and MATLAB.

See `instructions/` for more info


## Recording I/Q samples (Python + UHD)

`x310usrp_record.py` streams the TwinRX channels straight to disk as [SigMF](https://sigmf.org) recordings. Defaults: GPS L1 on RX0, GPS L5 on RX1, 2 MS/s, external 10 MHz reference.

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
uv run x310usrp_record.py --rate 5e6 --gain 10 --name test1  # see --help for all options
```

Output in `recordings/`, per channel:

- `<name>_ch<k>.sigmf-data`: interleaved int16 I/Q (`ci16_le`), 4 bytes per sample
- `<name>_ch<k>.sigmf-meta`: JSON with sample rate, center frequency, gain, start time and any overflow gaps

Over 1 GbE, keep `channels × rate` ≲ 25 MS/s (≈12.5 MS/s per channel when recording both). X310 rates are 200 MHz / integer, so the requested rate may be coerced. The script prints the actual value.

### Load

Python:

```python
import numpy as np
x = np.fromfile("recordings/<name>_ch0.sigmf-data", dtype=np.int16).astype(np.float32).view(np.complex64)
```

MATLAB:

```matlab
fid = fopen("recordings/<name>_ch0.sigmf-data"); raw = fread(fid, [2 Inf], "int16=>double"); fclose(fid);
x = complex(raw(1,:), raw(2,:)).';
```
