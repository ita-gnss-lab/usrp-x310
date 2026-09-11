clear; clc;

% Saved X310 configuration
radio = radioConfigurations("My USRP X310 (TwinRX, None)");

% GPS frequencies
fL1 = 1575.42e6;
fL5 = 1176.45e6;

% TwinRX: two RX frontends on daughterboard A
rx = basebandReceiver(radio, ...
    Antennas=["RFA:TX/RX","RFA:RX2"], ...
    CenterFrequency=[fL1 fL5], ...
    SampleRate=2e6, ...
    RadioGain=[1 1]);

% Capture 10 ms
[data,~] = capture(rx,milliseconds(10));

% Channels
L1 = data(:,1);   % RX frontend 1 -> GPS L1
L5 = data(:,2);   % RX frontend 2 -> GPS L5
