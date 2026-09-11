clear 
close  all
clc

rx = comm.SDRuReceiver(Platform = "X310", ...
                    IPAddress = '192.168.10.2', ...
                    OutputDataType = "double", ...
                    IsTwinRXDaughterboard = true, ...
                    EnableTwinRXPhaseSynchronization = true, ...
                    ChannelMapping = [1 2], ...
                    MasterClockRate = 200e6, ...
                    DecimationFactor = 100, ...
                    Gain = 70, ...
                    CenterFrequency = 1575.42e6, ...
                    SamplesPerFrame = 4000);

frameduration = (rx.SamplesPerFrame)/(200e6/200); 
time = 0; 
timeScope = timescope(TimeSpanSource = "Property",...
                      TimeSpan = 4/30e3,SampleRate = 200e6/100);
spectrumScope = spectrumAnalyzer('SampleRate',200e6/200); 
spectrumScope.ReducePlotRate = true; 
disp("Reception Started");

counter = 0;
j = 1;
while time < 10000 && counter < 10000 
 data = rx();  
    amp(1) = max(abs(data(:,1))); 
    amp(2) = max(abs(data(:,2))); 
       maxAmp = max(amp); 
    if any(~amp)  
       normalizedData = data; 
    else 
      normalizedData(:,1) = maxAmp/amp(1)*data(:,1); 
      normalizedData(:,2) = maxAmp/amp(2)*data(:,2); 
    end 
    freqOfFirst = fft(normalizedData(:,1)); 
    freqOfSecond = fft(normalizedData(:,2)); 
    angle1 = rad2deg(angle(max(freqOfFirst)/max(freqOfSecond))); 
    angle_plot(j) = angle1;
    %timeScope([real(normalizedData),imag(normalizedData)]); 
    %timeScope([real(normalizedData)]); 
    timeScope([abs(normalizedData)]); 
    spectrumScope(normalizedData); 
    time = time + frameduration;
    counter = counter +1;
    disp([' Phase difference between channel 1 and 2: ', num2str(angle1)]); 
    disp(' ');
    j = j+1;
end 

release(timeScope); 

release(spectrumScope); 

release(rx);
disp("Reception ended"); 

figure;
plot(angle_plot)
xlabel('samples')
title(' Phase difference between channel 1 and 2')